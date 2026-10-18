"""Repository onboarding through browser HTTP, PostgreSQL and GitHub transport."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from http import HTTPStatus
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, event, select, update
from test_repositories_api import Catalog
from test_repository_reader_wiring import browser_cookie

from app.core.config import Settings
from app.db.models import Repository, RepositoryAccess, RepositorySettings

pytest_plugins = (
    "test_postgres_integration",
    "test_repositories_api",
    "test_repository_reader_wiring",
)


def github_repository(
    *, external_id: str | None = None, owner: str = "octocat", name: str = "project"
) -> dict[str, Any]:
    return {
        "id": int(external_id or str(uuid.uuid4().int >> 64)),
        "owner": {"login": owner},
        "name": name,
        "permissions": {"pull": True, "admin": False},
        "private_metadata": "must-not-leak",
    }


def available_dto(repository: dict[str, Any]) -> dict[str, str]:
    return {
        "provider": "github",
        "external_id": str(repository["id"]),
        "owner": repository["owner"]["login"],
        "name": repository["name"],
    }


@pytest_asyncio.fixture
async def onboarding(
    wired_catalog: Catalog, reader_settings: Settings
) -> AsyncIterator[Catalog]:
    reader_settings.auth.frontend_origins = ["https://frontend.test"]
    try:
        yield wired_catalog
    finally:
        # Track server-generated ids for the existing PostgreSQL harness cleanup.
        async with wired_catalog.postgres.engine.connect() as connection:
            ids = list(
                await connection.scalars(
                    select(RepositoryAccess.repository_id).where(
                        RepositoryAccess.user_id.in_(
                            [wired_catalog.user, wired_catalog.other]
                        )
                    )
                )
            )
            for repository_id in ids:
                wired_catalog.postgres.track("repository", repository_id)
            settings = await connection.scalars(
                select(RepositorySettings.id).where(
                    RepositorySettings.repository_id.in_(ids)
                )
            )
            for settings_id in settings:
                wired_catalog.postgres.track("repository_settings", settings_id)


async def test_available_uses_user_github_page_and_exact_dto(
    onboarding: Catalog,
) -> None:
    payload = [github_repository(name="zeta"), github_repository(name="alpha")]

    def provider(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-user-secret"
        assert request.url.path == "/user/repos"
        assert request.url.params["per_page"] == "20"
        assert request.url.params["page"] == "1"
        return httpx.Response(200, json=payload)

    onboarding.provider = provider
    response = await onboarding.client.get(
        "/api/v1/repositories/available", headers=browser_cookie(onboarding.user)
    )
    assert response.status_code == HTTPStatus.OK
    assert response.json() == {
        "items": [available_dto(repository) for repository in payload],
        "next_cursor": None,
    }


async def test_available_excludes_only_current_users_connections(
    onboarding: Catalog,
) -> None:
    own = await onboarding.connect("octocat", "own")
    foreign = await onboarding.connect("octocat", "foreign", user=onboarding.other)
    local = await onboarding.connect("octocat", "unconnected")
    async with onboarding.postgres.engine.begin() as connection:
        await connection.execute(
            delete(RepositoryAccess).where(
                RepositoryAccess.repository_id == uuid.UUID(local["id"])
            )
        )
    payload = [
        github_repository(external_id=item["external_id"], name=item["name"])
        for item in (foreign, own, local)
    ]
    onboarding.provider = lambda request: httpx.Response(200, json=payload)
    response = await onboarding.client.get(
        "/api/v1/repositories/available", headers=browser_cookie(onboarding.user)
    )
    assert response.status_code == HTTPStatus.OK
    assert response.json() == {
        "items": [available_dto(payload[0]), available_dto(payload[2])],
        "next_cursor": None,
    }


async def test_available_keeps_native_continuation_after_empty_filtered_page(
    onboarding: Catalog,
) -> None:
    own = await onboarding.connect("octocat", "connected")
    first = github_repository(external_id=own["external_id"], name="connected")
    last = github_repository(name="last")
    pages: list[str] = []

    def provider(request: httpx.Request) -> httpx.Response:
        assert request.url.params["per_page"] == "1"
        page = request.url.params["page"]
        pages.append(page)
        if page == "1":
            return httpx.Response(
                200,
                json=[first],
                headers={
                    "Link": (
                        "<https://api.github.com/user/repos?per_page=1&page=7>;"
                        ' rel="next"'
                    )
                },
            )
        assert page == "7"
        return httpx.Response(200, json=[last])

    onboarding.provider = provider
    headers = browser_cookie(onboarding.user)
    response = await onboarding.client.get(
        "/api/v1/repositories/available", params={"limit": 1}, headers=headers
    )
    assert response.status_code == HTTPStatus.OK
    assert response.json()["items"] == []
    assert pages == ["1"], "Do not automatically drain GitHub pages"
    cursor = response.json()["next_cursor"]
    assert isinstance(cursor, str) and cursor
    continuation = await onboarding.client.get(
        "/api/v1/repositories/available",
        params={"limit": 1, "cursor": cursor},
        headers=headers,
    )
    assert continuation.status_code == HTTPStatus.OK
    assert continuation.json() == {
        "items": [available_dto(last)],
        "next_cursor": None,
    }
    assert pages == ["1", "7"]


def mutation_headers(user: uuid.UUID) -> dict[str, str]:
    return {
        **browser_cookie(user),
        "Origin": "https://frontend.test",
        "X-CSRF-Token": "test-csrf",
    }


async def test_connect_creates_repository_access_and_initial_settings_atomically(
    onboarding: Catalog,
) -> None:
    payload = github_repository()
    external_id = str(payload["id"])

    def provider(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-user-secret"
        if request.url.path == "/user/repos":
            return httpx.Response(200, json=[payload])
        assert request.url.path == f"/repositories/{external_id}"
        return httpx.Response(200, json=payload)

    onboarding.provider = provider
    response = await onboarding.client.post(
        "/api/v1/repositories/connect",
        json={"provider": "github", "external_id": external_id},
        headers=mutation_headers(onboarding.user),
    )
    assert response.status_code == HTTPStatus.CREATED
    body = response.json()
    assert body == {
        **available_dto(payload),
        "id": body["id"],
        "role": "reviewer",
        "capabilities": {"can_start_review": True, "can_manage_settings": False},
    }
    listed = await onboarding.client.get(
        "/api/v1/repositories", headers=browser_cookie(onboarding.user)
    )
    assert listed.json() == {"items": [body], "next_cursor": None}
    available = await onboarding.client.get(
        "/api/v1/repositories/available", headers=browser_cookie(onboarding.user)
    )
    assert available.json() == {"items": [], "next_cursor": None}

    # Settings HTTP handlers are outside this slice. Check the required storage
    # invariants directly: same-repository current pointer, digest and defaults.
    async with onboarding.postgres.engine.connect() as connection:
        row = (
            await connection.execute(
                select(RepositorySettings)
                .join(
                    Repository, Repository.current_settings_id == RepositorySettings.id
                )
                .where(Repository.id == uuid.UUID(body["id"]))
            )
        ).one()
    assert row.repository_id == uuid.UUID(body["id"])
    assert row.rules == {"instructions": []}
    assert row.ignores == {"globs": []}
    assert (row.max_starts_per_hour, row.max_active_jobs) == (10, 4)
    assert (row.output_language, row.surrounding_lines) == ("ru", 5)
    assert row.rules_digest == (
        "76eb80bcc51fe28db9c1489e104e7237950feb72b9495062186c4488de69ebbf"
    )


async def test_repeated_connect_reuses_connection_and_preserves_custom_settings(
    onboarding: Catalog,
) -> None:
    payload = github_repository()
    onboarding.provider = lambda request: httpx.Response(200, json=payload)
    request = {"provider": "github", "external_id": str(payload["id"])}
    headers = mutation_headers(onboarding.user)
    first = await onboarding.client.post(
        "/api/v1/repositories/connect", json=request, headers=headers
    )
    assert first.status_code == HTTPStatus.CREATED
    repository_id = uuid.UUID(first.json()["id"])
    async with onboarding.postgres.engine.begin() as connection:
        await connection.execute(
            update(RepositorySettings)
            .where(RepositorySettings.repository_id == repository_id)
            .values(
                max_starts_per_hour=2,
                max_active_jobs=1,
                surrounding_lines=12,
                rules={"instructions": [{"id": "custom", "text": "Keep this rule"}]},
                ignores={"globs": ["vendor/**"]},
                rules_digest="existing-custom-digest",
            )
        )
        before = (
            await connection.execute(
                select(RepositorySettings).where(
                    RepositorySettings.repository_id == repository_id
                )
            )
        ).all()
    repeated = await onboarding.client.post(
        "/api/v1/repositories/connect", json=request, headers=headers
    )
    assert repeated.status_code == HTTPStatus.OK
    assert repeated.json() == first.json()
    async with onboarding.postgres.engine.connect() as connection:
        after = (
            await connection.execute(
                select(RepositorySettings).where(
                    RepositorySettings.repository_id == repository_id
                )
            )
        ).all()
        pointer = await connection.scalar(
            select(Repository.current_settings_id).where(Repository.id == repository_id)
        )
    assert after == before
    assert pointer == before[0].id


async def test_connect_derives_and_refreshes_role_from_current_github_permissions(
    onboarding: Catalog,
) -> None:
    payload = github_repository()
    payload["permissions"]["admin"] = True
    onboarding.provider = lambda request: httpx.Response(200, json=payload)
    request = {"provider": "github", "external_id": str(payload["id"])}
    headers = mutation_headers(onboarding.user)
    admin = await onboarding.client.post(
        "/api/v1/repositories/connect", json=request, headers=headers
    )
    assert admin.status_code == HTTPStatus.CREATED
    assert admin.json()["role"] == "admin"
    assert admin.json()["capabilities"]["can_manage_settings"] is True
    payload["permissions"]["admin"] = False
    reviewer = await onboarding.client.post(
        "/api/v1/repositories/connect", json=request, headers=headers
    )
    assert reviewer.status_code == HTTPStatus.OK
    assert reviewer.json()["id"] == admin.json()["id"]
    assert reviewer.json()["role"] == "reviewer"
    assert reviewer.json()["capabilities"]["can_manage_settings"] is False
    listed = await onboarding.client.get(
        "/api/v1/repositories", headers=browser_cookie(onboarding.user)
    )
    assert listed.json()["items"] == [reviewer.json()]


@pytest.mark.parametrize("two_users", [False, True])
async def test_concurrent_connect_has_one_repository_and_settings_version(
    onboarding: Catalog, two_users: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = github_repository()
    arrived = 0
    ready = asyncio.Event()

    async def provider(request: httpx.Request) -> httpx.Response:
        nonlocal arrived
        assert request.url.path == f"/repositories/{payload['id']}"
        arrived += 1
        if arrived == len(users):
            ready.set()
        await asyncio.wait_for(ready.wait(), timeout=5)
        return httpx.Response(200, json=payload)

    # Only the external HTTP transport is replaced; all four requests use their
    # real browser session, user token and independent PostgreSQL transactions.
    monkeypatch.setattr(onboarding, "provider", provider)
    users = [onboarding.user, onboarding.user] + [
        onboarding.other if two_users else onboarding.user
    ] * 2
    responses = await asyncio.gather(
        *(
            onboarding.client.post(
                "/api/v1/repositories/connect",
                json={"provider": "github", "external_id": str(payload["id"])},
                headers=mutation_headers(user),
            )
            for user in users
        )
    )
    assert sorted(response.status_code for response in responses) == (
        [200, 200, 201, 201] if two_users else [200, 200, 200, 201]
    )
    assert all(response.json() == responses[0].json() for response in responses)
    repository_id = uuid.UUID(responses[0].json()["id"])
    onboarding.provider = lambda request: httpx.Response(200, json=payload)
    for user in set(users):
        listed = await onboarding.client.get(
            "/api/v1/repositories", headers=browser_cookie(user)
        )
        assert listed.json() == {"items": [responses[0].json()], "next_cursor": None}
    async with onboarding.postgres.engine.connect() as connection:
        settings = (
            await connection.execute(
                select(RepositorySettings.id).where(
                    RepositorySettings.repository_id == repository_id
                )
            )
        ).all()
    assert len(settings) == 1


@pytest.mark.parametrize("missing", ["session", "Origin", "X-CSRF-Token"])
async def test_connect_requires_verified_session_origin_and_csrf(
    onboarding: Catalog, missing: str
) -> None:
    headers = mutation_headers(onboarding.user)
    headers.pop("Cookie" if missing == "session" else missing)

    def unexpected(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Rejected mutation must not reach GitHub")

    onboarding.provider = unexpected
    response = await onboarding.client.post(
        "/api/v1/repositories/connect",
        json={"provider": "github", "external_id": "123456"},
        headers=headers,
    )
    assert response.status_code == (401 if missing == "session" else 403)


@pytest.mark.parametrize(
    "body",
    [
        {"provider": "github", "external_id": "123456", "role": "admin"},
        {"provider": "github", "external_id": "123456", "owner": "injected"},
        {"provider": "github", "external_id": "123456", "name": "injected"},
        {"provider": "github", "external_id": "123456", "token": "client-secret"},
        {"provider": "gitlab", "external_id": "123456"},
        {"provider": "github", "external_id": 123456},
        {"provider": "github", "external_id": "../123456"},
    ],
)
async def test_connect_accepts_only_provider_and_positive_string_external_id(
    onboarding: Catalog, body: dict[str, Any]
) -> None:
    def unexpected(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Invalid request must not reach GitHub")

    onboarding.provider = unexpected
    response = await onboarding.client.post(
        "/api/v1/repositories/connect",
        json=body,
        headers=mutation_headers(onboarding.user),
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()["code"] == "VALIDATION_FAILED"
    assert "client-secret" not in response.text


@pytest.mark.parametrize("failure", ["not_found", "no_read", "different_id"])
async def test_connect_rechecks_access_to_exact_id_including_repeated_connection(
    onboarding: Catalog, failure: str
) -> None:
    payload = github_repository()
    onboarding.provider = lambda request: httpx.Response(200, json=payload)
    request = {"provider": "github", "external_id": str(payload["id"])}
    headers = mutation_headers(onboarding.user)
    first = await onboarding.client.post(
        "/api/v1/repositories/connect", json=request, headers=headers
    )
    assert first.status_code == HTTPStatus.CREATED
    if failure == "no_read":
        payload["permissions"]["pull"] = False
    elif failure == "different_id":
        payload["id"] += 1
    else:
        onboarding.provider = lambda request: httpx.Response(
            404, json={"message": "private-provider-metadata"}
        )
    denied = await onboarding.client.post(
        "/api/v1/repositories/connect", json=request, headers=headers
    )
    assert denied.status_code == HTTPStatus.FORBIDDEN
    assert denied.json()["code"] == "ACCESS_DENIED"
    assert "private-provider-metadata" not in denied.text


async def test_late_postgres_failure_rolls_back_whole_connection(
    onboarding: Catalog,
) -> None:
    payload = github_repository()

    def provider(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=[payload] if request.url.path == "/user/repos" else payload
        )

    onboarding.provider = provider
    engine = onboarding.postgres.engine.sync_engine

    def fail_access_insert(
        connection: Any, cursor: Any, statement: str, parameters: Any, *args: Any
    ) -> tuple[str, Any]:
        if statement.startswith("INSERT INTO repository_access"):
            statement = statement.replace("role", "unavailable_column")
        return statement, parameters

    event.listen(engine, "before_cursor_execute", fail_access_insert, retval=True)
    request = {"provider": "github", "external_id": str(payload["id"])}
    headers = mutation_headers(onboarding.user)
    try:
        failed = await onboarding.client.post(
            "/api/v1/repositories/connect", json=request, headers=headers
        )
    finally:
        event.remove(engine, "before_cursor_execute", fail_access_insert)
    assert failed.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert failed.json()["code"] == "SERVICE_UNAVAILABLE"
    assert "unavailable_column" not in failed.text
    # There is no settings HTTP seam in this scope. Verify rollback left no
    # aggregate, then prove a retry is a fresh connection through HTTP.
    async with onboarding.postgres.engine.connect() as connection:
        assert (
            await connection.scalar(
                select(Repository.id).where(
                    Repository.external_id == str(payload["id"])
                )
            )
        ) is None
    available = await onboarding.client.get(
        "/api/v1/repositories/available", headers=browser_cookie(onboarding.user)
    )
    assert available.json()["items"] == [available_dto(payload)]
    retry = await onboarding.client.post(
        "/api/v1/repositories/connect", json=request, headers=headers
    )
    assert retry.status_code == HTTPStatus.CREATED


async def test_new_endpoints_release_postgres_before_github_http(
    onboarding: Catalog,
) -> None:
    payload = github_repository()
    active: set[int] = set()

    def checkout(dbapi: Any, record: Any, proxy: Any) -> None:
        active.add(id(record))

    def checkin(dbapi: Any, record: Any) -> None:
        active.discard(id(record))

    def provider(request: httpx.Request) -> httpx.Response:
        assert not active, "PostgreSQL connection held during GitHub HTTP"
        return httpx.Response(
            200, json=[payload] if request.url.path == "/user/repos" else payload
        )

    onboarding.provider = provider
    engine = onboarding.postgres.engine.sync_engine
    event.listen(engine, "checkout", checkout)
    event.listen(engine, "checkin", checkin)
    try:
        available = await onboarding.client.get(
            "/api/v1/repositories/available", headers=browser_cookie(onboarding.user)
        )
        assert available.status_code == HTTPStatus.OK
        connected = await onboarding.client.post(
            "/api/v1/repositories/connect",
            json={"provider": "github", "external_id": str(payload["id"])},
            headers=mutation_headers(onboarding.user),
        )
        assert connected.status_code == HTTPStatus.CREATED
    finally:
        event.remove(engine, "checkout", checkout)
        event.remove(engine, "checkin", checkin)


async def test_connect_rejects_excessively_nested_json_safely(
    onboarding: Catalog,
) -> None:
    def unexpected(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Invalid request must not reach GitHub")

    onboarding.provider = unexpected
    response = await onboarding.client.post(
        "/api/v1/repositories/connect",
        content="[" * 1500 + "0" + "]" * 1500,
        headers={
            **mutation_headers(onboarding.user),
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()["code"] == "VALIDATION_FAILED"


@pytest.mark.parametrize(
    ("limit", "status"),
    [
        (1, HTTPStatus.OK),
        (100, HTTPStatus.OK),
        (0, HTTPStatus.UNPROCESSABLE_ENTITY),
        (101, HTTPStatus.UNPROCESSABLE_ENTITY),
    ],
)
async def test_available_limit_bounds(
    onboarding: Catalog, limit: int, status: HTTPStatus
) -> None:
    def provider(request: httpx.Request) -> httpx.Response:
        assert request.url.params["per_page"] == str(limit)
        return httpx.Response(200, json=[])

    onboarding.provider = provider
    response = await onboarding.client.get(
        "/api/v1/repositories/available",
        params={"limit": limit},
        headers=browser_cookie(onboarding.user),
    )
    assert response.status_code == status


async def test_available_cursor_is_scoped_and_does_not_accept_a_provider_url(
    onboarding: Catalog,
) -> None:
    onboarding.provider = lambda request: httpx.Response(
        200,
        json=[],
        headers={
            "Link": (
                '<https://api.github.com/user/repos?per_page=20&page=2>; rel="next"'
            )
        },
    )
    first = await onboarding.client.get(
        "/api/v1/repositories/available", headers=browser_cookie(onboarding.user)
    )
    assert first.status_code == HTTPStatus.OK
    cursor = first.json()["next_cursor"]

    def unexpected(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Invalid continuation must not reach GitHub")

    onboarding.provider = unexpected
    for user, params in (
        (onboarding.user, {"cursor": "https://evil.test/steal-token"}),
        (onboarding.user, {"cursor": cursor, "limit": "1"}),
        (onboarding.other, {"cursor": cursor}),
    ):
        invalid = await onboarding.client.get(
            "/api/v1/repositories/available",
            params=params,
            headers=browser_cookie(user),
        )
        assert invalid.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
        assert invalid.json()["code"] == "VALIDATION_FAILED"


async def test_available_validates_provider_continuation_before_returning_a_page(
    onboarding: Catalog,
) -> None:
    onboarding.provider = lambda request: httpx.Response(
        200,
        json=[github_repository()],
        headers={"Link": '<https://evil.test/user/repos?page=2>; rel="next"'},
    )
    response = await onboarding.client.get(
        "/api/v1/repositories/available", headers=browser_cookie(onboarding.user)
    )
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json()["code"] == "UPSTREAM_UNAVAILABLE"
    assert "items" not in response.json()


async def test_available_omits_repositories_without_current_read_permission(
    onboarding: Catalog,
) -> None:
    denied = github_repository(name="denied")
    denied["permissions"]["pull"] = False
    readable = github_repository(name="readable")
    onboarding.provider = lambda request: httpx.Response(200, json=[denied, readable])
    response = await onboarding.client.get(
        "/api/v1/repositories/available", headers=browser_cookie(onboarding.user)
    )
    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"items": [available_dto(readable)], "next_cursor": None}


@pytest.mark.parametrize("endpoint", ["available", "connect"])
@pytest.mark.parametrize("status", [429, 503])
async def test_provider_errors_are_safe_and_preserve_rate_limit_delay(
    onboarding: Catalog, endpoint: str, status: int, caplog: pytest.LogCaptureFixture
) -> None:
    onboarding.provider = lambda request: httpx.Response(
        status,
        headers={"Retry-After": "17"},
        json={"message": "test-user-secret private-provider-metadata"},
    )
    if endpoint == "available":
        response = await onboarding.client.get(
            "/api/v1/repositories/available", headers=browser_cookie(onboarding.user)
        )
    else:
        response = await onboarding.client.post(
            "/api/v1/repositories/connect",
            json={"provider": "github", "external_id": "123456"},
            headers=mutation_headers(onboarding.user),
        )
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json()["code"] == (
        "UPSTREAM_RATE_LIMITED"
        if status == HTTPStatus.TOO_MANY_REQUESTS
        else "UPSTREAM_UNAVAILABLE"
    )
    assert response.json()["retryable"] is True
    if status == HTTPStatus.TOO_MANY_REQUESTS:
        assert response.json()["retry_after"] == 17  # noqa: PLR2004 -- provider delay
    assert "items" not in response.json()
    assert "test-user-secret" not in response.text + caplog.text
    assert "private-provider-metadata" not in response.text + caplog.text


@pytest.mark.parametrize("endpoint", ["available", "connect"])
@pytest.mark.parametrize("invalid", ["metadata", "permission"])
async def test_malformed_provider_repository_is_rejected_safely(
    onboarding: Catalog, endpoint: str, invalid: str
) -> None:
    payload = github_repository(external_id="123456")
    if invalid == "metadata":
        payload["owner"]["login"] = "../injected"
    else:
        payload["permissions"]["pull"] = "true"
    onboarding.provider = lambda request: httpx.Response(
        200, json=[payload] if endpoint == "available" else payload
    )
    if endpoint == "available":
        response = await onboarding.client.get(
            "/api/v1/repositories/available", headers=browser_cookie(onboarding.user)
        )
    else:
        response = await onboarding.client.post(
            "/api/v1/repositories/connect",
            json={"provider": "github", "external_id": "123456"},
            headers=mutation_headers(onboarding.user),
        )
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json()["code"] == "UPSTREAM_UNAVAILABLE"


async def test_available_database_failure_is_safe(onboarding: Catalog) -> None:
    onboarding.provider = lambda request: httpx.Response(
        200, json=[github_repository()]
    )

    def fail_filter(
        connection: Any, cursor: Any, statement: str, parameters: Any, *args: Any
    ) -> tuple[str, Any]:
        if statement.startswith("SELECT repository.external_id"):
            statement = statement.replace("repository.external_id", "repository.absent")
        return statement, parameters

    engine = onboarding.postgres.engine.sync_engine
    event.listen(engine, "before_cursor_execute", fail_filter, retval=True)
    try:
        response = await onboarding.client.get(
            "/api/v1/repositories/available", headers=browser_cookie(onboarding.user)
        )
    finally:
        event.remove(engine, "before_cursor_execute", fail_filter)
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json()["code"] == "SERVICE_UNAVAILABLE"
    assert "absent" not in response.text


@pytest.mark.parametrize("endpoint", ["available", "connect"])
async def test_new_endpoints_enforce_total_provider_body_deadline(
    onboarding: Catalog, monkeypatch: pytest.MonkeyPatch, endpoint: str
) -> None:
    monkeypatch.setattr("app.api.repositories._READ_TIMEOUT_SECONDS", 0.02)
    closed: list[bool] = []

    class SlowBody(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            await asyncio.sleep(0.1)
            yield b"{}"

        async def aclose(self) -> None:
            closed.append(True)

    onboarding.provider = lambda request: httpx.Response(200, stream=SlowBody())
    if endpoint == "available":
        response = await onboarding.client.get(
            "/api/v1/repositories/available", headers=browser_cookie(onboarding.user)
        )
    else:
        response = await onboarding.client.post(
            "/api/v1/repositories/connect",
            json={"provider": "github", "external_id": "123456"},
            headers=mutation_headers(onboarding.user),
        )
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json()["code"] == "UPSTREAM_UNAVAILABLE"
    assert closed == [True]
