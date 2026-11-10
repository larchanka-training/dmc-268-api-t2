"""Current personal GitHub permissions at the authoritative session HTTP seam."""

from collections.abc import AsyncIterator
from http import HTTPStatus

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import update
from test_postgres_integration import PostgresHarness
from test_rule_simulations_access import SimulationScope, assert_simulation_response

from app.core.config import Settings
from app.db import models

pytest_plugins = ("test_rule_simulations_access",)


@pytest.mark.parametrize("role", ["read", "write", "maintain"])
async def test_non_admin_github_permission_overrides_local_admin_access(  # noqa: PLR0913, PLR0917 -- explicit HTTP/session and external provider fixtures
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    postgres: PostgresHarness,
    github_requests: list[httpx.Request],
    provider: dict[str, httpx.Response],
    role: str,
) -> None:
    async with postgres.engine.begin() as connection:
        await connection.execute(
            update(models.RepositoryAccess)
            .where(models.RepositoryAccess.repository_id == simulation_scope.repository)
            .values(role="admin")
        )
    path = f"/repositories/{simulation_scope.external_id}"
    payload = provider[path].json()
    payload["permissions"] = {
        "pull": True,
        "push": role in {"write", "maintain"},
        "maintain": role == "maintain",
        "admin": False,
    }
    provider[path] = httpx.Response(200, json=payload)
    await assert_simulation_response(
        browser,
        simulation_scope,
        postgres,
        github_requests,
        simulation_scope.repository,
        HTTPStatus.FORBIDDEN,
        "ACCESS_DENIED",
        "Access is denied.",
    )


@pytest.mark.parametrize("status", [401, 403, 404])
async def test_revoked_github_access_is_denied_despite_local_access(  # noqa: PLR0913, PLR0917 -- explicit HTTP/session and external provider fixtures
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    postgres: PostgresHarness,
    github_requests: list[httpx.Request],
    provider: dict[str, httpx.Response],
    status: int,
) -> None:
    provider[f"/repositories/{simulation_scope.external_id}"] = httpx.Response(status)
    await assert_simulation_response(
        browser,
        simulation_scope,
        postgres,
        github_requests,
        simulation_scope.repository,
        HTTPStatus.FORBIDDEN,
        "ACCESS_DENIED",
        "Access is denied.",
    )


class BrokenGithubStream(httpx.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        raise httpx.ReadError("private-transport-detail")
        yield b""  # pragma: no cover -- marks this as an async iterator


@pytest.mark.parametrize("failure", ["rate-limit", "server", "transport"])
async def test_github_unavailability_is_safe_and_retryable(  # noqa: PLR0913, PLR0917 -- explicit HTTP/session and external provider fixtures
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    postgres: PostgresHarness,
    github_requests: list[httpx.Request],
    provider: dict[str, httpx.Response],
    failure: str,
) -> None:
    if failure == "rate-limit":
        response = httpx.Response(429, headers={"Retry-After": "37"})
    elif failure == "server":
        response = httpx.Response(503, text="private-provider-detail")
    else:
        response = httpx.Response(200, stream=BrokenGithubStream())
    provider[f"/repositories/{simulation_scope.external_id}"] = response
    await assert_simulation_response(
        browser,
        simulation_scope,
        postgres,
        github_requests,
        simulation_scope.repository,
        HTTPStatus.SERVICE_UNAVAILABLE,
        "SERVICE_UNAVAILABLE",
        "GitHub is temporarily unavailable.",
        retryable=True,
        retry_after=37 if failure == "rate-limit" else None,
    )


@pytest.mark.parametrize("credential", ["", "invalid-encrypted-credential"])
async def test_unavailable_personal_credential_never_falls_back_to_bot(  # noqa: PLR0913, PLR0917 -- explicit HTTP/session and external provider fixtures
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    postgres: PostgresHarness,
    github_requests: list[httpx.Request],
    auth_settings: Settings,
    credential: str,
) -> None:
    actor = (await browser.get("/api/v1/me")).json()["user"]["id"]
    auth_settings.github.token = SecretStr("synthetic-bot-must-not-be-used")
    async with postgres.engine.begin() as connection:
        await connection.execute(
            update(models.GithubIdentity)
            .where(models.GithubIdentity.user_id == actor)
            .values(encrypted_token=credential)
        )
    await assert_simulation_response(
        browser,
        simulation_scope,
        postgres,
        github_requests,
        simulation_scope.repository,
        HTTPStatus.SERVICE_UNAVAILABLE,
        "SERVICE_UNAVAILABLE",
        "Service is unavailable.",
        retryable=True,
        github_read=False,
    )


async def test_permissions_are_rechecked_for_each_request_in_the_same_session(
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    postgres: PostgresHarness,
    github_requests: list[httpx.Request],
    provider: dict[str, httpx.Response],
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
    provider[f"/repositories/{simulation_scope.external_id}"] = httpx.Response(404)
    await assert_simulation_response(
        browser,
        simulation_scope,
        postgres,
        github_requests,
        simulation_scope.repository,
        HTTPStatus.FORBIDDEN,
        "ACCESS_DENIED",
        "Access is denied.",
    )


@pytest.mark.parametrize("resource", ["missing", "foreign"])
async def test_local_not_found_precedes_unavailable_github_credential(
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    postgres: PostgresHarness,
    github_requests: list[httpx.Request],
    resource: str,
) -> None:
    actor = (await browser.get("/api/v1/me")).json()["user"]["id"]
    async with postgres.engine.begin() as connection:
        await connection.execute(
            update(models.GithubIdentity)
            .where(models.GithubIdentity.user_id == actor)
            .values(encrypted_token="")
        )
    await assert_simulation_response(
        browser,
        simulation_scope,
        postgres,
        github_requests,
        simulation_scope.foreign_repository if resource == "foreign" else "0" * 32,
        HTTPStatus.NOT_FOUND,
        "RESOURCE_NOT_FOUND",
        "Resource was not found.",
    )
