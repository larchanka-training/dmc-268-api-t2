# ruff: noqa: PLR2004 -- fixed canonical settings and retry contract vectors

from collections.abc import AsyncIterator
from dataclasses import dataclass
from http import HTTPStatus
from uuid import UUID

import httpx
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from test_postgres_integration import _create_settings
from test_repositories_api import Catalog
from test_review_results_api import assert_schema

from app.api.auth import VerifiedMutationIdentity, get_verified_mutation_identity
from app.db import models
from app.db.session import get_session
from app.domain.settings import compute_rules_digest

pytest_plugins = ("test_repositories_api",)


@dataclass
class SettingsScope:
    catalog: Catalog
    repository: UUID
    version: UUID

    @property
    def url(self) -> str:
        return f"/api/v1/repositories/{self.repository}/settings"


@pytest_asyncio.fixture
async def settings_scope(catalog: Catalog) -> AsyncIterator[SettingsScope]:
    repository = await catalog.connect("team", "settings", role="admin")
    identifier = UUID(repository["id"])
    async with catalog.postgres.engine.begin() as connection:
        version = await _create_settings(
            catalog.postgres,
            connection,
            identifier,
            rules_digest=compute_rules_digest({"instructions": []}),
        )
        await connection.execute(
            update(models.Repository)
            .where(models.Repository.id == identifier)
            .values(current_settings_id=version)
        )

    def provider(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": int(repository["external_id"]),
                "owner": {"login": "team"},
                "name": "settings",
                "permissions": {"pull": True, "admin": True},
            },
        )

    catalog.provider = provider
    catalog.app.dependency_overrides[get_verified_mutation_identity] = lambda: (
        VerifiedMutationIdentity(catalog.user, True, True, True)
    )
    yield SettingsScope(catalog, identifier, version)
    async with catalog.postgres.engine.connect() as connection:
        versions = await connection.scalars(
            select(models.RepositorySettings.id).where(
                models.RepositorySettings.repository_id == identifier
            )
        )
        for saved in versions:
            catalog.postgres.track("repository_settings", saved)


async def test_admin_can_read_current_canonical_settings_with_etag(
    settings_scope: SettingsScope,
) -> None:
    response = await settings_scope.catalog.client.get(settings_scope.url)
    assert response.status_code == HTTPStatus.OK
    assert_schema(response, "RepositorySettings")
    body = response.json()
    assert body["version"] == str(settings_scope.version)
    assert body["rules"] == {"instructions": []}
    assert body["ignores"] == {"globs": []}
    assert body["max_starts_per_hour"] == 10
    assert body["max_active_jobs"] == 4
    assert body["output_language"] == "ru"
    assert body["surrounding_lines"] == 5
    assert response.headers["ETag"] == f'"{body["version"]}"'


async def test_provider_admin_revocation_overrides_cached_local_admin_role(
    settings_scope: SettingsScope,
) -> None:
    catalog = settings_scope.catalog
    original = catalog.provider

    def provider(request: httpx.Request) -> httpx.Response:
        body = original(request).json()
        body["permissions"]["admin"] = False
        return httpx.Response(200, json=body)

    catalog.provider = provider
    response = await catalog.client.get(settings_scope.url)
    assert response.status_code == HTTPStatus.FORBIDDEN
    assert response.json()["code"] == "ACCESS_DENIED"
    assert "rules" not in response.json()


async def test_github_permission_denial_is_a_safe_settings_access_error(
    settings_scope: SettingsScope,
) -> None:
    settings_scope.catalog.provider = lambda request: httpx.Response(403)
    response = await settings_scope.catalog.client.get(settings_scope.url)
    assert response.status_code == HTTPStatus.FORBIDDEN
    assert response.json()["code"] == "ACCESS_DENIED"


async def test_settings_permission_rate_limit_returns_retryable_error(
    settings_scope: SettingsScope,
) -> None:
    settings_scope.catalog.provider = lambda request: httpx.Response(
        429, headers={"Retry-After": "37"}
    )
    response = await settings_scope.catalog.client.get(settings_scope.url)
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json()["retryable"] is True
    assert response.json()["retry_after"] == 37


async def test_settings_reads_project_exact_legacy_empty_documents(
    settings_scope: SettingsScope,
) -> None:
    async with settings_scope.catalog.postgres.engine.begin() as connection:
        await connection.execute(
            update(models.RepositorySettings)
            .where(models.RepositorySettings.id == settings_scope.version)
            .values(rules={}, ignores={}, rules_digest=compute_rules_digest({}))
        )
    response = await settings_scope.catalog.client.get(settings_scope.url)
    assert response.status_code == HTTPStatus.OK
    assert response.json()["rules"] == {"instructions": []}
    assert response.json()["ignores"] == {"globs": []}
    assert response.headers["ETag"] == f'"{settings_scope.version}"'


async def test_unsupported_legacy_settings_return_replaceable_etag_without_raw_data(
    settings_scope: SettingsScope,
) -> None:
    async with settings_scope.catalog.postgres.engine.begin() as connection:
        await connection.execute(
            update(models.RepositorySettings)
            .where(models.RepositorySettings.id == settings_scope.version)
            .values(rules={"unsupported": "PRIVATE_LEGACY"})
        )
    response = await settings_scope.catalog.client.get(settings_scope.url)
    assert response.status_code == HTTPStatus.CONFLICT
    assert response.json()["code"] == "LEGACY_SETTINGS_UNSUPPORTED"
    assert response.headers["ETag"] == f'"{settings_scope.version}"'
    assert "PRIVATE_LEGACY" not in response.text
    assert_schema(response, "Error")


async def test_admin_can_fully_replace_settings_and_read_new_immutable_version(
    settings_scope: SettingsScope,
) -> None:
    initial = await settings_scope.catalog.client.get(settings_scope.url)
    replacement = {
        "rules": {"instructions": ["[SEC-1] Bind tenant to the current session."]},
        "ignores": {"globs": ["vendor/**"]},
        "max_starts_per_hour": 12,
        "max_active_jobs": 3,
        "output_language": "ru",
        "surrounding_lines": 8,
    }
    response = await settings_scope.catalog.client.put(
        settings_scope.url,
        json=replacement,
        headers={
            "If-Match": initial.headers["ETag"],
            "Origin": "https://trusted.example",
            "X-CSRF-Token": "trusted",
        },
    )
    assert response.status_code == HTTPStatus.OK
    assert_schema(response, "RepositorySettings")
    assert response.json()["version"] != initial.json()["version"]
    assert response.headers["ETag"] != initial.headers["ETag"]
    current = await settings_scope.catalog.client.get(settings_scope.url)
    assert current.json() == response.json()
    for key, value in replacement.items():
        assert current.json()[key] == value


async def test_stale_settings_etag_cannot_replace_a_new_current_version(
    settings_scope: SettingsScope,
) -> None:
    client = settings_scope.catalog.client
    initial = await client.get(settings_scope.url)
    keys = (
        "rules",
        "ignores",
        "max_starts_per_hour",
        "max_active_jobs",
        "output_language",
        "surrounding_lines",
    )
    replacement = {key: initial.json()[key] for key in keys}
    headers = {
        "If-Match": initial.headers["ETag"],
        "Origin": "https://trusted.example",
        "X-CSRF-Token": "trusted",
    }
    first = await client.put(settings_scope.url, json=replacement, headers=headers)
    assert first.status_code == HTTPStatus.OK
    stale = await client.put(
        settings_scope.url,
        json={**replacement, "surrounding_lines": 99},
        headers=headers,
    )
    assert stale.status_code == HTTPStatus.CONFLICT
    assert stale.json()["code"] == "SETTINGS_VERSION_CONFLICT"
    current = await client.get(settings_scope.url)
    assert current.json() == first.json()


async def test_settings_replacement_requires_if_match(
    settings_scope: SettingsScope,
) -> None:
    client = settings_scope.catalog.client
    current = await client.get(settings_scope.url)
    replacement = {
        key: current.json()[key]
        for key in (
            "rules",
            "ignores",
            "max_starts_per_hour",
            "max_active_jobs",
            "output_language",
            "surrounding_lines",
        )
    }
    response = await client.put(
        settings_scope.url,
        json=replacement,
        headers={"Origin": "https://trusted.example", "X-CSRF-Token": "trusted"},
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()["code"] == "VALIDATION_FAILED"


async def test_legacy_settings_expose_safe_review_policy_defaults(
    settings_scope: SettingsScope,
) -> None:
    response = await settings_scope.catalog.client.get(settings_scope.url)
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["auto_review_enabled"] is False
    assert body["base_branches"] == []
    assert body["minimum_severity"] == "low"


async def test_admin_policy_save_persists_across_fresh_settings_reads(
    settings_scope: SettingsScope,
) -> None:
    client = settings_scope.catalog.client
    initial = await client.get(settings_scope.url)
    replacement = {
        key: value
        for key, value in initial.json().items()
        if key not in {"version", "created_at"}
    }
    replacement.update(
        auto_review_enabled=True,
        base_branches=["main", "release/v1"],
        minimum_severity="medium",
    )
    saved = await client.put(
        settings_scope.url,
        json=replacement,
        headers={
            "If-Match": initial.headers["ETag"],
            "Origin": "https://trusted.example",
            "X-CSRF-Token": "trusted",
        },
    )
    assert saved.status_code == HTTPStatus.OK
    current = await client.get(settings_scope.url)
    assert current.json()["auto_review_enabled"] is True
    assert current.json()["base_branches"] == ["main", "release/v1"]
    assert current.json()["minimum_severity"] == "medium"
    assert "authorized_by" not in current.json()


async def test_malformed_repository_id_read_is_a_canonical_masked_error(
    settings_scope: SettingsScope,
) -> None:
    response = await settings_scope.catalog.client.get(
        "/api/v1/repositories/not-a-uuid/settings"
    )
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert_schema(response, "Error")
    assert response.json()["code"] == "RESOURCE_NOT_FOUND"


async def test_malformed_repository_id_write_is_a_canonical_masked_error(
    settings_scope: SettingsScope,
) -> None:
    response = await settings_scope.catalog.client.put(
        "/api/v1/repositories/not-a-uuid/settings",
        json={},
        headers={
            "If-Match": f'"{settings_scope.version}"',
            "Origin": "https://trusted.example",
            "X-CSRF-Token": "trusted",
        },
    )
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert_schema(response, "Error")
    assert response.json()["code"] == "RESOURCE_NOT_FOUND"


async def replace_settings(scope: SettingsScope, **changes: object) -> httpx.Response:
    current = await scope.catalog.client.get(scope.url)
    body = {
        key: value
        for key, value in current.json().items()
        if key not in {"version", "created_at"}
    }
    body.update(changes)
    return await scope.catalog.client.put(
        scope.url,
        json=body,
        headers={
            "If-Match": current.headers["ETag"],
            "Origin": "https://trusted.example",
            "X-CSRF-Token": "trusted",
        },
    )


async def test_start_quota_outside_postgres_integer_range_is_rejected(
    settings_scope: SettingsScope,
) -> None:
    response = await replace_settings(settings_scope, max_starts_per_hour=2147483648)
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()["code"] == "VALIDATION_FAILED"
    assert_schema(response, "Error")


async def test_active_quota_outside_postgres_integer_range_is_rejected(
    settings_scope: SettingsScope,
) -> None:
    response = await replace_settings(settings_scope, max_active_jobs=2147483648)
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()["code"] == "VALIDATION_FAILED"


async def test_surrounding_lines_outside_postgres_integer_range_is_rejected(
    settings_scope: SettingsScope,
) -> None:
    response = await replace_settings(settings_scope, surrounding_lines=2147483648)
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()["code"] == "VALIDATION_FAILED"


async def test_base_branch_policy_rejects_globs_instead_of_saving_ambiguous_names(
    settings_scope: SettingsScope,
) -> None:
    response = await replace_settings(settings_scope, base_branches=["release/*"])
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()["code"] == "VALIDATION_FAILED"


async def test_base_branch_policy_rejects_invalid_git_ref_structure(
    settings_scope: SettingsScope,
) -> None:
    response = await replace_settings(settings_scope, base_branches=["release/../main"])
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


async def test_base_branch_policy_bounds_number_of_exact_names(
    settings_scope: SettingsScope,
) -> None:
    response = await replace_settings(
        settings_scope, base_branches=[f"branch-{i}" for i in range(129)]
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


async def test_base_branch_policy_bounds_encoded_name_size(
    settings_scope: SettingsScope,
) -> None:
    response = await replace_settings(settings_scope, base_branches=["я" * 513])
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


async def test_settings_database_unavailability_returns_safe_retryable_error(
    settings_scope: SettingsScope,
) -> None:
    engine = create_async_engine(
        "postgresql+asyncpg://unused:unused@127.0.0.1:1/unused",
        connect_args={"timeout": 0.2},
    )
    factory = async_sessionmaker(engine)

    async def unavailable() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    app = settings_scope.catalog.app
    app.dependency_overrides[get_session] = unavailable
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            response = await client.get(settings_scope.url)
        assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
        assert response.json()["code"] == "SERVICE_UNAVAILABLE"
        assert response.json()["retryable"] is True
        assert_schema(response, "Error")
    finally:
        await engine.dispose()


async def test_settings_put_database_unavailability_is_retryable(
    settings_scope: SettingsScope,
) -> None:
    initial = await settings_scope.catalog.client.get(settings_scope.url)
    replacement = {
        key: value
        for key, value in initial.json().items()
        if key not in {"version", "created_at"}
    }
    engine = create_async_engine(
        "postgresql+asyncpg://unused:unused@127.0.0.1:1/unused",
        connect_args={"timeout": 0.2},
    )
    factory = async_sessionmaker(engine)

    async def unavailable() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    app = settings_scope.catalog.app
    app.dependency_overrides[get_session] = unavailable
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            response = await client.put(
                settings_scope.url,
                json=replacement,
                headers={
                    "If-Match": initial.headers["ETag"],
                    "Origin": "https://trusted.example",
                    "X-CSRF-Token": "trusted",
                },
            )
        assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
        assert response.json()["code"] == "SERVICE_UNAVAILABLE"
        assert response.json()["retryable"] is True
    finally:
        await engine.dispose()
