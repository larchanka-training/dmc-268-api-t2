import os
import select
import subprocess
import sys
import time

import pytest
from sqlalchemy.engine import make_url


def _cli_environment() -> dict[str, str]:
    environment = os.environ.copy()
    database_url = environment.get("DMC268_TEST_DATABASE_URL")
    if database_url:
        url = make_url(database_url)
        environment.update(
            {
                "POSTGRES__HOST": url.host or "localhost",
                "POSTGRES__PORT": str(url.port or 5432),
                "POSTGRES__USER": url.username or "",
                "POSTGRES__PASSWORD": url.password or "",
                "POSTGRES__DB": url.database or "",
            }
        )
    return environment


def test_recovery_cli_has_once_mode() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "app.webhook_worker", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "--once" in result.stdout


@pytest.mark.skipif(
    not os.environ.get("DMC268_TEST_DATABASE_URL"),
    reason="requires disposable PostgreSQL",
)
def test_recovery_cli_can_run_empty_batch() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "app.webhook_worker", "--once"],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
        env=_cli_environment(),
    )
    assert result.returncode == 0
    assert "webhook_batch" in result.stdout


@pytest.mark.skipif(
    not os.environ.get("DMC268_TEST_DATABASE_URL"),
    reason="requires disposable PostgreSQL",
)
def test_recovery_cli_polls_until_stopped() -> None:

    process = subprocess.Popen(
        [sys.executable, "-m", "app.webhook_worker"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_cli_environment(),
    )
    try:
        assert process.stdout is not None
        assert select.select([process.stdout], [], [], 10)[0], (
            "worker did not report a batch"
        )
        assert "webhook_batch" in process.stdout.readline()
        time.sleep(0.2)
        assert process.poll() is None
    finally:
        process.terminate()
        process.communicate(timeout=10)
