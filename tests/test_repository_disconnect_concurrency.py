"""Disconnect races through browser HTTP and native PostgreSQL row locks."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, event, select, text, update
from sqlalchemy.engine import AdaptedConnection, Connection
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_postgres_integration import _create_change_request
from test_repositories_api import Catalog
from test_repository_onboarding_api import github_repository, mutation_headers
from test_review_start_api import (
    BASE,
    HEAD,
    AllowedAuthorizer,
    FakeStartGithub,
    fake_pull_requests,
)

from app.api.review_start_dependencies import (
    get_review_configuration_resolver,
    get_review_start_authorizer,
)
from app.application.github_ports import GithubPort
from app.core import github_runtime
from app.core.config import Settings
from app.db import models
from app.domain.frozen_configuration import ModelRoute, admission_policy
from app.review.configuration import FrozenAdmissionResolver, RuntimeProfile

pytest_plugins = (
    "test_postgres_integration",
    "test_repositories_api",
    "test_repository_reader_wiring",
    "test_repository_onboarding_api",
)
_WAIT_SECONDS = 10


@dataclass
class RaceScope:
    catalog: Catalog
    repository_id: uuid.UUID
    settings_id: uuid.UUID
    payload: dict[str, Any]
    github: FakeStartGithub

    @property
    def disconnect_url(self) -> str:
        return f"/api/v1/repositories/{self.repository_id}/connection"

    def disconnect(self) -> Coroutine[Any, Any, httpx.Response]:
        return self.catalog.client.delete(
            self.disconnect_url, headers=mutation_headers(self.catalog.user)
        )

    def start(self) -> Coroutine[Any, Any, httpx.Response]:
        return self.catalog.client.post(
            f"/api/v1/repositories/{self.repository_id}/pull-requests/13/reviews",
            json={"requested_head_sha": HEAD},
            headers={
                **mutation_headers(self.catalog.user),
                "Idempotency-Key": "disconnect-race",
            },
        )

    def connect(self) -> Coroutine[Any, Any, httpx.Response]:
        return self.catalog.client.post(
            "/api/v1/repositories/connect",
            json={"provider": "github", "external_id": str(self.payload["id"])},
            headers=mutation_headers(self.catalog.user),
        )


@pytest_asyncio.fixture
async def race(
    onboarding: Catalog, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[RaceScope]:
    payload = github_repository()
    onboarding.provider = lambda request: httpx.Response(200, json=payload)
    connected = await onboarding.client.post(
        "/api/v1/repositories/connect",
        json={"provider": "github", "external_id": str(payload["id"])},
        headers=mutation_headers(onboarding.user),
    )
    assert connected.status_code == HTTPStatus.CREATED
    repository_id = uuid.UUID(connected.json()["id"])
    async with onboarding.postgres.engine.begin() as connection:
        settings_id = await connection.scalar(
            select(models.Repository.current_settings_id).where(
                models.Repository.id == repository_id
            )
        )
        assert isinstance(settings_id, uuid.UUID)
        change_id = await _create_change_request(
            onboarding.postgres, connection, repository_id
        )
        await connection.execute(
            update(models.ChangeRequest)
            .where(models.ChangeRequest.id == change_id)
            .values(
                external_number="13",
                head_sha=HEAD,
                base_sha=BASE,
                source_repository_external_id=str(payload["id"]),
            )
        )
    github = FakeStartGithub()

    @asynccontextmanager
    async def gateway(settings: Settings) -> AsyncIterator[GithubPort]:
        yield github

    onboarding.app.dependency_overrides[get_review_start_authorizer] = AllowedAuthorizer
    onboarding.app.dependency_overrides[get_review_configuration_resolver] = lambda: (
        FrozenAdmissionResolver(
            async_sessionmaker(onboarding.postgres.engine, expire_on_commit=False),
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
    monkeypatch.setattr(github_runtime, "github_connection", gateway)
    fake_pull_requests(monkeypatch, github)
    try:
        yield RaceScope(onboarding, repository_id, settings_id, payload, github)
    finally:
        async with onboarding.postgres.engine.begin() as connection:
            reviews = select(models.ReviewJob.id).where(
                models.ReviewJob.repository_id == repository_id
            )
            for entity in (
                models.IdempotencyRecord,
                models.OutboxEvent,
                models.Publication,
            ):
                await connection.execute(
                    delete(entity).where(entity.review_job_id.in_(reviews))
                )
            await connection.execute(
                delete(models.ReviewJob).where(
                    models.ReviewJob.repository_id == repository_id
                )
            )
            await connection.execute(
                delete(models.QuotaUsage).where(
                    models.QuotaUsage.repository_id == repository_id
                )
            )
            # Disconnect may remove the row used by onboarding's cleanup tracker.
            onboarding.postgres.track("repository", repository_id)
            onboarding.postgres.track("repository_settings", settings_id)


@dataclass
class SqlGate:
    arrived: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    pid: int | None = None
    tasks: list[asyncio.Task[httpx.Response]] = field(default_factory=list)

    def run(
        self, request: Coroutine[Any, Any, httpx.Response]
    ) -> asyncio.Task[httpx.Response]:
        task = asyncio.create_task(request)
        self.tasks.append(task)
        return task

    async def wait(self) -> None:
        await asyncio.wait_for(self.arrived.wait(), timeout=_WAIT_SECONDS)


@asynccontextmanager
async def sql_gate(
    race: RaceScope, matches: Callable[[str], bool]
) -> AsyncIterator[SqlGate]:
    """Park after a real statement acquires its native PostgreSQL row lock."""
    gate = SqlGate()

    def after_execute(  # noqa: PLR0913, PLR0917 -- SQLAlchemy event signature
        connection: Connection,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if gate.arrived.is_set() or not matches(statement):
            return

        async def pause(raw: Any) -> None:
            gate.pid = raw.get_server_pid()
            gate.arrived.set()
            await asyncio.wait_for(gate.release.wait(), timeout=_WAIT_SECONDS)

        adapted = cast(AdaptedConnection, connection.connection.dbapi_connection)
        adapted.run_async(pause)

    engine = race.catalog.postgres.engine.sync_engine
    event.listen(engine, "after_cursor_execute", after_execute)
    try:
        yield gate
    finally:
        gate.release.set()
        await asyncio.wait_for(
            asyncio.gather(*gate.tasks, return_exceptions=True), timeout=_WAIT_SECONDS
        )
        event.remove(engine, "after_cursor_execute", after_execute)


async def wait_for_blocked(race: RaceScope, gate: SqlGate, query: str) -> None:
    """Use PostgreSQL's actual wait graph as the barrier, without timed sleeps."""
    assert gate.pid is not None
    async with asyncio.timeout(_WAIT_SECONDS):
        async with race.catalog.postgres.engine.connect() as observer:
            # Refresh pg_stat_activity on every probe rather than caching the
            # first transaction snapshot before the waiter enters PostgreSQL.
            await observer.execution_options(isolation_level="AUTOCOMMIT")
            while True:
                blocked = await observer.scalar(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                        "WHERE :blocker = ANY(pg_blocking_pids(pid)) "
                        "AND query LIKE :query)"
                    ),
                    {"blocker": gate.pid, "query": query},
                )
                if blocked:
                    return


async def assert_access(race: RaceScope, *, role: str | None) -> None:
    async with race.catalog.postgres.engine.connect() as connection:
        rows = (
            await connection.execute(
                select(models.RepositoryAccess.role).where(
                    models.RepositoryAccess.repository_id == race.repository_id,
                    models.RepositoryAccess.user_id == race.catalog.user,
                )
            )
        ).all()
        assert [row.role for row in rows] == ([] if role is None else [role])
        assert (
            await connection.scalar(
                select(models.Repository.current_settings_id).where(
                    models.Repository.id == race.repository_id
                )
            )
            == race.settings_id
        )
        assert list(
            await connection.scalars(
                select(models.RepositorySettings.id).where(
                    models.RepositorySettings.repository_id == race.repository_id
                )
            )
        ) == [race.settings_id]


async def test_accepted_start_holds_access_until_job_commit_then_disconnects(
    race: RaceScope,
) -> None:
    async with sql_gate(
        race, lambda sql: "FROM repository_access" in sql and "FOR SHARE" in sql
    ) as gate:
        start = gate.run(race.start())
        await gate.wait()
        disconnect = gate.run(race.disconnect())
        await wait_for_blocked(race, gate, "DELETE FROM repository_access%")
        gate.release.set()
        started, disconnected = await asyncio.gather(start, disconnect)
    assert started.status_code == HTTPStatus.ACCEPTED
    assert disconnected.status_code == HTTPStatus.NO_CONTENT
    assert disconnected.content == b""
    review_id = uuid.UUID(started.json()["review_id"])
    async with race.catalog.postgres.engine.connect() as connection:
        assert (
            await connection.scalar(
                select(models.ReviewJob.status).where(models.ReviewJob.id == review_id)
            )
            == "QUEUED"
        )
        assert (
            await connection.scalar(
                select(models.OutboxEvent.review_job_id).where(
                    models.OutboxEvent.review_job_id == review_id
                )
            )
            == review_id
        )
        assert (
            await connection.scalar(
                select(models.Publication.review_job_id).where(
                    models.Publication.review_job_id == review_id
                )
            )
            == review_id
        )
    await assert_access(race, role=None)


async def test_disconnect_wins_before_locked_start_recheck(race: RaceScope) -> None:
    async with sql_gate(
        race, lambda sql: sql.startswith("DELETE FROM repository_access")
    ) as gate:
        disconnect = gate.run(race.disconnect())
        await gate.wait()
        start = gate.run(race.start())
        await wait_for_blocked(race, gate, "%FROM repository_access%FOR SHARE%")
        gate.release.set()
        disconnected, started = await asyncio.gather(disconnect, start)
    assert disconnected.status_code == HTTPStatus.NO_CONTENT
    assert started.status_code == HTTPStatus.NOT_FOUND
    assert started.json()["code"] == "RESOURCE_NOT_FOUND"
    assert race.github.calls == 1
    await assert_access(race, role=None)
    async with race.catalog.postgres.engine.connect() as connection:
        assert (
            await connection.scalar(
                select(models.ReviewJob.id).where(
                    models.ReviewJob.repository_id == race.repository_id
                )
            )
            is None
        )
    # A later request fails before provider revalidation as well.
    later = await race.start()
    assert later.status_code == HTTPStatus.NOT_FOUND
    assert race.github.calls == 1


async def test_overlapping_disconnects_are_both_idempotent(race: RaceScope) -> None:
    async with sql_gate(
        race, lambda sql: sql.startswith("DELETE FROM repository_access")
    ) as gate:
        first = gate.run(race.disconnect())
        await gate.wait()
        second = gate.run(race.disconnect())
        await wait_for_blocked(race, gate, "DELETE FROM repository_access%")
        gate.release.set()
        responses = await asyncio.gather(first, second)
    assert [response.status_code for response in responses] == [204, 204]
    assert all(response.content == b"" for response in responses)
    assert race.github.calls == 0
    await assert_access(race, role=None)


async def test_connect_waits_for_disconnect_and_reuses_settings(
    race: RaceScope,
) -> None:
    race.payload["permissions"]["admin"] = True
    async with sql_gate(
        race, lambda sql: sql.startswith("DELETE FROM repository_access")
    ) as gate:
        disconnect = gate.run(race.disconnect())
        await gate.wait()
        connect = gate.run(race.connect())
        await wait_for_blocked(race, gate, "INSERT INTO repository_access%")
        gate.release.set()
        disconnected, connected = await asyncio.gather(disconnect, connect)
    assert disconnected.status_code == HTTPStatus.NO_CONTENT
    assert connected.status_code == HTTPStatus.CREATED
    assert connected.json()["id"] == str(race.repository_id)
    assert connected.json()["role"] == "admin"
    await assert_access(race, role="admin")


async def test_disconnect_waits_for_connect_role_refresh(race: RaceScope) -> None:
    race.payload["permissions"]["admin"] = True
    async with sql_gate(
        race, lambda sql: sql.startswith("UPDATE repository_access")
    ) as gate:
        connect = gate.run(race.connect())
        await gate.wait()
        disconnect = gate.run(race.disconnect())
        await wait_for_blocked(race, gate, "DELETE FROM repository_access%")
        gate.release.set()
        connected, disconnected = await asyncio.gather(connect, disconnect)
    assert connected.status_code == HTTPStatus.OK
    assert connected.json()["role"] == "admin"
    assert disconnected.status_code == HTTPStatus.NO_CONTENT
    await assert_access(race, role=None)


async def test_disconnect_of_absent_access_overlaps_reconnect(race: RaceScope) -> None:
    assert (await race.disconnect()).status_code == HTTPStatus.NO_CONTENT
    async with sql_gate(
        race, lambda sql: sql.startswith("INSERT INTO repository_access")
    ) as gate:
        connect = gate.run(race.connect())
        await gate.wait()
        # The new access is not committed. READ COMMITTED DELETE may observe the
        # previously absent row and complete before the overlapping Connect.
        disconnected = await race.disconnect()
        assert disconnected.status_code == HTTPStatus.NO_CONTENT
        gate.release.set()
        connected = await connect
    assert connected.status_code == HTTPStatus.CREATED
    assert connected.json()["id"] == str(race.repository_id)
    await assert_access(race, role="reviewer")
