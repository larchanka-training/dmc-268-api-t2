"""Browser OAuth/session contract over PostgreSQL and the GitHub HTTP boundary."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from http import HTTPStatus
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
import pytest_asyncio
import yaml
from cryptography.fernet import Fernet
from fastapi import FastAPI
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import SecretStr
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from test_postgres_integration import PostgresHarness

from app.api.github_auth import get_oauth_http
from app.core.config import Settings, get_settings
from app.db.models import AuthSession, GithubIdentity, GithubOAuthFlow, User
from app.db.session import get_session
from app.main import create_app

pytest_plugins = ("test_postgres_integration",)


@pytest.fixture
def provider() -> dict[str, httpx.Response]:
    return {
        "/login/oauth/access_token": httpx.Response(
            200, json={"access_token": "test-user-secret", "token_type": "bearer"}
        ),
        "/user": httpx.Response(
            200,
            json={
                "id": uuid.uuid4().int >> 64,
                "login": "octocat",
                "private": "secret",
            },
        ),
    }


@pytest.fixture
def github_requests() -> list[httpx.Request]:
    return []


@pytest.fixture
def auth_settings() -> Settings:
    return Settings.model_validate(
        {
            "auth": {
                "github_client_id": "test-client",
                "github_client_secret": "test-oauth-secret",
                "github_redirect_uri": "https://test/api/v1/auth/github/callback",
                "jwt_secret": "test-signing-key-32-bytes-minimum!!",
                "credential_key": "dGVzdC1lbmNyeXB0aW9uLWtleS0zMi1ieXRlcyEhISE=",
                "frontend_origins": ["https://frontend.test"],
            }
        }
    )


@pytest_asyncio.fixture
async def auth_app() -> FastAPI:
    return create_app()


@pytest_asyncio.fixture
async def browser(
    postgres: PostgresHarness,
    provider: dict[str, httpx.Response],
    github_requests: list[httpx.Request],
    auth_settings: Settings,
    auth_app: FastAPI,
) -> AsyncIterator[httpx.AsyncClient]:
    app = auth_app
    factory = async_sessionmaker(postgres.engine, expire_on_commit=False)

    async def session() -> AsyncIterator[AsyncSession]:
        async with factory() as value:
            yield value

    app.dependency_overrides[get_settings] = lambda: auth_settings
    app.dependency_overrides[get_session] = session

    def dispatch(request: httpx.Request) -> httpx.Response:
        github_requests.append(request)
        assert request.url.scheme == "https"
        if request.url.host == "github.com":
            assert request.method == "POST"
            form = parse_qs(request.content.decode())
            assert form["client_id"] == ["test-client"]
            assert form["client_secret"] == ["test-oauth-secret"]
            assert form["redirect_uri"] == ["https://test/api/v1/auth/github/callback"]
            assert "Authorization" not in request.headers
        else:
            assert request.url.host == "api.github.com"
            token = provider["/login/oauth/access_token"].json()["access_token"]
            assert request.headers["Authorization"] == f"Bearer {token}"
        return provider[request.url.path]

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(dispatch)) as github,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://test"
        ) as client,
    ):
        app.dependency_overrides[get_oauth_http] = lambda: github
        try:
            yield client
        finally:
            app.dependency_overrides.clear()
            async with factory.begin() as cleanup:
                # Only AUTH-01 owns these tables in this disposable test DB.
                await cleanup.execute(delete(AuthSession))
                users = list(await cleanup.scalars(select(GithubIdentity.user_id)))
                await cleanup.execute(delete(GithubIdentity))
                await cleanup.execute(delete(User).where(User.id.in_(users)))
                await cleanup.execute(delete(GithubOAuthFlow))


@pytest.mark.parametrize(
    "return_to",
    [
        None,
        "/repos?tab=mine",
        "https://evil.test",
        "//evil.test",
        "/\\evil.test",
        "/%2f/evil.test",
        "/%0d%0aLocation:evil",
    ],
)
async def test_start_redirects_to_github_with_browser_bound_state(
    browser: httpx.AsyncClient, return_to: str | None
) -> None:
    response = await browser.get(
        "/api/v1/auth/github/start",
        params={} if return_to is None else {"return_to": return_to},
    )
    assert response.status_code == HTTPStatus.FOUND
    location = urlsplit(response.headers["location"])
    assert (location.scheme, location.netloc, location.path) == (
        "https",
        "github.com",
        "/login/oauth/authorize",
    )
    query = parse_qs(location.query)
    assert query["client_id"] == ["test-client"]
    assert query["redirect_uri"] == ["https://test/api/v1/auth/github/callback"]
    assert len(query["state"][0]) >= 32  # noqa: PLR2004
    assert "scope" not in query
    cookie = response.headers["set-cookie"]
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert "Max-Age=600" in cookie
    assert query["state"][0] not in cookie
    callback = await browser.get(
        "/api/v1/auth/github/callback",
        params={"state": query["state"][0], "code": "private-code"},
    )
    assert callback.headers["location"] == (
        return_to if return_to == "/repos?tab=mine" else "/"
    )
    again = await browser.get("/api/v1/auth/github/start")
    assert (
        parse_qs(urlsplit(again.headers["location"]).query)["state"] != query["state"]
    )


@pytest.mark.parametrize("fault", ["missing", "unknown", "other_browser", "expired"])
async def test_callback_rejects_state_without_a_fresh_bound_browser_flow(
    browser: httpx.AsyncClient, postgres: PostgresHarness, fault: str
) -> None:
    response = await browser.get("/api/v1/auth/github/start")
    state = parse_qs(urlsplit(response.headers["location"]).query)["state"][0]
    original_nonce = str(browser.cookies.get("__Host-dmc_oauth"))
    if fault == "missing":
        state = ""
    elif fault == "unknown":
        state = "forged-state"
    elif fault == "other_browser":
        browser.cookies.clear()
    else:
        async with postgres.engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE github_oauth_flow "
                    "SET expires_at = now() - interval '1 second'"
                )
            )
    callback = await browser.get(
        "/api/v1/auth/github/callback", params={"state": state, "code": "private-code"}
    )
    assert callback.status_code == HTTPStatus.SEE_OTHER
    assert callback.headers["location"] == "/login?error=invalid_state"
    assert "dmc_session" not in browser.cookies
    if fault == "other_browser":
        browser.cookies.set(
            "__Host-dmc_oauth", original_nonce, domain="test.local", path="/"
        )
        legitimate = await browser.get(
            "/api/v1/auth/github/callback",
            params={"state": state, "code": "private-code"},
        )
        assert legitimate.headers["location"] == "/"
        assert (await browser.get("/api/v1/me")).status_code == HTTPStatus.OK


async def test_callback_establishes_one_session_and_returns_to_original_path(
    browser: httpx.AsyncClient,
) -> None:
    response = await browser.get(
        "/api/v1/auth/github/start", params={"return_to": "/repos?tab=mine"}
    )
    state = parse_qs(urlsplit(response.headers["location"]).query)["state"][0]
    nonce = browser.cookies.get("__Host-dmc_oauth")
    response = await browser.get(
        "/api/v1/auth/github/callback", params={"state": state, "code": "private-code"}
    )
    assert response.status_code == HTTPStatus.SEE_OTHER
    assert response.headers["location"] == "/repos?tab=mine"
    cookie = response.headers.get_list("set-cookie")
    session_cookie = next(value for value in cookie if value.startswith("dmc_session="))
    assert all(
        value in session_cookie
        for value in ["Secure", "HttpOnly", "SameSite=lax", "Max-Age=1800"]
    )
    assert "__Host-dmc_oauth" not in browser.cookies
    assert "test-user-secret" not in str(response.headers)
    # Replaying even the original browser cookie cannot mint another session.
    original = browser.cookies.get("dmc_session")
    browser.cookies.set("__Host-dmc_oauth", str(nonce), domain="test.local", path="/")
    replay = await browser.get(
        "/api/v1/auth/github/callback", params={"state": state, "code": "private-code"}
    )
    assert replay.headers["location"] == "/login?error=invalid_state"
    assert browser.cookies.get("dmc_session") == original


async def login(
    browser: httpx.AsyncClient, return_to: str = "/repos"
) -> httpx.Response:
    start = await browser.get(
        "/api/v1/auth/github/start", params={"return_to": return_to}
    )
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    return await browser.get(
        "/api/v1/auth/github/callback", params={"state": state, "code": "private-code"}
    )


@pytest.mark.parametrize(
    "session_state",
    [
        "active",
        "absent",
        "forged",
        "expired_jwt",
        "revoked",
        "expired_db",
        "unknown_sid",
        "wrong_user",
        "unsigned",
        "missing_exp",
    ],
)
async def test_me_requires_signed_unexpired_cookie_and_active_postgres_session(
    browser: httpx.AsyncClient,
    postgres: PostgresHarness,
    provider: dict[str, httpx.Response],
    session_state: str,
) -> None:
    await login(browser)
    token = str(browser.cookies.get("dmc_session"))
    claims = jwt.decode(
        token,
        "test-signing-key-32-bytes-minimum!!",
        algorithms=["HS256"],
        audience="dmc268-browser",
        issuer="dmc268",
    )
    assert claims["exp"] - claims["iat"] == 1800  # noqa: PLR2004
    if session_state == "absent":
        browser.cookies.clear()
    elif session_state == "forged":
        token = jwt.encode(
            claims, "different-test-key-32-bytes-minimum", algorithm="HS256"
        )
    elif session_state == "expired_jwt":
        claims["iat"], claims["exp"] = 1, 2
    elif session_state == "unknown_sid":
        claims["sid"] = str(uuid.uuid4())
    elif session_state == "wrong_user":
        claims["sub"] = str(uuid.uuid4())
    elif session_state == "unsigned":
        token = jwt.encode(claims, "", algorithm="none")
    elif session_state == "missing_exp":
        del claims["exp"]
    elif session_state in {"revoked", "expired_db"}:
        async with postgres.engine.begin() as connection:
            column = "revoked_at" if session_state == "revoked" else "expires_at"
            await connection.execute(
                text(
                    f"UPDATE auth_session SET {column} = now() - interval '1 second' "
                    "WHERE id=:sid"
                ),
                {"sid": uuid.UUID(claims["sid"])},
            )
    if session_state in {"expired_jwt", "unknown_sid", "wrong_user", "missing_exp"}:
        token = jwt.encode(
            claims, "test-signing-key-32-bytes-minimum!!", algorithm="HS256"
        )
    headers = {} if session_state == "absent" else {"Cookie": f"dmc_session={token}"}
    response = await browser.get("/api/v1/me", headers=headers)
    if session_state == "active":
        data = response.json()
        assert response.status_code == HTTPStatus.OK
        contract = yaml.safe_load(Path("openapi.yaml").read_text())
        Draft202012Validator(
            contract["components"]["schemas"]["Me"], format_checker=FormatChecker()
        ).validate(data)
        assert data["user"] == {
            "id": claims["sub"],
            "provider": "github",
            "external_id": str(provider["/user"].json()["id"]),
            "login": "octocat",
        }
        assert (
            datetime.fromisoformat(data["session"]["expires_at"]).timestamp()
            == claims["exp"]
        )
        assert data["csrf_token"]
        assert response.headers["cache-control"] == "no-store"
        assert all(
            secret not in response.text
            for secret in [
                "test-user-secret",
                "test-oauth-secret",
                "private-code",
                "encrypted_token",
                "sid",
            ]
        )
    else:
        assert response.status_code == HTTPStatus.UNAUTHORIZED
        assert response.json() == {
            "code": "AUTHENTICATION_REQUIRED",
            "message": "Authentication is required.",
            "request_id": response.headers["x-request-id"],
            "retryable": False,
            "retry_after": None,
        }
        assert "location" not in response.headers
        refresh = await browser.post("/api/v1/auth/refresh", headers=headers)
        assert refresh.status_code == HTTPStatus.UNAUTHORIZED
        assert refresh.json()["code"] == "AUTHENTICATION_REQUIRED"
        assert "set-cookie" not in refresh.headers


async def test_provider_denial_consumes_flow_without_using_injected_code(
    browser: httpx.AsyncClient, github_requests: list[httpx.Request]
) -> None:
    start = await browser.get("/api/v1/auth/github/start")
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    nonce = str(browser.cookies.get("__Host-dmc_oauth"))
    response = await browser.get(
        "/api/v1/auth/github/callback",
        params={
            "state": state,
            "error": "access_denied&secret=provider-description",
            "code": "private-code",
        },
    )
    assert response.status_code == HTTPStatus.SEE_OTHER
    assert response.headers["location"] == "/login?error=github_denied"
    assert "dmc_session" not in browser.cookies
    assert not github_requests
    assert (await browser.get("/api/v1/me")).status_code == HTTPStatus.UNAUTHORIZED
    browser.cookies.set("__Host-dmc_oauth", nonce, domain="test.local", path="/")
    replay = await browser.get(
        "/api/v1/auth/github/callback", params={"state": state, "code": "private-code"}
    )
    assert replay.headers["location"] == "/login?error=invalid_state"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("github_client_id", ""),
        ("github_client_secret", ""),
        ("github_redirect_uri", ""),
        ("jwt_secret", ""),
        ("credential_key", ""),
        ("jwt_secret", "short"),
        ("credential_key", "malformed"),
        ("github_redirect_uri", "https://evil.test/auth/callback"),
        ("github_redirect_uri", "http://test/api/v1/auth/github/callback"),
    ],
)
async def test_missing_or_invalid_auth_configuration_fails_closed_at_runtime(
    browser: httpx.AsyncClient,
    auth_settings: Settings,
    github_requests: list[httpx.Request],
    field: str,
    value: str,
) -> None:
    start = await browser.get("/api/v1/auth/github/start")
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    setattr(
        auth_settings.auth,
        field,
        SecretStr(value)
        if field in {"jwt_secret", "credential_key", "github_client_secret"}
        else value,
    )
    unavailable = await browser.get("/api/v1/auth/github/start")
    assert unavailable.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert unavailable.json()["code"] == "SERVICE_UNAVAILABLE"
    callback = await browser.get(
        "/api/v1/auth/github/callback", params={"state": state, "code": "private-code"}
    )
    assert callback.headers["location"] == "/login?error=auth_unavailable"
    assert "dmc_session" not in browser.cookies
    assert not github_requests
    assert (await browser.get("/healthcheck")).status_code == HTTPStatus.OK


@pytest.mark.parametrize(
    "failure",
    [
        "exchange_http",
        "exchange_error",
        "exchange_json",
        "exchange_type",
        "identity_http",
        "identity_json",
        "identity_invalid",
        "timeout",
    ],
)
async def test_github_failure_is_a_safe_redirect_without_a_session(
    browser: httpx.AsyncClient,
    provider: dict[str, httpx.Response],
    failure: str,
) -> None:
    if failure == "exchange_http":
        provider["/login/oauth/access_token"] = httpx.Response(
            502, text="private-provider-secret"
        )
    elif failure == "exchange_error":
        provider["/login/oauth/access_token"] = httpx.Response(
            200,
            json={
                "error": "bad_verification_code",
                "error_description": "private-provider-secret",
            },
        )
    elif failure == "exchange_json":
        provider["/login/oauth/access_token"] = httpx.Response(
            200, text="private-provider-secret"
        )
    elif failure == "exchange_type":
        provider["/login/oauth/access_token"] = httpx.Response(
            200, json={"access_token": "test-user-secret", "token_type": ["bearer"]}
        )
    elif failure == "identity_http":
        provider["/user"] = httpx.Response(403, text="private-provider-secret")
    elif failure == "identity_json":
        provider["/user"] = httpx.Response(200, text="private-provider-secret")
    elif failure == "identity_invalid":
        provider["/user"] = httpx.Response(200, json={"id": True, "login": "octocat"})
    else:

        class Interrupted(httpx.AsyncByteStream):
            async def __aiter__(self) -> AsyncIterator[bytes]:
                raise httpx.ReadTimeout("private-provider-secret")
                yield b""  # pragma: no cover

        provider["/login/oauth/access_token"] = httpx.Response(
            200, stream=Interrupted()
        )
    response = await login(browser)
    assert response.status_code == HTTPStatus.SEE_OTHER
    assert response.headers["location"] == "/login?error=github_unavailable"
    assert "dmc_session" not in browser.cookies
    assert (await browser.get("/api/v1/me")).status_code == HTTPStatus.UNAUTHORIZED
    assert "private-provider-secret" not in str(response.headers)


async def test_repeated_github_login_preserves_user_id_and_encrypts_rotated_token(
    browser: httpx.AsyncClient,
    provider: dict[str, httpx.Response],
    postgres: PostgresHarness,
    auth_settings: Settings,
) -> None:
    await login(browser)
    first = (await browser.get("/api/v1/me")).json()
    provider["/user"] = httpx.Response(
        200, json={"id": int(first["user"]["external_id"]), "login": "renamed-octocat"}
    )
    provider["/login/oauth/access_token"] = httpx.Response(
        200, json={"access_token": "rotated-user-secret", "token_type": "bearer"}
    )
    old_cookie = browser.cookies.get("dmc_session")
    await login(browser)
    second = (await browser.get("/api/v1/me")).json()
    assert second["user"]["id"] == first["user"]["id"]
    assert second["user"]["login"] == "renamed-octocat"
    assert browser.cookies.get("dmc_session") != old_cookie
    assert second["csrf_token"] != first["csrf_token"]
    # At-rest encryption is a storage requirement and cannot be observed in DTOs.
    async with postgres.engine.connect() as connection:
        encrypted = (
            await connection.execute(
                select(GithubIdentity.encrypted_token).where(
                    GithubIdentity.user_id == uuid.UUID(first["user"]["id"])
                )
            )
        ).scalar_one()
    assert "rotated-user-secret" not in encrypted
    assert (
        Fernet(auth_settings.auth.credential_key.get_secret_value()).decrypt(
            encrypted.encode()
        )
        == b"rotated-user-secret"
    )


async def test_concurrent_callback_replay_creates_only_one_session(
    browser: httpx.AsyncClient, github_requests: list[httpx.Request]
) -> None:
    start = await browser.get(
        "/api/v1/auth/github/start", params={"return_to": "/repos"}
    )
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    nonce = browser.cookies.get("__Host-dmc_oauth")
    responses = await asyncio.gather(
        *(
            browser.get(
                "/api/v1/auth/github/callback",
                params={"state": state, "code": "private-code"},
                headers={"Cookie": f"__Host-dmc_oauth={nonce}"},
            )
            for _ in range(2)
        )
    )
    assert sorted(response.headers["location"] for response in responses) == [
        "/login?error=invalid_state",
        "/repos",
    ]
    assert len(github_requests) == 2  # noqa: PLR2004 -- one exchange + identity read
    assert (await browser.get("/api/v1/me")).status_code == HTTPStatus.OK


async def test_refresh_extends_same_session_and_returns_exact_me(
    browser: httpx.AsyncClient, postgres: PostgresHarness
) -> None:
    await login(browser)
    before = (await browser.get("/api/v1/me")).json()
    original = jwt.decode(
        str(browser.cookies.get("dmc_session")),
        options={"verify_signature": False},
    )
    # Make the old DB expiry observably earlier, without waiting for wall time.
    async with postgres.engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE auth_session SET expires_at = "
                "clock_timestamp() + interval '2 minutes'"
            ),
        )
        db_before = (
            await connection.execute(text("SELECT clock_timestamp()"))
        ).scalar_one()
    response = await browser.post(
        "/api/v1/auth/refresh",
        headers={
            "Origin": "https://frontend.test",
            "X-CSRF-Token": before["csrf_token"],
        },
    )
    assert response.status_code == HTTPStatus.OK
    data = response.json()
    contract = yaml.safe_load(Path("openapi.yaml").read_text())
    Draft202012Validator(
        contract["components"]["schemas"]["Me"], format_checker=FormatChecker()
    ).validate(data)
    assert data["user"] == before["user"]
    assert data["csrf_token"] == before["csrf_token"]
    async with postgres.engine.connect() as connection:
        db_after = (
            await connection.execute(text("SELECT clock_timestamp()"))
        ).scalar_one()
    expiry = datetime.fromisoformat(data["session"]["expires_at"])
    assert (
        db_before + timedelta(minutes=30, seconds=-1)
        <= expiry
        <= db_after + timedelta(minutes=30)
    )
    assert (await browser.get("/api/v1/me")).json() == data
    renewed = jwt.decode(
        str(browser.cookies.get("dmc_session")),
        "test-signing-key-32-bytes-minimum!!",
        algorithms=["HS256"],
        audience="dmc268-browser",
        issuer="dmc268",
    )
    assert renewed["sid"] == original["sid"]
    assert renewed["sub"] == original["sub"]
    assert renewed["exp"] == int(expiry.timestamp())
    assert renewed["exp"] - renewed["iat"] == 1800  # noqa: PLR2004 -- 30-minute TTL
    cookie = response.headers["set-cookie"]
    assert all(
        part in cookie
        for part in ["Secure", "HttpOnly", "SameSite=lax", "Max-Age=1800", "Path=/"]
    )
    assert "Domain=" not in cookie
    assert response.headers["cache-control"] == "no-store"


async def test_logout_revokes_session_and_clears_cookie(
    browser: httpx.AsyncClient,
) -> None:
    await login(browser)
    token = browser.cookies.get("dmc_session")
    me = (await browser.get("/api/v1/me")).json()
    response = await browser.post(
        "/api/v1/auth/logout",
        headers={"Origin": "https://frontend.test", "X-CSRF-Token": me["csrf_token"]},
    )
    assert response.status_code == HTTPStatus.NO_CONTENT
    assert response.content == b""
    cookie = response.headers["set-cookie"]
    assert all(
        part in cookie
        for part in [
            "dmc_session=",
            "Max-Age=0",
            "Secure",
            "HttpOnly",
            "SameSite=lax",
            "Path=/",
        ]
    )
    assert "Domain=" not in cookie
    assert "dmc_session" not in browser.cookies
    assert response.headers["cache-control"] == "no-store"
    assert (
        await browser.get("/api/v1/me", headers={"Cookie": f"dmc_session={token}"})
    ).status_code == HTTPStatus.UNAUTHORIZED


@pytest.mark.parametrize(
    "state", ["absent", "forged", "expired_jwt", "expired_db", "revoked", "unknown_sid"]
)
async def test_logout_invalid_session_is_idempotent_without_origin_or_csrf(
    browser: httpx.AsyncClient, postgres: PostgresHarness, state: str
) -> None:
    await login(browser)
    token = str(browser.cookies.get("dmc_session"))
    claims = jwt.decode(token, options={"verify_signature": False})
    if state == "absent":
        browser.cookies.clear()
    elif state == "forged":
        token = "not-a-jwt"
    elif state == "expired_jwt":
        claims["iat"], claims["exp"] = 1, 2
        token = jwt.encode(
            claims, "test-signing-key-32-bytes-minimum!!", algorithm="HS256"
        )
    elif state == "unknown_sid":
        claims["sid"] = str(uuid.uuid4())
        token = jwt.encode(
            claims, "test-signing-key-32-bytes-minimum!!", algorithm="HS256"
        )
    else:
        column = "expires_at" if state == "expired_db" else "revoked_at"
        async with postgres.engine.begin() as connection:
            await connection.execute(
                text(
                    f"UPDATE auth_session SET {column} = "
                    "clock_timestamp() - interval '1 second' WHERE id=:sid"
                ),
                {"sid": uuid.UUID(claims["sid"])},
            )
    for _ in range(2):
        response = await browser.post(
            "/api/v1/auth/logout",
            headers={} if state == "absent" else {"Cookie": f"dmc_session={token}"},
        )
        assert response.status_code == HTTPStatus.NO_CONTENT
        assert response.content == b""
        assert "Max-Age=0" in response.headers["set-cookie"]
        assert "dmc_session" not in browser.cookies


async def wait_for_session_mutations(postgres: PostgresHarness, count: int) -> None:
    # Synchronize on PostgreSQL locks, rather than scheduling guesses/sleeps.
    async with asyncio.timeout(10):
        while True:
            async with postgres.engine.connect() as observer:
                waiting = await observer.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE wait_event_type = 'Lock' "
                        "AND (query LIKE 'UPDATE auth_session%' OR "
                        "query LIKE 'SELECT auth_session.id%FOR UPDATE%')"
                    )
                )
            if waiting == count:
                return
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("winner", ["logout", "expiry"])
async def test_refresh_cannot_resurrect_session_invalidated_while_waiting_for_lock(
    browser: httpx.AsyncClient, postgres: PostgresHarness, winner: str
) -> None:
    await login(browser)
    token = str(browser.cookies.get("dmc_session"))
    claims = jwt.decode(token, options={"verify_signature": False})
    me = (await browser.get("/api/v1/me")).json()
    headers = {
        "Cookie": f"dmc_session={token}",
        "Origin": "https://frontend.test",
        "X-CSRF-Token": me["csrf_token"],
    }
    tasks: list[asyncio.Task[httpx.Response]] = []
    try:
        async with postgres.engine.begin() as blocker:
            await blocker.execute(
                text("SELECT id FROM auth_session WHERE id=:sid FOR UPDATE"),
                {"sid": uuid.UUID(claims["sid"])},
            )
            if winner == "logout":
                tasks.append(
                    asyncio.create_task(
                        browser.post("/api/v1/auth/logout", headers=headers)
                    )
                )
                await wait_for_session_mutations(postgres, 1)
            tasks.append(
                asyncio.create_task(
                    browser.post("/api/v1/auth/refresh", headers=headers)
                )
            )
            await wait_for_session_mutations(postgres, len(tasks))
            if winner == "expiry":
                await blocker.execute(
                    text(
                        "UPDATE auth_session SET expires_at="
                        "clock_timestamp() - interval '1 second' WHERE id=:sid"
                    ),
                    {"sid": uuid.UUID(claims["sid"])},
                )
        responses = await asyncio.gather(*tasks)
        if winner == "logout":
            assert responses[0].status_code == HTTPStatus.NO_CONTENT
        refresh = responses[-1]
        assert refresh.status_code == HTTPStatus.UNAUTHORIZED
        assert "set-cookie" not in refresh.headers
        assert (
            await browser.get("/api/v1/me", headers={"Cookie": f"dmc_session={token}"})
        ).status_code == HTTPStatus.UNAUTHORIZED
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("endpoint", ["refresh", "logout"])
@pytest.mark.parametrize("phase", ["update", "commit"])
async def test_session_mutation_database_failure_is_safe_503_without_cookie_change(
    browser: httpx.AsyncClient, postgres: PostgresHarness, endpoint: str, phase: str
) -> None:
    await login(browser)
    me = (await browser.get("/api/v1/me")).json()
    # Real PostgreSQL errors at statement and deferred-commit boundaries.
    async with postgres.engine.begin() as connection:
        await connection.execute(
            text(
                "CREATE FUNCTION auth02_fail_mutation() RETURNS trigger "
                "LANGUAGE plpgsql AS $$ BEGIN "
                "RAISE EXCEPTION 'private-database-detail'; END $$"
            )
        )
        trigger = (
            "CREATE TRIGGER auth02_failure BEFORE UPDATE ON auth_session "
            "FOR EACH ROW EXECUTE FUNCTION auth02_fail_mutation()"
            if phase == "update"
            else "CREATE CONSTRAINT TRIGGER auth02_failure "
            "AFTER UPDATE ON auth_session DEFERRABLE INITIALLY DEFERRED "
            "FOR EACH ROW EXECUTE FUNCTION auth02_fail_mutation()"
        )
        await connection.execute(text(trigger))
    try:
        response = await browser.post(
            f"/api/v1/auth/{endpoint}",
            headers={
                "Origin": "https://frontend.test",
                "X-CSRF-Token": me["csrf_token"],
            },
        )
        assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
        assert response.json() == {
            "code": "SERVICE_UNAVAILABLE",
            "message": "Service is unavailable.",
            "request_id": response.headers["x-request-id"],
            "retryable": True,
            "retry_after": None,
        }
        assert "set-cookie" not in response.headers
        assert "private-database-detail" not in response.text
    finally:
        async with postgres.engine.begin() as connection:
            await connection.execute(
                text("DROP TRIGGER auth02_failure ON auth_session")
            )
            await connection.execute(text("DROP FUNCTION auth02_fail_mutation()"))
    assert (await browser.get("/api/v1/me")).json() == me


@pytest.mark.parametrize("endpoint", ["refresh", "logout"])
async def test_missing_signing_dependency_is_503_and_never_successful_logout(
    browser: httpx.AsyncClient, auth_settings: Settings, endpoint: str
) -> None:
    await login(browser)
    auth_settings.auth.jwt_secret = SecretStr("")
    response = await browser.post(f"/api/v1/auth/{endpoint}")
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json()["code"] == "SERVICE_UNAVAILABLE"
    assert "set-cookie" not in response.headers


async def test_verified_browser_mutation_still_cannot_access_unconnected_repository(
    browser: httpx.AsyncClient,
) -> None:
    await login(browser)
    me = (await browser.get("/api/v1/me")).json()
    response = await browser.post(
        f"/api/v1/repositories/{uuid.uuid4()}/pull-requests/1/reviews",
        headers={"Origin": "https://frontend.test", "X-CSRF-Token": me["csrf_token"]},
        json={},
    )
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json()["code"] == "RESOURCE_NOT_FOUND"


async def test_refresh_cannot_renew_session_that_expires_while_row_is_locked(
    browser: httpx.AsyncClient, postgres: PostgresHarness
) -> None:
    await login(browser)
    me = (await browser.get("/api/v1/me")).json()
    async with postgres.engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE auth_session SET expires_at="
                "clock_timestamp() + interval '2 seconds'"
            )
        )
    task = None
    try:
        async with postgres.engine.begin() as blocker:
            await blocker.execute(text("SELECT id FROM auth_session FOR UPDATE"))
            task = asyncio.create_task(
                browser.post(
                    "/api/v1/auth/refresh",
                    headers={
                        "Origin": "https://frontend.test",
                        "X-CSRF-Token": me["csrf_token"],
                    },
                )
            )
            await wait_for_session_mutations(postgres, 1)
            # Let PostgreSQL time cross expiry without modifying the locked row.
            async with asyncio.timeout(10):
                while not await blocker.scalar(
                    text("SELECT expires_at <= clock_timestamp() FROM auth_session")
                ):
                    await asyncio.sleep(0.01)
        response = await task
        assert response.status_code == HTTPStatus.UNAUTHORIZED
        assert "set-cookie" not in response.headers
        assert (await browser.get("/api/v1/me")).status_code == HTTPStatus.UNAUTHORIZED
    finally:
        if task is not None:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("endpoint", ["refresh", "logout"])
async def test_unreachable_postgres_is_503_and_does_not_clear_session(
    browser: httpx.AsyncClient,
    auth_app: FastAPI,
    postgres: PostgresHarness,
    endpoint: str,
) -> None:
    await login(browser)
    me = (await browser.get("/api/v1/me")).json()
    unreachable = create_async_engine(
        postgres.engine.url.set(port=1), connect_args={"timeout": 1}
    )
    factory = async_sessionmaker(unreachable)

    async def unavailable_session() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    auth_app.dependency_overrides[get_session] = unavailable_session
    try:
        response = await browser.post(
            f"/api/v1/auth/{endpoint}",
            headers={
                "Origin": "https://frontend.test",
                "X-CSRF-Token": me["csrf_token"],
            },
        )
        assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
        assert response.json()["code"] == "SERVICE_UNAVAILABLE"
        assert response.json()["retryable"] is True
        assert "set-cookie" not in response.headers
    finally:
        await unreachable.dispose()


@pytest.mark.parametrize("endpoint", ["refresh", "logout"])
async def test_csrf_from_another_active_session_is_rejected(
    browser: httpx.AsyncClient, endpoint: str
) -> None:
    await login(browser)
    first = (await browser.get("/api/v1/me")).json()
    await login(browser)
    second = (await browser.get("/api/v1/me")).json()
    assert first["user"]["id"] == second["user"]["id"]
    assert first["csrf_token"] != second["csrf_token"]
    response = await browser.post(
        f"/api/v1/auth/{endpoint}",
        headers={
            "Origin": "https://frontend.test",
            "X-CSRF-Token": first["csrf_token"],
        },
    )
    assert response.status_code == HTTPStatus.FORBIDDEN
    assert "set-cookie" not in response.headers
    assert (await browser.get("/api/v1/me")).json() == second


@pytest.mark.parametrize("endpoint", ["refresh", "logout"])
@pytest.mark.parametrize(
    "origins",
    [
        [],
        ["*"],
        ["https://*.test"],
        ["https://frontend.test/path"],
        ["https://frontend.test", "https://frontend.test:bad"],
    ],
)
async def test_invalid_frontend_origin_configuration_fails_closed(
    browser: httpx.AsyncClient,
    auth_settings: Settings,
    endpoint: str,
    origins: list[str],
) -> None:
    await login(browser)
    me = (await browser.get("/api/v1/me")).json()
    auth_settings.auth.frontend_origins = origins
    response = await browser.post(
        f"/api/v1/auth/{endpoint}",
        headers={"Origin": "https://frontend.test", "X-CSRF-Token": me["csrf_token"]},
    )
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert "set-cookie" not in response.headers
    assert (await browser.get("/api/v1/me")).json() == me


async def test_logout_revokes_a_concurrently_refreshed_session(
    browser: httpx.AsyncClient, postgres: PostgresHarness
) -> None:
    await login(browser)
    me = (await browser.get("/api/v1/me")).json()
    token = str(browser.cookies.get("dmc_session"))
    headers = {
        "Cookie": f"dmc_session={token}",
        "Origin": "https://frontend.test",
        "X-CSRF-Token": me["csrf_token"],
    }
    tasks: list[asyncio.Task[httpx.Response]] = []
    try:
        async with postgres.engine.begin() as blocker:
            await blocker.execute(text("SELECT id FROM auth_session FOR UPDATE"))
            tasks.append(
                asyncio.create_task(
                    browser.post("/api/v1/auth/refresh", headers=headers)
                )
            )
            await wait_for_session_mutations(postgres, 1)
            tasks.append(
                asyncio.create_task(
                    browser.post("/api/v1/auth/logout", headers=headers)
                )
            )
            await wait_for_session_mutations(postgres, 2)
        refreshed, logged_out = await asyncio.gather(*tasks)
        assert refreshed.status_code == HTTPStatus.OK
        assert logged_out.status_code == HTTPStatus.NO_CONTENT
        renewed = refreshed.cookies.get("dmc_session")
        for cookie in (token, renewed):
            assert (
                await browser.get(
                    "/api/v1/me", headers={"Cookie": f"dmc_session={cookie}"}
                )
            ).status_code == HTTPStatus.UNAUTHORIZED
        repeated = await browser.post(
            "/api/v1/auth/logout", headers={"Cookie": f"dmc_session={renewed}"}
        )
        assert repeated.status_code == HTTPStatus.NO_CONTENT
        assert repeated.content == b""
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("endpoint", ["refresh", "logout"])
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Origin": "https://evil.test", "X-CSRF-Token": "correct"},
        {"Origin": "https://frontend.test", "X-CSRF-Token": "wrong"},
        {"Origin": "https://frontend.test"},
        {"Referer": "https://frontend.test/repos", "X-CSRF-Token": "correct"},
        {"Origin": "https://test", "X-CSRF-Token": "correct"},
        {"Origin": "null", "X-CSRF-Token": "correct"},
        {"Origin": "https://frontend.test.evil.test", "X-CSRF-Token": "correct"},
    ],
)
async def test_session_mutation_denies_unverified_origin_or_csrf_without_cookie_change(
    browser: httpx.AsyncClient, headers: dict[str, str], endpoint: str
) -> None:
    await login(browser)
    before = (await browser.get("/api/v1/me")).json()
    headers = {
        key: before["csrf_token"] if value == "correct" else value
        for key, value in headers.items()
    }
    response = await browser.post(f"/api/v1/auth/{endpoint}", headers=headers)
    assert response.status_code == HTTPStatus.FORBIDDEN
    assert response.json() == {
        "code": "ACCESS_DENIED",
        "message": "Access is denied.",
        "request_id": response.headers["x-request-id"],
        "retryable": False,
        "retry_after": None,
    }
    assert "set-cookie" not in response.headers
    assert (await browser.get("/api/v1/me")).json() == before
