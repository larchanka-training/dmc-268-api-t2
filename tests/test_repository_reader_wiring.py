"""Production reader wiring through browser sessions, PostgreSQL and GitHub HTTP."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from typing import Any

import httpx
import jwt
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import delete, event, insert, update
from test_repositories_api import Catalog

from app.api.auth import get_authenticated_principal
from app.api.repositories import get_repository_reader
from app.core.config import Settings, get_settings
from app.db.models import AuthSession, GithubIdentity

pytest_plugins = ("test_postgres_integration", "test_repositories_api")
_SIGNING_KEY = "repos01-test-signing-key-at-least-32-bytes"
_CREDENTIAL_KEY = "dGVzdC1lbmNyeXB0aW9uLWtleS0zMi1ieXRlcyEhISE="


def browser_cookie(user: uuid.UUID) -> dict[str, str]:
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "sub": str(user),
            "sid": str(user),
            "iat": now,
            "exp": now + timedelta(minutes=30),
            "iss": "dmc268",
            "aud": "dmc268-browser",
        },
        _SIGNING_KEY,
        algorithm="HS256",
    )
    return {"Cookie": f"dmc_session={token}"}


@pytest.fixture
def reader_settings() -> Settings:
    return Settings.model_validate(
        {
            "auth": {
                "jwt_secret": _SIGNING_KEY,
                "credential_key": _CREDENTIAL_KEY,
            },
            "github": {"token": "worker-secret-must-never-be-used"},
        }
    )


@pytest_asyncio.fixture
async def wired_catalog(
    catalog: Catalog, reader_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Catalog]:
    catalog.app.dependency_overrides.pop(get_authenticated_principal)
    catalog.app.dependency_overrides.pop(get_repository_reader)
    catalog.app.dependency_overrides[get_settings] = lambda: reader_settings
    cipher = Fernet(_CREDENTIAL_KEY)
    async with catalog.postgres.engine.begin() as connection:
        for user, token in (
            (catalog.user, "test-user-secret"),
            (catalog.other, "other-user-secret"),
        ):
            await connection.execute(
                insert(GithubIdentity).values(
                    user_id=user,
                    external_id=str(user.int),
                    login="octocat",
                    encrypted_token=cipher.encrypt(token.encode()).decode(),
                )
            )
            await connection.execute(
                insert(AuthSession).values(
                    id=user,
                    user_id=user,
                    expires_at=datetime.now(UTC) + timedelta(minutes=30),
                    csrf_token="test-csrf",
                )
            )

    # Replace only the external HTTP transport: the production dependency creates
    # and owns its real AsyncClient, including authentication headers and cleanup.
    opened: set[httpx.MockTransport] = set()

    class Transport(httpx.MockTransport):
        async def aclose(self) -> None:
            opened.discard(self)
            await super().aclose()

    def transport(**kwargs: Any) -> Transport:
        value = Transport(lambda request: catalog.provider(request))  # noqa: PLW0108
        opened.add(value)
        return value

    monkeypatch.setattr("httpx._client.AsyncHTTPTransport", transport)
    try:
        yield catalog
    finally:
        assert not opened, "Production reader must close every HTTP transport"
        async with catalog.postgres.engine.begin() as connection:
            await connection.execute(
                delete(AuthSession).where(
                    AuthSession.user_id.in_([catalog.user, catalog.other])
                )
            )
            await connection.execute(
                delete(GithubIdentity).where(
                    GithubIdentity.user_id.in_([catalog.user, catalog.other])
                )
            )


async def test_browser_session_selects_own_token_for_both_read_endpoints(
    wired_catalog: Catalog,
) -> None:
    catalog = wired_catalog
    own = await catalog.connect("alpha", "app")
    foreign = await catalog.connect("beta", "app", user=catalog.other)
    requests: list[httpx.Request] = []

    def provider(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/pulls"):
            return httpx.Response(200, json=[])
        return httpx.Response(
            200,
            json={
                "id": int(request.url.path.rsplit("/", 1)[1]),
                "permissions": {"pull": True},
            },
        )

    catalog.provider = provider
    for user, repository, secret in (
        (catalog.user, own, "test-user-secret"),
        (catalog.other, foreign, "other-user-secret"),
    ):
        headers = {
            **browser_cookie(user),
            "X-User-Id": str(catalog.other),
            "Authorization": "Bearer forged-http-secret",
        }
        repositories = await catalog.client.get("/api/v1/repositories", headers=headers)
        assert repositories.status_code == HTTPStatus.OK
        assert repositories.json() == {"items": [repository], "next_cursor": None}
        pulls = await catalog.client.get(
            f"/api/v1/repositories/{repository['id']}/pull-requests", headers=headers
        )
        assert pulls.status_code == HTTPStatus.OK
        assert pulls.json() == {"items": [], "next_cursor": None}
        assert [request.headers["Authorization"] for request in requests] == [
            f"Bearer {secret}",
            f"Bearer {secret}",
        ]
        requests.clear()


@pytest.mark.parametrize("credential", ["corrupt", "absent", "empty", "invalid_ascii"])
async def test_invalid_credentials_fail_safely_without_worker_fallback(
    wired_catalog: Catalog, credential: str, caplog: pytest.LogCaptureFixture
) -> None:
    catalog = wired_catalog
    repository = await catalog.connect("alpha", "app")
    encrypted = {
        "corrupt": "damaged-ciphertext",
        "absent": "",
        "empty": Fernet(_CREDENTIAL_KEY).encrypt(b"").decode(),
        "invalid_ascii": Fernet(_CREDENTIAL_KEY)
        .encrypt(b"secret\r\nInjected: yes")
        .decode(),
    }[credential]
    async with catalog.postgres.engine.begin() as connection:
        await connection.execute(
            update(GithubIdentity)
            .where(GithubIdentity.user_id == catalog.user)
            .values(encrypted_token=encrypted)
        )

    def unexpected(request: httpx.Request) -> httpx.Response:
        raise AssertionError("No GitHub request or worker fallback without credentials")

    catalog.provider = unexpected
    for path in (
        "/api/v1/repositories",
        f"/api/v1/repositories/{repository['id']}/pull-requests",
    ):
        response = await catalog.client.get(path, headers=browser_cookie(catalog.user))
        assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
        assert response.json() == {
            "code": "SERVICE_UNAVAILABLE",
            "message": "Service is unavailable.",
            "request_id": response.headers["X-Request-Id"],
            "retryable": True,
            "retry_after": None,
        }
    assert "damaged-ciphertext" not in caplog.text
    assert "test-user-secret" not in caplog.text
    assert "worker-secret" not in caplog.text


async def test_catalog_scans_bounded_batches_without_holding_db_during_http(
    wired_catalog: Catalog,
) -> None:
    catalog = wired_catalog
    revoked = {
        (await catalog.connect("alpha", f"app{i:03}"))["external_id"]
        for i in range(101)
    }
    first = await catalog.connect("beta", "app")
    last = await catalog.connect("gamma", "app")
    active: set[int] = set()
    queries: list[str] = []

    def checkout(dbapi: Any, record: Any, proxy: Any) -> None:
        active.add(id(record))

    def checkin(dbapi: Any, record: Any) -> None:
        active.discard(id(record))

    def query(connection: Any, cursor: Any, statement: str, *args: Any) -> None:
        if "JOIN repository_access" in statement:
            queries.append(statement)

    readable = catalog.provider

    def provider(request: httpx.Request) -> httpx.Response:
        assert not active, "PostgreSQL connection held during GitHub HTTP"
        if request.url.path.rsplit("/", 1)[1] in revoked:
            return httpx.Response(404)
        return readable(request)

    catalog.provider = provider
    engine = catalog.postgres.engine.sync_engine
    event.listen(engine, "checkout", checkout)
    event.listen(engine, "checkin", checkin)
    event.listen(engine, "before_cursor_execute", query)
    try:
        response = await catalog.client.get(
            "/api/v1/repositories",
            params={"limit": 1},
            headers=browser_cookie(catalog.user),
        )
    finally:
        event.remove(engine, "checkout", checkout)
        event.remove(engine, "checkin", checkin)
        event.remove(engine, "before_cursor_execute", query)
    assert response.status_code == HTTPStatus.OK
    assert response.json()["items"] == [first]
    assert response.json()["next_cursor"]
    assert len(queries) > 1
    assert all("LIMIT" in statement for statement in queries)
    continuation = await catalog.client.get(
        "/api/v1/repositories",
        params={"limit": 1, "cursor": response.json()["next_cursor"]},
        headers=browser_cookie(catalog.user),
    )
    assert continuation.json() == {"items": [last], "next_cursor": None}


@pytest.mark.parametrize("phase", ["body", "whole_page"])
async def test_catalog_deadline_fails_without_partial_page_and_closes_http(
    wired_catalog: Catalog, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    catalog = wired_catalog
    await catalog.connect("alpha", "app")
    await catalog.connect("beta", "app")
    monkeypatch.setattr(
        "app.api.repositories._READ_TIMEOUT_SECONDS", 0.04, raising=False
    )
    closed: set[str] = set()

    class SlowBody(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            await asyncio.sleep(0.08)
            yield b"{}"

        async def aclose(self) -> None:
            closed.add("body")

    class Transport(httpx.MockTransport):
        async def aclose(self) -> None:
            closed.add("transport")
            await super().aclose()

    async def provider(request: httpx.Request) -> httpx.Response:
        if phase == "body":
            return httpx.Response(200, stream=SlowBody())
        await asyncio.sleep(0.025)
        return catalog.provider(request)

    monkeypatch.setattr(
        "httpx._client.AsyncHTTPTransport", lambda **kwargs: Transport(provider)
    )
    response = await asyncio.wait_for(
        catalog.client.get(
            "/api/v1/repositories", headers=browser_cookie(catalog.user)
        ),
        timeout=1,
    )
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json()["code"] == "UPSTREAM_UNAVAILABLE"
    assert "items" not in response.json()
    assert "transport" in closed
    if phase == "body":
        assert "body" in closed


async def test_credential_database_failure_is_a_safe_service_error(
    wired_catalog: Catalog, caplog: pytest.LogCaptureFixture
) -> None:
    catalog = wired_catalog
    repository = await catalog.connect("alpha", "app")

    def break_credential_read(
        connection: Any, cursor: Any, statement: str, parameters: Any, *args: Any
    ) -> tuple[str, Any]:
        # Exercise a real PostgreSQL error only at the credential query; browser
        # session verification still succeeds against the actual database.
        return statement.replace(
            "github_identity.encrypted_token", "github_identity.unavailable_column"
        ), parameters

    def unexpected(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Credential DB failure must not reach GitHub")

    catalog.provider = unexpected
    engine = catalog.postgres.engine.sync_engine
    event.listen(engine, "before_cursor_execute", break_credential_read, retval=True)
    try:
        for path in (
            "/api/v1/repositories",
            f"/api/v1/repositories/{repository['id']}/pull-requests",
        ):
            response = await catalog.client.get(
                path, headers=browser_cookie(catalog.user)
            )
            assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
            assert response.json()["code"] == "SERVICE_UNAVAILABLE"
            assert "unavailable_column" not in response.text
    finally:
        event.remove(engine, "before_cursor_execute", break_credential_read)
    assert "test-user-secret" not in caplog.text
    assert "worker-secret" not in caplog.text


async def test_existing_access_gates_and_provider_errors_with_production_reader(
    wired_catalog: Catalog, caplog: pytest.LogCaptureFixture
) -> None:
    """Characterize inherited reader/error mapping through the new wiring."""
    catalog = wired_catalog
    await catalog.connect("alpha", "app")
    failing = await catalog.connect("beta", "app")
    foreign = await catalog.connect("gamma", "app", user=catalog.other)
    readable = catalog.provider
    headers = browser_cookie(catalog.user)
    observed: list[str] = []

    def provider(request: httpx.Request) -> httpx.Response:
        observed.append(request.url.path)
        assert request.headers["Authorization"] == "Bearer test-user-secret"
        return readable(request)

    catalog.provider = provider
    denied = await catalog.client.get(
        f"/api/v1/repositories/{foreign['id']}/pull-requests", headers=headers
    )
    assert denied.status_code == HTTPStatus.NOT_FOUND
    assert not observed

    # Check every request against GitHub; stale local access cannot grant reads.
    catalog.provider = lambda request: httpx.Response(404)
    revoked = await catalog.client.get("/api/v1/repositories", headers=headers)
    assert revoked.json() == {"items": [], "next_cursor": None}
    pulls = await catalog.client.get(
        f"/api/v1/repositories/{failing['id']}/pull-requests", headers=headers
    )
    assert pulls.status_code == HTTPStatus.FORBIDDEN
    assert pulls.json()["code"] == "ACCESS_DENIED"

    for status, code in ((429, "UPSTREAM_RATE_LIMITED"), (503, "UPSTREAM_UNAVAILABLE")):

        def unavailable(request: httpx.Request, status: int = status) -> httpx.Response:
            if request.url.path.endswith(
                failing["external_id"]
            ) or request.url.path.endswith("/pulls"):
                return httpx.Response(
                    status,
                    headers={"Retry-After": "17"},
                    json={"message": "test-user-secret private-provider-metadata"},
                )
            return readable(request)

        catalog.provider = unavailable
        for path in (
            "/api/v1/repositories",
            f"/api/v1/repositories/{failing['id']}/pull-requests",
        ):
            response = await catalog.client.get(path, headers=headers)
            assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
            assert response.json()["code"] == code
            assert "items" not in response.json()
            assert "test-user-secret" not in response.text
            assert "private-provider-metadata" not in response.text
    assert "test-user-secret" not in caplog.text
    assert "worker-secret" not in caplog.text
    assert "private-provider-metadata" not in caplog.text


@pytest.mark.parametrize("key", ["", "malformed"])
async def test_unavailable_credential_configuration_is_fail_closed(
    wired_catalog: Catalog, reader_settings: Settings, key: str
) -> None:
    """Missing and invalid keys never enable a worker-token fallback."""
    catalog = wired_catalog
    repository = await catalog.connect("alpha", "app")
    reader_settings.auth.credential_key = SecretStr(key)

    def unexpected(request: httpx.Request) -> httpx.Response:
        raise AssertionError("No provider request with unavailable credential key")

    catalog.provider = unexpected
    for path in (
        "/api/v1/repositories",
        f"/api/v1/repositories/{repository['id']}/pull-requests",
    ):
        response = await catalog.client.get(path, headers=browser_cookie(catalog.user))
        assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
        assert response.json()["code"] == "SERVICE_UNAVAILABLE"


async def test_missing_identity_does_not_fall_back_to_worker_token(
    wired_catalog: Catalog,
) -> None:
    catalog = wired_catalog
    repository = await catalog.connect("alpha", "app")
    async with catalog.postgres.engine.begin() as connection:
        await connection.execute(
            delete(GithubIdentity).where(GithubIdentity.user_id == catalog.user)
        )
    for path in (
        "/api/v1/repositories",
        f"/api/v1/repositories/{repository['id']}/pull-requests",
    ):
        response = await catalog.client.get(path, headers=browser_cookie(catalog.user))
        assert response.status_code == HTTPStatus.UNAUTHORIZED
        assert response.json()["code"] == "AUTHENTICATION_REQUIRED"
