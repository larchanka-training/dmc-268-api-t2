from http import HTTPStatus
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_frozen_configuration import PROMPTS, runtime_profiles
from test_postgres_integration import PostgresHarness
from test_review_start_api import BASE, HEAD, HEADERS, FakeStartGithub, StartScope

from app.api.review_start_dependencies import get_review_configuration_resolver
from app.application.github_ports import GithubRequest, GithubSnapshot
from app.db.models import RepositorySettings
from app.domain.frozen_configuration import ModelRoute, admission_policy
from app.domain.repository_policy import StoredReviewPolicy
from app.review.configuration import (
    FrozenAdmissionResolver,
    FrozenConfigurationError,
    FrozenConfigurationReader,
)

pytest_plugins = ("test_review_start_api",)


async def test_policy_mutation_during_provider_admission_is_a_settings_conflict(
    client: httpx.AsyncClient,
    scope: StartScope,
    configured: FakeStartGithub,
    postgres: PostgresHarness,
) -> None:
    policy = StoredReviewPolicy(minimum_severity="medium").model_dump(mode="json")
    async with postgres.engine.begin() as connection:
        await connection.execute(
            update(RepositorySettings)
            .where(RepositorySettings.id == scope.settings)
            .values(review_policy=policy)
        )
    original = configured.fetch

    async def fetch(request: GithubRequest) -> GithubSnapshot:
        async with postgres.engine.begin() as connection:
            await connection.execute(
                update(RepositorySettings)
                .where(RepositorySettings.id == scope.settings)
                .values(review_policy={**policy, "minimum_severity": "critical"})
            )
        return await original(request)

    configured.fetch = fetch  # type: ignore[method-assign]
    response = await client.post(
        scope.url, json={"requested_head_sha": HEAD}, headers=HEADERS
    )
    assert response.status_code == HTTPStatus.CONFLICT
    assert response.json()["code"] == "SETTINGS_VERSION_CONFLICT"


async def test_saved_policy_mutation_invalidates_frozen_settings_checksum(
    client: httpx.AsyncClient,
    scope: StartScope,
    configured: FakeStartGithub,
    postgres: PostgresHarness,
) -> None:
    policy = StoredReviewPolicy(minimum_severity="medium").model_dump(mode="json")
    async with postgres.engine.begin() as connection:
        await connection.execute(
            update(RepositorySettings)
            .where(RepositorySettings.id == scope.settings)
            .values(review_policy=policy)
        )
    response = await client.post(
        scope.url, json={"requested_head_sha": HEAD}, headers=HEADERS
    )
    assert response.status_code == HTTPStatus.ACCEPTED
    identifier = UUID(response.json()["review_id"])
    reader = FrozenConfigurationReader(
        async_sessionmaker(postgres.engine, expire_on_commit=False),
        PROMPTS,
        runtime_profiles(),
    )
    await reader.resolve(identifier)
    async with postgres.engine.begin() as connection:
        await connection.execute(
            update(RepositorySettings)
            .where(RepositorySettings.id == scope.settings)
            .values(review_policy={**policy, "minimum_severity": "critical"})
        )
    with pytest.raises(
        FrozenConfigurationError, match="repository_settings_digest_mismatch"
    ):
        await reader.resolve(identifier)


async def test_v4_effective_settings_keep_policy_when_original_source_changes(  # noqa: PLR0913, PLR0917 -- full HTTP/frozen/DB integration fixtures
    client: httpx.AsyncClient,
    app: FastAPI,
    scope: StartScope,
    configured: FakeStartGithub,
    postgres: PostgresHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = StoredReviewPolicy(minimum_severity="medium").model_dump(mode="json")
    async with postgres.engine.begin() as connection:
        await connection.execute(
            update(RepositorySettings)
            .where(RepositorySettings.id == scope.settings)
            .values(review_policy=policy)
        )
    factory = async_sessionmaker(postgres.engine, expire_on_commit=False)
    app.dependency_overrides[get_review_configuration_resolver] = lambda: (
        FrozenAdmissionResolver(
            factory,
            PROMPTS,
            runtime_profiles(),
            primary=ModelRoute(
                profile_ref="eurorouter/review-v1",
                model_ref="test-model",
                model_digest=None,
            ),
            policy=admission_policy("0" * 64),
            prompt_version="v4",
        )
    )

    def github(request: httpx.Request) -> httpx.Response:
        if "/contents/" in request.url.path:
            return httpx.Response(404)
        if "/commits/" in request.url.path:
            return httpx.Response(200, json={"sha": BASE})
        return httpx.Response(200, json={"id": 123456})

    monkeypatch.setattr(
        "httpx._client.AsyncHTTPTransport", lambda **kwargs: httpx.MockTransport(github)
    )
    response = await client.post(
        scope.url, json={"requested_head_sha": HEAD}, headers=HEADERS
    )
    assert response.status_code == HTTPStatus.ACCEPTED
    identifier = UUID(response.json()["review_id"])
    reader = FrozenConfigurationReader(factory, PROMPTS, runtime_profiles())
    frozen = await reader.resolve(identifier)
    postgres.track("repository_settings", frozen.repository_settings_id)
    assert frozen.repository_settings_id != scope.settings
    assert frozen.minimum_severity == "medium"
    async with postgres.engine.begin() as connection:
        await connection.execute(
            update(RepositorySettings)
            .where(RepositorySettings.id == scope.settings)
            .values(review_policy={**policy, "minimum_severity": "critical"})
        )
    assert (await reader.resolve(identifier)).minimum_severity == "medium"
