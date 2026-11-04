"""Dispatcher CLI scheduling on disposable PostgreSQL and Redis boundaries."""

import asyncio
import json
import os
import signal
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from redis.asyncio.connection import Connection
from sqlalchemy import func, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_outbox_dispatcher_cli import (
    STREAM,
    cli_environment,
    invoke,
    publication_lock,
    start_runtime,
    stop_process,
)
from test_postgres_integration import PostgresHarness, _create_outbox_event

from app.core.config import get_settings
from app.db.models import OutboxEvent
from app.outbox_dispatcher import _run as run_dispatcher

pytest_plugins = ("test_outbox_dispatcher_cli",)


@pytest.fixture
def main_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name, value in cli_environment().items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def runtime_database(
    main_environment: None, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[None]:
    from app.db import session  # noqa: PLC0415 -- settings configured by fixture

    # Own a normal pool on this pytest loop; never borrow/dispose the shared pool.
    engine = create_async_engine(os.environ["DMC268_TEST_DATABASE_URL"])
    monkeypatch.setattr(session, "engine", engine)
    monkeypatch.setattr(
        session,
        "async_session_factory",
        async_sessionmaker(engine, expire_on_commit=False),
    )
    try:
        async with engine.connect() as connection:
            assert (await connection.execute(text("SELECT 1"))).scalar_one() == 1
        yield
    finally:
        await engine.dispose()


async def test_continuous_starts_with_one_recovery_before_initial_dispatch() -> None:
    process = await start_runtime("--poll-interval", "60")
    try:
        assert process.stdout is not None
        async with asyncio.timeout(10):
            first = json.loads(await process.stdout.readline())
            assert first["event"] == "outbox_recovery"
            records = [first, json.loads(await process.stdout.readline())]
        assert [row["event"] for row in records] == ["outbox_recovery", "outbox_batch"]
        assert [row["confirmed"] for row in records] == [0, 0]
    finally:
        await stop_process(process)


async def test_once_recovers_legacy_analyze_but_publish_remains_parked(
    postgres: PostgresHarness,
    runtime_redis: Redis,
) -> None:
    event_id, _ = await _create_outbox_event(postgres)
    await _create_outbox_event(postgres, task_kind="publish")
    assert invoke("--once").returncode == 0
    await runtime_redis.delete(f"{STREAM}:analyze")
    async with postgres.engine.begin() as connection:
        await connection.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == event_id)
            .values(
                dispatch_not_before=func.clock_timestamp() - timedelta(seconds=1),
            )
        )
    result = invoke("--once")
    assert result.returncode == 0, result.stderr
    assert await runtime_redis.xlen(f"{STREAM}:analyze") == 1
    assert await runtime_redis.xlen(f"{STREAM}:publish") == 0


@pytest.mark.usefixtures("runtime_database")
async def test_continuous_ticks_during_backlog_use_completion_time_without_catchup(
    postgres: PostgresHarness,
    runtime_redis: Redis,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for _ in range(11):
        await _create_outbox_event(postgres)
    await _create_outbox_event(postgres, task_kind="publish")
    assert invoke("--once", "--batch-size", "1").returncode == 0
    rows = await runtime_redis.xrange(f"{STREAM}:analyze")
    assert rows
    first_receipt = str(rows[0][0])
    clock = 0.0
    schedule = (0, 29, 30, 100, 129, 130, 1000, 1000, 1029, 1030, 1030)
    ping_count = 0
    ticks: list[float] = []
    original = Connection.send_command

    async def observe(connection: Connection, *args: Any, **kwargs: Any) -> None:
        nonlocal clock, ping_count
        if args[0] == "PING":
            clock = schedule[ping_count]
            ping_count += 1
            # Keep the receipt due while only the scheduler clock advances.
            async with postgres.engine.begin() as database:
                await database.execute(
                    update(OutboxEvent)
                    .where(OutboxEvent.broker_message_id == first_receipt)
                    .values(
                        dispatch_not_before=func.clock_timestamp()
                        - timedelta(seconds=1)
                    )
                )
            if ping_count == len(schedule):
                signal.raise_signal(signal.SIGTERM)
                await asyncio.sleep(0.01)
        if args[0] == "XRANGE" and args[2] == first_receipt:
            ticks.append(clock)
            if clock == 30:  # noqa: PLR2004 -- long bounded recovery operation
                clock = 100
        await original(connection, *args, **kwargs)

    monkeypatch.setattr("app.outbox_dispatcher.monotonic", lambda: clock)
    monkeypatch.setattr(Connection, "send_command", observe)
    await asyncio.wait_for(
        run_dispatcher(once=False, batch_size=1, poll_interval=0.01), 10
    )
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert ticks == [0, 30, 130, 1000, 1030]
    assert len([r for r in records if r["event"] == "outbox_recovery"]) == 5  # noqa: PLR2004
    assert [r["confirmed"] for r in records if r["event"] == "outbox_batch"] == [1] * 10


@pytest.mark.usefixtures("runtime_database", "runtime_redis")
async def test_idle_poll_is_capped_by_next_recovery_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delays: list[float] = []
    original = asyncio.timeout

    @asynccontextmanager
    async def timeout(delay: float) -> AsyncIterator[None]:
        if delay in (3600, 30):
            delays.append(delay)
            signal.raise_signal(signal.SIGTERM)
        async with original(1 if delay in (3600, 30) else delay):
            yield

    monkeypatch.setattr("app.outbox_dispatcher.monotonic", lambda: 0.0)
    monkeypatch.setattr(asyncio, "timeout", timeout)
    await run_dispatcher(once=False, batch_size=100, poll_interval=3600)
    assert delays == [30.0]


@pytest.mark.parametrize("shutdown_signal", [signal.SIGTERM, signal.SIGINT])
async def test_stop_during_recovery_finishes_it_without_starting_initial_dispatch(
    postgres: PostgresHarness,
    runtime_redis: Redis,
    shutdown_signal: signal.Signals,
) -> None:
    event_id, _ = await _create_outbox_event(postgres)
    assert invoke("--once").returncode == 0
    await runtime_redis.delete(f"{STREAM}:analyze")
    async with postgres.engine.begin() as connection:
        await connection.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == event_id)
            .values(dispatch_not_before=func.clock_timestamp() - timedelta(seconds=1))
        )
    initial_id, _ = await _create_outbox_event(postgres)
    async with publication_lock(postgres, event_id) as (connection, key):
        process = await start_runtime("--poll-interval", "60")
        try:
            async with asyncio.timeout(10):
                while True:
                    await connection.commit()
                    blocked = (
                        await connection.execute(
                            text(
                                "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE "
                                "query LIKE 'UPDATE outbox_event%' "
                                "AND wait_event = 'advisory')"
                            )
                        )
                    ).scalar_one()
                    if blocked:
                        break
                    await asyncio.sleep(0.01)
            process.send_signal(shutdown_signal)
            await asyncio.sleep(0.05)
            assert process.returncode is None
            await connection.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": key}
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), 3)
            assert process.returncode == 0, stderr.decode()
            assert stderr == b""
            records = [json.loads(line) for line in stdout.splitlines()]
            assert [r["event"] for r in records] == [
                "outbox_recovery",
                "outbox_stopped",
            ]
            assert records[0]["confirmed"] == 1
            assert await runtime_redis.xlen(f"{STREAM}:analyze") == 1
            assert await runtime_redis.xlen(f"{STREAM}:publish") == 0
            async with async_sessionmaker(postgres.engine)() as session:
                recovered = await session.get(OutboxEvent, event_id)
                initial = await session.get(OutboxEvent, initial_id)
                assert recovered is not None and initial is not None
                assert recovered.dispatch_not_before is not None
                assert recovered.broker_message_id is not None
                assert initial.dispatch_not_before is None
        finally:
            await connection.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": key}
            )
            await stop_process(process)


# Fault injection stays at the external Redis boundary in a fresh CLI process.
# run_module executes the same __main__ entrypoint as python -m; SystemExit is real.
_CLI_REDIS_OUTAGE = """
import asyncio
import runpy
import signal
import sys
from redis.asyncio.connection import Connection
from redis.exceptions import ConnectionError

persistent = sys.argv.pop(1) == "persistent"
failed = False
original = Connection.send_command

async def outage(connection, *args, **kwargs):
    global failed
    if args[0] == "XRANGE" and not failed:
        failed = True
        raise ConnectionError("private-provider-payload")
    if failed and persistent:
        raise ConnectionError("private-provider-payload")
    if failed and args[0] == "PING":
        signal.raise_signal(signal.SIGTERM)
        await asyncio.sleep(0.01)
    await original(connection, *args, **kwargs)

Connection.send_command = outage
runpy.run_module("app.outbox_dispatcher", run_name="__main__")
"""


@pytest.mark.parametrize("persistent", [True, False])
async def test_recovery_broker_outage_exits_nonzero_without_successful_empty_tick(
    postgres: PostgresHarness,
    runtime_redis: Redis,
    persistent: bool,
) -> None:
    event_id, _ = await _create_outbox_event(postgres)
    assert invoke("--once").returncode == 0
    async with postgres.engine.begin() as connection:
        await connection.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == event_id)
            .values(dispatch_not_before=func.clock_timestamp() - timedelta(seconds=1))
        )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            _CLI_REDIS_OUTAGE,
            "persistent" if persistent else "transient",
            "--poll-interval",
            "0.01",
        ],
        env=cli_environment(),
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 1
    assert result.stderr == ""
    assert "private-" not in result.stdout + result.stderr
    records = [json.loads(line) for line in result.stdout.splitlines()]
    assert records[-1]["event"] == "outbox_failed"
    assert not any(r["event"] in {"outbox_recovery", "outbox_batch"} for r in records)


async def test_cli_defers_untrusted_probe_without_changing_persisted_authority(
    postgres: PostgresHarness, runtime_redis: Redis
) -> None:
    event_id, _ = await _create_outbox_event(postgres)
    await _create_outbox_event(postgres, task_kind="publish")
    assert invoke("--once").returncode == 0
    entries = await runtime_redis.xrange(f"{STREAM}:analyze")
    assert entries and entries[0][1]
    message_id, fields = entries[0]
    assert isinstance(message_id, str) and fields is not None
    forged = json.loads(fields["envelope"])
    forged["trace_id"] = "private-untrusted-source"
    await runtime_redis.delete(f"{STREAM}:analyze")
    await runtime_redis.xadd(
        f"{STREAM}:analyze", {"envelope": json.dumps(forged)}, id=message_id
    )

    async def authority() -> tuple[object, ...]:
        async with async_sessionmaker(postgres.engine)() as session:
            event = await session.get(OutboxEvent, event_id)
            assert event is not None
            return (
                event.broker_message_id,
                event.broker_published_at,
                event.dispatch_not_before,
            )

    async with postgres.engine.begin() as connection:
        await connection.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == event_id)
            .values(dispatch_not_before=func.clock_timestamp() - timedelta(seconds=1))
        )
    before = await authority()
    result = invoke("--once")
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert "private-" not in result.stdout
    records = [json.loads(line) for line in result.stdout.splitlines()]
    assert any(
        r["event"] == "outbox_transport_deferred" and r["state"] == "UNTRUSTED"
        for r in records
    )
    assert [
        r["confirmed"]
        for r in records
        if r["event"] in {"outbox_recovery", "outbox_batch"}
    ] == [0, 0]
    after = await authority()
    assert after[:2] == before[:2]
    assert after[2] > before[2]  # type: ignore[operator]
    assert await runtime_redis.xlen(f"{STREAM}:analyze") == 1
    assert await runtime_redis.xlen(f"{STREAM}:publish") == 0
