"""Regression: the review-cycle publication contract.

Pins the current cycle semantics (docs/github-review-publication.md): a
re-delivered cycle never duplicates comments; each cycle binds its review to
the head it captured; a new commit never merges into an old review and an old
review is never edited.

Cycle 2 models a real new commit: the PR head moves to a fresh SHA before the
second cycle publishes, so the cycle semantics are exercised on a new push,
not a re-delivery of the old head.

Known limitation, not the target contract: a cross-cycle repeat of the same
finding on an unchanged line is still published (markers are review-scoped).
Suppressing such repeats is the open sprint-4 acceptance (issue #53,
dmc-268-ui-t2#28/#33); this test pins today's behavior until it lands.
"""

# ruff: noqa: PLR2004 -- literal post and review counts

from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

import httpx
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from test_github_publisher_postgres import FakeGithub, setup_publication
from test_publications_postgres import lease_values, seed_review

from app.application.github_publication import GithubPublisher
from app.application.publication_ports import PublicationAuthority
from app.db.models import ChangeRequest, ReviewJob, TaskLease
from app.db.publications import PostgresPublicationStore
from app.vcs.github import GithubClient

pytest_plugins = ("test_publications_postgres",)


class TwoCycleGithub(FakeGithub):
    """FakeGithub whose POST response echoes the review it actually created."""

    def respond(self, request: httpx.Request) -> httpx.Response:
        response = super().respond(request)
        if request.method == "POST" and self.reviews:
            response = httpx.Response(200, json={"id": self.reviews[-1]["id"]})
        return response


def _publisher(
    store: PostgresPublicationStore,
    provider: FakeGithub,
    authority: PublicationAuthority,
) -> GithubPublisher:
    return GithubPublisher(
        store,
        GithubClient(
            httpx.AsyncClient(transport=httpx.MockTransport(provider.respond))
        ),
        authority=authority,
        bot_user_id="9002",
    )


async def test_new_cycle_publishes_its_own_review_and_redelivery_never_duplicates(
    publication_engine: AsyncEngine,
    payload_data: dict[str, object],
) -> None:
    engine = publication_engine
    store, authority = await setup_publication(engine, payload_data)
    review_1 = authority.review_job_id
    provider = TwoCycleGithub()
    publisher_1 = _publisher(store, provider, authority)

    outcome_1 = await publisher_1.publish(review_job_id=review_1)
    assert outcome_1["status"] == "PUBLISHED"
    assert len(provider.posts) == 1
    head_sha = cast(dict[str, Any], provider.metadata["head"])["sha"]
    assert provider.posts[0]["commit_id"] == head_sha
    assert f"review={review_1}" in provider.posts[0]["body"]
    assert all(
        "dmc268-finding:" in comment["body"]
        for comment in provider.posts[0]["comments"]
    )
    cycle_1_body = provider.reviews[0]["body"]

    # A new push starts a new cycle: the PR head moves to a new commit, and
    # the same findings on the unchanged lines are re-reported (the known
    # cross-cycle limitation pinned until #53 lands).
    async with async_sessionmaker(engine)() as session:
        change_id = await session.scalar(
            select(ReviewJob.change_request_id).where(ReviewJob.id == review_1)
        )
    new_head = "d" * 40  # cycle 2 reviews a real new commit, not the old head
    review_2, _ = await seed_review(
        engine, payload_data, change_id=change_id, head_sha=new_head
    )
    async with engine.begin() as connection:
        await connection.execute(
            update(ChangeRequest)
            .where(ChangeRequest.id == change_id)
            .values(head_sha=new_head)
        )
        await connection.execute(
            insert(TaskLease).values(
                **lease_values(
                    id=uuid4(),
                    review_job_id=review_2,
                    owner="cycle-2",
                    fencing_token=1,
                    expires_at=datetime.now(UTC) + timedelta(seconds=90),
                )
            )
        )
    authority_2 = PublicationAuthority(review_2, "cycle-2", 1)
    provider.review_change = {"id": 9002}  # distinct remote id for cycle 2
    # The push moved the PR head on the provider side too.
    provider.head = new_head
    provider.metadata = {
        **provider.metadata,
        "head": {"sha": new_head, "ref": "topic", "repo": {"id": 101}},
    }
    publisher_2 = _publisher(store, provider, authority_2)

    outcome_2 = await publisher_2.publish(review_job_id=review_2)
    assert outcome_2["status"] == "PUBLISHED", f"outcome_2={outcome_2}"
    assert len(provider.posts) == 2
    # The new cycle is bound to the new commit it captured, never the old head.
    assert provider.posts[1]["commit_id"] == new_head
    assert provider.posts[1]["commit_id"] != head_sha
    assert f"review={review_2}" in provider.posts[1]["body"]
    assert review_1 != review_2
    # Cross-cycle repeat of the unchanged-line finding is the pinned current
    # limitation: it lands in the new review; the old review is never edited
    # or merged into.
    assert provider.posts[1]["comments"] == provider.posts[0]["comments"]
    assert provider.reviews[0]["body"] == cycle_1_body
    assert len(provider.reviews) == 2

    # Re-delivery of cycle 1: retained outcome, zero provider writes.
    outcome_again = await publisher_1.publish(review_job_id=review_1)
    assert outcome_again == outcome_1
    assert len(provider.posts) == 2
    assert len(provider.reviews) == 2
