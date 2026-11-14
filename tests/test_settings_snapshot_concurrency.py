"""Immutable materialization through independent sessions on real PostgreSQL."""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import Any
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import delete, event, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.util import await_only
from test_postgres_integration import PostgresHarness

from app.context.loader import LoadedRules
from app.context.rules import ParsedRules
from app.context.snapshot import materialize_settings, snapshot_id, valid_snapshot
from app.db import models
from app.domain.frozen_configuration import FrozenConfigurationError

pytest_plugins = ("test_postgres_integration",)
REPOSITORY = UUID("dbbc595e-fabb-4ac4-9e8e-c12f38fe80c0")
SOURCE = UUID("cf8f47e8-8555-4919-8a48-c6d895fe15cc")
SOURCE_VALUES: dict[str, Any] = {
    "id": SOURCE,
    "repository_id": REPOSITORY,
    "rules": {"instructions": ["Keep source rules."]},
    "ignores": {"globs": ["vendor/**"]},
    "max_starts_per_hour": 10,
    "max_active_jobs": 4,
    "output_language": "ru",
    "surrounding_lines": 5,
}
LOADED = LoadedRules(
    ParsedRules(("Keep source rules.", "[SEC-1] Bind tenant to session."), "valid"),
    {
        "tenant": str(REPOSITORY),
        "repository_external_id": "snapshot-race",
        "revision": "b" * 40,
        "blob_sha": "c" * 40,
        "path": ".review/rules.md",
        "parser_version": "1",
        "state": "valid",
    },
)
EXPECTED_SNAPSHOT = {
    **SOURCE_VALUES,
    "id": UUID("b6f35ff5-dab9-58ab-9e56-2954499fadab"),
    "rules": {"instructions": list(LOADED.rules.instructions)},
    "rules_digest": (
        "64ba8066d0dc5bb794772477821f6a8c117fd733382e5c4dc5ae5d716fe5d8ae"
    ),
    "rules_source": {
        "source_settings_id": str(SOURCE),
        "source_settings_identity": "source-identity",
        "file": LOADED.provenance,
    },
}


@pytest_asyncio.fixture
async def snapshot_source(postgres: PostgresHarness) -> AsyncIterator[None]:
    postgres.track("repository", REPOSITORY)
    postgres.track("repository_settings", SOURCE)
    async with postgres.engine.begin() as connection:
        await connection.execute(
            insert(models.Repository).values(
                id=REPOSITORY,
                provider_name="github",
                external_id="snapshot-race",
                owner="team",
                repo_name="snapshot-race",
            )
        )
        await connection.execute(
            insert(models.RepositorySettings).values(
                **SOURCE_VALUES, rules_digest="source-digest"
            )
        )
        await connection.execute(
            update(models.Repository)
            .where(models.Repository.id == REPOSITORY)
            .values(current_settings_id=SOURCE)
        )
    try:
        yield
    finally:
        async with postgres.engine.connect() as connection:
            for identifier in await connection.scalars(
                select(models.RepositorySettings.id).where(
                    models.RepositorySettings.repository_id == REPOSITORY
                )
            ):
                postgres.track("repository_settings", identifier)


async def test_concurrent_materialization_preserves_one_identical_snapshot(
    snapshot_source: None, postgres: PostgresHarness
) -> None:
    factory = async_sessionmaker(postgres.engine, expire_on_commit=False)
    barrier = asyncio.Barrier(2)

    def synchronize_insert(
        connection: Any, cursor: Any, statement: str, *args: Any
    ) -> None:
        if statement.startswith("INSERT INTO repository_settings"):
            await_only(asyncio.wait_for(barrier.wait(), timeout=5))

    engine = postgres.engine.sync_engine
    event.listen(engine, "before_cursor_execute", synchronize_insert)
    try:
        async with factory() as left, factory() as right:
            # Prepare both independent connections before releasing each pair.
            await left.connection()
            await right.connection()
            # The barrier controls client overlap, not the server's internal
            # precheck/index order. Exercise a bounded set of fresh insertions.
            for _ in range(32):
                results = await asyncio.gather(
                    materialize_settings(
                        left, SOURCE_VALUES, "source-identity", LOADED
                    ),
                    materialize_settings(
                        right, SOURCE_VALUES, "source-identity", LOADED
                    ),
                    return_exceptions=True,
                )
                # Drain both callers before raising, so RED cannot leak a sibling.
                for result in results:
                    if isinstance(result, BaseException):
                        raise result
                assert results[0] == results[1]
                effective = results[0]
                assert isinstance(effective, dict)
                assert effective["id"] == UUID("b6f35ff5-dab9-58ab-9e56-2954499fadab")
                expected = {
                    **SOURCE_VALUES,
                    "id": effective["id"],
                    "rules": {"instructions": list(LOADED.rules.instructions)},
                    "rules_digest": (
                        "64ba8066d0dc5bb794772477821f6a8c1"
                        "17fd733382e5c4dc5ae5d716fe5d8ae"
                    ),
                    "rules_source": {
                        "source_settings_id": str(SOURCE),
                        "source_settings_identity": "source-identity",
                        "file": LOADED.provenance,
                    },
                }
                assert effective == expected
                async with factory() as observer:
                    rows = list(
                        await observer.scalars(
                            select(models.RepositorySettings).where(
                                models.RepositorySettings.repository_id == REPOSITORY
                            )
                        )
                    )
                    assert {row.id for row in rows} == {SOURCE, effective["id"]}
                    stored = next(row for row in rows if row.id == effective["id"])
                    assert {key: getattr(stored, key) for key in expected} == expected
                    source = next(row for row in rows if row.id == SOURCE)
                    assert {
                        key: getattr(source, key) for key in SOURCE_VALUES
                    } == SOURCE_VALUES
                    repository = await observer.get(models.Repository, REPOSITORY)
                    assert repository is not None
                    assert repository.current_settings_id == SOURCE
                    for model in (
                        models.ReviewJob,
                        models.QuotaUsage,
                        models.OutboxEvent,
                    ):
                        assert list(await observer.scalars(select(model))) == []
                await left.rollback()
                await right.rollback()
                # No review references this test-only row; make the next pair a
                # fresh insertion of the same deterministic identity.
                async with postgres.engine.begin() as connection:
                    await connection.execute(
                        delete(models.RepositorySettings).where(
                            models.RepositorySettings.id == effective["id"]
                        )
                    )
    finally:
        event.remove(engine, "before_cursor_execute", synchronize_insert)


async def _wait_for_snapshot_lock(
    engine: AsyncEngine, waiter_pid: int, blocker_pid: int
) -> dict[str, Any]:
    async with engine.connect() as connection:
        # Each poll sees fresh activity rather than a cached stats snapshot.
        observer = await connection.execution_options(isolation_level="AUTOCOMMIT")
        for _ in range(200):
            activity = (
                (
                    await observer.execute(
                        text(
                            """
                        SELECT pid, state, wait_event_type, wait_event,
                               query, pg_blocking_pids(pid) AS blockers
                        FROM pg_stat_activity
                        WHERE pid = :waiter
                        """
                        ),
                        {"waiter": waiter_pid},
                    )
                )
                .mappings()
                .one()
            )
            if (
                activity["state"] == "active"
                and activity["wait_event_type"] == "Lock"
                and activity["wait_event"] == "transactionid"
                and blocker_pid in activity["blockers"]
                and activity["query"].startswith("INSERT INTO repository_settings")
            ):
                return {"first_pid": blocker_pid, **dict(activity)}
            await asyncio.sleep(0.01)
    raise AssertionError(f"snapshot INSERT did not wait for {blocker_pid}: {activity}")


async def test_materialization_waits_for_uncommitted_snapshot(
    snapshot_source: None,
    postgres: PostgresHarness,
    record_property: Callable[[str, object], None],
) -> None:
    # Characterization of a controlled conflict, not of PostgreSQL index order.
    assert snapshot_id(EXPECTED_SNAPSHOT) == EXPECTED_SNAPSHOT["id"]
    factory = async_sessionmaker(postgres.engine, expire_on_commit=False)
    first_inserted = asyncio.Event()
    may_commit = asyncio.Event()

    def hold_first_commit(session: Any) -> None:
        first_inserted.set()
        await_only(asyncio.wait_for(may_commit.wait(), timeout=10))

    async with factory() as first, factory() as second:
        first_task: asyncio.Task[dict[str, Any]] | None = None
        second_task: asyncio.Task[dict[str, Any]] | None = None
        event.listen(first.sync_session, "before_commit", hold_first_commit)
        try:
            first_pid = int(
                (await first.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            )
            second_pid = int(
                (await second.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            )
            assert first_pid != second_pid
            first_task = asyncio.create_task(
                materialize_settings(first, SOURCE_VALUES, "source-identity", LOADED)
            )
            await asyncio.wait_for(first_inserted.wait(), timeout=5)
            async with factory() as observer:
                assert (
                    await observer.get(
                        models.RepositorySettings, EXPECTED_SNAPSHOT["id"]
                    )
                    is None
                )
            second_task = asyncio.create_task(
                materialize_settings(second, SOURCE_VALUES, "source-identity", LOADED)
            )
            activity = await _wait_for_snapshot_lock(
                postgres.engine, second_pid, first_pid
            )
            record_property("postgres_overlap", json.dumps(activity))
            assert not first_task.done()
            assert not second_task.done()
            may_commit.set()
            results = await asyncio.wait_for(
                asyncio.gather(first_task, second_task), timeout=5
            )
            assert results[0] == results[1] == EXPECTED_SNAPSHOT
        finally:
            may_commit.set()
            tasks = [task for task in (first_task, second_task) if task is not None]
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            event.remove(first.sync_session, "before_commit", hold_first_commit)
            await first.rollback()
            await second.rollback()

    async with factory() as observer:
        rows = list(
            await observer.scalars(
                select(models.RepositorySettings).where(
                    models.RepositorySettings.repository_id == REPOSITORY
                )
            )
        )
        assert {row.id for row in rows} == {SOURCE, EXPECTED_SNAPSHOT["id"]}
        stored = next(row for row in rows if row.id == EXPECTED_SNAPSHOT["id"])
        assert valid_snapshot(stored)
        assert {
            key: getattr(stored, key) for key in EXPECTED_SNAPSHOT
        } == EXPECTED_SNAPSHOT
        source = next(row for row in rows if row.id == SOURCE)
        assert {key: getattr(source, key) for key in SOURCE_VALUES} == SOURCE_VALUES
        repository = await observer.get(models.Repository, REPOSITORY)
        assert repository is not None
        assert repository.current_settings_id == SOURCE
        for model in (models.ReviewJob, models.QuotaUsage, models.OutboxEvent):
            assert list(await observer.scalars(select(model))) == []


async def test_materialization_insert_handles_all_unique_conflicts(
    snapshot_source: None, postgres: PostgresHarness
) -> None:
    statements: list[str] = []

    def capture_insert(
        connection: Any, cursor: Any, statement: str, *args: Any
    ) -> None:
        if statement.startswith("INSERT INTO repository_settings"):
            statements.append(" ".join(statement.upper().split()))

    engine = postgres.engine.sync_engine
    event.listen(engine, "after_cursor_execute", capture_insert)
    try:
        async with async_sessionmaker(postgres.engine)() as session:
            result = await materialize_settings(
                session, SOURCE_VALUES, "source-identity", LOADED
            )
        assert result == EXPECTED_SNAPSHOT
        assert len(statements) == 1
        assert "ON CONFLICT DO NOTHING" in statements[0]
        assert "ON CONFLICT (ID) DO NOTHING" not in statements[0]
    finally:
        event.remove(engine, "after_cursor_execute", capture_insert)


@pytest.mark.parametrize(
    "corruption", [{"rules_source": None}, {"rules_digest": "corrupt"}]
)
async def test_conflicting_snapshot_must_match_expected_content(
    snapshot_source: None, postgres: PostgresHarness, corruption: dict[str, Any]
) -> None:
    factory = async_sessionmaker(postgres.engine, expire_on_commit=False)
    async with factory() as session:
        effective = await materialize_settings(
            session, SOURCE_VALUES, "source-identity", LOADED
        )
    async with postgres.engine.begin() as connection:
        await connection.execute(
            update(models.RepositorySettings)
            .where(models.RepositorySettings.id == effective["id"])
            .values(**corruption)
        )
    async with factory() as session:
        with pytest.raises(FrozenConfigurationError, match="invalid_rules_snapshot"):
            await materialize_settings(
                session, SOURCE_VALUES, "source-identity", LOADED
            )
