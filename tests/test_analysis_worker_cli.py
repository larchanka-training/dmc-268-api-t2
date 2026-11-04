"""Public analysis CLI contract; no service dependencies for argument parsing."""

import os
import subprocess
import sys


def invoke(*arguments: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(
        POSTGRES__PORT="invalid-private-port",
        ANALYSIS__CONFIG_PATH="/missing-private-manifest",
    )
    return subprocess.run(
        [sys.executable, "-m", "app.analysis_worker", *arguments],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


def test_help_does_not_load_settings_or_run_preflight() -> None:
    result = invoke("--help")
    assert result.returncode == 0, result.stderr
    assert "--once" in result.stdout and "--poll-interval" in result.stdout
    assert "private-" not in result.stdout + result.stderr


def test_invalid_interval_is_rejected_before_startup() -> None:
    for interval in ("0", "-1", "nan", "inf", "no-number", "61"):
        result = invoke("--once", "--poll-interval", interval)
        assert result.returncode == 2, result.stderr  # noqa: PLR2004
        assert "must be finite and in [0.01, 60]" in result.stderr
        assert result.stdout == ""


def test_once_missing_manifest_fails_with_safe_error() -> None:
    environment = os.environ.copy()
    environment.update(
        ANALYSIS__CONFIG_PATH="/missing-private-manifest",
        LOGGING__FILE_ENABLED="false",
        LOGGING__FORMAT="json",
    )
    result = subprocess.run(
        [sys.executable, "-m", "app.analysis_worker", "--once"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 1
    assert result.stderr == ""
    assert '"event": "analysis_worker_failed"' in result.stdout
    assert '"error_type": "FrozenConfigurationError"' in result.stdout
    assert '"reason": "invalid_runtime_manifest"' in result.stdout
    assert "private-" not in result.stdout


def test_publish_and_unknown_options_are_not_supported() -> None:
    for arguments in (("publish",), ("--task-kind", "publish")):
        result = invoke(*arguments)
        assert result.returncode == 2  # noqa: PLR2004
        assert "unrecognized arguments" in result.stderr
        assert result.stdout == ""
        assert "private-" not in result.stderr


def test_sub_resolution_idle_delay_is_rejected_before_startup() -> None:
    result = invoke("--poll-interval", "1e-300")
    assert result.returncode == 2  # noqa: PLR2004
    assert "must be finite and in [0.01, 60]" in result.stderr
    assert result.stdout == ""
