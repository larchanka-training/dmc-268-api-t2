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
import httpx
import pytest
import pytest_asyncio
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from app import publication_worker
from app.core.config import Github, Settings
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


def test_check_config_validates_offline_without_worker_or_sentry_side_effects(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Offline configuration check attempted an external side effect")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANALYSIS__CONFIG_PATH", "/private-missing-analysis-manifest")
    settings = Settings(
        github=Github(
            write_token=SecretStr("private-synthetic-write"), bot_user_id="9002"
        ),
    )
    monkeypatch.setattr("app.core.config.get_settings", lambda: settings)
    monkeypatch.setattr(sys, "argv", ["publication-worker", "--check-config"])
    monkeypatch.setattr(publication_worker, "run", forbidden)
    monkeypatch.setattr("app.core.sentry.configure_sentry", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    monkeypatch.setattr(AsyncEngine, "connect", forbidden)
    monkeypatch.setattr(Redis, "execute_command", forbidden)
    publication_worker.main()
    captured = capsys.readouterr()
    assert captured.out.strip() == "Publication configuration is valid."
    assert captured.err == ""


@pytest.mark.parametrize(
    "token",
    [
        pytest.param("private-synthetic-write\r\n", id="control"),
        pytest.param("private-synthetic-é", id="unicode"),
    ],
)
def test_check_config_rejects_invalid_token_without_leaking_it(token: str) -> None:
    result = invoke(
        "--check-config",
        overrides={
            "POSTGRES__PORT": "5432",
            "GITHUB__WRITE_TOKEN": token,
            "GITHUB__BOT_USER_ID": "9002",
        },
    )
    assert result.returncode == 1
    assert '"error_type": "PublicationConfigError"' in result.stdout
    assert result.stderr == ""
    assert "private-" not in result.stdout


@pytest.mark.parametrize("missing", ["GITHUB__WRITE_TOKEN", "GITHUB__BOT_USER_ID"])
def test_check_config_requires_write_identity_and_never_falls_back_to_read_token(
    missing: str,
) -> None:
    overrides = {
        "POSTGRES__PORT": "5432",
        "GITHUB__TOKEN": "private-synthetic-read",
        "GITHUB__WRITE_TOKEN": "private-synthetic-write",
        "GITHUB__BOT_USER_ID": "9002",
    }
    overrides[missing] = ""
    result = invoke("--check-config", overrides=overrides)
    assert result.returncode == 1
    assert '"error_type": "PublicationConfigError"' in result.stdout
    assert result.stderr == ""
    assert "private-" not in result.stdout


def test_check_config_cannot_be_combined_with_a_publication_pass() -> None:
    result = invoke("--check-config", "--once")
    assert result.returncode == 2  # noqa: PLR2004 -- argparse usage error
    assert "not allowed with argument" in result.stderr
    assert result.stdout == ""


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
