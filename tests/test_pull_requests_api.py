from __future__ import annotations

import asyncio
import base64
import json
import uuid
from collections.abc import AsyncIterator
from http import HTTPStatus
from typing import Any

import httpx
import pytest
from sqlalchemy import delete, event, update
from test_repositories_api import Catalog
from test_review_results_api import assert_schema

from app.api.auth import AuthenticatedPrincipal, get_authenticated_principal
from app.api.repositories import get_repository_reader
from app.db import models
from app.main import create_app
from app.vcs.github import GithubClient

pytest_plugins = ("test_postgres_integration", "test_repositories_api")


def url(repository_id: str | None = None) -> str:
    return f"/api/v1/repositories/{repository_id or uuid.uuid4()}/pull-requests"


async def test_pull_requests_fail_closed_until_trusted_auth_is_connected() -> None:
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            url(),
            headers={
                "X-User-Id": str(uuid.uuid4()),
                "Authorization": "Bearer forged",
                "Cookie": "session=forged",
            },
        )
        assert response.status_code == HTTPStatus.UNAUTHORIZED
        assert response.json()["code"] == "AUTHENTICATION_REQUIRED"
        app.dependency_overrides[get_authenticated_principal] = lambda: (
            AuthenticatedPrincipal(uuid.uuid4())
        )
        response = await client.get(url())
        assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
        assert response.json()["code"] == "SERVICE_UNAVAILABLE"


async def test_only_local_user_connections_can_list_pull_requests(
    catalog: Catalog,
) -> None:
    foreign = await catalog.connect("private", "foreign", user=catalog.other)
    disconnected = await catalog.connect("private", "disconnected")
    unsupported = await catalog.connect("private", "unsupported")
    async with catalog.postgres.engine.begin() as connection:
        await connection.execute(
            delete(models.RepositoryAccess).where(
                models.RepositoryAccess.repository_id == uuid.UUID(disconnected["id"])
            )
        )
        await connection.execute(
            update(models.Repository)
            .where(models.Repository.id == uuid.UUID(unsupported["id"]))
            .values(provider_name="other")
        )

    def no_provider(request: httpx.Request) -> httpx.Response:
        raise AssertionError("No provider access without a local user connection")

    catalog.provider = no_provider
    for repository_id in (
        foreign["id"],
        disconnected["id"],
        unsupported["id"],
        str(uuid.uuid4()),
        "not-a-uuid",
    ):
        response = await catalog.client.get(
            url(repository_id), headers={"X-User-Id": str(catalog.other)}
        )
        assert response.status_code == HTTPStatus.NOT_FOUND
        assert response.json()["code"] == "RESOURCE_NOT_FOUND"


SECRET = "private-provider-secret"


def pull(external_id: str, number: int = 7) -> dict[str, Any]:
    return {
        "number": number,
        "title": "Fix authentication",
        "user": {"login": "alice", "email": SECRET},
        "state": "open",
        "head": {"repo": {"id": 111}, "ref": "feature/auth", "sha": "a" * 40},
        "base": {"repo": {"id": int(external_id)}, "ref": "main", "sha": "b" * 40},
        "body": SECRET,
    }


async def test_open_provider_pulls_are_safe_dtos_without_holding_db_connection(
    catalog: Catalog,
) -> None:
    repository = await catalog.connect("alpha", "app")
    active: set[int] = set()

    def checkout(dbapi: Any, record: Any, proxy: Any) -> None:
        active.add(id(record))

    def checkin(dbapi: Any, record: Any) -> None:
        active.discard(id(record))

    def provider(request: httpx.Request) -> httpx.Response:
        assert not active, "PostgreSQL connection held during provider HTTP"
        assert request.headers["Authorization"] == "Bearer test-user-secret"
        assert request.url.path == "/repos/alpha/app/pulls"
        assert request.url.params["state"] == "open"
        assert request.url.params["per_page"] == "20"
        closed = pull(repository["external_id"], 3)
        closed["state"] = "closed"
        return httpx.Response(200, json=[closed, pull(repository["external_id"])])

    catalog.provider = provider
    engine = catalog.postgres.engine.sync_engine
    event.listen(engine, "checkout", checkout)
    event.listen(engine, "checkin", checkin)
    try:
        response = await catalog.client.get(url(repository["id"]))
    finally:
        event.remove(engine, "checkout", checkout)
        event.remove(engine, "checkin", checkin)
    assert response.status_code == HTTPStatus.OK
    assert response.json() == {
        "items": [
            {
                "number": 7,
                "title": "Fix authentication",
                "author": {"login": "alice"},
                "state": "OPEN",
                "source": {
                    "repository_external_id": "111",
                    "ref": "feature/auth",
                    "sha": "a" * 40,
                },
                "base": {
                    "repository_external_id": repository["external_id"],
                    "ref": "main",
                    "sha": "b" * 40,
                },
            }
        ],
        "next_cursor": None,
    }
    assert SECRET not in response.text
    assert_schema(response, "PullRequestPage")


async def test_cursor_preserves_one_native_github_page_and_order(
    catalog: Catalog,
) -> None:
    repository = await catalog.connect("alpha", "app")
    seen: list[tuple[int, int]] = []
    maximum_limit, continuation_page = 100, 4

    def provider(request: httpx.Request) -> httpx.Response:
        size = int(request.url.params["per_page"])
        page = int(request.url.params.get("page", "1"))
        seen.append((size, page))
        assert request.url.params["state"] == "open"
        if page == 1:
            # No local re-sort, lookahead, or automatic filling of filtered pages.
            numbers = [9, 2] if size == maximum_limit else [9]
            return httpx.Response(
                200,
                json=[pull(repository["external_id"], n) for n in numbers],
                headers={
                    "Link": (
                        f"<https://api.github.com/repositories/{repository['external_id']}"
                        f'/pulls?state=open&per_page={size}&page=4>; rel="next", '
                        "<https://api.github.com/repos/alpha/app/pulls?page=8>; "
                        'rel="last"'
                    )
                },
            )
        assert page == continuation_page
        closed = pull(repository["external_id"], 4)
        closed["state"] = "closed"
        return httpx.Response(200, json=[closed])

    catalog.provider = provider
    first = await catalog.client.get(url(repository["id"]), params={"limit": 100})
    assert first.status_code == HTTPStatus.OK
    assert [item["number"] for item in first.json()["items"]] == [9, 2]
    cursor = first.json()["next_cursor"]
    assert isinstance(cursor, str) and cursor
    assert "https://" not in cursor
    assert seen == [(100, 1)]
    second = await catalog.client.get(
        url(repository["id"]), params={"limit": 100, "cursor": cursor}
    )
    assert second.json() == {"items": [], "next_cursor": None}
    assert seen == [(100, 1), (100, 4)]
    smallest = await catalog.client.get(url(repository["id"]), params={"limit": 1})
    assert len(smallest.json()["items"]) == 1
    assert seen[-1] == (1, 1)


async def test_invalid_pagination_is_canonical_and_never_reaches_github(
    catalog: Catalog,
) -> None:
    repository = await catalog.connect("alpha", "app")
    provider_requests: list[httpx.Request] = []

    def provider(request: httpx.Request) -> httpx.Response:
        provider_requests.append(request)
        return httpx.Response(200, json=[])

    def encoded(value: Any) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode()

    catalog.provider = provider
    invalid = [
        {"limit": value} for value in ("0", "101", "-1", "1.5", "nope", " 2", "٢")
    ] + [
        {"cursor": value}
        for value in (
            "",
            "not-base64!",
            "x" * 1025,
            encoded({"page": 2}),
            encoded([2, repository["id"], 20, 2]),
            encoded([1, str(uuid.uuid4()), 20, 2]),
            encoded([1, repository["id"], 100, 2]),
            encoded([1, repository["id"], 20, True]),
            encoded([1, repository["id"], 20, 0]),
            encoded([1, repository["id"], 20, 2147483648]),
            encoded([1, repository["id"], 20, "https://evil.test"]),
        )
    ]
    for params in invalid:
        response = await catalog.client.get(url(repository["id"]), params=params)
        assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
        assert response.json() == {
            "code": "VALIDATION_FAILED",
            "message": "Request is invalid.",
            "request_id": response.headers["X-Request-Id"],
            "retryable": False,
            "retry_after": None,
        }
    assert provider_requests == []


@pytest.mark.parametrize(
    ("failure", "status", "code", "retry_after"),
    [
        ("401", 403, "ACCESS_DENIED", None),
        ("403", 403, "ACCESS_DENIED", None),
        ("404", 403, "ACCESS_DENIED", None),
        ("429", 503, "UPSTREAM_RATE_LIMITED", 17),
        ("primary", 503, "UPSTREAM_RATE_LIMITED", 17),
        ("secondary", 503, "UPSTREAM_RATE_LIMITED", 60),
        ("503", 503, "UPSTREAM_UNAVAILABLE", None),
        ("302", 503, "UPSTREAM_UNAVAILABLE", None),
        ("timeout", 503, "UPSTREAM_UNAVAILABLE", None),
    ],
)
async def test_provider_failures_return_only_safe_canonical_errors(
    catalog: Catalog,
    failure: str,
    status: int,
    code: str,
    retry_after: int | None,
) -> None:
    repository = await catalog.connect("alpha", "app")

    def provider(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.github.com"
        if failure == "timeout":
            raise httpx.ReadTimeout(SECRET, request=request)
        if failure == "primary":
            return httpx.Response(
                403, headers={"x-ratelimit-remaining": "0", "Retry-After": "17"}
            )
        if failure == "secondary":
            return httpx.Response(
                403, json={"message": "secondary rate limit " + SECRET}
            )
        return httpx.Response(
            int(failure),
            headers={"Retry-After": "17"}
            if failure == "429"
            else {"Location": "https://evil.test/" + SECRET},
            json={"message": SECRET},
        )

    catalog.provider = provider
    response = await catalog.client.get(url(repository["id"]))
    assert response.status_code == status
    assert response.json() == {
        "code": code,
        "message": "Access is denied."
        if status == HTTPStatus.FORBIDDEN
        else "GitHub is temporarily unavailable.",
        "request_id": response.headers["X-Request-Id"],
        "retryable": status == HTTPStatus.SERVICE_UNAVAILABLE,
        "retry_after": retry_after,
    }
    assert SECRET not in response.text
    assert "test-user-secret" not in response.text
    assert_schema(response, "Error")


async def test_malformed_or_unsupported_provider_page_fails_safely(
    catalog: Catalog,
) -> None:
    repository = await catalog.connect("alpha", "app")
    valid = pull(repository["external_id"])
    invalid_pulls = [
        {**valid, "user": None},
        {**valid, "head": {**valid["head"], "repo": None}},
        {**valid, "number": True},
        {**valid, "title": None},
        {**valid, "head": {**valid["head"], "sha": "missing-sha"}},
        {**valid, "base": {**valid["base"], "repo": {"id": 999}}},
        {**valid, "user": {"login": ""}},
        {**valid, "base": {**valid["base"], "ref": ""}},
        {**valid, "state": "unsupported"},
        {key: value for key, value in valid.items() if key != "base"},
        None,
    ]
    replies = [httpx.Response(200, json=[valid, bad]) for bad in invalid_pulls] + [
        httpx.Response(200, json={"private": SECRET}),
        httpx.Response(200, content=SECRET.encode()),
        httpx.Response(
            200, content=json.dumps([{**valid, "title": "\ud800"}]).encode()
        ),
        httpx.Response(200, content=b"[" * 1100 + b"]" * 1100),
        httpx.Response(200, json=[valid] * 21),
        httpx.Response(200, content=b"x" * (10 * 1024 * 1024 + 1)),
    ]
    for reply in replies:

        def provider(
            request: httpx.Request, reply: httpx.Response = reply
        ) -> httpx.Response:
            return reply

        catalog.provider = provider
        response = await catalog.client.get(url(repository["id"]))
        assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
        assert response.json()["code"] == "UPSTREAM_UNAVAILABLE"
        assert SECRET not in response.text
        assert "items" not in response.json()


async def test_untrusted_provider_continuation_is_never_exposed_or_followed(
    catalog: Catalog,
) -> None:
    repository = await catalog.connect("alpha", "app")
    links = [
        "https://evil.test/pulls?state=open&per_page=20&page=2",
        "https://api.github.com/repos/other/private/pulls?state=open&per_page=20&page=2",
        "https://api.github.com/repos/alpha/app/pulls?state=closed&per_page=20&page=2",
        "https://api.github.com/repos/alpha/app/pulls?state=open&per_page=100&page=2",
        "https://api.github.com/repos/alpha/app/pulls?state=open&per_page=20&page=1",
        "https://api.github.com/repos/alpha/app/pulls?state=open&per_page=20&page=2&page=3",
        "https://api.github.com/repos/alpha/app/pulls?state=open&per_page=20&page=2&token="
        + SECRET,
        "https://api.github.com/repos/alpha/app/pulls?state=open&per_page=20&page=nope",
        "https://api.github.com/repos/alpha/app/pulls?state=open&per_page=20&page=2147483648",
        "http://api.github.com/repos/alpha/app/pulls?state=open&per_page=20&page=2",
    ]
    for link in links:
        requests: list[httpx.Request] = []

        def provider(
            request: httpx.Request,
            link: str = link,
            requests: list[httpx.Request] = requests,
        ) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200, json=[], headers={"Link": f'<{link}>; rel="next"'}
            )

        catalog.provider = provider
        response = await catalog.client.get(url(repository["id"]))
        assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
        assert response.json()["code"] == "UPSTREAM_UNAVAILABLE"
        assert SECRET not in response.text
        assert len(requests) == 1


@pytest.mark.parametrize("phase", ["http", "body"])
async def test_total_provider_deadline_includes_http_and_body_reading(
    catalog: Catalog,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    repository = await catalog.connect("alpha", "app")
    monkeypatch.setattr("app.vcs.github._TOTAL_TIMEOUT_SECONDS", 0.03)

    class SlowBody(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self) -> AsyncIterator[bytes]:
            for chunk in (b" ", b"[]"):
                await asyncio.sleep(0.04)
                yield chunk

        async def aclose(self) -> None:
            self.closed = True

    body = SlowBody()

    async def provider(request: httpx.Request) -> httpx.Response:
        if phase == "http":
            await asyncio.sleep(0.08)
            return httpx.Response(200, json=[])
        return httpx.Response(200, stream=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        catalog.app.dependency_overrides[get_repository_reader] = lambda: GithubClient(
            client
        )
        response = await asyncio.wait_for(catalog.client.get(url(repository["id"])), 1)
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json()["code"] == "UPSTREAM_UNAVAILABLE"
    if phase == "body":
        assert body.closed


async def test_invalid_repository_provider_coordinates_fail_without_http(
    catalog: Catalog,
) -> None:
    repository = await catalog.connect("alpha", "app")
    requests: list[httpx.Request] = []

    def provider(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[])

    catalog.provider = provider
    for coordinates in (
        {"owner": "../private"},
        {"repo_name": ".."},
        {"repo_name": "app?token=" + SECRET},
        {"external_id": "not-github-identity"},
    ):
        async with catalog.postgres.engine.begin() as connection:
            await connection.execute(
                update(models.Repository)
                .where(models.Repository.id == uuid.UUID(repository["id"]))
                .values(
                    owner="alpha",
                    repo_name="app",
                    external_id=repository["external_id"],
                )
            )
            await connection.execute(
                update(models.Repository)
                .where(models.Repository.id == uuid.UUID(repository["id"]))
                .values(**coordinates)
            )
        response = await catalog.client.get(url(repository["id"]))
        assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
        assert response.json()["code"] == "UPSTREAM_UNAVAILABLE"
    assert requests == []


async def test_non_ascii_provider_link_returns_safe_upstream_error(
    catalog: Catalog,
) -> None:
    repository = await catalog.connect("alpha", "app")
    catalog.provider = lambda request: httpx.Response(
        200, json=[], headers=[(b"Link", b"\xff")]
    )
    response = await catalog.client.get(url(repository["id"]))
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json()["code"] == "UPSTREAM_UNAVAILABLE"
    assert_schema(response, "Error")
