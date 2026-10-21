"""StartReview authorization through browser sessions, PostgreSQL and GitHub HTTP."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from fastapi import FastAPI
from sqlalchemy import delete, event, insert, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_postgres_integration import PostgresHarness
from test_repository_reader_wiring import (
    _CREDENTIAL_KEY,
    _SIGNING_KEY,
    browser_cookie,
)
from test_review_results_api import assert_schema
from test_review_start_api import HEAD, HEADERS, StartScope
from test_review_start_e2e import github_transport

from app.api.auth import get_verified_mutation_identity
from app.api.review_start_dependencies import get_review_configuration_resolver
from app.core.config import get_settings
from app.db import models
from app.domain.frozen_configuration import ModelRoute, admission_policy
from app.review.configuration import FrozenAdmissionResolver, RuntimeProfile

pytest_plugins = ("test_review_start_api",)
_PERSONAL_TOKEN = "personal-start-token"
_WORKER_TOKEN = "worker-snapshot-token"


@dataclass
class BrowserStart:
    scope: StartScope
    client: httpx.AsyncClient
    provider: Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]]
    requests: list[httpx.Request]

    @property
    def headers(self) -> dict[str, str]:
        return {**HEADERS, **browser_cookie(self.scope.user)}

    async def start(self) -> httpx.Response:
        return await self.client.post(
            self.scope.url,
            json={"requested_head_sha": HEAD},
            headers=self.headers,
        )


@pytest_asyncio.fixture
async def browser_start(
    scope: StartScope,
    client: httpx.AsyncClient,
    app: FastAPI,
    postgres: PostgresHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[BrowserStart]:
    app.dependency_overrides.pop(get_verified_mutation_identity)
    for key, value in {
        "AUTH__JWT_SECRET": _SIGNING_KEY,
        "AUTH__CREDENTIAL_KEY": _CREDENTIAL_KEY,
        "AUTH__FRONTEND_ORIGINS": '["https://trusted.example"]',
        "GITHUB__TOKEN": _WORKER_TOKEN,
        "GITHUB__WRITE_TOKEN": "publication-only-token",
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    async with postgres.engine.begin() as connection:
        await connection.execute(
            insert(models.GithubIdentity).values(
                user_id=scope.user,
                external_id=str(scope.user.int),
                login="octocat",
                encrypted_token=Fernet(_CREDENTIAL_KEY)
                .encrypt(_PERSONAL_TOKEN.encode())
                .decode(),
            )
        )
        await connection.execute(
            insert(models.AuthSession).values(
                id=scope.user,
                user_id=scope.user,
                csrf_token="trusted",
                expires_at=datetime.now(UTC) + timedelta(minutes=30),
            )
        )
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
    active: set[int] = set()
    opened: set[httpx.MockTransport] = set()

    def checkout(dbapi: Any, record: Any, proxy: Any) -> None:
        active.add(id(record))

    def checkin(dbapi: Any, record: Any) -> None:
        active.discard(id(record))

    snapshot = github_transport()

    def provider(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repositories/123456":
            return httpx.Response(
                200, json={"id": 123456, "permissions": {"pull": True}}
            )
        return snapshot.handle_request(request)

    browser = BrowserStart(scope, client, provider, [])

    async def respond(request: httpx.Request) -> httpx.Response:
        assert not active, "PostgreSQL connection held during GitHub HTTP"
        browser.requests.append(request)
        expected = (
            _PERSONAL_TOKEN
            if request.url.path.startswith("/repositories/")
            else _WORKER_TOKEN
        )
        assert request.headers["Authorization"] == f"Bearer {expected}"
        response = browser.provider(request)
        return response if isinstance(response, httpx.Response) else await response

    class Transport(httpx.MockTransport):
        async def aclose(self) -> None:
            opened.discard(self)
            await super().aclose()

    def transport(**kwargs: Any) -> Transport:
        value = Transport(respond)
        opened.add(value)
        return value

    monkeypatch.setattr("httpx._client.AsyncHTTPTransport", transport)
    engine = postgres.engine.sync_engine
    event.listen(engine, "checkout", checkout)
    event.listen(engine, "checkin", checkin)
    try:
        yield browser
    finally:
        event.remove(engine, "checkout", checkout)
        event.remove(engine, "checkin", checkin)
        get_settings.cache_clear()
        assert not opened, "Production GitHub transports must be closed"
        async with postgres.engine.begin() as connection:
            await connection.execute(
                delete(models.AuthSession).where(
                    models.AuthSession.user_id == scope.user
                )
            )
            await connection.execute(
                delete(models.GithubIdentity).where(
                    models.GithubIdentity.user_id == scope.user
                )
            )


@pytest.mark.parametrize("role", ["reviewer", "admin"])
async def test_personal_read_permission_starts_durable_review(
    browser_start: BrowserStart, postgres: PostgresHarness, role: str
) -> None:
    async with postgres.engine.begin() as connection:
        await connection.execute(
            update(models.RepositoryAccess)
            .where(models.RepositoryAccess.user_id == browser_start.scope.user)
            .values(role=role)
        )
    response = await browser_start.start()
    assert response.status_code == HTTPStatus.ACCEPTED
    assert_schema(response, "StartReviewResponse")
    review_id = UUID(response.json()["review_id"])
    async with postgres.engine.connect() as connection:
        assert (
            await connection.scalar(
                select(models.ReviewJob.id).where(models.ReviewJob.id == review_id)
            )
            == review_id
        )
        assert (
            await connection.scalar(
                select(models.OutboxEvent.task_kind).where(
                    models.OutboxEvent.review_job_id == review_id
                )
            )
            == "analyze"
        )
    assert browser_start.requests[0].url.path == "/repositories/123456"


async def assert_no_admission(browser: BrowserStart, postgres: PostgresHarness) -> None:
    async with postgres.engine.connect() as connection:
        for entity in (models.ReviewJob, models.QuotaUsage):
            assert (
                await connection.scalar(
                    select(entity.id).where(
                        entity.repository_id == browser.scope.repository
                    )
                )
                is None
            )
        assert (
            await connection.scalar(
                select(models.OutboxEvent.id)
                .join(models.ReviewJob)
                .where(models.ReviewJob.repository_id == browser.scope.repository)
            )
            is None
        )


async def test_revoked_permission_denies_start_without_admission(
    browser_start: BrowserStart, postgres: PostgresHarness
) -> None:
    browser_start.provider = lambda request: httpx.Response(
        200, json={"id": 123456, "permissions": {"pull": False}}
    )
    response = await browser_start.start()
    assert response.status_code == HTTPStatus.FORBIDDEN
    assert response.json()["code"] == "ACCESS_DENIED"
    assert_schema(response, "Error")
    await assert_no_admission(browser_start, postgres)


@pytest.mark.parametrize(
    ("case", "status", "code"),
    [
        ("missing_access", HTTPStatus.NOT_FOUND, "RESOURCE_NOT_FOUND"),
        ("missing_change", HTTPStatus.NOT_FOUND, "RESOURCE_NOT_FOUND"),
        ("replay", HTTPStatus.OK, None),
        ("conflict", HTTPStatus.CONFLICT, "IDEMPOTENCY_CONFLICT"),
    ],
)
async def test_early_checks_precede_credential_lookup_and_provider_revalidation(
    browser_start: BrowserStart,
    postgres: PostgresHarness,
    case: str,
    status: HTTPStatus,
    code: str | None,
) -> None:
    first = None
    if case in {"replay", "conflict"}:
        first = await browser_start.start()
        assert first.status_code == HTTPStatus.ACCEPTED
    async with postgres.engine.begin() as connection:
        await connection.execute(
            update(models.GithubIdentity)
            .where(models.GithubIdentity.user_id == browser_start.scope.user)
            .values(encrypted_token="broken-credential")
        )
        if case == "missing_access":
            await connection.execute(
                delete(models.RepositoryAccess).where(
                    models.RepositoryAccess.user_id == browser_start.scope.user
                )
            )
    browser_start.requests.clear()

    def break_credential_lookup(
        connection: Any, cursor: Any, statement: str, parameters: Any, *args: Any
    ) -> tuple[str, Any]:
        return statement.replace(
            "github_identity.encrypted_token", "github_identity.unavailable_column"
        ), parameters

    engine = postgres.engine.sync_engine
    event.listen(engine, "before_cursor_execute", break_credential_lookup, retval=True)
    try:
        response = await browser_start.client.post(
            browser_start.scope.url.replace("/13/", "/99/")
            if case == "missing_change"
            else browser_start.scope.url,
            json={"requested_head_sha": "c" * 40 if case == "conflict" else HEAD},
            headers=browser_start.headers,
        )
    finally:
        event.remove(engine, "before_cursor_execute", break_credential_lookup)
    assert response.status_code == status
    if code is not None:
        assert response.json()["code"] == code
    else:
        assert first is not None
        assert response.json() == first.json()
    assert not browser_start.requests


@pytest.mark.parametrize("failure", ["origin", "csrf", "expired", "revoked", "forged"])
async def test_browser_security_checks_precede_provider_authorization(
    browser_start: BrowserStart, postgres: PostgresHarness, failure: str
) -> None:
    headers = browser_start.headers
    if failure == "origin":
        headers["Origin"] = "https://untrusted.example"
    elif failure == "csrf":
        headers["X-CSRF-Token"] = "forged"
    elif failure == "forged":
        headers["Cookie"] = "dmc_session=forged"
    else:
        async with postgres.engine.begin() as connection:
            await connection.execute(
                update(models.AuthSession)
                .where(models.AuthSession.user_id == browser_start.scope.user)
                .values(
                    **{
                        "expires_at"
                        if failure == "expired"
                        else "revoked_at": datetime.now(UTC) - timedelta(minutes=1)
                    }
                )
            )
    response = await browser_start.client.post(
        browser_start.scope.url, json={"requested_head_sha": HEAD}, headers=headers
    )
    expected = (
        HTTPStatus.FORBIDDEN
        if failure in {"origin", "csrf"}
        else HTTPStatus.UNAUTHORIZED
    )
    assert response.status_code == expected
    assert not browser_start.requests
    await assert_no_admission(browser_start, postgres)


@pytest.mark.parametrize(
    ("failure", "retry_after"),
    [
        ("connect", None),
        ("timeout", None),
        ("server", None),
        ("rate_limit", 17),
        ("forbidden_rate_limit", 17),
        ("unsafe_retry", None),
    ],
)
async def test_provider_unavailable_preserves_safe_retry_semantics(
    browser_start: BrowserStart,
    postgres: PostgresHarness,
    caplog: pytest.LogCaptureFixture,
    failure: str,
    retry_after: int | None,
) -> None:
    def provider(request: httpx.Request) -> httpx.Response:
        if failure == "connect":
            raise httpx.ConnectError("private-provider-body", request=request)
        if failure == "timeout":
            raise httpx.ReadTimeout("private-provider-body", request=request)
        status = {
            "server": 503,
            "rate_limit": 429,
            "forbidden_rate_limit": 403,
            "unsafe_retry": 429,
        }[failure]
        return httpx.Response(
            status,
            headers={"Retry-After": "-17" if failure == "unsafe_retry" else "17"},
            json={"message": f"private-provider-body {_PERSONAL_TOKEN}"},
        )

    browser_start.provider = provider
    response = await browser_start.start()
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json()["code"] == "SERVICE_UNAVAILABLE"
    assert response.json()["retryable"] is True
    assert response.json()["retry_after"] == retry_after
    assert_schema(response, "Error")
    for secret in (_PERSONAL_TOKEN, _WORKER_TOKEN, "private-provider-body"):
        assert secret not in response.text + caplog.text
    await assert_no_admission(browser_start, postgres)


async def test_streaming_permission_deadline_is_retryable_without_admission(
    browser_start: BrowserStart,
    postgres: PostgresHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.api.review_start_dependencies._READ_TIMEOUT_SECONDS", 0.05, raising=False
    )
    closed = False
    cancelled = False

    class SlowBody(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b'{"id":123456,"permissions":{"pull":true}'
            try:
                # Each chunk arrives well within HTTPX's read timeout, but the
                # complete valid permission proof exceeds the overall deadline.
                for _ in range(100):
                    await asyncio.sleep(0.01)
                    yield b" "
            except asyncio.CancelledError:
                nonlocal cancelled
                cancelled = True
                raise
            yield b"}"

        async def aclose(self) -> None:
            nonlocal closed
            closed = True

    readable = browser_start.provider
    browser_start.provider = lambda request: (
        httpx.Response(200, stream=SlowBody())
        if request.url.path == "/repositories/123456"
        else readable(request)
    )
    response = await asyncio.wait_for(browser_start.start(), timeout=3)
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json() == {
        "code": "SERVICE_UNAVAILABLE",
        "message": "Service is unavailable.",
        "request_id": response.headers["X-Request-Id"],
        "retryable": True,
        "retry_after": None,
    }
    assert_schema(response, "Error")
    assert cancelled
    assert closed
    assert [request.url.path for request in browser_start.requests] == [
        "/repositories/123456"
    ]
    # browser_start also asserts that all production HTTP transports are closed.
    await assert_no_admission(browser_start, postgres)


async def test_repository_id_mismatch_denies_start(
    browser_start: BrowserStart, postgres: PostgresHarness
) -> None:
    browser_start.provider = lambda request: httpx.Response(
        200, json={"id": 654321, "permissions": {"pull": True}}
    )
    response = await browser_start.start()
    assert response.status_code == HTTPStatus.FORBIDDEN
    assert response.json()["code"] == "ACCESS_DENIED"
    await assert_no_admission(browser_start, postgres)


@pytest.mark.parametrize("body", ["[]", "not-json"])
async def test_malformed_permission_response_denies_start(
    browser_start: BrowserStart, postgres: PostgresHarness, body: str
) -> None:
    browser_start.provider = lambda request: httpx.Response(200, text=body)
    response = await browser_start.start()
    assert response.status_code == HTTPStatus.FORBIDDEN
    assert response.json()["code"] == "ACCESS_DENIED"
    await assert_no_admission(browser_start, postgres)


@pytest.mark.parametrize(
    "credential",
    [
        "missing_ciphertext",
        "corrupt",
        "empty",
        "invalid_header",
        "invalid_utf8",
        "wrong_key",
    ],
)
async def test_unusable_personal_credential_fails_safely(
    browser_start: BrowserStart,
    postgres: PostgresHarness,
    caplog: pytest.LogCaptureFixture,
    credential: str,
) -> None:
    cipher = Fernet(_CREDENTIAL_KEY)
    encrypted = {
        "missing_ciphertext": "",
        "corrupt": "damaged-private-ciphertext",
        "empty": cipher.encrypt(b"").decode(),
        "invalid_header": cipher.encrypt(b"secret\r\nInjected: yes").decode(),
        "invalid_utf8": cipher.encrypt(b"\xff").decode(),
        "wrong_key": Fernet(Fernet.generate_key()).encrypt(b"secret").decode(),
    }[credential]
    async with postgres.engine.begin() as connection:
        await connection.execute(
            update(models.GithubIdentity)
            .where(models.GithubIdentity.user_id == browser_start.scope.user)
            .values(encrypted_token=encrypted)
        )
    response = await browser_start.start()
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json() == {
        "code": "SERVICE_UNAVAILABLE",
        "message": "Service is unavailable.",
        "request_id": response.headers["X-Request-Id"],
        "retryable": True,
        "retry_after": None,
    }
    assert not browser_start.requests
    for secret in (_PERSONAL_TOKEN, _WORKER_TOKEN, encrypted):
        if secret:
            assert secret not in response.text + caplog.text
    await assert_no_admission(browser_start, postgres)


@pytest.mark.parametrize("status", [401, 403, 404])
async def test_provider_credential_revocation_denies_start(
    browser_start: BrowserStart, postgres: PostgresHarness, status: int
) -> None:
    browser_start.provider = lambda request: httpx.Response(status)
    response = await browser_start.start()
    assert response.status_code == HTTPStatus.FORBIDDEN
    assert response.json()["code"] == "ACCESS_DENIED"
    await assert_no_admission(browser_start, postgres)


@pytest.mark.parametrize(
    "proof",
    [
        {},
        {"id": "123456", "permissions": {"pull": True}},
        {"id": 123456},
        {"id": 123456, "permissions": None},
        {"id": 123456, "permissions": {}},
        {"id": 123456, "permissions": {"pull": 1}},
        {"id": 123456, "permissions": {"pull": "true"}},
    ],
)
async def test_missing_or_non_boolean_permission_proof_denies_start(
    browser_start: BrowserStart, postgres: PostgresHarness, proof: dict[str, Any]
) -> None:
    browser_start.provider = lambda request: httpx.Response(200, json=proof)
    response = await browser_start.start()
    assert response.status_code == HTTPStatus.FORBIDDEN
    assert response.json()["code"] == "ACCESS_DENIED"
    await assert_no_admission(browser_start, postgres)


@pytest.mark.parametrize("key", ["", "malformed"])
async def test_unavailable_fernet_configuration_is_safe(
    browser_start: BrowserStart,
    postgres: PostgresHarness,
    monkeypatch: pytest.MonkeyPatch,
    key: str,
) -> None:
    monkeypatch.setenv("AUTH__CREDENTIAL_KEY", key)
    get_settings.cache_clear()
    response = await browser_start.start()
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json()["code"] == "SERVICE_UNAVAILABLE"
    assert response.json()["retryable"] is True
    assert not browser_start.requests
    await assert_no_admission(browser_start, postgres)


async def test_credential_lookup_database_failure_is_safe(
    browser_start: BrowserStart,
    postgres: PostgresHarness,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def break_credential_lookup(
        connection: Any, cursor: Any, statement: str, parameters: Any, *args: Any
    ) -> tuple[str, Any]:
        return statement.replace(
            "github_identity.encrypted_token", "github_identity.unavailable_column"
        ), parameters

    engine = postgres.engine.sync_engine
    event.listen(engine, "before_cursor_execute", break_credential_lookup, retval=True)
    try:
        response = await browser_start.start()
    finally:
        event.remove(engine, "before_cursor_execute", break_credential_lookup)
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json()["code"] == "SERVICE_UNAVAILABLE"
    assert response.json()["retryable"] is True
    assert "unavailable_column" not in response.text + caplog.text
    assert not browser_start.requests
    await assert_no_admission(browser_start, postgres)


async def test_disconnect_during_permission_check_prevents_durable_admission(
    browser_start: BrowserStart, postgres: PostgresHarness
) -> None:
    readable = browser_start.provider

    async def provider(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/repositories/"):
            # Separate HTTP requests run in separate tasks; preserve each
            # request's contextvars just as the production ASGI server does.
            disconnected = await asyncio.create_task(
                browser_start.client.delete(
                    f"/api/v1/repositories/{browser_start.scope.repository}/connection",
                    headers=browser_start.headers,
                )
            )
            assert disconnected.status_code == HTTPStatus.NO_CONTENT
        response = readable(request)
        return response if isinstance(response, httpx.Response) else await response

    browser_start.provider = provider
    response = await browser_start.start()
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json()["code"] == "RESOURCE_NOT_FOUND"
    await assert_no_admission(browser_start, postgres)


async def test_excessively_nested_permission_proof_denies_start_safely(
    browser_start: BrowserStart, postgres: PostgresHarness
) -> None:
    body = '{"nested":' + "[" * 100_000 + "0" + "]" * 100_000 + "}"
    browser_start.provider = lambda request: httpx.Response(200, text=body)
    response = await browser_start.start()
    assert response.status_code == HTTPStatus.FORBIDDEN
    assert response.json()["code"] == "ACCESS_DENIED"
    await assert_no_admission(browser_start, postgres)
