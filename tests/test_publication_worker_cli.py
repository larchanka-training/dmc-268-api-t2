"""Public publication CLI contract, including safe startup failures."""

import os
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg  # type: ignore[import-untyped]
import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.db import models, publication_models  # noqa: F401 -- register ORM metadata
from app.db.base import Base


def invoke(
    *arguments: str, overrides: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(
        POSTGRES__PORT="private-invalid-port",
        ANALYSIS__CONFIG_PATH="/private-missing-manifest",
        LOGGING__FILE_ENABLED="false",
    )
    environment.update(overrides or {})
    return subprocess.run(
        [sys.executable, "-m", "app.publication_worker", *arguments],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


def test_help_needs_no_database_credentials_or_analysis_manifest() -> None:
    result = invoke("--help")
    assert result.returncode == 0, result.stderr
    assert "--once" in result.stdout and "--poll-interval" in result.stdout
    assert "private-" not in result.stdout + result.stderr


def test_invalid_poll_interval_is_rejected_before_startup() -> None:
    result = invoke("--poll-interval", "nan")
    assert result.returncode == 2  # noqa: PLR2004
    assert "must be finite and in [0.01, 60]" in result.stderr
    assert result.stdout == ""


def test_startup_error_contains_no_private_configuration() -> None:
    result = invoke("--once")
    assert result.returncode == 1
    assert result.stderr == ""
    assert '"event": "publication_worker_failed"' in result.stdout
    assert '"error_type": "ValidationError"' in result.stdout
    assert "private-" not in result.stdout


@pytest_asyncio.fixture
async def cli_services() -> AsyncIterator[dict[str, str]]:
    if not os.environ.get("DMC268_TEST_DATABASE_URL") or not os.environ.get(
        "DMC268_TEST_REDIS_URL"
    ):
        pytest.skip("requires disposable PostgreSQL and Redis")
    url = make_url(os.environ["DMC268_TEST_DATABASE_URL"])
    database = f"publication_cli_{uuid.uuid4().hex}"
    admin = await asyncpg.connect(
        url.set(drivername="postgresql").render_as_string(hide_password=False)
    )
    await admin.execute(f'CREATE DATABASE "{database}"')
    engine = create_async_engine(url.set(database=database), poolclass=NullPool)
    redis_url = (
        make_url(os.environ["DMC268_TEST_REDIS_URL"])
        .set(database="15")
        .render_as_string(hide_password=False)
    )

    async def clear_streams() -> None:
        async with Redis.from_url(
            redis_url, socket_timeout=5, socket_connect_timeout=5
        ) as client:
            await client.delete(
                "dmc268:review-tasks:publish", "dmc268:review-tasks:dead"
            )

    try:
        await clear_streams()
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        await engine.dispose()
        yield {
            "POSTGRES__HOST": url.host or "localhost",
            "POSTGRES__PORT": str(url.port or 5432),
            "POSTGRES__USER": url.username or "",
            "POSTGRES__PASSWORD": url.password or "",
            "POSTGRES__DB": database,
            "REDIS__URL": redis_url,
            "GITHUB__WRITE_TOKEN": "private-test-write",
            "GITHUB__BOT_USER_ID": "9002",
            "LOGGING__FORMAT": "json",
        }
    finally:
        try:
            await clear_streams()
        finally:
            await engine.dispose()
            await admin.execute(f'DROP DATABASE "{database}"')
            await admin.close()


def test_once_runs_one_empty_pass_without_analysis_configuration(
    cli_services: dict[str, str],
) -> None:
    result = invoke("--once", overrides=cli_services)
    assert result.returncode == 0, result.stderr
    assert '"event": "publication_worker_pass"' in result.stdout
    assert '"acknowledged": false' in result.stdout
    assert "private-" not in result.stdout + result.stderr


def test_continuous_process_waits_and_stops_cleanly_on_sigterm(
    cli_services: dict[str, str], tmp_path: Path
) -> None:
    environment = os.environ | cli_services | {"PYTHONUNBUFFERED": "1"}
    log_path = tmp_path / "publication-worker.log"
    with log_path.open("w") as output:
        process = subprocess.Popen(
            [sys.executable, "-m", "app.publication_worker", "--poll-interval", "0.05"],
            env=environment,
            stdout=output,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 10
            while (
                "publication_worker_pass" not in log_path.read_text()
                and time.monotonic() < deadline
            ):
                if process.poll() is not None:
                    break
                time.sleep(0.02)
            assert "publication_worker_pass" in log_path.read_text()
            time.sleep(0.1)
            assert process.poll() is None
            process.send_signal(signal.SIGTERM)
            _, stderr = process.communicate(timeout=10)
            assert process.returncode == 0 and stderr == ""
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=10)
    assert '"event": "publication_worker_stopped"' in log_path.read_text()


def test_runtime_suppresses_private_http_debug_diagnostics(
    cli_services: dict[str, str],
) -> None:
    environment = os.environ | cli_services | {"LOGGING__LEVEL": "DEBUG"}
    script = (
        "import asyncio, logging; from app.publication_worker import run; "
        "asyncio.run(run(once=True, poll_interval=1)); "
        "logging.getLogger('httpcore').debug('private-header: credential'); "
        "logging.getLogger('httpx').debug('private-provider-diagnostic')"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "publication_worker_stopped" in result.stdout
    assert "private-" not in result.stdout + result.stderr
