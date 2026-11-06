"""Read-only live rules preview through authenticated HTTP and GitHub reads."""

import base64
import hashlib
from collections.abc import AsyncIterator
from http import HTTPStatus

import httpx
import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from test_repository_settings_api import SettingsScope
from test_review_results_api import assert_schema

from app.db.models import RepositorySettings
from app.db.session import get_session
from app.domain.settings import compute_rules_digest

pytest_plugins = ("test_repositories_api", "test_repository_settings_api")
FIRST = "a" * 40
SECOND = "b" * 40
RULES = "# Security\n## SEC-1: Check input\nUse validation.\n"


def contents(text: str) -> dict[str, object]:
    raw = text.encode()
    return {
        "type": "file",
        "size": len(raw),
        "encoding": "base64",
        "content": base64.b64encode(raw).decode(),
        "sha": hashlib.sha1(
            b"blob " + str(len(raw)).encode() + b"\0" + raw, usedforsecurity=False
        ).hexdigest(),
    }


async def test_reader_previews_only_file_rules_at_current_default_branch_sha(
    settings_scope: SettingsScope,
) -> None:
    catalog = settings_scope.catalog
    async with catalog.postgres.engine.begin() as connection:
        await connection.execute(
            update(RepositorySettings)
            .where(RepositorySettings.id == settings_scope.version)
            .values(
                rules={"instructions": ["PRIVATE_DB_RULE"]},
                rules_digest=compute_rules_digest(
                    {"instructions": ["PRIVATE_DB_RULE"]}
                ),
            )
        )
    original = catalog.provider
    calls: list[str] = []

    def provider(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "/branches/" in request.url.path:
            return httpx.Response(
                200, json={"name": "release/next", "commit": {"sha": FIRST}}
            )
        if "/contents/" in request.url.path:
            assert request.url.params["ref"] == FIRST
            return httpx.Response(200, json=contents(RULES))
        if "/commits/" in request.url.path:
            return httpx.Response(200, json={"sha": FIRST})
        body = original(request).json()
        body.update(
            default_branch="release/next", permissions={"pull": True, "admin": False}
        )
        return httpx.Response(200, json=body)

    catalog.provider = provider
    response = await catalog.client.get(
        f"/api/v1/repositories/{settings_scope.repository}/rules-preview"
    )
    assert response.status_code == HTTPStatus.OK
    assert_schema(response, "RepositoryRulesPreview")
    body = response.json()
    assert body["default_branch"] == "release/next"
    assert body["provenance"]["revision"] == FIRST
    assert body["provenance"]["state"] == "valid"
    assert body["instructions"] == ["[SEC-1] Security: Check input\nUse validation."]
    assert "PRIVATE_DB_RULE" not in response.text
    assert any("/branches/release%2Fnext" in call for call in calls)


async def test_refresh_resolves_moving_branch_instead_of_reusing_old_sha(
    settings_scope: SettingsScope,
) -> None:
    catalog = settings_scope.catalog
    original = catalog.provider
    revision = FIRST
    reads: list[str] = []

    def provider(request: httpx.Request) -> httpx.Response:
        if "/branches/" in request.url.path:
            return httpx.Response(
                200, json={"name": "main", "commit": {"sha": revision}}
            )
        if "/contents/" in request.url.path:
            reads.append(request.url.params["ref"])
            return httpx.Response(
                200,
                json=contents(
                    RULES
                    if revision == FIRST
                    else RULES.replace("validation", "escaping")
                ),
            )
        if "/commits/" in request.url.path:
            return httpx.Response(200, json={"sha": revision})
        return httpx.Response(
            200, json={**original(request).json(), "default_branch": "main"}
        )

    catalog.provider = provider
    url = f"/api/v1/repositories/{settings_scope.repository}/rules-preview"
    first = await catalog.client.get(url)
    revision = SECOND
    second = await catalog.client.get(url)
    assert first.status_code == second.status_code == HTTPStatus.OK
    assert second.json()["provenance"]["revision"] == SECOND
    assert first.json()["instructions"] != second.json()["instructions"]
    assert reads == [FIRST, SECOND]


@pytest.mark.parametrize(
    ("content", "state", "diagnostics"),
    [
        (None, "absent", []),
        ("", "empty", []),
        ("# PRIVATE_INVALID_SOURCE", "invalid", ["invalid_structure:1"]),
    ],
)
async def test_preview_reuses_distinct_safe_content_states_without_db_fallback_text(
    settings_scope: SettingsScope,
    content: str | None,
    state: str,
    diagnostics: list[str],
) -> None:
    catalog = settings_scope.catalog
    original = catalog.provider

    def provider(request: httpx.Request) -> httpx.Response:
        if "/branches/" in request.url.path:
            return httpx.Response(200, json={"name": "main", "commit": {"sha": FIRST}})
        if "/contents/" in request.url.path:
            return (
                httpx.Response(404)
                if content is None
                else httpx.Response(200, json=contents(content))
            )
        if "/commits/" in request.url.path:
            return httpx.Response(200, json={"sha": FIRST})
        return httpx.Response(
            200, json={**original(request).json(), "default_branch": "main"}
        )

    catalog.provider = provider
    response = await catalog.client.get(
        f"/api/v1/repositories/{settings_scope.repository}/rules-preview"
    )
    assert response.status_code == HTTPStatus.OK
    assert response.json()["instructions"] == []
    assert response.json()["provenance"]["state"] == state
    assert response.json()["provenance"]["diagnostics"] == diagnostics
    assert "PRIVATE_INVALID_SOURCE" not in response.text


@pytest.mark.parametrize(
    ("provider_status", "api_status", "code", "retryable"),
    [
        (403, 403, "ACCESS_DENIED", False),
        (429, 503, "SERVICE_UNAVAILABLE", True),
        (503, 503, "SERVICE_UNAVAILABLE", True),
    ],
)
async def test_preview_provider_failure_is_explicit_not_a_missing_file(
    settings_scope: SettingsScope,
    provider_status: int,
    api_status: int,
    code: str,
    retryable: bool,
) -> None:
    settings_scope.catalog.provider = lambda request: httpx.Response(
        provider_status,
        headers={"Retry-After": "23"}
        if provider_status == HTTPStatus.TOO_MANY_REQUESTS
        else {},
        json={"message": "PRIVATE_PROVIDER_BODY"},
    )
    response = await settings_scope.catalog.client.get(
        f"/api/v1/repositories/{settings_scope.repository}/rules-preview"
    )
    assert response.status_code == api_status
    assert response.json()["code"] == code
    assert response.json()["retryable"] is retryable
    assert "PRIVATE_PROVIDER_BODY" not in response.text
    assert "provenance" not in response.json()


async def test_preview_database_unavailability_is_canonical_retryable_error(
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
            response = await client.get(
                f"/api/v1/repositories/{settings_scope.repository}/rules-preview"
            )
        assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
        assert response.json()["code"] == "SERVICE_UNAVAILABLE"
        assert response.json()["retryable"] is True
        assert_schema(response, "Error")
    finally:
        await engine.dispose()


async def test_same_sha_cache_hit_reauthorizes_after_fresh_branch_resolution(
    settings_scope: SettingsScope,
) -> None:
    catalog = settings_scope.catalog
    original = catalog.provider
    denied = False
    contents_reads, branch_reads = 0, 0

    def provider(request: httpx.Request) -> httpx.Response:
        nonlocal contents_reads, branch_reads
        if "/branches/" in request.url.path:
            branch_reads += 1
            return httpx.Response(200, json={"name": "main", "commit": {"sha": FIRST}})
        if "/contents/" in request.url.path:
            contents_reads += 1
            return httpx.Response(200, json=contents(RULES))
        if request.url.path == "/repos/team/settings" and denied:
            return httpx.Response(403, json={"message": "PRIVATE_PROVIDER_BODY"})
        if "/commits/" in request.url.path:
            return httpx.Response(200, json={"sha": FIRST})
        return httpx.Response(
            200, json={**original(request).json(), "default_branch": "main"}
        )

    catalog.provider = provider
    url = f"/api/v1/repositories/{settings_scope.repository}/rules-preview"
    assert (await catalog.client.get(url)).status_code == HTTPStatus.OK
    denied = True
    revoked = await catalog.client.get(url)
    assert revoked.status_code == HTTPStatus.FORBIDDEN
    assert revoked.json()["code"] == "ACCESS_DENIED"
    expected_branch_reads = 2
    assert contents_reads == 1 and branch_reads == expected_branch_reads
    assert "PRIVATE_PROVIDER_BODY" not in revoked.text
    assert "Use validation" not in revoked.text
