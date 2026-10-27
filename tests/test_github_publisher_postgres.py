# ruff: noqa: PLR2004, RUF001 - literal contract examples and Russian fixtures
"""GitHub publication through its service, with PostgreSQL and HTTP boundary."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import insert, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from test_publications_postgres import (
    BASE,
    HEAD,
    MERGE_BASE,
    lease_values,
    seed_review,
)
from test_publications_postgres import (
    publication_engine as publication_engine,  # noqa: PLC0414 - pytest fixture export
)

from app.application.github_publication import GithubPublisher
from app.application.publication_ports import (
    PublicationAuthority,
    PublicationAuthorityError,
)
from app.db.models import TaskLease
from app.db.publications import PostgresPublicationStore
from app.vcs.github import GithubClient


async def setup_publication(
    engine: AsyncEngine,
    payload: dict[str, Any],
) -> tuple[PostgresPublicationStore, PublicationAuthority]:
    payload["snapshot"].update(
        head_sha=HEAD,
        base_sha=BASE,
        merge_base_sha=MERGE_BASE,
    )
    review_id, _ = await seed_review(engine, payload)
    async with engine.begin() as connection:
        await connection.execute(
            insert(TaskLease).values(
                **lease_values(
                    id=uuid4(),
                    review_job_id=review_id,
                    owner="publisher-a",
                    fencing_token=1,
                    expires_at=datetime.now(UTC) + timedelta(seconds=90),
                )
            )
        )
    return (
        PostgresPublicationStore(async_sessionmaker(engine, expire_on_commit=False)),
        PublicationAuthority(review_id, "publisher-a", 1),
    )


def pull_metadata() -> dict[str, object]:
    return {
        "state": "open",
        "number": 42,
        "title": "Review",
        "body": None,
        "head": {"sha": HEAD, "ref": "topic", "repo": {"id": 101}},
        "base": {"sha": BASE, "ref": "main", "repo": {"id": 100}},
    }


async def test_publisher_submits_saved_result_and_persists_remote_references(
    publication_engine: AsyncEngine,
    payload_data: dict[str, Any],
) -> None:
    """AC-PUB-01: saved analysis is published once and readable as PUBLISHED."""
    store, authority = await setup_publication(publication_engine, payload_data)
    posts: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posts.append(json.loads(request.content))
            return httpx.Response(200, json={"id": 9001})
        if request.url.path.endswith("/pulls/42"):
            return httpx.Response(200, json=pull_metadata())
        if request.url.path.endswith("/reviews"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 9001,
                        "body": posts[0]["body"],
                        "commit_id": HEAD,
                        "state": "COMMENTED",
                        "user": {"id": 9002},
                    }
                ]
                if posts
                else [],
            )
        if request.url.path.endswith("/reviews/9001/comments"):
            return httpx.Response(
                200,
                json=[
                    {
                        **posts[0]["comments"][0],
                        "id": 9003,
                        "user": {"id": 9002},
                        "commit_id": HEAD,
                    }
                ],
            )
        raise AssertionError(
            f"Unexpected HTTP boundary: {request.method} {request.url}"
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        result = await GithubPublisher(
            store,
            GithubClient(http),
            authority=authority,
            bot_user_id="9002",
        ).publish(review_job_id=authority.review_job_id)

    assert len(posts) == 1
    assert posts[0]["commit_id"] == HEAD
    assert posts[0]["event"] == "COMMENT"
    assert posts[0]["comments"][0]["line"] == 39
    assert result["status"] == "PUBLISHED"
    state = await store.get(authority.review_job_id)
    assert state is not None and state.status == "PUBLISHED"
    assert state.provider_metadata is not None
    assert state.provider_metadata["remote_review_id"] == "9001"
    assert state.provider_metadata["comment_ids"] == {"finding-v1": "9003"}


async def test_reconcile_empty_read_preserves_unknown_until_delayed_write_is_visible(
    publication_engine: AsyncEngine,
    payload_data: dict[str, Any],
) -> None:
    """AC-PUB-14: empty GET cannot disprove a delayed accepted POST."""
    store, authority = await setup_publication(publication_engine, payload_data)
    provider = FakeGithub()
    provider.lose_post_response = True
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(provider.respond)
    ) as http:
        await GithubPublisher(
            store,
            GithubClient(http),
            authority=authority,
            bot_user_id="9002",
        ).publish(review_job_id=authority.review_job_id)
        delayed_reviews = provider.reviews
        delayed_comments = provider.comments
        provider.reviews = []
        provider.comments = []
        async with publication_engine.begin() as connection:
            await connection.execute(
                update(TaskLease)
                .where(
                    TaskLease.review_job_id == authority.review_job_id,
                )
                .values(
                    owner="reconciler",
                    fencing_token=2,
                    expires_at=datetime.now(UTC) + timedelta(seconds=90),
                )
            )
        result = await GithubPublisher(
            store,
            GithubClient(http),
            authority=PublicationAuthority(authority.review_job_id, "reconciler", 2),
            bot_user_id="9002",
        ).reconcile(review_job_id=authority.review_job_id)

    assert result["status"] == "UNKNOWN"
    assert len(provider.posts) == 1
    state = await store.get(authority.review_job_id)
    assert state is not None and state.status == "UNKNOWN"
    assert state.provider_metadata is not None
    assert state.provider_metadata["confirmed_absent"] is False
    operation = await store.get_operation(authority.review_job_id)
    assert operation is not None and operation.state == "UNKNOWN"

    provider.reviews = delayed_reviews
    provider.comments = delayed_comments
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(provider.respond)
    ) as http:
        resolved = await GithubPublisher(
            store,
            GithubClient(http),
            authority=PublicationAuthority(authority.review_job_id, "reconciler", 2),
            bot_user_id="9002",
        ).reconcile(review_job_id=authority.review_job_id)
    assert resolved["status"] == "PUBLISHED"
    assert len(provider.posts) == 1


class FakeGithub:
    """External-system simulator; production services and PostgreSQL stay real."""

    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.requests: list[tuple[str, str]] = []
        self.reviews: list[dict[str, Any]] = []
        self.comments: list[dict[str, Any]] = []
        self.comments_for_review: dict[str, list[dict[str, Any]]] = {}
        self.lose_post_response = False
        self.reject_post: int | None = None
        self.drop_comments = False
        self.comment_change: dict[str, Any] = {}
        self.review_change: dict[str, Any] = {}
        self.metadata = pull_metadata()
        # Head the fake reports for records it creates; tests moving the PR
        # head (new push) set this to the new SHA.
        self.head = HEAD

    def respond(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path))
        if request.method == "POST":
            payload = json.loads(request.content)
            self.posts.append(payload)
            if self.reject_post is not None:
                return httpx.Response(
                    self.reject_post, json={"message": "provider-secret"}
                )
            self.reviews.append(
                {
                    "id": 9001,
                    "body": payload["body"],
                    "commit_id": self.head,
                    "state": "COMMENTED",
                    "user": {"id": 9002},
                }
            )
            self.reviews[-1].update(self.review_change)
            self.comments = [
                {
                    **item,
                    "id": 9003 + index,
                    "user": {"id": 9002},
                    "commit_id": self.head,
                }
                for index, item in enumerate(payload["comments"])
            ]
            if self.drop_comments:
                self.comments = []
            for comment in self.comments:
                comment.update(self.comment_change)
            if self.lose_post_response:
                raise httpx.ReadTimeout("provider-secret", request=request)
            return httpx.Response(200, json={"id": 9001})
        if request.url.path.endswith("/pulls/42"):
            return httpx.Response(200, json=self.metadata)
        if request.url.path.endswith("/reviews"):
            return httpx.Response(200, json=self.reviews)
        if "/reviews/" in request.url.path and request.url.path.endswith("/comments"):
            remote_id = request.url.path.split("/")[-2]
            return httpx.Response(
                200, json=self.comments_for_review.get(remote_id, self.comments)
            )
        raise AssertionError(
            f"Unexpected HTTP boundary: {request.method} {request.url}"
        )


async def test_publisher_persists_unknown_after_github_accepts_and_response_is_lost(
    publication_engine: AsyncEngine,
    payload_data: dict[str, Any],
) -> None:
    """AC-PUB-02: accepted POST with lost response is durable UNKNOWN."""
    store, authority = await setup_publication(publication_engine, payload_data)
    provider = FakeGithub()
    provider.lose_post_response = True

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(provider.respond)
    ) as http:
        result = await GithubPublisher(
            store,
            GithubClient(http),
            authority=authority,
            bot_user_id="9002",
        ).publish(review_job_id=authority.review_job_id)

    assert result["status"] == "UNKNOWN"
    assert len(provider.posts) == 1
    state = await store.get(authority.review_job_id)
    assert state is not None and state.status == "UNKNOWN"
    assert "provider-secret" not in str(state.provider_metadata)


async def test_publisher_rechecks_authority_between_provider_pages(
    publication_engine: AsyncEngine,
    payload_data: dict[str, Any],
) -> None:
    """AC-PUB-15: pagination never starts another HTTP request after lease expiry."""
    store, authority = await setup_publication(publication_engine, payload_data)
    provider = FakeGithub()
    pages: list[str] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/reviews"):
            page = request.url.params["page"]
            pages.append(page)
            if page == "1":
                async with publication_engine.begin() as connection:
                    await connection.execute(
                        update(TaskLease)
                        .where(
                            TaskLease.review_job_id == authority.review_job_id,
                        )
                        .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
                    )
                return httpx.Response(
                    200,
                    json=[],
                    headers={
                        "Link": (
                            f"<https://api.github.com{request.url.path}"
                            '?per_page=100&page=2>; rel="next"'
                        ),
                    },
                )
            return httpx.Response(200, json=[])
        return provider.respond(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(PublicationAuthorityError):
            await GithubPublisher(
                store,
                GithubClient(http),
                authority=authority,
                bot_user_id="9002",
            ).publish(review_job_id=authority.review_job_id)

    assert pages == ["1"]
    assert provider.posts == []


async def test_reconcile_lost_response_recovers_saved_remote_effect_without_post(
    publication_engine: AsyncEngine,
    payload_data: dict[str, Any],
) -> None:
    """AC-PUB-13: fresh-fence reconciliation resolves UNKNOWN without new writes."""
    store, authority = await setup_publication(publication_engine, payload_data)
    provider = FakeGithub()
    provider.lose_post_response = True
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(provider.respond)
    ) as http:
        original = await GithubPublisher(
            store,
            GithubClient(http),
            authority=authority,
            bot_user_id="9002",
        ).publish(review_job_id=authority.review_job_id)
        assert original["status"] == "UNKNOWN"
        async with publication_engine.begin() as connection:
            await connection.execute(
                update(TaskLease)
                .where(
                    TaskLease.review_job_id == authority.review_job_id,
                )
                .values(
                    owner="reconciler",
                    fencing_token=2,
                    expires_at=datetime.now(UTC) + timedelta(seconds=90),
                )
            )
        recovered = await GithubPublisher(
            store,
            GithubClient(http),
            authority=PublicationAuthority(authority.review_job_id, "reconciler", 2),
            bot_user_id="9002",
        ).reconcile(review_job_id=authority.review_job_id)

    assert recovered["status"] == "PUBLISHED"
    assert recovered["comment_ids"] == {"finding-v1": "9003"}
    assert len(provider.posts) == 1
    state = await store.get(authority.review_job_id)
    assert state is not None and state.status == "PUBLISHED"


async def test_publisher_persists_confirmed_absent_rejected_write(
    publication_engine: AsyncEngine,
    payload_data: dict[str, Any],
) -> None:
    """AC-PUB-03: explicit forbidden POST is FAILED with confirmed absence."""
    store, authority = await setup_publication(publication_engine, payload_data)
    provider = FakeGithub()
    provider.reject_post = 403
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(provider.respond)
    ) as http:
        result = await GithubPublisher(
            store,
            GithubClient(http),
            authority=authority,
            bot_user_id="9002",
        ).publish(review_job_id=authority.review_job_id)

    assert result["status"] == "FAILED"
    state = await store.get(authority.review_job_id)
    assert state is not None and state.status == "FAILED"
    assert state.provider_metadata is not None
    assert state.provider_metadata["confirmed_absent"] is True


@pytest.mark.parametrize("change", ["head", "closed"])
async def test_publisher_skips_stale_or_closed_pull_without_post(
    publication_engine: AsyncEngine,
    payload_data: dict[str, Any],
    change: str,
) -> None:
    """AC-PUB-04: stale/closed PR preserves saved analysis without posting."""
    store, authority = await setup_publication(publication_engine, payload_data)
    provider = FakeGithub()
    if change == "head":
        provider.metadata["head"] = {
            "sha": "d" * 40,
            "ref": "topic",
            "repo": {"id": 101},
        }
    else:
        provider.metadata["state"] = "closed"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(provider.respond)
    ) as http:
        result = await GithubPublisher(
            store,
            GithubClient(http),
            authority=authority,
            bot_user_id="9002",
        ).publish(review_job_id=authority.review_job_id)

    assert result["status"] == "SKIPPED"
    assert provider.posts == []
    state = await store.get(authority.review_job_id)
    assert state is not None and state.status == "SKIPPED"


async def test_publisher_reconciles_existing_review_before_any_post(
    publication_engine: AsyncEngine,
    payload_data: dict[str, Any],
) -> None:
    """AC-PUB-05: marker recovery after local commit loss avoids a second POST."""
    store, authority = await setup_publication(publication_engine, payload_data)
    provider = FakeGithub()
    marker = (
        f"<!-- dmc268-review:v1 review={authority.review_job_id} "
        f"head={HEAD} config=config-v1 -->"
    )
    provider.reviews = [
        {
            "id": 9001,
            "body": marker + "\nСохранённая сводка",
            "commit_id": HEAD,
            "state": "COMMENTED",
            "user": {"id": 9002},
        }
    ]
    provider.comments = [
        {
            "id": 9003,
            "body": "Замечание\n<!-- dmc268-finding:finding-v1 -->",
            "path": "src/review.py",
            "line": 39,
            "side": "RIGHT",
            "commit_id": HEAD,
            "user": {"id": 9002},
        }
    ]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(provider.respond)
    ) as http:
        result = await GithubPublisher(
            store,
            GithubClient(http),
            authority=authority,
            bot_user_id="9002",
        ).publish(review_job_id=authority.review_job_id)

    assert provider.posts == []
    assert result["status"] == "PUBLISHED"
    state = await store.get(authority.review_job_id)
    assert state is not None and state.status == "PUBLISHED"


async def test_publisher_ignores_human_review_with_forged_marker(
    publication_engine: AsyncEngine,
    payload_data: dict[str, Any],
) -> None:
    """AC-PUB-06: matching text from another actor never substitutes bot output."""
    store, authority = await setup_publication(publication_engine, payload_data)
    provider = FakeGithub()
    marker = (
        f"<!-- dmc268-review:v1 review={authority.review_job_id} "
        f"head={HEAD} config=config-v1 -->"
    )
    provider.reviews = [
        {
            "id": 8001,
            "body": marker,
            "commit_id": HEAD,
            "state": "COMMENTED",
            "user": {"id": 9009},
        }
    ]
    provider.comments_for_review["8001"] = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(provider.respond)
    ) as http:
        result = await GithubPublisher(
            store,
            GithubClient(http),
            authority=authority,
            bot_user_id="9002",
        ).publish(review_job_id=authority.review_job_id)

    assert len(provider.posts) == 1
    assert result["remote_review_id"] == "9001"


async def test_publisher_rechecks_live_authority_after_preflight(
    publication_engine: AsyncEngine,
    payload_data: dict[str, Any],
) -> None:
    """AC-PUB-07: an expired worker must not start a subsequent provider call."""
    store, authority = await setup_publication(publication_engine, payload_data)
    provider = FakeGithub()

    async def respond(request: httpx.Request) -> httpx.Response:
        response = provider.respond(request)
        if request.url.path.endswith("/pulls/42"):
            async with publication_engine.begin() as connection:
                await connection.execute(
                    update(TaskLease)
                    .where(
                        TaskLease.review_job_id == authority.review_job_id,
                    )
                    .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
                )
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(PublicationAuthorityError):
            await GithubPublisher(
                store,
                GithubClient(http),
                authority=authority,
                bot_user_id="9002",
            ).publish(review_job_id=authority.review_job_id)

    assert provider.posts == []
    assert provider.requests == [("GET", "/repos/larchanka-training/demo/pulls/42")]


async def test_publisher_preserves_confirmed_partial_publication(
    publication_engine: AsyncEngine,
    payload_data: dict[str, Any],
) -> None:
    """AC-PUB-08: confirmed missing comments are PARTIAL and not auto-completed."""
    store, authority = await setup_publication(publication_engine, payload_data)
    provider = FakeGithub()
    provider.drop_comments = True
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(provider.respond)
    ) as http:
        result = await GithubPublisher(
            store,
            GithubClient(http),
            authority=authority,
            bot_user_id="9002",
        ).publish(review_job_id=authority.review_job_id)

    assert result["status"] == "PARTIAL"
    assert len(provider.posts) == 1
    state = await store.get(authority.review_job_id)
    assert state is not None and state.status == "PARTIAL"


@pytest.mark.parametrize(
    "change",
    [
        {"user": {"id": 9009}},
        {"commit_id": "d" * 40},
        {"path": "src/other.py"},
        {"line": 99},
        {"side": "LEFT"},
        {"start_line": 38, "start_side": "RIGHT"},
    ],
)
async def test_publisher_confirms_comment_identity_and_exact_coordinates(
    publication_engine: AsyncEngine,
    payload_data: dict[str, Any],
    change: dict[str, Any],
) -> None:
    """AC-PUB-09: a fingerprint alone is insufficient publication proof."""
    store, authority = await setup_publication(publication_engine, payload_data)
    provider = FakeGithub()
    provider.comment_change = change
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(provider.respond)
    ) as http:
        result = await GithubPublisher(
            store,
            GithubClient(http),
            authority=authority,
            bot_user_id="9002",
        ).publish(review_job_id=authority.review_job_id)

    assert result["status"] == "PARTIAL"
    assert result["comment_ids"] == {}


@pytest.mark.parametrize(
    "outcome", ["PUBLISHED", "PARTIAL", "UNKNOWN", "FAILED", "SKIPPED"]
)
async def test_repeated_delivery_returns_durable_outcome_without_new_post(
    publication_engine: AsyncEngine,
    payload_data: dict[str, Any],
    outcome: str,
) -> None:
    """AC-PUB-10: delivery replay never reopens a finished publication cycle."""
    store, authority = await setup_publication(publication_engine, payload_data)
    provider = FakeGithub()
    provider.drop_comments = outcome == "PARTIAL"
    provider.lose_post_response = outcome == "UNKNOWN"
    provider.reject_post = 403 if outcome == "FAILED" else None
    if outcome == "SKIPPED":
        provider.metadata["state"] = "closed"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(provider.respond)
    ) as http:
        publisher = GithubPublisher(
            store,
            GithubClient(http),
            authority=authority,
            bot_user_id="9002",
        )
        first = await publisher.publish(review_job_id=authority.review_job_id)
        post_count = len(provider.posts)
        repeated = await publisher.publish(review_job_id=authority.review_job_id)

    assert first["status"] == outcome
    assert repeated["status"] == outcome
    assert len(provider.posts) == post_count


@pytest.mark.parametrize(
    "mode",
    [
        "existing_wrong_commit",
        "existing_pending",
        "created_wrong_actor",
        "created_wrong_commit",
        "created_pending",
    ],
)
async def test_publisher_requires_confirmed_overview_identity_and_commit(
    publication_engine: AsyncEngine,
    payload_data: dict[str, Any],
    mode: str,
) -> None:
    """AC-PUB-11: malformed/inconsistent Overview receipt stays UNKNOWN."""
    store, authority = await setup_publication(publication_engine, payload_data)
    provider = FakeGithub()
    change: dict[str, Any] = (
        {"state": "PENDING"}
        if mode.endswith("pending")
        else {"user": {"id": 9009}}
        if mode.endswith("actor")
        else {"commit_id": "d" * 40}
    )
    if mode.startswith("existing"):
        marker = (
            f"<!-- dmc268-review:v1 review={authority.review_job_id} "
            f"head={HEAD} config=config-v1 -->"
        )
        provider.reviews = [
            {
                "id": 9001,
                "body": marker,
                "commit_id": HEAD,
                "state": "COMMENTED",
                "user": {"id": 9002},
                **change,
            }
        ]
        provider.comments = [
            {
                "id": 9003,
                "body": "Comment\n<!-- dmc268-finding:finding-v1 -->",
                "path": "src/review.py",
                "line": 39,
                "side": "RIGHT",
                "commit_id": HEAD,
                "user": {"id": 9002},
            }
        ]
    else:
        provider.review_change = change
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(provider.respond)
    ) as http:
        result = await GithubPublisher(
            store,
            GithubClient(http),
            authority=authority,
            bot_user_id="9002",
        ).publish(review_job_id=authority.review_job_id)

    assert result["status"] == "UNKNOWN"
    assert len(provider.posts) == (0 if mode.startswith("existing") else 1)


@pytest.mark.parametrize("failure_at", ["preflight", "before", "after", "comments"])
async def test_provider_read_failure_does_not_lose_publication_decision(
    publication_engine: AsyncEngine,
    payload_data: dict[str, Any],
    failure_at: str,
) -> None:
    """AC-PUB-12: failed reconciliation reads preserve an UNKNOWN decision."""
    store, authority = await setup_publication(publication_engine, payload_data)
    provider = FakeGithub()

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and (
            (failure_at == "preflight" and request.url.path.endswith("/pulls/42"))
            or (
                failure_at == "before"
                and request.url.path.endswith("/reviews")
                and not provider.posts
            )
            or (
                failure_at == "after"
                and request.url.path.endswith("/reviews")
                and provider.posts
            )
            or (failure_at == "comments" and request.url.path.endswith("/comments"))
        ):
            return httpx.Response(503, json={"message": "provider-secret"})
        return provider.respond(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        result = await GithubPublisher(
            store,
            GithubClient(http),
            authority=authority,
            bot_user_id="9002",
        ).publish(review_job_id=authority.review_job_id)

    assert result["status"] == "UNKNOWN"
    assert len(provider.posts) == (0 if failure_at in {"preflight", "before"} else 1)
    state = await store.get(authority.review_job_id)
    assert state is not None and state.status == "UNKNOWN"
    assert "provider-secret" not in str(state.provider_metadata)
