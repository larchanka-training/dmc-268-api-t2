"""Analysis process/runtime against disposable PostgreSQL/Redis and HTTP edges."""

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_analysis_terminal import freeze
from test_postgres_integration import PostgresHarness
from test_runtime_manifest import manifest_values, write_manifest

from app.adapters.outbox_dispatcher import PostgresOutboxDispatcher
from app.adapters.redis_queue import RedisQueue
from app.db.models import ReviewEvent, ReviewJob, ReviewLlmAttempt, TaskLease
from app.db.task_leases import PostgresTaskLeaseStore

pytest_plugins = ("test_analysis_handler",)
STREAM = "dmc268:review-tasks"


def environment_for(path: Path) -> dict[str, str]:
    environment = os.environ.copy()
    url = make_url(environment["DMC268_TEST_DATABASE_URL"])
    environment.update(
        POSTGRES__HOST=url.host or "localhost",
        POSTGRES__PORT=str(url.port or 5432),
        POSTGRES__USER=url.username or "",
        POSTGRES__PASSWORD=url.password or "",
        POSTGRES__DB=url.database or "",
        REDIS__URL=environment["DMC268_TEST_REDIS_URL"],
        ANALYSIS__CONFIG_PATH=str(path),
        LOGGING__FORMAT="json",
        LOGGING__FILE_ENABLED="false",
    )
    environment.pop("LLM__EUROROUTER__API_KEYS", None)
    return environment


def invoke(
    environment: dict[str, str], *args: str, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "app.analysis_worker", *args],
        env=environment,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )


# A pending job incompatible with the manifest no longer blocks startup; see
# test_incompatible_pending_review_does_not_block_startup_and_fails.
@pytest.mark.parametrize("defect", ["credentials", "manifest", "artifacts"])
async def test_untrusted_startup_fails_before_receive_or_mutation(
    postgres: PostgresHarness,
    redis_client: Redis,
    review_id: uuid.UUID,
    tmp_path: Path,
    defect: str,
) -> None:
    factory = async_sessionmaker(postgres.engine, expire_on_commit=False)
    await freeze(factory, review_id)
    queue = RedisQueue(redis_client)
    await PostgresOutboxDispatcher(factory, queue).dispatch_batch()

    async def state() -> tuple[Any, ...]:
        async with factory() as session:
            job = await session.get(ReviewJob, review_id)
            assert job is not None
            counts = [
                await session.scalar(select(func.count()).select_from(table))
                for table in (TaskLease, ReviewLlmAttempt, ReviewEvent)
            ]
            return (job.status, job.analysis_attempts, job.started_at, *counts)

    try:
        before = await state()
        entries = await redis_client.xrange(f"{STREAM}:analyze")
        environment = environment_for(write_manifest(tmp_path, manifest_values()))
        if defect != "credentials":
            environment["LLM__EUROROUTER__API_KEYS"] = "synthetic-test-key"
        if defect == "manifest":
            environment["ANALYSIS__CONFIG_PATH"] = str(tmp_path / "missing")
        result = invoke(
            environment, "--once", cwd=tmp_path if defect == "artifacts" else None
        )
        assert result.returncode == 1, result.stdout + result.stderr
        assert result.stderr == ""
        records = [json.loads(line) for line in result.stdout.splitlines()]
        assert records[-1]["error_type"] == "FrozenConfigurationError"
        assert await state() == before
        assert await redis_client.xrange(f"{STREAM}:analyze") == entries
        assert await redis_client.xinfo_groups(f"{STREAM}:analyze") == []
    finally:
        await redis_client.delete(f"{STREAM}:analyze", f"{STREAM}:publish")


async def test_empty_once_is_one_new_receive_and_closes_connections(
    postgres: PostgresHarness, redis_client: Redis, tmp_path: Path
) -> None:
    environment = environment_for(write_manifest(tmp_path, manifest_values()))
    environment["LLM__EUROROUTER__API_KEYS"] = "synthetic-test-key"
    before = {row["id"] for row in await redis_client.client_list()}
    try:
        result = invoke(environment, "--once")
        assert result.returncode == 0, result.stdout + result.stderr
        records = [json.loads(line) for line in result.stdout.splitlines()]
        assert [row["event"] for row in records] == [
            "analysis_worker_pass",
            "analysis_worker_stopped",
        ]
        assert records[0]["acknowledged"] is False
        groups = await redis_client.xinfo_groups(f"{STREAM}:analyze")
        assert len(groups) == 1 and groups[0]["pending"] == 0
        assert await redis_client.xlen(f"{STREAM}:publish") == 0
        assert {row["id"] for row in await redis_client.client_list()} == before
        async with postgres.engine.connect() as connection:
            assert (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname=current_database() "
                        "AND pid <> pg_backend_pid() AND backend_type='client backend'"
                    )
                )
            ).scalar_one() == 0
    finally:
        await redis_client.delete(f"{STREAM}:analyze", f"{STREAM}:publish")


async def test_continuous_idle_waits_between_passes_and_stops_without_receive(
    postgres: PostgresHarness, redis_client: Redis, tmp_path: Path
) -> None:
    environment = environment_for(write_manifest(tmp_path, manifest_values()))
    environment["LLM__EUROROUTER__API_KEYS"] = "synthetic-test-key"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "app.analysis_worker",
        "--poll-interval",
        "0.2",
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert process.stdout is not None
        for index in range(3):
            start = time.monotonic()
            line = await asyncio.wait_for(process.stdout.readline(), 10)
            assert line, "continuous runtime exited"
            record = json.loads(line)
            assert record["event"] == "analysis_worker_pass"
            assert record["acknowledged"] is False
            if index:
                assert time.monotonic() - start >= 0.15  # noqa: PLR2004
        process.send_signal(signal.SIGTERM)
        stdout, stderr = await asyncio.wait_for(process.communicate(), 3)
        assert process.returncode == 0, stderr.decode()
        assert stderr == b""
        assert [json.loads(line)["event"] for line in stdout.splitlines()] == [
            "analysis_worker_stopped"
        ]
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()
        await redis_client.delete(f"{STREAM}:analyze", f"{STREAM}:publish")


async def test_bounded_once_retains_deferred_deliveries_and_changes_boot_owner(
    postgres: PostgresHarness,
    redis_client: Redis,
    review_id: uuid.UUID,
    tmp_path: Path,
) -> None:
    factory = async_sessionmaker(postgres.engine, expire_on_commit=False)
    await freeze(factory, review_id)
    queue = RedisQueue(redis_client)
    await PostgresOutboxDispatcher(factory, queue).dispatch_batch()
    entries = await redis_client.xrange(f"{STREAM}:analyze")
    assert entries and entries[0][1] is not None
    serialized = entries[0][1]["envelope"]
    assert isinstance(serialized, str)
    await redis_client.xadd(f"{STREAM}:analyze", {"envelope": serialized})
    leases = PostgresTaskLeaseStore(factory)
    lease = await leases.acquire(
        review_id, owner="other-live-worker", task_kind="analyze"
    )
    assert lease is not None
    environment = environment_for(write_manifest(tmp_path, manifest_values()))
    environment["LLM__EUROROUTER__API_KEYS"] = "synthetic-test-key"
    owners = []
    try:
        for count in (1, 2):
            result = invoke(environment, "--once")
            assert result.returncode == 0, result.stdout + result.stderr
            record = json.loads(result.stdout.splitlines()[0])
            assert record["acknowledged"] is False
            owners.append(record["owner"])
            pending = await redis_client.xpending_range(
                f"{STREAM}:analyze", "review-workers", "-", "+", 10
            )
            assert len(pending) == count
            assert all(row["times_delivered"] == 1 for row in pending)
            assert [row["consumer"] for row in pending] == owners
        assert owners[0] != owners[1]
        for owner in owners:
            uuid.UUID(owner.removeprefix("analysis-"))
        async with factory() as session:
            job = await session.get(ReviewJob, review_id)
            assert job is not None and job.analysis_attempts == 0
            assert (
                await session.scalar(select(func.count()).select_from(ReviewLlmAttempt))
                == 0
            )
    finally:
        await leases.release(lease)
        await redis_client.delete(f"{STREAM}:analyze", f"{STREAM}:publish")


@pytest.mark.parametrize("failure", ["settings", "postgres", "redis"])
def test_infrastructure_failure_is_nonzero_and_never_logs_exception_text(
    tmp_path: Path, failure: str
) -> None:
    environment = environment_for(write_manifest(tmp_path, manifest_values()))
    environment["LLM__EUROROUTER__API_KEYS"] = "synthetic-private-key"
    environment["GITHUB__TOKEN"] = "synthetic-private-github-key"
    environment["LOGGING__LEVEL"] = "DEBUG"
    if failure == "settings":
        environment["POSTGRES__PORT"] = "private-invalid-port"
    elif failure == "postgres":
        environment["POSTGRES__PORT"] = "1"
        environment["POSTGRES__PASSWORD"] = "private-password"
    else:
        environment["REDIS__URL"] = "redis://:private-password@127.0.0.1:1/0"
    result = invoke(environment, "--once")
    assert result.returncode == 1
    assert result.stderr == ""
    assert "private-" not in result.stdout
    assert "Traceback" not in result.stdout
    assert "analysis_worker_failed" in result.stdout
    assert "analysis_worker_pass" not in result.stdout
