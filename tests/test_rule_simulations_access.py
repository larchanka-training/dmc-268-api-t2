"""Local repository access at the real browser-session HTTP boundary."""

import json
from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
import yaml
from fastapi import FastAPI
from jsonschema import Draft202012Validator
from sqlalchemy import Row, delete, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_auth_api import login
from test_postgres_integration import (
    PostgresHarness,
    _create_repository,
    _create_settings,
)

from app.db import models
from app.db.session import get_session

pytest_plugins = ("test_auth_api",)

VALID_GIT_DIFF = (
    "diff --git a/private.py b/private.py\n"
    "--- a/private.py\n+++ b/private.py\n@@ -1 +1 @@\n"
    "-stage2d-private-old\n+stage2d-private-new\n"
)

VALID_SIMULATION_BODY = (
    b'{"input":{"kind":"code","path":"playground/example.py",'
    b'"source":"print(\'Hello\')"},"rules":{"format":"rules-md/1",'
    b'"markdown":"# Security\\n## SEC-1: Example\\nCheck the input."}}'
)


@dataclass
class SimulationScope:
    repository: UUID
    foreign_repository: UUID
    headers: dict[str, str]
    external_id: str

    def url(self, repository: UUID | str) -> str:
        return f"/api/v1/repositories/{repository}/rule-simulations"


@pytest_asyncio.fixture
async def no_external_http(
    monkeypatch: pytest.MonkeyPatch,
    provider: dict[str, httpx.Response],
    github_requests: list[httpx.Request],
    postgres: PostgresHarness,
) -> AsyncIterator[None]:
    unexpected: list[str] = []

    async def dispatch(request: httpx.Request) -> httpx.Response:
        if (
            request.url.scheme == "https"
            and request.url.host == "api.github.com"
            and request.url.path.startswith("/repositories/")
            and request.url.path in provider
        ):
            # Observe real PostgreSQL at the network boundary: no request
            # transaction may remain open while waiting for GitHub.
            async with postgres.engine.connect() as connection:
                assert (
                    await connection.scalar(
                        text(
                            "SELECT count(*) FROM pg_stat_activity "
                            "WHERE datname=current_database() "
                            "AND pid<>pg_backend_pid() AND state='idle in transaction'"
                        )
                    )
                    == 0
                )
            github_requests.append(request)
            assert request.method == "GET"
            assert request.headers["Authorization"] == "Bearer test-user-secret"
            return provider[request.url.path]
        unexpected.append(str(request.url))
        raise AssertionError("Unexpected external HTTP request")

    transport = httpx.MockTransport(dispatch)

    async def handle(
        self: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        return await transport.handle_async_request(request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle)
    yield
    assert unexpected == []


@pytest_asyncio.fixture
async def simulation_scope(
    browser: httpx.AsyncClient,
    postgres: PostgresHarness,
    no_external_http: None,
    provider: dict[str, httpx.Response],
) -> AsyncIterator[SimulationScope]:
    await login(browser)
    me = await browser.get("/api/v1/me")
    assert me.status_code == HTTPStatus.OK
    actor = UUID(me.json()["user"]["id"])
    other_user = uuid4()
    repositories: list[UUID] = []
    try:
        async with postgres.engine.begin() as connection:
            await connection.execute(insert(models.User).values(id=other_user))
            for user in (actor, other_user):
                repository = await _create_repository(
                    postgres, connection, external_id=str(uuid4().int >> 64)
                )
                repositories.append(repository)
                settings = await _create_settings(postgres, connection, repository)
                await connection.execute(
                    update(models.Repository)
                    .where(models.Repository.id == repository)
                    .values(current_settings_id=settings)
                )
                await connection.execute(
                    insert(models.RepositoryAccess).values(
                        id=uuid4(),
                        repository_id=repository,
                        user_id=user,
                        role="reviewer",
                    )
                )
        async with postgres.engine.connect() as connection:
            external_id = str(
                await connection.scalar(
                    select(models.Repository.external_id).where(
                        models.Repository.id == repositories[0]
                    )
                )
            )
        provider[f"/repositories/{external_id}"] = httpx.Response(
            200,
            json={
                "id": int(external_id),
                "owner": {"login": "team"},
                "name": "simulation",
                "permissions": {"pull": True, "admin": True},
            },
        )
        yield SimulationScope(
            repositories[0],
            repositories[1],
            {
                "Origin": "https://frontend.test",
                "X-CSRF-Token": me.json()["csrf_token"],
                "Content-Type": "application/json",
                "Idempotency-Key": "preliminary-opaque-key",
            },
            external_id,
        )
    finally:
        # Access FKs must be gone before browser deletes its auth users, and
        # before PostgresHarness deletes the tracked repositories/settings.
        async with postgres.engine.begin() as connection:
            await connection.execute(
                delete(models.RepositoryAccess).where(
                    models.RepositoryAccess.repository_id.in_(repositories)
                )
            )
            await connection.execute(
                delete(models.User).where(models.User.id == other_user)
            )


async def simulation_state(postgres: PostgresHarness) -> list[list[Row[Any]]]:
    tables = (
        models.Repository,
        models.RepositorySettings,
        models.RepositoryAccess,
        models.ReviewJob,
        models.OutboxEvent,
        models.QuotaUsage,
    )
    async with postgres.engine.connect() as connection:
        return [
            list((await connection.execute(select(table).order_by(table.id))).all())
            for table in tables
        ]


async def assert_simulation_response(  # noqa: PLR0913, PLR0917 -- explicit HTTP expectations and real boundary fixtures
    browser: httpx.AsyncClient,
    scope: SimulationScope,
    postgres: PostgresHarness,
    github_requests: list[httpx.Request],
    repository: UUID | str,
    status: HTTPStatus,
    code: str,
    message: str,
    *,
    retryable: bool = False,
    retry_after: int | None = None,
    github_read: bool | None = None,
    expected_extra: dict[str, object] | None = None,
    content: bytes | AsyncIterable[bytes] | None = VALID_SIMULATION_BODY,
    headers: dict[str, str] | list[tuple[str, str]] | None = None,
) -> None:
    # DB snapshots are explicit side-effect checks; the result is asserted at
    # HTTP, with real authentication and PostgreSQL rather than service doubles.
    before = await simulation_state(postgres)
    github_before = list(github_requests)
    identity_before = (await browser.get("/api/v1/me")).json()
    cookies_before = dict(browser.cookies)
    request_headers = httpx.Headers(scope.headers)
    request_headers.update(headers or {})
    response = await browser.post(
        scope.url(repository),
        headers=request_headers,
        content=content,
        follow_redirects=False,
    )
    assert response.status_code == status
    assert response.headers["X-Request-Id"]
    assert response.json() == {
        "code": code,
        "message": message,
        "request_id": response.headers["X-Request-Id"],
        "retryable": retryable,
        "retry_after": retry_after,
        **(expected_extra or {}),
    }
    document = yaml.safe_load(
        (Path(__file__).parents[1] / "openapi.yaml").read_text(encoding="utf-8")
    )
    declared = document["paths"]["/api/v1/repositories/{id}/rule-simulations"]["post"][
        "responses"
    ][str(response.status_code)]
    if "$ref" in declared:
        declared = document["components"]["responses"][
            declared["$ref"].rsplit("/", 1)[-1]
        ]
    Draft202012Validator(
        {
            **declared["content"]["application/json"]["schema"],
            "components": document["components"],
        }
    ).validate(response.json())
    if expected_extra is not None:
        assert len(json.dumps(response.json()["diagnostics"]).encode("utf-8")) <= 8192  # noqa: PLR2004 -- wire diagnostics byte budget
        document = yaml.safe_load(
            (Path(__file__).parents[1] / "openapi.yaml").read_text(encoding="utf-8")
        )
        Draft202012Validator(
            {
                "$ref": "#/components/schemas/SimulationRulesValidationError",
                "components": document["components"],
            }
        ).validate(response.json())
    assert "Location" not in response.headers
    assert "Set-Cookie" not in response.headers
    assert dict(browser.cookies) == cookies_before
    assert (await browser.get("/api/v1/me")).json() == identity_before
    added = github_requests[len(github_before) :]
    if github_read is None:
        github_read = repository == scope.repository
    if github_read:
        assert [request.url.path for request in added] == [
            f"/repositories/{scope.external_id}"
        ]
    else:
        assert added == []
    assert await simulation_state(postgres) == before


async def test_missing_repository_is_canonical_not_found(
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    postgres: PostgresHarness,
    github_requests: list[httpx.Request],
) -> None:
    await assert_simulation_response(
        browser,
        simulation_scope,
        postgres,
        github_requests,
        uuid4(),
        HTTPStatus.NOT_FOUND,
        "RESOURCE_NOT_FOUND",
        "Resource was not found.",
    )


async def test_foreign_repository_is_indistinguishable_from_missing(
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    postgres: PostgresHarness,
    github_requests: list[httpx.Request],
) -> None:
    await assert_simulation_response(
        browser,
        simulation_scope,
        postgres,
        github_requests,
        simulation_scope.foreign_repository,
        HTTPStatus.NOT_FOUND,
        "RESOURCE_NOT_FOUND",
        "Resource was not found.",
    )


async def test_invalid_repository_id_is_canonical_not_found(
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    postgres: PostgresHarness,
    github_requests: list[httpx.Request],
) -> None:
    await assert_simulation_response(
        browser,
        simulation_scope,
        postgres,
        github_requests,
        "not-a-uuid",
        HTTPStatus.NOT_FOUND,
        "RESOURCE_NOT_FOUND",
        "Resource was not found.",
    )


async def test_accessible_repository_reaches_temporary_fail_closed_handler(
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    postgres: PostgresHarness,
    github_requests: list[httpx.Request],
) -> None:
    await assert_simulation_response(
        browser,
        simulation_scope,
        postgres,
        github_requests,
        simulation_scope.repository,
        HTTPStatus.SERVICE_UNAVAILABLE,
        "SERVICE_UNAVAILABLE",
        "Service is unavailable.",
    )


async def test_repository_database_failure_is_safe_and_retryable(
    browser: httpx.AsyncClient,
    auth_app: FastAPI,
    simulation_scope: SimulationScope,
    postgres: PostgresHarness,
    github_requests: list[httpx.Request],
) -> None:
    async def timed_session() -> AsyncIterator[AsyncSession]:
        async with async_sessionmaker(postgres.engine)() as session:
            await session.execute(text("SET LOCAL statement_timeout = '100ms'"))
            yield session

    before = await simulation_state(postgres)
    github_before = list(github_requests)
    identity_before = (await browser.get("/api/v1/me")).json()
    cookies_before = dict(browser.cookies)
    # Only the database session factory changes; authoritative auth remains real.
    original = auth_app.dependency_overrides[get_session]
    auth_app.dependency_overrides[get_session] = timed_session
    try:
        async with postgres.engine.connect() as blocked, blocked.begin():
            await blocked.execute(
                text("LOCK TABLE repository IN ACCESS EXCLUSIVE MODE")
            )
            response = await browser.post(
                simulation_scope.url(simulation_scope.repository),
                headers=simulation_scope.headers,
                follow_redirects=False,
            )
    finally:
        auth_app.dependency_overrides[get_session] = original

    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json() == {
        "code": "SERVICE_UNAVAILABLE",
        "message": "Service is unavailable.",
        "request_id": response.headers["X-Request-Id"],
        "retryable": True,
        "retry_after": None,
    }
    assert "Location" not in response.headers
    assert "Set-Cookie" not in response.headers
    assert dict(browser.cookies) == cookies_before
    assert (await browser.get("/api/v1/me")).json() == identity_before
    assert github_requests == github_before
    assert await simulation_state(postgres) == before
