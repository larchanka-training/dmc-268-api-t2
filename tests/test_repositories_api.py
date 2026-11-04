from __future__ import annotations

import base64
import json
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import delete, insert, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_postgres_integration import PostgresHarness

from app.api.auth import AuthenticatedPrincipal, get_authenticated_principal
from app.api.repositories import get_repository_reader
from app.db import models
from app.db.session import get_session
from app.main import create_app
from app.vcs.github import GithubClient

pytest_plugins = ("test_postgres_integration",)


@dataclass
class Catalog:
    app: FastAPI
    client: httpx.AsyncClient
    postgres: PostgresHarness
    user: uuid.UUID
    other: uuid.UUID
    provider: Callable[[httpx.Request], httpx.Response]

    async def connect(
        self,
        owner: str,
        name: str,
        *,
        user: uuid.UUID | None = None,
        role: str = "reviewer",
        repo_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        repo_id = self.postgres.track("repository", repo_id or uuid.uuid4())
        external_id = str(repo_id.int)
        async with self.postgres.engine.begin() as connection:
            await connection.execute(
                insert(models.Repository).values(
                    id=repo_id,
                    external_id=external_id,
                    provider_name="github",
                    owner=owner,
                    repo_name=name,
                )
            )
            await connection.execute(
                insert(models.RepositoryAccess).values(
                    id=uuid.uuid4(),
                    repository_id=repo_id,
                    user_id=user or self.user,
                    role=role,
                )
            )
        return {
            "id": str(repo_id),
            "provider": "github",
            "external_id": external_id,
            "owner": owner,
            "name": name,
            "role": role,
            "capabilities": {
                "can_start_review": True,
                "can_manage_settings": role == "admin",
            },
        }


@pytest_asyncio.fixture
async def catalog(postgres: PostgresHarness) -> AsyncIterator[Catalog]:
    app = create_app()
    user, other = uuid.uuid4(), uuid.uuid4()
    async with postgres.engine.begin() as connection:
        await connection.execute(insert(models.User), [{"id": user}, {"id": other}])
    factory = async_sessionmaker(postgres.engine)

    async def session() -> AsyncIterator[AsyncSession]:
        async with factory() as value:
            yield value

    async def principal() -> AuthenticatedPrincipal:
        return AuthenticatedPrincipal(user)

    def readable(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.github.com"
        assert request.headers["Authorization"] == "Bearer test-user-secret"
        return httpx.Response(
            200,
            json={
                "id": int(request.url.path.rsplit("/", 1)[1]),
                "permissions": {"pull": True},
                "credential": "private-provider-metadata",
            },
        )

    value: Catalog

    def dispatch(request: httpx.Request) -> httpx.Response:
        return value.provider(request)

    async with (
        httpx.AsyncClient(
            transport=httpx.MockTransport(dispatch),
            headers={"Authorization": "Bearer test-user-secret"},
        ) as github,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client,
    ):

        async def reader() -> GithubClient:
            return GithubClient(github)

        app.dependency_overrides[get_session] = session
        app.dependency_overrides[get_authenticated_principal] = principal
        app.dependency_overrides[get_repository_reader] = reader
        value = Catalog(app, client, postgres, user, other, readable)
        try:
            yield value
        finally:
            app.dependency_overrides.clear()
            async with postgres.engine.begin() as connection:
                await connection.execute(
                    delete(models.RepositoryAccess).where(
                        models.RepositoryAccess.user_id.in_([user, other])
                    )
                )
                await connection.execute(
                    delete(models.User).where(models.User.id.in_([user, other]))
                )


async def test_repositories_authentication_is_fail_closed() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        response = await client.get(
            "/api/v1/repositories",
            params={"user_id": str(uuid.uuid4())},
            headers={
                "X-User-Id": str(uuid.uuid4()),
                "Cookie": "session=forged",
                "Authorization": "Bearer forged",
            },
        )
    assert response.status_code == HTTPStatus.UNAUTHORIZED
    assert response.json()["code"] == "AUTHENTICATION_REQUIRED"


async def test_user_provider_authorization_is_unavailable_by_default() -> None:
    app = create_app()
    app.dependency_overrides[get_authenticated_principal] = lambda: (
        AuthenticatedPrincipal(uuid.uuid4())
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/v1/repositories")
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json()["code"] == "SERVICE_UNAVAILABLE"
    assert response.json()["retryable"] is True


async def test_only_user_connections_are_returned_as_repository_dtos(
    catalog: Catalog,
) -> None:
    reviewer = await catalog.connect("alpha", "app")
    admin = await catalog.connect("beta", "app", role="admin")
    await catalog.connect("private", "foreign", user=catalog.other)
    unconnected = await catalog.connect("public", "unconnected")
    async with catalog.postgres.engine.begin() as connection:
        await connection.execute(
            delete(models.RepositoryAccess).where(
                models.RepositoryAccess.repository_id == uuid.UUID(unconnected["id"])
            )
        )

    response = await catalog.client.get("/api/v1/repositories")

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"items": [reviewer, admin], "next_cursor": None}


async def test_access_revocation_is_rechecked_on_every_request(
    catalog: Catalog,
) -> None:
    repository = await catalog.connect("alpha", "app")
    response = await catalog.client.get("/api/v1/repositories")
    assert response.json()["items"] == [repository]

    catalog.provider = lambda request: httpx.Response(
        404, json={"message": "private-provider-metadata"}
    )
    revoked = await catalog.client.get("/api/v1/repositories")
    assert revoked.status_code == HTTPStatus.OK
    assert revoked.json() == {"items": [], "next_cursor": None}

    async with catalog.postgres.engine.begin() as connection:
        await connection.execute(
            delete(models.RepositoryAccess).where(
                models.RepositoryAccess.user_id == catalog.user
            )
        )

    def unexpected_provider_call(request: httpx.Request) -> httpx.Response:
        raise AssertionError("No provider read without a local connection")

    catalog.provider = unexpected_provider_call
    disconnected = await catalog.client.get("/api/v1/repositories")
    assert disconnected.json() == {"items": [], "next_cursor": None}


async def test_repository_pages_use_stable_keys_and_accessible_lookahead(
    catalog: Catalog,
) -> None:
    high = await catalog.connect("alpha", "app", repo_id=uuid.UUID(int=14))
    low = await catalog.connect("alpha", "app", repo_id=uuid.UUID(int=13))
    zeta = await catalog.connect("alpha", "zeta")
    beta = await catalog.connect("beta", "app")
    tail = [await catalog.connect("zulu", f"app{i:02}") for i in range(21)]
    denied = [
        await catalog.connect("aaa", "revoked"),
        await catalog.connect("alpha", "middle"),
        await catalog.connect("zzz", "revoked"),
    ]
    readable = catalog.provider
    denied_ids = {item["external_id"] for item in denied}
    catalog.provider = lambda request: (
        httpx.Response(404)
        if request.url.path.rsplit("/", 1)[1] in denied_ids
        else readable(request)
    )
    expected = [low, high, zeta, beta, *tail]

    default = await catalog.client.get("/api/v1/repositories")
    assert default.status_code == HTTPStatus.OK
    assert default.json()["items"] == expected[:20]
    assert default.json()["next_cursor"]

    first = await catalog.client.get("/api/v1/repositories", params={"limit": 1})
    assert first.json()["items"] == [low]
    cursor = first.json()["next_cursor"]
    assert cursor
    # Removing the anchor connection must not break continuation.
    async with catalog.postgres.engine.begin() as connection:
        await connection.execute(
            delete(models.RepositoryAccess).where(
                models.RepositoryAccess.repository_id == uuid.UUID(low["id"])
            )
        )
    second = await catalog.client.get(
        "/api/v1/repositories", params={"limit": 2, "cursor": cursor}
    )
    assert second.json()["items"] == [high, zeta]
    last = await catalog.client.get(
        "/api/v1/repositories",
        params={"limit": 100, "cursor": second.json()["next_cursor"]},
    )
    assert last.json() == {"items": [beta, *tail], "next_cursor": None}


@pytest.mark.parametrize(
    "params",
    [
        {"limit": "0"},
        {"limit": "101"},
        {"limit": "nope"},
        {"cursor": ""},
        {"cursor": "not-base64!"},
        {
            "cursor": base64.urlsafe_b64encode(
                json.dumps([2, "a", "b", "bad"]).encode()
            ).decode()
        },
        {
            "cursor": base64.urlsafe_b64encode(
                json.dumps([1, "\u0000", "b", str(uuid.UUID(int=13))]).encode()
            ).decode()
        },
        {
            "cursor": base64.urlsafe_b64encode(
                json.dumps([1, "a", "\ud800", str(uuid.UUID(int=13))]).encode()
            ).decode()
        },
    ],
)
async def test_invalid_page_parameters_return_canonical_validation_error(
    catalog: Catalog, params: dict[str, str]
) -> None:
    response = await catalog.client.get("/api/v1/repositories", params=params)
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json() == {
        "code": "VALIDATION_FAILED",
        "message": "Request is invalid.",
        "request_id": response.headers["X-Request-Id"],
        "retryable": False,
        "retry_after": None,
    }


@pytest.mark.parametrize("failure", ["server", "timeout", "rate_limit", "malformed"])
async def test_provider_errors_fail_closed_without_credentials_or_partial_page(
    catalog: Catalog, failure: str
) -> None:
    await catalog.connect("alpha", "readable")
    failing = await catalog.connect("beta", "unavailable")
    readable = catalog.provider

    def provider(request: httpx.Request) -> httpx.Response:
        if not request.url.path.endswith(failing["external_id"]):
            return readable(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("test-user-secret", request=request)
        if failure == "malformed":
            return httpx.Response(200, content=b"private-provider-metadata")
        return httpx.Response(
            429 if failure == "rate_limit" else 503,
            headers={"Retry-After": "17"},
            json={"message": "test-user-secret private-provider-metadata"},
        )

    catalog.provider = provider
    response = await catalog.client.get("/api/v1/repositories")
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json() == {
        "code": "UPSTREAM_RATE_LIMITED"
        if failure == "rate_limit"
        else "UPSTREAM_UNAVAILABLE",
        "message": "GitHub is temporarily unavailable.",
        "request_id": response.headers["X-Request-Id"],
        "retryable": True,
        "retry_after": 17 if failure == "rate_limit" else None,
    }


async def test_repository_catalog_only_contains_github_connections(
    catalog: Catalog,
) -> None:
    unsupported = await catalog.connect("alpha", "unsupported")
    async with catalog.postgres.engine.begin() as connection:
        await connection.execute(
            update(models.Repository)
            .where(models.Repository.id == uuid.UUID(unsupported["id"]))
            .values(provider_name="other")
        )
    github = await catalog.connect("beta", "app")
    response = await catalog.client.get("/api/v1/repositories")
    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"items": [github], "next_cursor": None}
