"""First StartReview through real browser auth, PostgreSQL and GitHub HTTP."""

import asyncio
import inspect
from collections.abc import AsyncIterator
from http import HTTPStatus
from uuid import UUID

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, select, update
from test_postgres_integration import PostgresHarness
from test_review_results_api import assert_schema
from test_review_start_api import CONCURRENT_REQUESTS, HEAD
from test_review_start_authorization import BrowserStart, assert_no_admission

from app.db import models

pytest_plugins = ("test_review_start_authorization",)
FIRST_START_READS = 6  # Personal permission, discovery, four-read pinned capture.
FIRST_CAPTURE_READ = 2


@pytest_asyncio.fixture
async def unknown_start(
    browser_start: BrowserStart, postgres: PostgresHarness
) -> AsyncIterator[BrowserStart]:
    async with postgres.engine.begin() as connection:
        await connection.execute(
            delete(models.ChangeRequest).where(
                models.ChangeRequest.id == browser_start.scope.change
            )
        )
    try:
        yield browser_start
    finally:
        async with postgres.engine.connect() as connection:
            for change_id in await connection.scalars(
                select(models.ChangeRequest.id).where(
                    models.ChangeRequest.repository_id == browser_start.scope.repository
                )
            ):
                postgres.track("change_request", change_id)


async def test_first_start_materializes_provider_pr_and_admits_one_review(
    unknown_start: BrowserStart, postgres: PostgresHarness
) -> None:
    response = await unknown_start.start()
    assert response.status_code == HTTPStatus.ACCEPTED
    assert_schema(response, "StartReviewResponse")
    assert len(unknown_start.requests) == FIRST_START_READS
    review_id = UUID(response.json()["review_id"])
    async with postgres.engine.connect() as connection:
        change = (
            await connection.execute(
                select(models.ChangeRequest.__table__).where(
                    models.ChangeRequest.repository_id == unknown_start.scope.repository
                )
            )
        ).one()
        assert change.external_number == "13"
        assert change.title == "Fix division"
        assert change.description == "Description"
        assert change.source_ref == "feature/postgres-validation"
        assert change.base_ref == "main"
        assert change.source_repository_external_id == "123456"
        assert change.current_status == "open"
        assert (
            await connection.execute(
                select(models.ReviewJob.id, models.ReviewJob.change_request_id).where(
                    models.ReviewJob.repository_id == unknown_start.scope.repository
                )
            )
        ).one() == (review_id, change.id)
        assert (
            await connection.scalars(
                select(models.OutboxEvent.task_kind).where(
                    models.OutboxEvent.review_job_id == review_id
                )
            )
        ).all() == ["analyze"]


async def assert_no_materialization(
    browser: BrowserStart, postgres: PostgresHarness
) -> None:
    await assert_no_admission(browser, postgres)
    async with postgres.engine.connect() as connection:
        assert (
            await connection.scalar(
                select(models.ChangeRequest.id).where(
                    models.ChangeRequest.repository_id == browser.scope.repository
                )
            )
            is None
        )


@pytest.mark.parametrize(
    ("case", "status", "code"),
    [
        ("missing", 404, "RESOURCE_NOT_FOUND"),
        ("closed", 404, "RESOURCE_NOT_FOUND"),
        ("head", 409, "PR_HEAD_MISMATCH"),
        ("capture_stale", 404, "RESOURCE_NOT_FOUND"),
        ("base_repository", 403, "ACCESS_DENIED"),
    ],
)
async def test_provider_refusal_does_not_materialize(
    unknown_start: BrowserStart,
    postgres: PostgresHarness,
    case: str,
    status: int,
    code: str,
) -> None:
    original = unknown_start.provider
    pull_reads = 0

    async def provider(request: httpx.Request) -> httpx.Response:
        nonlocal pull_reads
        response = original(request)
        if inspect.isawaitable(response):
            response = await response
        if not request.url.path.endswith("/pulls/13"):
            return response
        pull_reads += 1
        if case == "missing":
            return httpx.Response(404)
        pull = response.json()
        if case == "closed" or (
            case == "capture_stale" and pull_reads == FIRST_CAPTURE_READ
        ):
            pull["state"] = "closed"
        elif case == "head":
            pull["head"]["sha"] = "c" * 40
        elif case == "base_repository":
            pull["base"]["repo"]["id"] = 999
        return httpx.Response(200, json=pull)

    unknown_start.provider = provider
    response = await unknown_start.start()
    assert response.status_code == status
    assert response.json()["code"] == code
    assert_schema(response, "Error")
    await assert_no_materialization(unknown_start, postgres)


@pytest.mark.parametrize("same_key", [False, True])
async def test_concurrent_first_starts_reuse_one_change_request(
    unknown_start: BrowserStart, postgres: PostgresHarness, same_key: bool
) -> None:
    original = unknown_start.provider
    metadata_reads = 0
    both_read = asyncio.Event()

    async def provider(request: httpx.Request) -> httpx.Response:
        nonlocal metadata_reads
        response = original(request)
        if inspect.isawaitable(response):
            response = await response
        if request.url.path.endswith("/pulls/13"):
            metadata_reads += 1
            if metadata_reads == CONCURRENT_REQUESTS:
                both_read.set()
            await asyncio.wait_for(both_read.wait(), timeout=5)
        return response

    unknown_start.provider = provider
    responses = await asyncio.gather(
        unknown_start.start(),
        unknown_start.client.post(
            unknown_start.scope.url,
            json={"requested_head_sha": HEAD},
            headers={
                **unknown_start.headers,
                "Idempotency-Key": "one" if same_key else "two",
            },
        ),
    )
    assert sorted(response.status_code for response in responses) == (
        [200, 202] if same_key else [202, 409]
    )
    if same_key:
        assert responses[0].json() == responses[1].json()
    else:
        rejected = next(
            response
            for response in responses
            if response.status_code == HTTPStatus.CONFLICT
        )
        assert rejected.json()["code"] == "REVIEW_ALREADY_ACTIVE"
    async with postgres.engine.connect() as connection:
        assert (
            len(
                (
                    await connection.scalars(
                        select(models.ChangeRequest.id).where(
                            models.ChangeRequest.repository_id
                            == unknown_start.scope.repository
                        )
                    )
                ).all()
            )
            == 1
        )
        assert (
            len(
                (
                    await connection.scalars(
                        select(models.ReviewJob.id).where(
                            models.ReviewJob.repository_id
                            == unknown_start.scope.repository
                        )
                    )
                ).all()
            )
            == 1
        )
        assert (
            await connection.scalars(
                select(models.QuotaUsage.value).where(
                    models.QuotaUsage.repository_id == unknown_start.scope.repository
                )
            )
        ).all() == [1]


async def test_first_start_replay_returns_same_review_without_provider_reads(
    unknown_start: BrowserStart, postgres: PostgresHarness
) -> None:
    first = await unknown_start.start()
    assert first.status_code == HTTPStatus.ACCEPTED
    unknown_start.requests.clear()
    async with postgres.engine.begin() as connection:
        await connection.execute(
            update(models.GithubIdentity)
            .where(models.GithubIdentity.user_id == unknown_start.scope.user)
            .values(encrypted_token="unusable-on-replay")
        )
    replay = await unknown_start.start()
    assert replay.status_code == HTTPStatus.OK
    assert replay.json() == first.json()
    assert not unknown_start.requests
    async with postgres.engine.connect() as connection:
        assert (
            await connection.scalars(
                select(models.ReviewJob.id).where(
                    models.ReviewJob.repository_id == unknown_start.scope.repository
                )
            )
        ).all() == [UUID(first.json()["review_id"])]


async def test_admission_quota_refusal_rolls_back_new_change_request(
    unknown_start: BrowserStart, postgres: PostgresHarness
) -> None:
    async with postgres.engine.begin() as connection:
        await connection.execute(
            update(models.RepositorySettings)
            .where(models.RepositorySettings.id == unknown_start.scope.settings)
            .values(max_active_jobs=0)
        )
    response = await unknown_start.start()
    assert response.status_code == HTTPStatus.TOO_MANY_REQUESTS
    assert response.json()["code"] == "QUOTA_EXCEEDED"
    assert len(unknown_start.requests) == FIRST_START_READS
    await assert_no_materialization(unknown_start, postgres)


async def test_personal_permission_denial_precedes_missing_pr_discovery(
    unknown_start: BrowserStart, postgres: PostgresHarness
) -> None:
    unknown_start.provider = lambda request: httpx.Response(
        200, json={"id": 123456, "permissions": {"pull": False}}
    )
    response = await unknown_start.start()
    assert response.status_code == HTTPStatus.FORBIDDEN
    assert response.json()["code"] == "ACCESS_DENIED"
    assert [request.url.path for request in unknown_start.requests] == [
        "/repositories/123456"
    ]
    await assert_no_materialization(unknown_start, postgres)


STALE_SAVED_HEAD = "d" * 40


async def test_start_after_push_refreshes_saved_pr_head_from_github(
    browser_start: BrowserStart, postgres: PostgresHarness
) -> None:
    # The PR was first seen at an older commit; GitHub now reports HEAD.
    async with postgres.engine.begin() as connection:
        await connection.execute(
            update(models.ChangeRequest)
            .where(models.ChangeRequest.id == browser_start.scope.change)
            .values(head_sha=STALE_SAVED_HEAD)
        )

    response = await browser_start.start()

    assert response.status_code == HTTPStatus.ACCEPTED
    review_id = UUID(response.json()["review_id"])
    async with postgres.engine.connect() as connection:
        assert (
            await connection.scalar(
                select(models.ChangeRequest.head_sha).where(
                    models.ChangeRequest.id == browser_start.scope.change
                )
            )
            == HEAD
        )
        assert (
            await connection.scalar(
                select(models.ReviewJob.requested_head_sha).where(
                    models.ReviewJob.id == review_id
                )
            )
            == HEAD
        )


async def test_start_for_superseded_head_is_conflict_and_admits_nothing(
    browser_start: BrowserStart, postgres: PostgresHarness
) -> None:
    # Saved row and request both name the old commit, but GitHub has moved on.
    async with postgres.engine.begin() as connection:
        await connection.execute(
            update(models.ChangeRequest)
            .where(models.ChangeRequest.id == browser_start.scope.change)
            .values(head_sha=STALE_SAVED_HEAD)
        )

    response = await browser_start.client.post(
        browser_start.scope.url,
        json={"requested_head_sha": STALE_SAVED_HEAD},
        headers=browser_start.headers,
    )

    assert response.status_code == HTTPStatus.CONFLICT
    assert response.json()["code"] == "PR_HEAD_MISMATCH"
    await assert_no_admission(browser_start, postgres)
