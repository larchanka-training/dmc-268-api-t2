"""Regression: the review-cycle publication contract.

Pins the current cycle semantics (docs/github-review-publication.md): a
re-delivered cycle never duplicates comments; each cycle binds its review to
the head it captured; a new commit never merges into an old review and an old
review is never edited.

Cycle 2 models a real new commit: the PR head moves to a fresh SHA before the
second cycle publishes, so the cycle semantics are exercised on a new push,
not a re-delivery of the old head.

Cross-cycle suppression (issue #53): a repeat of a finding the bot already
published in an earlier cycle is not posted again as an inline comment; the
new review names it as "already noted" in its body. Suppression reads
fingerprints from earlier-cycle comments before the publish claim is taken,
so payload_hash stays deterministic for reconciliation.
"""

# ruff: noqa: PLR2004 -- literal post and review counts

from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from test_github_publisher_postgres import FakeGithub, setup_publication
from test_publications_postgres import lease_values, seed_review

from app.application.github_publication import GithubPublisher
from app.application.publication_ports import PublicationAuthority
from app.db.models import (
    ChangeRequest,
    ChunkResult,
    Finding,
    ReviewJob,
    TaskLease,
)
from app.db.publications import PostgresPublicationStore
from app.vcs.github import GithubClient

pytest_plugins = ("test_publications_postgres",)

SHIFTED_LINE = 45


def _shifted_hunk() -> dict[str, object]:
    """The fixture hunk with its added line moved from 39 to 45 (new commit)."""
    return {
        "hunk_id": "hunk_1",
        "old_start": 38,
        "old_count": 2,
        "new_start": 44,
        "new_count": 2,
        "lines": [
            {
                "kind": "context",
                "text": "def validate(data):",
                "old_line": 38,
                "new_line": 44,
            },
            {
                "kind": "deleted",
                "text": "    return data",
                "old_line": 39,
                "new_line": None,
            },
            {
                "kind": "added",
                "text": "    return Schema.model_validate(data)",
                "old_line": None,
                "new_line": SHIFTED_LINE,
            },
        ],
    }


async def _shift_cycle_two(engine: AsyncEngine, review_2: UUID) -> None:
    """Re-point the seeded finding to line 45: a real line shift between commits.

    Same path/side/category/title identity, new coordinates and therefore a
    new fingerprint — the shift only survives suppression via the line-free
    dmc268-suppress marker published by cycle 1.
    """
    async with engine.begin() as connection:
        await connection.execute(
            update(Finding)
            .where(
                Finding.chunk_result_id.in_(
                    select(ChunkResult.id).where(ChunkResult.review_job_id == review_2)
                )
            )
            .values(
                fingerprint="finding-v1-shifted",
                start_line=SHIFTED_LINE,
                end_line=SHIFTED_LINE,
            )
        )


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
    # the same finding on the unchanged line is suppressed by its fingerprint
    # from the earlier cycle (issue #53).
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
    # Cross-cycle suppression (#53): the unchanged-line repeat is not posted
    # again as an inline comment; the new review names it as already noted.
    # The old review is never edited or merged into.
    assert provider.posts[1]["comments"] == []
    assert "Уже отмечено" in provider.posts[1]["body"]
    assert "<!-- dmc268-finding:finding-v1 -->" in provider.posts[1]["body"]
    assert provider.reviews[0]["body"] == cycle_1_body
    assert len(provider.reviews) == 2

    # Re-delivery of cycle 1: retained outcome, zero provider writes.
    outcome_again = await publisher_1.publish(review_job_id=review_1)
    assert outcome_again == outcome_1
    assert len(provider.posts) == 2
    assert len(provider.reviews) == 2


async def test_reconcile_of_suppressed_cycle_resolves_without_new_writes(
    publication_engine: AsyncEngine,
    payload_data: dict[str, Any],
) -> None:
    """Reconciliation reproduces the suppressed payload, not a duplicate.

    Cycle 2 loses the POST response (durable UNKNOWN). Reconciliation must
    re-derive the same suppression set from earlier-cycle comments and land
    PUBLISHED; recomputing the payload without suppression would reject the
    frozen operation it saved itself.
    """
    engine = publication_engine
    store, authority = await setup_publication(engine, payload_data)
    review_1 = authority.review_job_id
    provider = TwoCycleGithub()
    publisher_1 = _publisher(store, provider, authority)

    outcome_1 = await publisher_1.publish(review_job_id=review_1)
    assert outcome_1["status"] == "PUBLISHED"
    cycle_1_comments = list(provider.comments)
    # Cycle 1 kept remote id 9001; cycle 2 will take the distinct id 9002.
    provider.comments_for_review = {"9001": cycle_1_comments}

    async with async_sessionmaker(engine)() as session:
        change_id = await session.scalar(
            select(ReviewJob.change_request_id).where(ReviewJob.id == review_1)
        )
    new_head = "e" * 40
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
    provider.review_change = {"id": 9002}
    provider.head = new_head
    provider.metadata = {
        **provider.metadata,
        "head": {"sha": new_head, "ref": "topic", "repo": {"id": 101}},
    }
    # The accepted POST response is lost: the remote effect happened, the
    # outcome parks as UNKNOWN.
    provider.lose_post_response = True
    outcome_2 = await _publisher(store, provider, authority_2).publish(
        review_job_id=review_2
    )
    assert outcome_2["status"] == "UNKNOWN"
    assert len(provider.posts) == 2
    assert "Уже отмечено" in provider.reviews[-1]["body"]

    async with engine.begin() as connection:
        await connection.execute(
            update(TaskLease)
            .where(TaskLease.review_job_id == review_2)
            .values(
                owner="reconciler",
                fencing_token=2,
                expires_at=datetime.now(UTC) + timedelta(seconds=90),
            )
        )
    resolved = await GithubPublisher(
        store,
        GithubClient(
            httpx.AsyncClient(transport=httpx.MockTransport(provider.respond))
        ),
        authority=PublicationAuthority(review_2, "reconciler", 2),
        bot_user_id="9002",
    ).reconcile(review_job_id=review_2)

    assert resolved["status"] == "PUBLISHED", f"resolved={resolved}"
    assert len(provider.posts) == 2  # reconciliation never POSTs again
    state = await store.get(review_2)
    assert state is not None and state.status == "PUBLISHED"


async def test_new_cycle_suppresses_repeat_after_line_shift(
    publication_engine: AsyncEngine,
    payload_data: dict[str, Any],
) -> None:
    """The same finding moved between commits is still suppressed (#53 review).

    Cycle 1 publishes the finding at line 39 with a line-free dmc268-suppress
    marker. Cycle 2 reviews a new commit where the code moved to line 45: the
    full fingerprint changed, so only the suppress marker can suppress the
    repeat. Reconciliation of a lost cycle-2 POST still reproduces it.
    """
    engine = publication_engine
    store, authority = await setup_publication(engine, payload_data)
    review_1 = authority.review_job_id
    provider = TwoCycleGithub()

    outcome_1 = await _publisher(store, provider, authority).publish(
        review_job_id=review_1
    )
    assert outcome_1["status"] == "PUBLISHED"
    cycle_1_comment = provider.posts[0]["comments"][0]
    assert "<!-- dmc268-suppress:" in cycle_1_comment["body"]
    assert cycle_1_comment["body"].endswith("<!-- dmc268-finding:finding-v1 -->")
    provider.comments_for_review = {"9001": list(provider.comments)}

    async with async_sessionmaker(engine)() as session:
        change_id = await session.scalar(
            select(ReviewJob.change_request_id).where(ReviewJob.id == review_1)
        )
    new_head = "f" * 40
    # The new commit moved the reviewed code: the hunk's added line is now 45.
    payload_data["files"][0]["diff"]["hunks"] = [_shifted_hunk()]
    payload_data["coverage"]["included"][0]["ranges"] = [
        {"start_line": SHIFTED_LINE, "end_line": SHIFTED_LINE}
    ]
    review_2, _ = await seed_review(
        engine, payload_data, change_id=change_id, head_sha=new_head
    )
    await _shift_cycle_two(engine, review_2)
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
    provider.review_change = {"id": 9002}
    provider.head = new_head
    provider.metadata = {
        **provider.metadata,
        "head": {"sha": new_head, "ref": "topic", "repo": {"id": 101}},
    }
    # The accepted cycle-2 POST loses its response: durable UNKNOWN, so the
    # reconciliation path below must reproduce the suppressed payload.
    provider.lose_post_response = True

    outcome_2 = await _publisher(store, provider, authority_2).publish(
        review_job_id=review_2
    )
    assert outcome_2["status"] == "UNKNOWN"
    assert len(provider.posts) == 2
    assert provider.posts[1]["commit_id"] == new_head
    # The shifted repeat is not posted again; the body names it as noted.
    assert provider.posts[1]["comments"] == []
    assert "Уже отмечено в прошлых циклах" in provider.posts[1]["body"]
    assert "finding-v1-shifted" in provider.posts[1]["body"]
    assert provider.reviews[0]["body"].startswith(
        f"<!-- dmc268-review:v1 review={review_1} "
    )  # the old review is never edited

    # A lost cycle-2 POST still reconciles against the same suppression set.
    async with engine.begin() as connection:
        await connection.execute(
            update(TaskLease)
            .where(TaskLease.review_job_id == review_2)
            .values(
                owner="reconciler",
                fencing_token=2,
                expires_at=datetime.now(UTC) + timedelta(seconds=90),
            )
        )
    resolved = await GithubPublisher(
        store,
        GithubClient(
            httpx.AsyncClient(transport=httpx.MockTransport(provider.respond))
        ),
        authority=PublicationAuthority(review_2, "reconciler", 2),
        bot_user_id="9002",
    ).reconcile(review_job_id=review_2)
    assert resolved["status"] == "PUBLISHED", f"resolved={resolved}"
    assert len(provider.posts) == 2  # reconciliation never POSTs again
