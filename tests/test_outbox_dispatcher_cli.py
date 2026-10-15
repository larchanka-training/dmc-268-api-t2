"""Exercise the dispatcher process against disposable PostgreSQL and Redis."""

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy import func, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection, async_sessionmaker
from test_postgres_integration import PostgresHarness, _create_outbox_event

from app.db.models import OutboxEvent

pytest_plugins = ("test_postgres_integration",)
pytestmark = pytest.mark.skipif(
    not os.environ.get("DMC268_TEST_DATABASE_URL")
    or not os.environ.get("DMC268_TEST_REDIS_URL"),
    reason="requires disposable PostgreSQL and Redis",
)
STREAM = "dmc268:review-tasks"
MIN_POLL_DELAY = 0.15
ARGPARSE_ERROR = 2


@pytest_asyncio.fixture
async def runtime_redis() -> AsyncIterator[Redis]:
    async with Redis.from_url(
        os.environ["DMC268_TEST_REDIS_URL"], decode_responses=True
    ) as client:
        try:
            yield client
        finally:
            await client.delete(f"{STREAM}:analyze", f"{STREAM}:publish")


def cli_environment() -> dict[str, str]:
    environment = os.environ.copy()
    url = make_url(environment["DMC268_TEST_DATABASE_URL"])
    environment.update(
        POSTGRES__HOST=url.host or "localhost",
        POSTGRES__PORT=str(url.port or 5432),
        POSTGRES__USER=url.username or "",
        POSTGRES__PASSWORD=url.password or "",
        POSTGRES__DB=url.database or "",
        REDIS__URL=environment["DMC268_TEST_REDIS_URL"],
        LOGGING__FORMAT="json",
        LOGGING__FILE_ENABLED="false",
    )
    return environment


def invoke(
    *arguments: str, environment: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "app.outbox_dispatcher", *arguments],
        env=environment or cli_environment(),
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )


def test_once_completes_one_empty_batch() -> None:
    result = invoke("--once")
    assert result.returncode == 0, result.stderr
    assert '"event": "outbox_batch"' in result.stdout
    assert '"confirmed": 0' in result.stdout


@pytest.mark.parametrize("failure", ["database", "settings"])
def test_failure_exits_nonzero_with_only_safe_structured_metadata(failure: str) -> None:
    environment = cli_environment()
    environment["POSTGRES__PORT"] = "1" if failure == "database" else "private-port"
    environment["POSTGRES__DB"] = "private-database"
    result = invoke("--once", environment=environment)
    assert result.returncode != 0
    assert result.stderr == ""
    assert "private-" not in result.stdout
    records = [json.loads(line) for line in result.stdout.splitlines()]
    assert not any(record["event"] == "outbox_batch" for record in records)
    assert any(record["event"] == "outbox_failed" for record in records)


async def test_once_bounds_delivery_and_durably_excludes_confirmed_events(
    postgres: PostgresHarness, runtime_redis: Redis
) -> None:
    analyze_id, review_id = await _create_outbox_event(postgres)
    publish_id, _ = await _create_outbox_event(postgres, task_kind="publish")
    factory = async_sessionmaker(postgres.engine)

    first = invoke("--once", "--batch-size", "1")
    assert first.returncode == 0, first.stderr
    messages = await runtime_redis.xrange(f"{STREAM}:analyze")
    assert messages is not None and len(messages) == 1
    fields = messages[0][1]
    assert fields is not None
    assert json.loads(fields["envelope"]) == {
        "schema_version": 1,
        "event_id": str(analyze_id),
        "review_id": str(review_id),
        "task_kind": "analyze",
        "attempt": 2,
        "trace_id": "trace-outbox-test",
    }
    assert await runtime_redis.xlen(f"{STREAM}:publish") == 0
    async with factory() as session:
        analyze = await session.get(OutboxEvent, analyze_id)
        publish = await session.get(OutboxEvent, publish_id)
        assert analyze is not None and analyze.broker_published_at is not None
        assert publish is not None and publish.broker_published_at is None

    second = invoke("--once", "--batch-size", "1")
    assert second.returncode == 0, second.stderr
    third = invoke("--once", "--batch-size", "1")
    assert third.returncode == 0, third.stderr
    assert '"confirmed": 0' in third.stdout
    assert await runtime_redis.xlen(f"{STREAM}:analyze") == 1
    assert await runtime_redis.xlen(f"{STREAM}:publish") == 0
    async with factory() as session:
        publish = await session.get(OutboxEvent, publish_id)
        assert publish is not None and publish.broker_published_at is None
        assert publish.dispatch_generation == 0


@pytest.mark.skipif(
    shutil.which("docker") is None or os.environ.get("DMC268_RUN_DOCKER_TESTS") != "1",
    reason="set DMC268_RUN_DOCKER_TESTS=1 with Docker running",
)
def test_standard_image_starts_help_and_empty_once() -> None:
    image = "dmc268-outbox-dispatcher-test"
    built = subprocess.run(
        ["docker", "build", "-t", image, "."],
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert built.returncode == 0, built.stderr
    environment = cli_environment()
    run = ["docker", "run", "--rm", "--network", "host"]
    for key in environment:
        if key.startswith(("POSTGRES__", "REDIS__", "LOGGING__")):
            run.extend(("-e", key))
    for mode in ("--help", "--once"):
        result = subprocess.run(
            [*run, image, "python", "-m", "app.outbox_dispatcher", mode],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
        assert result.returncode == 0, result.stderr
        if mode == "--help":
            assert "--once" in result.stdout and "--poll-interval" in result.stdout
        else:
            assert '"confirmed": 0' in result.stdout


async def start_runtime(*arguments: str) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "app.outbox_dispatcher",
        *arguments,
        env=cli_environment(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def batch_record(process: asyncio.subprocess.Process) -> dict[str, object]:
    assert process.stdout is not None
    async with asyncio.timeout(10):
        line = await process.stdout.readline()
    assert line, "dispatcher exited before reporting a batch"
    record = json.loads(line)
    assert isinstance(record, dict) and record["event"] == "outbox_batch"
    return record


async def stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        process.terminate()
    await asyncio.wait_for(process.communicate(), 10)


async def test_continuous_runs_multiple_bounded_batches_with_idle_polling(
    postgres: PostgresHarness, runtime_redis: Redis
) -> None:
    await _create_outbox_event(postgres)
    await _create_outbox_event(postgres)
    await _create_outbox_event(postgres, task_kind="publish")
    process = await start_runtime("--batch-size", "1", "--poll-interval", "0.2")
    try:
        assert (await batch_record(process))["confirmed"] == 1
        first_at = time.monotonic()
        assert (await batch_record(process))["confirmed"] == 1
        assert time.monotonic() - first_at >= MIN_POLL_DELAY
        assert (await batch_record(process))["confirmed"] == 0
        idle_at = time.monotonic()
        assert (await batch_record(process))["confirmed"] == 0
        assert time.monotonic() - idle_at >= MIN_POLL_DELAY
        assert process.returncode is None
        assert await runtime_redis.xlen(f"{STREAM}:analyze") == 2  # noqa: PLR2004
        assert await runtime_redis.xlen(f"{STREAM}:publish") == 0
    finally:
        await stop_process(process)


@asynccontextmanager
async def publication_lock(
    postgres: PostgresHarness, event_id: uuid.UUID
) -> AsyncIterator[tuple[AsyncConnection, int]]:
    name = f"runtime_lock_{uuid.uuid4().hex}"
    key = uuid.uuid4().int % 2_000_000_000
    async with postgres.engine.begin() as connection:
        await connection.execute(
            text(f"""
            CREATE FUNCTION {name}() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.id = '{event_id}'::uuid
                   AND NEW.broker_message_id IS NOT NULL THEN
                    PERFORM pg_advisory_xact_lock({key});
                END IF;
                RETURN NEW;
            END $$
        """)
        )
        await connection.execute(
            text(f"""
            CREATE TRIGGER {name} BEFORE UPDATE ON outbox_event
            FOR EACH ROW EXECUTE FUNCTION {name}()
        """)
        )
    try:
        async with postgres.engine.connect() as connection:
            await connection.execute(
                text("SELECT pg_advisory_lock(:key)"), {"key": key}
            )
            try:
                yield connection, key
            finally:
                await connection.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": key}
                )
    finally:
        async with postgres.engine.begin() as connection:
            await connection.execute(text(f"DROP TRIGGER {name} ON outbox_event"))
            await connection.execute(text(f"DROP FUNCTION {name}()"))


@pytest.mark.parametrize("shutdown_signal", [signal.SIGTERM, signal.SIGINT])
async def test_shutdown_finishes_current_batch_without_starting_another(
    postgres: PostgresHarness, runtime_redis: Redis, shutdown_signal: signal.Signals
) -> None:
    event_id, _ = await _create_outbox_event(postgres)
    next_id, _ = await _create_outbox_event(postgres, task_kind="publish")
    clients_before = {client["id"] for client in await runtime_redis.client_list()}
    async with publication_lock(postgres, event_id) as (connection, key):
        process = await start_runtime("--batch-size", "1", "--poll-interval", "30")
        try:
            async with asyncio.timeout(10):
                while await runtime_redis.xlen(f"{STREAM}:analyze") == 0:
                    await asyncio.sleep(0.02)
            async with asyncio.timeout(10):
                backend_pid = None
                while backend_pid is None:
                    await connection.commit()
                    backend_pid = (
                        await connection.execute(
                            text(
                                "SELECT pid FROM pg_stat_activity WHERE query LIKE "
                                "'UPDATE outbox_event%' AND wait_event = 'advisory'"
                            )
                        )
                    ).scalar_one_or_none()
                    if backend_pid is None:
                        await asyncio.sleep(0.02)
            runtime_clients = {
                client["id"]
                for client in await runtime_redis.client_list()
                if client["id"] not in clients_before and client["cmd"] == "xadd"
            }
            assert runtime_clients
            process.send_signal(shutdown_signal)
            await asyncio.sleep(0.1)
            assert process.returncode is None, "shutdown cancelled the current batch"
            await connection.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": key}
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), 3)
            assert process.returncode == 0, stderr.decode()
            assert stderr == b""
            records = [json.loads(line) for line in stdout.splitlines()]
            batches = [
                record for record in records if record["event"] == "outbox_batch"
            ]
            assert [record["confirmed"] for record in batches] == [1]
            assert records[-1]["event"] == "outbox_stopped"
            assert not runtime_clients.intersection(
                client["id"] for client in await runtime_redis.client_list()
            )
            await connection.commit()
            assert not (
                await connection.execute(
                    text(
                        "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE pid = :pid)"
                    ),
                    {"pid": backend_pid},
                )
            ).scalar_one()
            assert await runtime_redis.xlen(f"{STREAM}:publish") == 0
            async with async_sessionmaker(postgres.engine)() as session:
                event = await session.get(OutboxEvent, event_id)
                following = await session.get(OutboxEvent, next_id)
                assert event is not None and event.broker_published_at is not None
                assert following is not None and following.broker_published_at is None
        finally:
            await connection.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": key}
            )
            await stop_process(process)


@pytest.mark.parametrize("shutdown_signal", [signal.SIGTERM, signal.SIGINT])
async def test_shutdown_interrupts_idle_poll_without_a_new_batch(
    shutdown_signal: signal.Signals,
) -> None:
    process = await start_runtime("--poll-interval", "30")
    try:
        assert (await batch_record(process))["confirmed"] == 0
        process.send_signal(shutdown_signal)
        stdout, stderr = await asyncio.wait_for(process.communicate(), 3)
        assert process.returncode == 0, stderr.decode()
        assert stderr == b""
        records = [json.loads(line) for line in stdout.splitlines()]
        assert [record["event"] for record in records] == ["outbox_stopped"]
    finally:
        await stop_process(process)


@pytest.mark.parametrize("interval", ["0", "-1", "nan", "inf"])
def test_poll_interval_rejects_busy_or_nonfinite_values(interval: str) -> None:
    result = invoke("--once", "--poll-interval", interval)
    assert result.returncode == ARGPARSE_ERROR
    assert "outbox_batch" not in result.stdout
    assert "must be finite and positive" in result.stderr


@pytest.mark.parametrize("batch_size", ["0", "-1", "not-a-number"])
@pytest.mark.parametrize("mode", [(), ("--once",)])
def test_batch_size_requires_a_positive_integer_before_runtime_start(
    batch_size: str, mode: tuple[str, ...]
) -> None:
    environment = cli_environment()
    environment["POSTGRES__PORT"] = "invalid-private-port"
    result = invoke(*mode, "--batch-size", batch_size, environment=environment)
    assert result.returncode == ARGPARSE_ERROR
    assert result.stdout == ""
    assert "argument --batch-size: must be a positive integer" in result.stderr
    assert "invalid-private-port" not in result.stderr

    # No upper bound is contracted: a large positive size reaches runtime settings.
    accepted = invoke(*mode, "--batch-size", "1000000", environment=environment)
    assert accepted.returncode == 1
    assert "outbox_failed" in accepted.stdout
    assert accepted.stderr == ""


def test_unavailable_redis_is_not_reported_as_success_even_with_empty_outbox() -> None:
    environment = cli_environment()
    environment["REDIS__URL"] = "redis://:private-password@127.0.0.1:1/0"
    result = invoke("--once", environment=environment)
    assert result.returncode == 1
    assert result.stderr == ""
    assert "private-" not in result.stdout
    assert "outbox_failed" in result.stdout
    assert "outbox_batch" not in result.stdout


async def test_enqueue_failure_preserves_retry_eligibility_without_logging_source(
    postgres: PostgresHarness, runtime_redis: Redis
) -> None:
    event_id, _ = await _create_outbox_event(postgres)
    username = uuid.uuid4().hex
    password = "private-password"
    await runtime_redis.acl_setuser(
        username,
        enabled=True,
        passwords=[f"+{password}"],
        commands=["+@all", "-xadd"],
        keys=[f"{STREAM}:*"],
    )
    environment = cli_environment()
    environment["REDIS__URL"] = environment["REDIS__URL"].replace(
        "://", f"://{username}:{password}@", 1
    )
    try:
        result = invoke("--once", environment=environment)
        assert result.returncode == 1
        assert result.stderr == ""
        assert "private-" not in result.stdout
        assert "outbox_batch" not in result.stdout
        assert '"error_type": "NoPermissionError"' in result.stdout
        assert await runtime_redis.xlen(f"{STREAM}:analyze") == 0
        async with async_sessionmaker(postgres.engine)() as session:
            event = await session.get(OutboxEvent, event_id)
            assert event is not None
            assert event.broker_published_at is None
            assert event.quarantined_at is None
    finally:
        await runtime_redis.acl_deluser(username)

    restarted = invoke("--once")
    assert restarted.returncode == 0, restarted.stderr
    assert await runtime_redis.xlen(f"{STREAM}:analyze") == 0
    async with postgres.engine.begin() as connection:
        await connection.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == event_id)
            .values(
                dispatch_not_before=func.clock_timestamp() - text("INTERVAL '1 second'")
            )
        )
    retried = invoke("--once")
    assert retried.returncode == 0, retried.stderr
    assert await runtime_redis.xlen(f"{STREAM}:analyze") == 1
    async with async_sessionmaker(postgres.engine)() as session:
        event = await session.get(OutboxEvent, event_id)
        assert event is not None
        assert event.dispatch_generation == 2  # noqa: PLR2004 -- first recovery
        assert event.recovery_send_count == 1
        assert event.broker_message_id is not None


async def test_cli_preserves_quarantine_and_does_not_log_private_source_at_debug(
    postgres: PostgresHarness, runtime_redis: Redis
) -> None:
    invalid_id, _ = await _create_outbox_event(postgres)
    valid_id, _ = await _create_outbox_event(postgres)
    async with postgres.engine.begin() as connection:
        await connection.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == invalid_id)
            .values(schema_version=2, trace_id="private-source")
        )
    environment = cli_environment()
    environment["LOGGING__LEVEL"] = "DEBUG"
    result = invoke("--once", environment=environment)
    assert result.returncode == 0, result.stderr
    assert "private-source" not in result.stdout + result.stderr
    assert '"confirmed": 1' in result.stdout
    assert await runtime_redis.xlen(f"{STREAM}:analyze") == 1
    assert await runtime_redis.xlen(f"{STREAM}:publish") == 0
    async with async_sessionmaker(postgres.engine)() as session:
        rejected = await session.get(OutboxEvent, invalid_id)
        accepted = await session.get(OutboxEvent, valid_id)
        assert rejected is not None and accepted is not None
        assert rejected.schema_version == 2  # noqa: PLR2004 -- unsupported source v2
        assert rejected.trace_id == "private-source"
        assert rejected.broker_published_at is None
        assert rejected.quarantined_at is not None
        assert rejected.quarantine_reason_code == "UNSUPPORTED_SCHEMA_VERSION"
        assert accepted.broker_published_at is not None
    again = invoke("--once")
    assert again.returncode == 0, again.stderr
    assert '"confirmed": 0' in again.stdout
    assert await runtime_redis.xlen(f"{STREAM}:publish") == 0
