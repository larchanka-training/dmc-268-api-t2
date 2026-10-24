import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_postgres_integration import (
    PostgresHarness,
    ReviewScope,
    _create_change_request,
    _create_repository,
    _create_review_fixture,
    _create_review_job,
    _create_settings,
)
from test_review_results_api import assert_schema

from app.api.auth import (
    AuthenticatedPrincipal,
    VerifiedMutationIdentity,
    get_authenticated_principal,
    get_verified_mutation_identity,
)
from app.api.review_start_dependencies import (
    get_review_configuration_resolver,
    get_review_start_authorizer,
)
from app.application.github_ports import (
    GithubFailure,
    GithubPort,
    GithubPullRequest,
    GithubPullRequestReader,
    GithubRequest,
    GithubSnapshot,
)
from app.application.review_start_ports import (
    FrozenAnalysisConfiguration,
    RepositoryIdentity,
)
from app.core import github_runtime
from app.core.config import Settings
from app.db import models
from app.db.session import get_session
from app.domain.frozen_configuration import ModelRoute, admission_policy
from app.domain.settings import compute_rules_digest
from app.main import create_app
from app.review.configuration import (
    FrozenAdmissionResolver,
    FrozenConfigurationError,
    RuntimeProfile,
)

RETRY_AFTER_SECONDS = 30
# Saved scope PR and the second PR from add_second_pr.
FAKE_PR_NUMBERS = frozenset({13, 14})
CONCURRENT_REQUESTS = 2


pytest_plugins = ("test_postgres_integration",)


@pytest_asyncio.fixture
async def app() -> AsyncIterator[FastAPI]:
    application = create_app()
    yield application
    application.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as value:
        yield value


async def test_production_start_rejects_forged_identity(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/api/v1/repositories/00000000-0000-0000-0000-000000000001/pull-requests/13/reviews",
        json={"requested_head_sha": "head-sha"},
        headers={
            "Origin": "https://trusted.example",
            "Idempotency-Key": "one",
            "X-CSRF-Token": "forged",
            "X-User-Id": "00000000-0000-0000-0000-000000000001",
            "Cookie": "session=forged; csrf=forged",
        },
    )
    assert response.status_code == HTTPStatus.UNAUTHORIZED
    assert response.json()["code"] == "AUTHENTICATION_REQUIRED"
    assert_schema(response, "Error")


async def test_principal_alone_does_not_verify_browser_mutation(
    client: httpx.AsyncClient, app: FastAPI
) -> None:

    app.dependency_overrides[get_authenticated_principal] = lambda: (
        AuthenticatedPrincipal(UUID("00000000-0000-0000-0000-000000000001"))
    )
    response = await client.post(
        "/api/v1/repositories/00000000-0000-0000-0000-000000000001/pull-requests/13/reviews",
        json={"requested_head_sha": "head-sha"},
        headers={"Origin": "https://trusted.example", "X-CSRF-Token": "forged"},
    )
    assert response.status_code == HTTPStatus.UNAUTHORIZED
    assert response.json()["code"] == "AUTHENTICATION_REQUIRED"
    assert_schema(response, "Error")


async def test_unverified_mutation_identity_is_rejected(
    client: httpx.AsyncClient, app: FastAPI
) -> None:

    app.dependency_overrides[get_verified_mutation_identity] = lambda: (
        VerifiedMutationIdentity(
            UUID("00000000-0000-0000-0000-000000000001"), True, False, True
        )
    )
    response = await client.post(
        "/api/v1/repositories/00000000-0000-0000-0000-000000000001/pull-requests/13/reviews",
        json={"requested_head_sha": "head-sha"},
    )
    assert response.status_code == HTTPStatus.FORBIDDEN
    assert response.json()["code"] == "ACCESS_DENIED"


HEAD = "a" * 40
BASE = "b" * 40
HEADERS = {
    "Origin": "https://trusted.example",
    "Idempotency-Key": "one",
    "X-CSRF-Token": "trusted",
}


@dataclass
class StartScope:
    repository: uuid.UUID
    change: uuid.UUID
    settings: uuid.UUID
    user: uuid.UUID

    @property
    def url(self) -> str:
        return f"/api/v1/repositories/{self.repository}/pull-requests/13/reviews"


@pytest_asyncio.fixture
async def scope(postgres: PostgresHarness, app: FastAPI) -> AsyncIterator[StartScope]:
    user = uuid.uuid4()
    async with postgres.engine.begin() as conn:
        repository = await _create_repository(postgres, conn, external_id="123456")
        settings = await _create_settings(
            postgres,
            conn,
            repository,
            rules_digest=compute_rules_digest({"instructions": []}),
        )
        change = await _create_change_request(postgres, conn, repository)
        await conn.execute(
            update(models.Repository)
            .where(models.Repository.id == repository)
            .values(current_settings_id=settings)
        )
        await conn.execute(
            update(models.ChangeRequest)
            .where(models.ChangeRequest.id == change)
            .values(
                external_number="13",
                head_sha=HEAD,
                base_sha=BASE,
                source_repository_external_id="123456",
            )
        )
        await conn.execute(insert(models.User).values(id=user))
        await conn.execute(
            insert(models.RepositoryAccess).values(
                id=uuid.uuid4(), user_id=user, repository_id=repository, role="reviewer"
            )
        )
    factory = async_sessionmaker(postgres.engine, expire_on_commit=False)

    async def session() -> AsyncIterator[AsyncSession]:
        async with factory() as value:
            yield value

    async def identity() -> VerifiedMutationIdentity:
        return VerifiedMutationIdentity(user, True, True, True)

    app.dependency_overrides[get_session] = session
    app.dependency_overrides[get_verified_mutation_identity] = identity
    yield StartScope(repository, change, settings, user)
    async with postgres.engine.begin() as conn:
        reviews = select(models.ReviewJob.id).where(
            models.ReviewJob.repository_id == repository
        )
        chunks = select(models.ChunkResult.id).where(
            models.ChunkResult.review_job_id.in_(reviews)
        )
        await conn.execute(
            delete(models.Finding).where(models.Finding.chunk_result_id.in_(chunks))
        )
        for entity in (
            models.IdempotencyRecord,
            models.OutboxEvent,
            models.Publication,
            models.TaskLease,
            models.ReviewEvent,
            models.ChunkResult,
            models.ContextPayload,
        ):
            await conn.execute(delete(entity).where(entity.review_job_id.in_(reviews)))
        await conn.execute(
            delete(models.ReviewJob).where(models.ReviewJob.repository_id == repository)
        )
        await conn.execute(
            delete(models.QuotaUsage).where(
                models.QuotaUsage.repository_id == repository
            )
        )
        await conn.execute(
            delete(models.RepositoryAccess).where(
                models.RepositoryAccess.repository_id == repository
            )
        )
        await conn.execute(delete(models.User).where(models.User.id == user))


@pytest.mark.parametrize("existing_foreign_repository", [False, True])
async def test_missing_repository_has_canonical_not_found(
    client: httpx.AsyncClient,
    scope: StartScope,
    postgres: PostgresHarness,
    existing_foreign_repository: bool,
) -> None:
    repository_id = uuid.uuid4()
    if existing_foreign_repository:
        async with postgres.engine.begin() as conn:
            repository_id = await _create_repository(postgres, conn)
    response = await client.post(
        scope.url.replace(str(scope.repository), str(repository_id)),
        json={"requested_head_sha": HEAD},
        headers=HEADERS,
    )
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json()["code"] == "RESOURCE_NOT_FOUND"
    assert_schema(response, "Error")


async def test_missing_pull_request_is_not_found(
    client: httpx.AsyncClient,
    scope: StartScope,
    providers: FakeStartGithub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "httpx._client.AsyncHTTPTransport",
        lambda **kwargs: httpx.MockTransport(lambda request: httpx.Response(404)),
    )
    response = await client.post(
        scope.url.replace("/13/", "/99/"),
        json={"requested_head_sha": HEAD},
        headers=HEADERS,
    )
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json()["code"] == "RESOURCE_NOT_FOUND"


async def test_invalid_start_body_is_canonical_validation_error(
    client: httpx.AsyncClient, scope: StartScope
) -> None:
    response = await client.post(
        scope.url,
        json={"requested_head_sha": HEAD, "model_ref": "user-owned"},
        headers=HEADERS,
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()["code"] == "VALIDATION_FAILED"
    assert_schema(response, "Error")


async def test_saved_head_mismatch_is_conflict(
    client: httpx.AsyncClient, scope: StartScope, providers: FakeStartGithub
) -> None:
    response = await client.post(
        scope.url, json={"requested_head_sha": "c" * 40}, headers=HEADERS
    )
    assert response.status_code == HTTPStatus.CONFLICT
    assert response.json()["code"] == "PR_HEAD_MISMATCH"


async def test_closed_saved_pr_is_not_found(
    client: httpx.AsyncClient, scope: StartScope, postgres: PostgresHarness
) -> None:
    async with postgres.engine.begin() as conn:
        await conn.execute(
            update(models.ChangeRequest)
            .where(models.ChangeRequest.id == scope.change)
            .values(current_status="closed")
        )
    response = await client.post(
        scope.url, json={"requested_head_sha": HEAD}, headers=HEADERS
    )
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json()["code"] == "RESOURCE_NOT_FOUND"
    assert_schema(response, "Error")


@pytest.mark.parametrize("field", ["rules", "ignores"])
@pytest.mark.parametrize("value", [{"legacy": "keep"}, None, [], "", {"globs": None}])
async def test_legacy_settings_are_rejected_without_rewriting(
    client: httpx.AsyncClient,
    scope: StartScope,
    postgres: PostgresHarness,
    field: str,
    value: object,
) -> None:
    async with postgres.engine.begin() as conn:
        await conn.execute(
            update(models.RepositorySettings)
            .where(models.RepositorySettings.id == scope.settings)
            .values(**{field: value})
        )
    response = await client.post(
        scope.url, json={"requested_head_sha": HEAD}, headers=HEADERS
    )
    assert response.status_code == HTTPStatus.CONFLICT
    assert response.json()["code"] == "LEGACY_SETTINGS_UNSUPPORTED"
    async with postgres.engine.connect() as conn:
        assert (
            await conn.scalar(
                select(getattr(models.RepositorySettings, field)).where(
                    models.RepositorySettings.id == scope.settings
                )
            )
            == value
        )
        assert not (
            await conn.execute(
                select(models.ReviewJob.id).where(
                    models.ReviewJob.repository_id == scope.repository
                )
            )
        ).all()


async def test_required_start_headers_have_canonical_validation(
    client: httpx.AsyncClient, scope: StartScope
) -> None:
    response = await client.post(
        scope.url,
        json={"requested_head_sha": HEAD},
        headers={"Origin": "https://trusted.example"},
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()["code"] == "VALIDATION_FAILED"


async def test_request_id_is_safe_in_start_error(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/v1/repositories/missing/pull-requests/13/reviews",
        headers={"X-Request-Id": "unsafe\r\nInjected: value"},
        json={"requested_head_sha": HEAD},
    )
    assert response.status_code == HTTPStatus.UNAUTHORIZED
    request_id = response.json()["request_id"]
    assert request_id.isascii() and all(c.isalnum() or c in "_.-" for c in request_id)
    assert request_id == response.headers["X-Request-Id"]


@pytest.mark.parametrize(
    "parent_scope", ["missing", "foreign_repository", "foreign_pr"]
)
async def test_bad_rerun_reference_is_rejected(
    client: httpx.AsyncClient,
    scope: StartScope,
    postgres: PostgresHarness,
    parent_scope: str,
) -> None:
    parent_id = uuid.uuid4()
    if parent_scope == "foreign_repository":
        async with postgres.engine.begin() as conn:
            _, parent_id = await _create_review_fixture(postgres, conn)
    elif parent_scope == "foreign_pr":
        await add_second_pr(postgres, scope)
        async with postgres.engine.begin() as conn:
            change_id = await conn.scalar(
                select(models.ChangeRequest.id).where(
                    models.ChangeRequest.repository_id == scope.repository,
                    models.ChangeRequest.external_number == "14",
                )
            )
            assert change_id is not None
            parent_id = await _create_review_job(
                postgres,
                conn,
                ReviewScope(scope.repository, change_id, scope.settings),
            )
    response = await client.post(
        scope.url,
        json={"requested_head_sha": HEAD, "rerun_of": str(parent_id)},
        headers=HEADERS,
    )
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert_schema(response, "Error")


async def test_unavailable_admission_is_retryable_and_atomic(
    client: httpx.AsyncClient, scope: StartScope, postgres: PostgresHarness
) -> None:
    response = await client.post(
        scope.url, json={"requested_head_sha": HEAD}, headers=HEADERS
    )
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json()["code"] == "SERVICE_UNAVAILABLE"
    assert response.json()["retryable"] is True
    async with postgres.engine.connect() as conn:
        assert not (
            await conn.execute(
                select(models.ReviewJob.id).where(
                    models.ReviewJob.repository_id == scope.repository
                )
            )
        ).all()
        assert not (
            await conn.execute(
                select(models.QuotaUsage.id).where(
                    models.QuotaUsage.repository_id == scope.repository
                )
            )
        ).all()


async def test_provider_denial_is_canonical_and_atomic(
    client: httpx.AsyncClient, scope: StartScope, app: FastAPI
) -> None:

    class Denied:
        async def authorize_start(
            self, *, user_id: uuid.UUID, repository: RepositoryIdentity
        ) -> None:
            raise GithubFailure("VCS_ACCESS_DENIED", retryable=False)

    app.dependency_overrides[get_review_start_authorizer] = Denied
    response = await client.post(
        scope.url, json={"requested_head_sha": HEAD}, headers=HEADERS
    )
    assert response.status_code == HTTPStatus.FORBIDDEN
    assert response.json()["code"] == "ACCESS_DENIED"
    assert_schema(response, "Error")


async def test_provider_failure_is_safe_retryable_unavailable(
    client: httpx.AsyncClient, scope: StartScope, app: FastAPI
) -> None:

    class Unavailable:
        async def authorize_start(
            self, *, user_id: uuid.UUID, repository: RepositoryIdentity
        ) -> None:
            raise GithubFailure("VCS_UNAVAILABLE", retryable=True, retry_after=30)

    app.dependency_overrides[get_review_start_authorizer] = Unavailable
    response = await client.post(
        scope.url, json={"requested_head_sha": HEAD}, headers=HEADERS
    )
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json()["retryable"] is True
    assert response.json()["retry_after"] == RETRY_AFTER_SECONDS
    assert_schema(response, "Error")


async def test_github_permanent_failure_is_not_retryable(
    client: httpx.AsyncClient,
    scope: StartScope,
    providers: FakeStartGithub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Untrustworthy:
        async def fetch(self, request: GithubRequest) -> GithubSnapshot:
            raise GithubFailure("DIFF_UNTRUSTWORTHY", retryable=False)

    @asynccontextmanager
    async def gateway(settings: Settings) -> AsyncIterator[GithubPort]:
        yield Untrustworthy()

    monkeypatch.setattr(github_runtime, "github_connection", gateway)
    response = await client.post(
        scope.url, json={"requested_head_sha": HEAD}, headers=HEADERS
    )
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json()["code"] == "SERVICE_UNAVAILABLE"
    assert response.json()["retryable"] is False
    assert response.json()["retry_after"] is None
    assert_schema(response, "Error")


@pytest.mark.parametrize("retryable", [False, True], ids=["permanent", "transient"])
async def test_github_retry_after_is_only_reported_for_retryable_failure(
    client: httpx.AsyncClient,
    scope: StartScope,
    providers: FakeStartGithub,
    monkeypatch: pytest.MonkeyPatch,
    retryable: bool,
) -> None:
    class Unavailable:
        async def fetch(self, request: GithubRequest) -> GithubSnapshot:
            raise GithubFailure(
                "VCS_UNAVAILABLE",
                retryable=retryable,
                retry_after=RETRY_AFTER_SECONDS,
            )

    @asynccontextmanager
    async def gateway(settings: Settings) -> AsyncIterator[GithubPort]:
        yield Unavailable()

    monkeypatch.setattr(github_runtime, "github_connection", gateway)
    response = await client.post(
        scope.url, json={"requested_head_sha": HEAD}, headers=HEADERS
    )
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json()["code"] == "SERVICE_UNAVAILABLE"
    assert response.json()["retryable"] is retryable
    if retryable:
        assert response.json()["retry_after"] == RETRY_AFTER_SECONDS
    else:
        assert response.json()["retry_after"] is None
    assert_schema(response, "Error")


async def test_github_revalidation_refuses_stale_pr(
    client: httpx.AsyncClient,
    scope: StartScope,
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    class Allowed:
        async def authorize_start(
            self, *, user_id: uuid.UUID, repository: RepositoryIdentity
        ) -> None:
            return None

    class Stale:
        async def fetch(self, request: GithubRequest) -> GithubSnapshot:
            raise GithubFailure("PR_STALE_OR_CLOSED", retryable=False)

    @asynccontextmanager
    async def gateway(settings: Settings) -> AsyncIterator[GithubPort]:
        yield Stale()

    app.dependency_overrides[get_review_start_authorizer] = Allowed
    monkeypatch.setattr(github_runtime, "github_connection", gateway)
    fake_pull_requests(monkeypatch)
    response = await client.post(
        scope.url, json={"requested_head_sha": HEAD}, headers=HEADERS
    )
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json()["code"] == "RESOURCE_NOT_FOUND"
    assert_schema(response, "Error")


async def test_invalid_pr_number_has_canonical_error(
    client: httpx.AsyncClient, scope: StartScope
) -> None:
    response = await client.post(
        scope.url.replace("/13/", "/oops/"),
        json={"requested_head_sha": HEAD},
        headers=HEADERS,
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json().get("code") == "VALIDATION_FAILED"
    assert_schema(response, "Error")


class AllowedAuthorizer:
    async def authorize_start(
        self, *, user_id: uuid.UUID, repository: RepositoryIdentity
    ) -> None:
        return None


class FakeStartGithub:
    def __init__(self) -> None:
        self.calls = 0
        # Live PR as GitHub reports it; matches the saved scope row (no push yet).
        self.pull_request = GithubPullRequest(
            head_sha=HEAD,
            base_sha=BASE,
            source_repository_external_id="123456",
            source_ref="feature/postgres-validation",
            base_ref="main",
            title="Fix division",
            description="Description",
        )

    async def get_pull_request(
        self, owner: str, name: str, repository_external_id: str, number: int
    ) -> GithubPullRequest:
        # Same-repository PR: the source repository is the base repository.
        return replace(
            self.pull_request, source_repository_external_id=repository_external_id
        )

    async def fetch(self, request: GithubRequest) -> GithubSnapshot:
        self.calls += 1
        return GithubSnapshot(
            metadata={
                "snapshot": {
                    "provider": "github",
                    "head_sha": request.head_sha,
                    "base_sha": request.base_sha,
                    "base_repository_id": request.repository_external_id,
                    "head_repository_id": request.repository_external_id,
                },
                "metadata": {
                    "head_ref": "feature/postgres-validation",
                    "base_ref": "main",
                },
            },
            raw_diff="",
        )


def fake_pull_requests(
    monkeypatch: pytest.MonkeyPatch, github: FakeStartGithub | None = None
) -> FakeStartGithub:
    """Serve live PR reads for the saved scope PRs; other numbers stay real HTTP."""
    fake = github or FakeStartGithub()
    real_pull_requests = github_runtime.github_pull_request_connection

    class PullRequests:
        def __init__(self, real: GithubPullRequestReader) -> None:
            self.real = real

        async def get_pull_request(
            self, owner: str, name: str, repository_external_id: str, number: int
        ) -> GithubPullRequest:
            if number in FAKE_PR_NUMBERS:
                return await fake.get_pull_request(
                    owner, name, repository_external_id, number
                )
            return await self.real.get_pull_request(
                owner, name, repository_external_id, number
            )

    @asynccontextmanager
    async def pull_requests(
        settings: Settings,
    ) -> AsyncIterator[GithubPullRequestReader]:
        async with real_pull_requests(settings) as real:
            yield PullRequests(real)

    monkeypatch.setattr(github_runtime, "github_pull_request_connection", pull_requests)
    return fake


@pytest.fixture
def providers(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> FakeStartGithub:
    github = FakeStartGithub()

    @asynccontextmanager
    async def gateway(settings: Settings) -> AsyncIterator[GithubPort]:
        yield github

    app.dependency_overrides[get_review_start_authorizer] = AllowedAuthorizer
    monkeypatch.setattr(github_runtime, "github_connection", gateway)
    fake_pull_requests(monkeypatch, github)
    return github


async def test_production_configuration_resolver_is_fail_closed(
    client: httpx.AsyncClient, scope: StartScope, providers: FakeStartGithub
) -> None:
    response = await client.post(
        scope.url, json={"requested_head_sha": HEAD}, headers=HEADERS
    )
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json()["message"] == "Review configuration is unavailable."
    assert_schema(response, "Error")


@pytest.fixture
def configured(
    app: FastAPI, providers: FakeStartGithub, postgres: PostgresHarness
) -> FakeStartGithub:

    app.dependency_overrides[get_review_configuration_resolver] = lambda: (
        FrozenAdmissionResolver(
            async_sessionmaker(postgres.engine, expire_on_commit=False),
            Path(__file__).resolve().parents[1] / ".agents/reviewer",
            {
                "eurorouter/review-v1": RuntimeProfile(
                    provider="eurorouter",
                    base_url="https://llm.invalid/v1",
                    api_keys=("synthetic-test-key",),
                    allowed_models=("test-model",),
                )
            },
            primary=ModelRoute(
                profile_ref="eurorouter/review-v1",
                model_ref="test-model",
                model_digest=None,
            ),
            policy=admission_policy("0" * 64),
        )
    )
    return providers


async def test_new_start_is_durable_and_atomic(
    client: httpx.AsyncClient,
    scope: StartScope,
    configured: FakeStartGithub,
    postgres: PostgresHarness,
) -> None:

    response = await client.post(
        scope.url, json={"requested_head_sha": HEAD}, headers=HEADERS
    )
    assert response.status_code == HTTPStatus.ACCEPTED
    assert_schema(response, "StartReviewResponse")
    data = response.json()
    assert data["status"] == "QUEUED"
    assert (
        data["resource_url"]
        == response.headers["Location"]
        == f"/api/v1/reviews/{data['review_id']}"
    )
    async with async_sessionmaker(postgres.engine)() as session:
        review = await session.get(models.ReviewJob, uuid.UUID(data["review_id"]))
        assert review is not None
        assert (
            review.repository_id,
            review.change_request_id,
            review.repository_settings_id,
        ) == (scope.repository, scope.change, scope.settings)
        assert (
            review.requested_head_sha,
            review.model_ref,
            review.prompt_version,
            review.rules_digest,
        ) == (
            HEAD,
            "test-model",
            "v2",
            compute_rules_digest({"instructions": []}),
        )
        publication = (
            await session.scalars(
                select(models.Publication).where(
                    models.Publication.review_job_id == review.id
                )
            )
        ).one()
        outbox = (
            await session.scalars(
                select(models.OutboxEvent).where(
                    models.OutboxEvent.review_job_id == review.id
                )
            )
        ).one()
        record = (
            await session.scalars(
                select(models.IdempotencyRecord).where(
                    models.IdempotencyRecord.review_job_id == review.id
                )
            )
        ).one()
        quota = (
            await session.scalars(
                select(models.QuotaUsage).where(
                    models.QuotaUsage.repository_id == scope.repository
                )
            )
        ).one()
        assert publication.status == "NOT_READY"
        assert outbox.task_kind == "analyze" and outbox.broker_published_at is None
        assert record.expires_at - record.created_at == timedelta(hours=24)
        assert (
            quota.type == "starts" and quota.value == 1 and quota.user_id == scope.user
        )
        assert quota.window_start is not None
        assert (
            quota.window_start.minute
            == quota.window_start.second
            == quota.window_start.microsecond
            == 0
        )


async def test_replay_keeps_frozen_configuration_and_does_not_revalidate(
    client: httpx.AsyncClient,
    scope: StartScope,
    configured: FakeStartGithub,
    postgres: PostgresHarness,
    app: FastAPI,
) -> None:
    first = await client.post(
        scope.url, json={"requested_head_sha": HEAD}, headers=HEADERS
    )
    review_id = uuid.UUID(first.json()["review_id"])
    async with postgres.engine.begin() as conn:
        initial_expiry = await conn.scalar(
            select(models.IdempotencyRecord.expires_at).where(
                models.IdempotencyRecord.review_job_id == review_id
            )
        )
        await conn.execute(
            update(models.ReviewJob)
            .where(models.ReviewJob.id == review_id)
            .values(
                status="FAILED",
                finished_at=datetime.now(UTC),
            )
        )
        await conn.execute(
            update(models.RepositorySettings)
            .where(models.RepositorySettings.id == scope.settings)
            .values(rules={"legacy": "keep"})
        )
    app.dependency_overrides.pop(get_review_start_authorizer)
    app.dependency_overrides.pop(get_review_configuration_resolver)
    second = await client.post(
        scope.url, json={"requested_head_sha": HEAD}, headers=HEADERS
    )
    assert second.status_code == HTTPStatus.OK
    assert_schema(second, "StartReviewReplayResponse")
    assert (
        second.json()["review_id"] == str(review_id)
        and second.json()["status"] == "FAILED"
    )
    assert configured.calls == 1
    async with async_sessionmaker(postgres.engine)() as session:
        quota = (
            await session.scalars(
                select(models.QuotaUsage).where(
                    models.QuotaUsage.repository_id == scope.repository
                )
            )
        ).one()
        records = (
            await session.scalars(
                select(models.IdempotencyRecord).where(
                    models.IdempotencyRecord.review_job_id == review_id
                )
            )
        ).all()
        outbox = (
            await session.scalars(
                select(models.OutboxEvent).where(
                    models.OutboxEvent.review_job_id == review_id
                )
            )
        ).all()
        assert quota.value == len(records) == len(outbox) == 1
        assert records[0].expires_at == initial_expiry
        review = await session.get(models.ReviewJob, review_id)
        assert review is not None and review.repository_settings_id == scope.settings


async def test_same_key_different_body_is_idempotency_conflict(
    client: httpx.AsyncClient, scope: StartScope, configured: FakeStartGithub
) -> None:
    first = await client.post(
        scope.url, json={"requested_head_sha": HEAD}, headers=HEADERS
    )
    assert first.status_code == HTTPStatus.ACCEPTED
    second = await client.post(
        scope.url, json={"requested_head_sha": "c" * 40}, headers=HEADERS
    )
    assert second.status_code == HTTPStatus.CONFLICT
    assert second.json()["code"] == "IDEMPOTENCY_CONFLICT"
    assert_schema(second, "Error")
    assert configured.calls == 1


async def test_concurrent_identical_requests_create_one_durable_run(
    client: httpx.AsyncClient,
    scope: StartScope,
    configured: FakeStartGithub,
    postgres: PostgresHarness,
) -> None:

    both_ready = asyncio.Event()
    original = configured.fetch
    entered = 0

    async def fetch(request: GithubRequest) -> GithubSnapshot:
        nonlocal entered
        entered += 1
        if entered == CONCURRENT_REQUESTS:
            both_ready.set()
        await asyncio.wait_for(both_ready.wait(), 10)
        return await original(request)

    configured.fetch = fetch  # type: ignore[method-assign]
    responses = await asyncio.gather(
        *(
            client.post(scope.url, json={"requested_head_sha": HEAD}, headers=HEADERS)
            for _ in range(CONCURRENT_REQUESTS)
        )
    )
    assert sorted(response.status_code for response in responses) == [
        HTTPStatus.OK,
        HTTPStatus.ACCEPTED,
    ]
    assert len({response.json()["review_id"] for response in responses}) == 1
    async with async_sessionmaker(postgres.engine)() as session:
        assert (
            len(
                (
                    await session.scalars(
                        select(models.ReviewJob.id).where(
                            models.ReviewJob.repository_id == scope.repository
                        )
                    )
                ).all()
            )
            == 1
        )
        assert (
            await session.scalars(
                select(models.QuotaUsage.value).where(
                    models.QuotaUsage.repository_id == scope.repository
                )
            )
        ).one() == 1
        review_id = uuid.UUID(responses[0].json()["review_id"])
        for entity in (
            models.Publication,
            models.OutboxEvent,
            models.IdempotencyRecord,
        ):
            assert (
                len(
                    (
                        await session.scalars(
                            select(entity.id).where(entity.review_job_id == review_id)
                        )
                    ).all()
                )
                == 1
            )


async def test_starts_quota_zero_rejects_without_side_effects(
    client: httpx.AsyncClient,
    scope: StartScope,
    configured: FakeStartGithub,
    postgres: PostgresHarness,
) -> None:
    async with postgres.engine.begin() as conn:
        await conn.execute(
            update(models.RepositorySettings)
            .where(models.RepositorySettings.id == scope.settings)
            .values(max_starts_per_hour=0)
        )
    response = await client.post(
        scope.url, json={"requested_head_sha": HEAD}, headers=HEADERS
    )
    assert response.status_code == HTTPStatus.TOO_MANY_REQUESTS
    assert response.json()["code"] == "QUOTA_EXCEEDED"
    assert_schema(response, "Error")
    async with async_sessionmaker(postgres.engine)() as session:
        assert not (
            await session.scalars(
                select(models.ReviewJob.id).where(
                    models.ReviewJob.repository_id == scope.repository
                )
            )
        ).all()
        assert not (
            await session.scalars(
                select(models.QuotaUsage.id).where(
                    models.QuotaUsage.repository_id == scope.repository
                )
            )
        ).all()


async def test_active_quota_zero_rejects_without_side_effects(
    client: httpx.AsyncClient,
    scope: StartScope,
    configured: FakeStartGithub,
    postgres: PostgresHarness,
) -> None:
    async with postgres.engine.begin() as conn:
        await conn.execute(
            update(models.RepositorySettings)
            .where(models.RepositorySettings.id == scope.settings)
            .values(max_active_jobs=0)
        )
    response = await client.post(
        scope.url, json={"requested_head_sha": HEAD}, headers=HEADERS
    )
    assert response.status_code == HTTPStatus.TOO_MANY_REQUESTS
    assert response.json()["code"] == "QUOTA_EXCEEDED"
    assert_schema(response, "Error")
    async with async_sessionmaker(postgres.engine)() as session:
        assert not (
            await session.scalars(
                select(models.ReviewJob.id).where(
                    models.ReviewJob.repository_id == scope.repository
                )
            )
        ).all()
        assert not (
            await session.scalars(
                select(models.QuotaUsage.id).where(
                    models.QuotaUsage.repository_id == scope.repository
                )
            )
        ).all()


async def add_second_pr(postgres: PostgresHarness, scope: StartScope) -> str:
    async with postgres.engine.begin() as conn:
        change = await _create_change_request(postgres, conn, scope.repository)
        await conn.execute(
            update(models.ChangeRequest)
            .where(models.ChangeRequest.id == change)
            .values(
                external_number="14",
                head_sha=HEAD,
                base_sha=BASE,
                source_repository_external_id="123456",
            )
        )
    return scope.url.replace("/13/", "/14/")


async def test_concurrent_different_prs_respect_starts_quota(
    client: httpx.AsyncClient,
    scope: StartScope,
    configured: FakeStartGithub,
    postgres: PostgresHarness,
) -> None:

    second_url = await add_second_pr(postgres, scope)
    async with postgres.engine.begin() as conn:
        await conn.execute(
            update(models.RepositorySettings)
            .where(models.RepositorySettings.id == scope.settings)
            .values(max_starts_per_hour=1)
        )
    ready = asyncio.Event()
    original = configured.fetch
    entered = 0

    async def fetch(request: GithubRequest) -> GithubSnapshot:
        nonlocal entered
        entered += 1
        if entered == CONCURRENT_REQUESTS:
            ready.set()
        await asyncio.wait_for(ready.wait(), 10)
        return await original(request)

    configured.fetch = fetch  # type: ignore[method-assign]
    responses = await asyncio.gather(
        client.post(scope.url, json={"requested_head_sha": HEAD}, headers=HEADERS),
        client.post(
            second_url,
            json={"requested_head_sha": HEAD},
            headers={**HEADERS, "Idempotency-Key": "two"},
        ),
    )
    assert sorted(response.status_code for response in responses) == [
        HTTPStatus.ACCEPTED,
        HTTPStatus.TOO_MANY_REQUESTS,
    ]
    rejected = next(
        response
        for response in responses
        if response.status_code == HTTPStatus.TOO_MANY_REQUESTS
    )
    assert_schema(rejected, "Error")
    assert rejected.json()["code"] == "QUOTA_EXCEEDED"
    async with async_sessionmaker(postgres.engine)() as session:
        assert (
            await session.scalars(
                select(models.QuotaUsage.value).where(
                    models.QuotaUsage.repository_id == scope.repository
                )
            )
        ).one() == 1
        assert (
            len(
                (
                    await session.scalars(
                        select(models.ReviewJob.id).where(
                            models.ReviewJob.repository_id == scope.repository
                        )
                    )
                ).all()
            )
            == 1
        )


async def test_concurrent_different_prs_respect_active_quota(
    client: httpx.AsyncClient,
    scope: StartScope,
    configured: FakeStartGithub,
    postgres: PostgresHarness,
) -> None:

    second_url = await add_second_pr(postgres, scope)
    async with postgres.engine.begin() as conn:
        await conn.execute(
            update(models.RepositorySettings)
            .where(models.RepositorySettings.id == scope.settings)
            .values(max_active_jobs=1)
        )
    ready = asyncio.Event()
    original = configured.fetch
    entered = 0

    async def fetch(request: GithubRequest) -> GithubSnapshot:
        nonlocal entered
        entered += 1
        if entered == CONCURRENT_REQUESTS:
            ready.set()
        await asyncio.wait_for(ready.wait(), 10)
        return await original(request)

    configured.fetch = fetch  # type: ignore[method-assign]
    responses = await asyncio.gather(
        client.post(scope.url, json={"requested_head_sha": HEAD}, headers=HEADERS),
        client.post(
            second_url,
            json={"requested_head_sha": HEAD},
            headers={**HEADERS, "Idempotency-Key": "two"},
        ),
    )
    assert sorted(response.status_code for response in responses) == [
        HTTPStatus.ACCEPTED,
        HTTPStatus.TOO_MANY_REQUESTS,
    ]
    rejected = next(
        response
        for response in responses
        if response.status_code == HTTPStatus.TOO_MANY_REQUESTS
    )
    assert_schema(rejected, "Error")
    assert rejected.json()["code"] == "QUOTA_EXCEEDED"
    async with async_sessionmaker(postgres.engine)() as session:
        assert (
            await session.scalars(
                select(models.QuotaUsage.value).where(
                    models.QuotaUsage.repository_id == scope.repository
                )
            )
        ).one() == 1
        assert (
            len(
                (
                    await session.scalars(
                        select(models.ReviewJob.id).where(
                            models.ReviewJob.repository_id == scope.repository
                        )
                    )
                ).all()
            )
            == 1
        )


async def test_terminal_job_releases_active_capacity_and_rerun_has_lineage(
    client: httpx.AsyncClient,
    scope: StartScope,
    configured: FakeStartGithub,
    postgres: PostgresHarness,
) -> None:

    async with postgres.engine.begin() as conn:
        await conn.execute(
            update(models.RepositorySettings)
            .where(models.RepositorySettings.id == scope.settings)
            .values(max_active_jobs=1)
        )
    first = await client.post(
        scope.url, json={"requested_head_sha": HEAD}, headers=HEADERS
    )
    parent_id = uuid.UUID(first.json()["review_id"])
    async with postgres.engine.begin() as conn:
        await conn.execute(
            update(models.ReviewJob)
            .where(models.ReviewJob.id == parent_id)
            .values(status="COMPLETED", finished_at=datetime.now(UTC))
        )
    rerun = await client.post(
        scope.url,
        json={"requested_head_sha": HEAD, "rerun_of": str(parent_id)},
        headers={**HEADERS, "Idempotency-Key": "rerun"},
    )
    assert rerun.status_code == HTTPStatus.ACCEPTED
    assert_schema(rerun, "StartReviewResponse")
    async with async_sessionmaker(postgres.engine)() as session:
        review = await session.get(
            models.ReviewJob, uuid.UUID(rerun.json()["review_id"])
        )
        assert review is not None and review.rerun_of == parent_id
        assert (
            await session.scalars(
                select(models.QuotaUsage.value).where(
                    models.QuotaUsage.repository_id == scope.repository
                )
            )
        ).one() == CONCURRENT_REQUESTS


async def test_expired_key_is_reusable_after_terminal_outcome(
    client: httpx.AsyncClient,
    scope: StartScope,
    configured: FakeStartGithub,
    postgres: PostgresHarness,
) -> None:

    first = await client.post(
        scope.url, json={"requested_head_sha": HEAD}, headers=HEADERS
    )
    first_id = uuid.UUID(first.json()["review_id"])
    async with postgres.engine.begin() as conn:
        await conn.execute(
            update(models.ReviewJob)
            .where(models.ReviewJob.id == first_id)
            .values(status="FAILED", finished_at=datetime.now(UTC))
        )
        await conn.execute(
            update(models.IdempotencyRecord)
            .where(models.IdempotencyRecord.review_job_id == first_id)
            .values(
                created_at=datetime.now(UTC) - timedelta(hours=25),
                expires_at=datetime.now(UTC) - timedelta(hours=1),
            )
        )
    second = await client.post(
        scope.url, json={"requested_head_sha": HEAD}, headers=HEADERS
    )
    assert second.status_code == HTTPStatus.ACCEPTED
    assert second.json()["review_id"] != str(first_id)
    async with async_sessionmaker(postgres.engine)() as session:
        records = (
            await session.scalars(
                select(models.IdempotencyRecord)
                .join(models.ReviewJob)
                .where(models.ReviewJob.repository_id == scope.repository)
            )
        ).all()
        assert len(records) == 1
        assert records[0].expires_at - records[0].created_at == timedelta(hours=24)
        assert (
            await session.scalars(
                select(models.QuotaUsage.value).where(
                    models.QuotaUsage.repository_id == scope.repository
                )
            )
        ).one() == CONCURRENT_REQUESTS


async def test_expired_key_cannot_duplicate_equivalent_active_review(
    client: httpx.AsyncClient,
    scope: StartScope,
    configured: FakeStartGithub,
    postgres: PostgresHarness,
) -> None:

    first = await client.post(
        scope.url, json={"requested_head_sha": HEAD}, headers=HEADERS
    )
    async with postgres.engine.begin() as conn:
        await conn.execute(
            update(models.IdempotencyRecord)
            .where(
                models.IdempotencyRecord.review_job_id
                == uuid.UUID(first.json()["review_id"])
            )
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    response = await client.post(
        scope.url, json={"requested_head_sha": HEAD}, headers=HEADERS
    )
    assert response.status_code == HTTPStatus.CONFLICT
    assert response.json()["code"] == "REVIEW_ALREADY_ACTIVE"
    assert_schema(response, "Error")
    async with async_sessionmaker(postgres.engine)() as session:
        assert (
            await session.scalars(
                select(models.QuotaUsage.value).where(
                    models.QuotaUsage.repository_id == scope.repository
                )
            )
        ).one() == 1
        assert (
            len(
                (
                    await session.scalars(
                        select(models.ReviewJob.id).where(
                            models.ReviewJob.repository_id == scope.repository
                        )
                    )
                ).all()
            )
            == 1
        )


async def test_current_settings_changed_during_provider_call_rejects_before_writes(
    client: httpx.AsyncClient,
    scope: StartScope,
    configured: FakeStartGithub,
    postgres: PostgresHarness,
) -> None:
    original = configured.fetch

    async def fetch(request: GithubRequest) -> GithubSnapshot:
        async with postgres.engine.begin() as conn:
            new_settings = await _create_settings(
                postgres,
                conn,
                scope.repository,
                rules_digest=compute_rules_digest({"instructions": []}),
            )
            await conn.execute(
                update(models.Repository)
                .where(models.Repository.id == scope.repository)
                .values(current_settings_id=new_settings)
            )
        return await original(request)

    configured.fetch = fetch  # type: ignore[method-assign]
    response = await client.post(
        scope.url, json={"requested_head_sha": HEAD}, headers=HEADERS
    )
    assert response.status_code == HTTPStatus.CONFLICT
    assert response.json()["code"] == "SETTINGS_VERSION_CONFLICT"
    assert_schema(response, "Error")
    async with async_sessionmaker(postgres.engine)() as session:
        assert not (
            await session.scalars(
                select(models.ReviewJob.id).where(
                    models.ReviewJob.repository_id == scope.repository
                )
            )
        ).all()
        assert not (
            await session.scalars(
                select(models.QuotaUsage.id).where(
                    models.QuotaUsage.repository_id == scope.repository
                )
            )
        ).all()


async def test_access_revoked_during_provider_call_is_rechecked(
    client: httpx.AsyncClient,
    scope: StartScope,
    configured: FakeStartGithub,
    postgres: PostgresHarness,
) -> None:
    original = configured.fetch

    async def fetch(request: GithubRequest) -> GithubSnapshot:
        async with postgres.engine.begin() as conn:
            await conn.execute(
                delete(models.RepositoryAccess).where(
                    models.RepositoryAccess.repository_id == scope.repository
                )
            )
        return await original(request)

    configured.fetch = fetch  # type: ignore[method-assign]
    response = await client.post(
        scope.url, json={"requested_head_sha": HEAD}, headers=HEADERS
    )
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert_schema(response, "Error")
    async with async_sessionmaker(postgres.engine)() as session:
        assert not (
            await session.scalars(
                select(models.ReviewJob.id).where(
                    models.ReviewJob.repository_id == scope.repository
                )
            )
        ).all()


@pytest.mark.parametrize(
    ("changes", "status", "code"),
    [
        ({"head_sha": "d" * 40}, HTTPStatus.CONFLICT, "PR_HEAD_MISMATCH"),
        ({"current_status": "closed"}, HTTPStatus.NOT_FOUND, "RESOURCE_NOT_FOUND"),
        ({"base_sha": "e" * 40}, HTTPStatus.NOT_FOUND, "RESOURCE_NOT_FOUND"),
        (
            {"source_repository_external_id": "999"},
            HTTPStatus.NOT_FOUND,
            "RESOURCE_NOT_FOUND",
        ),
    ],
)
async def test_head_changed_during_provider_call_is_rechecked(  # noqa: PLR0913, PLR0917 -- explicit fixture and parameter boundaries
    client: httpx.AsyncClient,
    scope: StartScope,
    configured: FakeStartGithub,
    postgres: PostgresHarness,
    changes: dict[str, str],
    status: HTTPStatus,
    code: str,
) -> None:
    original = configured.fetch

    async def fetch(request: GithubRequest) -> GithubSnapshot:
        async with postgres.engine.begin() as conn:
            await conn.execute(
                update(models.ChangeRequest)
                .where(models.ChangeRequest.id == scope.change)
                .values(**changes)
            )
        return await original(request)

    configured.fetch = fetch  # type: ignore[method-assign]
    response = await client.post(
        scope.url, json={"requested_head_sha": HEAD}, headers=HEADERS
    )
    assert response.status_code == status
    assert response.json()["code"] == code
    assert_schema(response, "Error")


async def test_postgresql_commit_failure_rolls_back_entire_start(
    client: httpx.AsyncClient,
    scope: StartScope,
    configured: FakeStartGithub,
    postgres: PostgresHarness,
) -> None:

    async with postgres.engine.begin() as conn:
        await conn.execute(
            text(
                """CREATE FUNCTION review_start_commit_fault() RETURNS trigger LANGUAGE
plpgsql AS $$ BEGIN RAISE EXCEPTION 'test commit fault'; END $$"""
            )
        )
        await conn.execute(
            text(
                """CREATE CONSTRAINT TRIGGER review_start_commit_fault AFTER INSERT ON
review_job DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION
review_start_commit_fault()"""
            )
        )
    try:
        response = await client.post(
            scope.url, json={"requested_head_sha": HEAD}, headers=HEADERS
        )
        assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
        assert_schema(response, "Error")
        assert "test commit fault" not in response.text
        async with async_sessionmaker(postgres.engine)() as session:
            assert not (
                await session.scalars(
                    select(models.ReviewJob.id).where(
                        models.ReviewJob.repository_id == scope.repository
                    )
                )
            ).all()
            assert not (
                await session.scalars(
                    select(models.QuotaUsage.id).where(
                        models.QuotaUsage.repository_id == scope.repository
                    )
                )
            ).all()
            assert not (
                await session.scalars(
                    select(models.IdempotencyRecord.id).where(
                        models.IdempotencyRecord.scope.like(f"%:{scope.repository}")
                    )
                )
            ).all()
            assert not (await session.scalars(select(models.Publication.id))).all()
            assert not (await session.scalars(select(models.OutboxEvent.id))).all()
    finally:
        async with postgres.engine.begin() as conn:
            await conn.execute(
                text("DROP TRIGGER review_start_commit_fault ON review_job")
            )
            await conn.execute(text("DROP FUNCTION review_start_commit_fault()"))


async def test_concurrent_same_key_different_pr_has_canonical_conflict(
    client: httpx.AsyncClient,
    scope: StartScope,
    configured: FakeStartGithub,
    postgres: PostgresHarness,
) -> None:

    second_url = await add_second_pr(postgres, scope)
    ready = asyncio.Event()
    original = configured.fetch
    entered = 0

    async def fetch(request: GithubRequest) -> GithubSnapshot:
        nonlocal entered
        entered += 1
        if entered == CONCURRENT_REQUESTS:
            ready.set()
        await asyncio.wait_for(ready.wait(), 10)
        return await original(request)

    configured.fetch = fetch  # type: ignore[method-assign]
    responses = await asyncio.gather(
        *(
            client.post(url, json={"requested_head_sha": HEAD}, headers=HEADERS)
            for url in (scope.url, second_url)
        )
    )
    assert sorted(response.status_code for response in responses) == [
        HTTPStatus.ACCEPTED,
        HTTPStatus.CONFLICT,
    ]
    rejected = next(
        response
        for response in responses
        if response.status_code == HTTPStatus.CONFLICT
    )
    assert rejected.json()["code"] == "IDEMPOTENCY_CONFLICT"
    assert_schema(rejected, "Error")


@pytest.mark.parametrize("field", ["external_id", "owner", "repo_name"])
async def test_repository_identity_changed_during_provider_call_is_rechecked(
    client: httpx.AsyncClient,
    scope: StartScope,
    configured: FakeStartGithub,
    postgres: PostgresHarness,
    field: str,
) -> None:
    original = configured.fetch

    async def fetch(request: GithubRequest) -> GithubSnapshot:
        async with postgres.engine.begin() as conn:
            await conn.execute(
                update(models.Repository)
                .where(models.Repository.id == scope.repository)
                .values(**{field: "changed-identity"})
            )
        return await original(request)

    configured.fetch = fetch  # type: ignore[method-assign]
    response = await client.post(
        scope.url, json={"requested_head_sha": HEAD}, headers=HEADERS
    )
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert_schema(response, "Error")


async def test_same_settings_version_mutated_during_provider_call_is_rejected(
    client: httpx.AsyncClient,
    scope: StartScope,
    configured: FakeStartGithub,
    postgres: PostgresHarness,
) -> None:
    original = configured.fetch

    async def fetch(request: GithubRequest) -> GithubSnapshot:
        rules = {"instructions": ["Changed while resolving"]}
        async with postgres.engine.begin() as conn:
            await conn.execute(
                update(models.RepositorySettings)
                .where(models.RepositorySettings.id == scope.settings)
                .values(rules=rules, rules_digest=compute_rules_digest(rules))
            )
        return await original(request)

    configured.fetch = fetch  # type: ignore[method-assign]
    response = await client.post(
        scope.url, json={"requested_head_sha": HEAD}, headers=HEADERS
    )
    assert response.status_code == HTTPStatus.CONFLICT
    assert response.json()["code"] == "SETTINGS_VERSION_CONFLICT"
    assert_schema(response, "Error")


async def test_invalid_verified_identity_is_rejected_before_database_access(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    app.dependency_overrides[get_verified_mutation_identity] = lambda: (
        VerifiedMutationIdentity(
            cast(UUID, None),
            True,
            True,
            True,
        )
    )
    response = await client.post(
        "/api/v1/repositories/00000000-0000-0000-0000-000000000001/pull-requests/13/reviews",
        json={"requested_head_sha": HEAD},
        headers=HEADERS,
    )
    assert response.status_code == HTTPStatus.FORBIDDEN
    assert response.json()["code"] == "ACCESS_DENIED"
    assert_schema(response, "Error")


async def test_rerun_parent_moved_during_provider_call_is_rejected(
    client: httpx.AsyncClient,
    scope: StartScope,
    configured: FakeStartGithub,
    postgres: PostgresHarness,
) -> None:

    accepted = await client.post(
        scope.url, json={"requested_head_sha": HEAD}, headers=HEADERS
    )
    parent_id = uuid.UUID(accepted.json()["review_id"])
    await add_second_pr(postgres, scope)
    async with postgres.engine.begin() as conn:
        second_id = await conn.scalar(
            select(models.ChangeRequest.id).where(
                models.ChangeRequest.repository_id == scope.repository,
                models.ChangeRequest.external_number == "14",
            )
        )
        await conn.execute(
            update(models.ReviewJob)
            .where(models.ReviewJob.id == parent_id)
            .values(status="COMPLETED", finished_at=datetime.now(UTC))
        )
    original = configured.fetch

    async def fetch(request: GithubRequest) -> GithubSnapshot:
        async with postgres.engine.begin() as conn:
            await conn.execute(
                update(models.ReviewJob)
                .where(models.ReviewJob.id == parent_id)
                .values(change_request_id=second_id)
            )
        return await original(request)

    configured.fetch = fetch  # type: ignore[method-assign]
    response = await client.post(
        scope.url,
        json={"requested_head_sha": HEAD, "rerun_of": str(parent_id)},
        headers={**HEADERS, "Idempotency-Key": "rerun-race"},
    )
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert_schema(response, "Error")
    async with async_sessionmaker(postgres.engine)() as session:
        assert (
            len(
                (
                    await session.scalars(
                        select(models.ReviewJob.id).where(
                            models.ReviewJob.repository_id == scope.repository
                        )
                    )
                ).all()
            )
            == 1
        )
        assert (
            await session.scalars(
                select(models.QuotaUsage.value).where(
                    models.QuotaUsage.repository_id == scope.repository
                )
            )
        ).one() == 1


@pytest.mark.parametrize(
    ("field", "value", "expected_status", "code"),
    [
        ("head_sha", "c" * 40, HTTPStatus.CONFLICT, "PR_HEAD_MISMATCH"),
        ("base_sha", "c" * 40, HTTPStatus.NOT_FOUND, "RESOURCE_NOT_FOUND"),
        (
            "head_repository_id",
            "different-source",
            HTTPStatus.NOT_FOUND,
            "RESOURCE_NOT_FOUND",
        ),
        (
            "base_repository_id",
            "different-base",
            HTTPStatus.NOT_FOUND,
            "RESOURCE_NOT_FOUND",
        ),
    ],
)
async def test_provider_snapshot_contradicting_saved_pr_is_rejected(  # noqa: PLR0913, PLR0917 -- explicit fixture and parameter boundaries
    client: httpx.AsyncClient,
    scope: StartScope,
    configured: FakeStartGithub,
    postgres: PostgresHarness,
    field: str,
    value: str,
    expected_status: HTTPStatus,
    code: str,
) -> None:
    original = configured.fetch

    async def fetch(request: GithubRequest) -> GithubSnapshot:
        snapshot = await original(request)
        snapshot.metadata["snapshot"][field] = value
        return snapshot

    configured.fetch = fetch  # type: ignore[method-assign]
    response = await client.post(
        scope.url, json={"requested_head_sha": HEAD}, headers=HEADERS
    )
    assert response.status_code == expected_status
    assert response.json()["code"] == code
    assert_schema(response, "Error")
    async with async_sessionmaker(postgres.engine)() as session:
        assert not (
            await session.scalars(
                select(models.ReviewJob.id).where(
                    models.ReviewJob.repository_id == scope.repository
                )
            )
        ).all()


@pytest.mark.parametrize("proof", [None, {}, {"provider": "other"}])
async def test_incomplete_provider_revalidation_is_unavailable(
    client: httpx.AsyncClient,
    scope: StartScope,
    configured: FakeStartGithub,
    proof: object,
) -> None:
    async def fetch(request: GithubRequest) -> GithubSnapshot:
        return GithubSnapshot(metadata={"snapshot": proof}, raw_diff="")

    configured.fetch = fetch  # type: ignore[method-assign]
    response = await client.post(
        scope.url, json={"requested_head_sha": HEAD}, headers=HEADERS
    )
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert_schema(response, "Error")


async def test_database_read_failure_returns_safe_retryable_error(
    client: httpx.AsyncClient,
    app: FastAPI,
    scope: StartScope,
    configured: FakeStartGithub,
    postgres: PostgresHarness,
) -> None:

    async def timed_session() -> AsyncIterator[AsyncSession]:
        async with async_sessionmaker(postgres.engine)() as session:
            await session.execute(text("SET LOCAL statement_timeout = '100ms'"))
            yield session

    app.dependency_overrides[get_session] = timed_session
    async with postgres.engine.connect() as blocked, blocked.begin():
        await blocked.execute(text("LOCK TABLE repository IN ACCESS EXCLUSIVE MODE"))
        response = await client.post(
            scope.url, json={"requested_head_sha": HEAD}, headers=HEADERS
        )
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json()["retryable"] is True
    assert_schema(response, "Error")
    assert "statement timeout" not in response.text
    assert configured.calls == 0


@pytest.mark.parametrize("field", ["model_ref", "prompt_version", "prompt_digest"])
async def test_missing_frozen_configuration_identity_is_unavailable(  # noqa: PLR0913, PLR0917 -- explicit fixture and parameter boundaries
    client: httpx.AsyncClient,
    app: FastAPI,
    scope: StartScope,
    configured: FakeStartGithub,
    postgres: PostgresHarness,
    field: str,
) -> None:

    class Resolver:
        async def resolve(
            self,
            *,
            repository: RepositoryIdentity,
            settings_id: uuid.UUID,
            rules_digest: str,
        ) -> FrozenAnalysisConfiguration:
            values = {
                "model_ref": "test-model",
                "prompt_version": "test-prompt",
                "prompt_digest": "test-digest",
            }
            values[field] = ""
            return FrozenAnalysisConfiguration(
                values["model_ref"],
                None,
                {},
                values["prompt_version"],
                values["prompt_digest"],
            )

    app.dependency_overrides[get_review_configuration_resolver] = Resolver
    response = await client.post(
        scope.url, json={"requested_head_sha": HEAD}, headers=HEADERS
    )
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert_schema(response, "Error")
    async with async_sessionmaker(postgres.engine)() as session:
        assert not (
            await session.scalars(
                select(models.ReviewJob.id).where(
                    models.ReviewJob.repository_id == scope.repository
                )
            )
        ).all()
        assert not (
            await session.scalars(
                select(models.QuotaUsage.id).where(
                    models.QuotaUsage.repository_id == scope.repository
                )
            )
        ).all()


async def test_database_equivalent_unique_conflict_returns_409_and_rolls_back(
    client: httpx.AsyncClient,
    scope: StartScope,
    configured: FakeStartGithub,
    postgres: PostgresHarness,
) -> None:

    async with postgres.engine.begin() as conn:
        await conn.execute(
            text("""CREATE FUNCTION review_start_duplicate_fault() RETURNS trigger
LANGUAGE plpgsql AS $$
        BEGIN
          IF pg_trigger_depth() =
1 THEN
            INSERT INTO review_job SELECT
(jsonb_populate_record(NULL::review_job,
              to_jsonb(NEW)
|| jsonb_build_object('id', gen_random_uuid()))).*;
          END IF;
RETURN NEW;
        END $$""")
        )
        await conn.execute(
            text(
                """CREATE TRIGGER review_start_duplicate_fault BEFORE INSERT ON
review_job FOR EACH ROW EXECUTE FUNCTION
review_start_duplicate_fault()"""
            )
        )
    try:
        response = await client.post(
            scope.url, json={"requested_head_sha": HEAD}, headers=HEADERS
        )
        assert response.status_code == HTTPStatus.CONFLICT
        assert response.json()["code"] == "REVIEW_ALREADY_ACTIVE"
        assert_schema(response, "Error")
        async with async_sessionmaker(postgres.engine)() as session:
            assert not (
                await session.scalars(
                    select(models.ReviewJob.id).where(
                        models.ReviewJob.repository_id == scope.repository
                    )
                )
            ).all()
            assert not (
                await session.scalars(
                    select(models.QuotaUsage.id).where(
                        models.QuotaUsage.repository_id == scope.repository
                    )
                )
            ).all()
    finally:
        async with postgres.engine.begin() as conn:
            await conn.execute(
                text("DROP TRIGGER review_start_duplicate_fault ON review_job")
            )
            await conn.execute(text("DROP FUNCTION review_start_duplicate_fault()"))


@pytest.mark.parametrize("field", ["head_ref", "base_ref"])
async def test_provider_refs_contradicting_saved_pr_are_rejected(
    client: httpx.AsyncClient,
    scope: StartScope,
    configured: FakeStartGithub,
    field: str,
) -> None:
    original = configured.fetch

    async def fetch(request: GithubRequest) -> GithubSnapshot:
        snapshot = await original(request)
        snapshot.metadata["metadata"][field] = "different-ref"
        return snapshot

    configured.fetch = fetch  # type: ignore[method-assign]
    response = await client.post(
        scope.url, json={"requested_head_sha": HEAD}, headers=HEADERS
    )
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert_schema(response, "Error")


async def test_closed_pr_with_changed_head_returns_safe_not_found(
    client: httpx.AsyncClient,
    scope: StartScope,
    postgres: PostgresHarness,
) -> None:
    async with postgres.engine.begin() as conn:
        await conn.execute(
            update(models.ChangeRequest)
            .where(models.ChangeRequest.id == scope.change)
            .values(current_status="closed", head_sha="c" * 40)
        )
    response = await client.post(
        scope.url, json={"requested_head_sha": HEAD}, headers=HEADERS
    )
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json()["code"] == "RESOURCE_NOT_FOUND"
    assert_schema(response, "Error")


async def test_empty_stored_output_language_is_unsupported_without_rewrites(
    client: httpx.AsyncClient,
    scope: StartScope,
    configured: FakeStartGithub,
    postgres: PostgresHarness,
) -> None:
    async with postgres.engine.begin() as conn:
        await conn.execute(
            update(models.RepositorySettings)
            .where(models.RepositorySettings.id == scope.settings)
            .values(output_language="")
        )
    response = await client.post(
        scope.url, json={"requested_head_sha": HEAD}, headers=HEADERS
    )
    assert response.status_code == HTTPStatus.CONFLICT
    assert response.json()["code"] == "LEGACY_SETTINGS_UNSUPPORTED"
    assert_schema(response, "Error")
    assert configured.calls == 0
    async with async_sessionmaker(postgres.engine)() as session:
        settings = await session.get(models.RepositorySettings, scope.settings)
        assert settings is not None and settings.output_language == ""
        assert settings.rules_digest == compute_rules_digest({"instructions": []})
        assert not (
            await session.scalars(
                select(models.ReviewJob.id).where(
                    models.ReviewJob.repository_id == scope.repository
                )
            )
        ).all()
        assert not (
            await session.scalars(
                select(models.QuotaUsage.id).where(
                    models.QuotaUsage.repository_id == scope.repository
                )
            )
        ).all()


@pytest.mark.parametrize(
    ("rules", "ignores"),
    [
        ({"instructions": []}, {"globs": []}),
        ({}, {"globs": []}),
        ({"instructions": []}, {}),
        ({}, {}),
    ],
    ids=["canonical", "empty-rules", "empty-ignores", "both-empty"],
)
@pytest.mark.usefixtures("configured")
async def test_start_preserves_exact_historical_empty_settings(
    client: httpx.AsyncClient,
    scope: StartScope,
    postgres: PostgresHarness,
    rules: dict[str, object],
    ignores: dict[str, object],
) -> None:
    digest = compute_rules_digest(rules)
    async with postgres.engine.begin() as conn:
        await conn.execute(
            update(models.RepositorySettings)
            .where(models.RepositorySettings.id == scope.settings)
            .values(
                rules=rules, ignores=ignores, rules_digest=digest, output_language="en"
            )
        )
    response = await client.post(
        scope.url, json={"requested_head_sha": HEAD}, headers=HEADERS
    )
    assert response.status_code == HTTPStatus.ACCEPTED
    replay = await client.post(
        scope.url, json={"requested_head_sha": HEAD}, headers=HEADERS
    )
    assert replay.status_code == HTTPStatus.OK
    assert replay.json()["review_id"] == response.json()["review_id"]
    async with async_sessionmaker(postgres.engine)() as session:
        settings = await session.get(models.RepositorySettings, scope.settings)
        review = await session.get(
            models.ReviewJob, uuid.UUID(response.json()["review_id"])
        )
        assert settings is not None and review is not None
        assert (settings.rules, settings.ignores, settings.rules_digest) == (
            rules,
            ignores,
            digest,
        )
        assert settings.output_language == "en"
        assert review.repository_settings_id == scope.settings
        assert review.rules_digest == digest
        quota = (
            await session.scalars(
                select(models.QuotaUsage).where(
                    models.QuotaUsage.repository_id == scope.repository,
                    models.QuotaUsage.type == "starts",
                )
            )
        ).one()
        assert quota.value == 1


async def test_incomplete_server_policy_cannot_admit_executable_job(
    client: httpx.AsyncClient,
    scope: StartScope,
    configured: FakeStartGithub,
    app: FastAPI,
) -> None:
    class IncompleteResolver:
        async def resolve(self, **kwargs: Any) -> FrozenAnalysisConfiguration:
            return FrozenAnalysisConfiguration(
                "test-model", None, {"temperature": 0}, "v1", "opaque"
            )

    app.dependency_overrides[get_review_configuration_resolver] = IncompleteResolver
    response = await client.post(
        scope.url, json={"requested_head_sha": HEAD}, headers=HEADERS
    )
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json()["message"] == "Review configuration is unavailable."


@pytest.mark.parametrize("defect", ["settings_digest", "prompt_digest", "model_digest"])
async def test_complete_but_unsupported_admission_identity_is_unavailable(
    client: httpx.AsyncClient,
    app: FastAPI,
    scope: StartScope,
    configured: FakeStartGithub,
    defect: str,
) -> None:

    original = app.dependency_overrides[get_review_configuration_resolver]()

    class UnsupportedResolver:
        async def resolve(self, **kwargs: Any) -> FrozenAnalysisConfiguration:
            config: FrozenAnalysisConfiguration = await original.resolve(**kwargs)
            if defect == "settings_digest":
                values = dict(config.model_settings)
                values["repository_settings_digest"] = "0" * 64
                return replace(config, model_settings=values)
            if defect == "model_digest":
                return replace(config, model_digest="a" * 64)
            return replace(config, prompt_digest="0" * 64)

    app.dependency_overrides[get_review_configuration_resolver] = UnsupportedResolver
    response = await client.post(
        scope.url, json={"requested_head_sha": HEAD}, headers=HEADERS
    )
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json()["message"] == "Review configuration is unavailable."


async def test_trusted_resolver_rejection_is_a_safe_http_unavailable(
    client: httpx.AsyncClient,
    app: FastAPI,
    scope: StartScope,
    configured: FakeStartGithub,
) -> None:
    class UnsupportedResolver:
        async def resolve(self, **kwargs: Any) -> FrozenAnalysisConfiguration:
            raise FrozenConfigurationError("model_digest_unattestable")

    app.dependency_overrides[get_review_configuration_resolver] = UnsupportedResolver
    response = await client.post(
        scope.url, json={"requested_head_sha": HEAD}, headers=HEADERS
    )
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json()["message"] == "Review configuration is unavailable."
    assert "model_digest_unattestable" not in response.text


async def test_admission_uses_deployment_registry_after_process_cwd_changes(
    client: httpx.AsyncClient,
    scope: StartScope,
    configured: FakeStartGithub,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # The trusted resolver retains its absolute registry root; the HTTP gate
    # must verify the same deployment assets without depending on process CWD.
    monkeypatch.chdir(tmp_path)
    response = await client.post(
        scope.url, json={"requested_head_sha": HEAD}, headers=HEADERS
    )
    assert response.status_code == HTTPStatus.ACCEPTED


@pytest.mark.parametrize("marker", [True, 1.0])
async def test_admission_rejects_noninteger_frozen_configuration_marker(
    client: httpx.AsyncClient,
    app: FastAPI,
    scope: StartScope,
    configured: FakeStartGithub,
    marker: Any,
) -> None:
    original = app.dependency_overrides[get_review_configuration_resolver]()

    class NonintegerMarkerResolver:
        async def resolve(self, **kwargs: Any) -> FrozenAnalysisConfiguration:
            config: FrozenAnalysisConfiguration = await original.resolve(**kwargs)
            return replace(config, frozen_configuration_version=marker)

    app.dependency_overrides[get_review_configuration_resolver] = (
        NonintegerMarkerResolver
    )
    response = await client.post(
        scope.url, json={"requested_head_sha": HEAD}, headers=HEADERS
    )
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json()["message"] == "Review configuration is unavailable."


async def test_concurrent_starts_after_push_admit_one_review_for_the_new_head(
    client: httpx.AsyncClient,
    scope: StartScope,
    configured: FakeStartGithub,
    postgres: PostgresHarness,
) -> None:
    # Saved row still names the pre-push commit; GitHub reports HEAD.
    async with postgres.engine.begin() as conn:
        await conn.execute(
            update(models.ChangeRequest)
            .where(models.ChangeRequest.id == scope.change)
            .values(head_sha="d" * 40)
        )
    both_ready = asyncio.Event()
    original = configured.fetch
    entered = 0

    async def fetch(request: GithubRequest) -> GithubSnapshot:
        nonlocal entered
        entered += 1
        if entered == CONCURRENT_REQUESTS:
            both_ready.set()
        await asyncio.wait_for(both_ready.wait(), 10)
        return await original(request)

    configured.fetch = fetch  # type: ignore[method-assign]
    responses = await asyncio.gather(
        *(
            client.post(
                scope.url,
                json={"requested_head_sha": HEAD},
                headers={**HEADERS, "Idempotency-Key": f"double-click-{index}"},
            )
            for index in range(CONCURRENT_REQUESTS)
        )
    )

    assert sorted(response.status_code for response in responses) == [
        HTTPStatus.ACCEPTED,
        HTTPStatus.CONFLICT,
    ]
    conflict = next(r for r in responses if r.status_code == HTTPStatus.CONFLICT)
    assert conflict.json()["code"] == "REVIEW_ALREADY_ACTIVE"
