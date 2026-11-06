"""Publication uses its own opt-in process, credentials and deployment gate."""

import os
import subprocess
from pathlib import Path

import pytest
import yaml
from deploy_remote_fakes import install_deploy_remote_fakes
from test_analysis_deployment import (
    ROOT,
    compose_config,
    workflow_step,
    writer_environment,
)

PREFLIGHT_FAILURE = 42
START_FAILURE = 43


def test_publication_worker_is_opt_in_and_uses_only_publication_credentials() -> None:
    assert "publication-worker" not in compose_config()["services"]
    services = compose_config(profiles="publication")["services"]
    worker = services["publication-worker"]
    assert worker["image"] == services["api"]["image"]
    assert worker["command"] == ["python", "-m", "app.publication_worker"]
    assert worker["restart"] == "unless-stopped"
    assert worker["depends_on"]["db"]["condition"] == "service_healthy"
    assert worker["depends_on"]["redis"]["condition"] == "service_healthy"
    assert worker["networks"] == services["api"]["networks"]
    for key in ("POSTGRES__HOST", "POSTGRES__DB", "REDIS__URL"):
        assert worker["environment"][key] == services["api"]["environment"][key]
    assert {"GITHUB__WRITE_TOKEN", "GITHUB__BOT_USER_ID"} <= worker[
        "environment"
    ].keys()
    assert not any(
        key.startswith(("LLM__", "AUTH__", "ANALYSIS__")) or key == "GITHUB__TOKEN"
        for key in worker["environment"]
    )
    assert not worker.get("volumes")
    raw = yaml.safe_load((ROOT / "docker-compose.staging.yml").read_text())
    assert raw["services"]["publication-worker"]["profiles"] == ["publication"]


def runtime_environment(
    *, analysis: str = "false", publication: str = "false", target: str = "staging"
) -> subprocess.CompletedProcess[str]:
    writer = workflow_step("Write runtime environment")["run"]
    writer = writer.split("} | sshpass -e ssh", 1)[0] + "}\n"
    return subprocess.run(
        ["bash", "-euc", writer],
        env=writer_environment()
        | {
            "ENV_NAME": target,
            "ANALYSIS_ENABLED": analysis,
            "PUBLICATION_ENABLED": publication,
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


def test_publication_can_be_selected_without_analysis() -> None:
    result = runtime_environment(publication="true")
    assert result.returncode == 0, result.stderr
    entries = dict(line.split("=", 1) for line in result.stdout.splitlines())
    assert entries["COMPOSE_PROFILES"] == "publication"
    assert entries["ANALYSIS__CONFIG_PATH"] == ""


def test_default_runtime_profiles_keep_both_workers_disabled() -> None:
    result = runtime_environment()
    assert result.returncode == 0, result.stderr
    entries = dict(line.split("=", 1) for line in result.stdout.splitlines())
    assert entries["COMPOSE_PROFILES"] == ""
    assert entries["ANALYSIS__CONFIG_PATH"] == ""


@pytest.mark.parametrize(
    "step_name", ["Validate deployment configuration", "Write runtime environment"]
)
def test_invalid_publication_flag_is_rejected_before_deployment(step_name: str) -> None:
    if step_name == "Write runtime environment":
        result = runtime_environment(publication="invalid-synthetic-flag")
    else:
        result = subprocess.run(
            ["bash", "-euc", workflow_step(step_name)["run"]],
            env=writer_environment()
            | {
                "SSH_HOST": "synthetic.invalid",
                "SSH_USER": "synthetic-user",
                "SSH_PASSWORD": "synthetic-password",
                "API_SHA": "a" * 40,
                "UI_SHA": "",
                "PUBLICATION_ENABLED": "invalid-synthetic-flag",
            },
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    assert result.returncode != 0
    assert "STAGING_PUBLICATION_ENABLED must be true or false" in result.stderr
    assert result.stdout == ""


def deploy_remote(
    tmp_path: Path,
    *,
    analysis: str,
    publication: str,
    overrides: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    written = runtime_environment(analysis=analysis, publication=publication)
    assert written.returncode == 0, written.stderr
    (tmp_path / ".env.test").write_text(written.stdout)
    binary = tmp_path / "bin"
    install_deploy_remote_fakes(binary)
    for name in ("sudo", "backup-postgres.sh"):
        path = binary / name if name == "sudo" else tmp_path / name
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
    remote = workflow_step("Deploy application")["run"].split("<<'REMOTE'\n", 1)[1]
    remote = remote.split("\nREMOTE", 1)[0]
    trace = tmp_path / "trace"
    result = subprocess.run(
        ["bash", "-euc", remote],
        env={
            "PATH": f"{binary}:{os.environ['PATH']}",
            "DEPLOY_TRACE": str(trace),
            "MANIFEST_SHA256": "a" * 64,
            "INSTALLER_SHA256": "b" * 64,
            "DEPLOY_PATH": str(tmp_path),
            "ENV_FILE": ".env.test",
            "ENV_NAME": "staging",
            "WEB_PORT": "8080",
            "COMPOSE_PROJECT": "dmc-268-t2-staging",
        }
        | (overrides or {}),
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    return result, trace.read_text().splitlines()


def test_combined_profiles_preserve_analysis_processes(tmp_path: Path) -> None:
    result, calls = deploy_remote(tmp_path, analysis="true", publication="true")
    assert result.returncode == 0, result.stderr
    starts = [line for line in calls if " up -d " in line]
    assert any("outbox-dispatcher" in line and "worker" in line for line in starts)
    assert any("webhook-worker" in line for line in starts)


def test_publication_deploy_preflights_before_start_without_analysis(
    tmp_path: Path,
) -> None:
    result, calls = deploy_remote(tmp_path, analysis="false", publication="true")
    assert result.returncode == 0, result.stderr
    stop = next(
        index for index, line in enumerate(calls) if " stop publication-worker" in line
    )
    migration = next(
        index for index, line in enumerate(calls) if "alembic upgrade head" in line
    )
    preflight = next(
        index
        for index, line in enumerate(calls)
        if "publication-worker python -m app.publication_worker --check-config" in line
    )
    start = next(
        index
        for index, line in enumerate(calls)
        if " up -d " in line and "publication-worker" in line
    )
    assert stop < migration < preflight < start
    selected = calls[start].split(" up -d ", 1)[1].split()
    assert "publication-worker" in selected
    assert "worker" not in selected and "outbox-dispatcher" not in selected
    assert all(
        "-p dmc-268-t2-staging " in line
        for line in calls
        if line.startswith("compose ")
    )


def test_publication_image_must_match_selected_api_pin(tmp_path: Path) -> None:
    result, _ = deploy_remote(
        tmp_path,
        analysis="false",
        publication="true",
        overrides={"PUBLICATION_IMAGE_MISMATCH": "1"},
    )
    assert result.returncode != 0
    assert "running publication-worker image does not match API_IMAGE" in result.stdout


def test_missing_publisher_at_image_check_has_explicit_diagnostics(
    tmp_path: Path,
) -> None:
    result, calls = deploy_remote(
        tmp_path,
        analysis="false",
        publication="true",
        overrides={"PUBLICATION_DISAPPEARS": "1"},
    )
    assert result.returncode != 0
    assert "publication-worker is not running" in result.stdout
    assert any(" logs --tail=100 publication-worker" in line for line in calls)


def test_failed_offline_preflight_never_starts_publisher(tmp_path: Path) -> None:
    result, calls = deploy_remote(
        tmp_path,
        analysis="false",
        publication="true",
        overrides={"PUBLICATION_PREFLIGHT_FAILURE": str(PREFLIGHT_FAILURE)},
    )
    assert result.returncode == PREFLIGHT_FAILURE
    assert not any(" up -d " in line and "publication-worker" in line for line in calls)


def test_failed_publisher_start_fails_deployment(tmp_path: Path) -> None:
    result, _ = deploy_remote(
        tmp_path,
        analysis="false",
        publication="true",
        overrides={"PUBLICATION_START_FAILURE": str(START_FAILURE)},
    )
    assert result.returncode == START_FAILURE
    assert "Starting web..." not in result.stdout


def test_disabled_publisher_is_stopped_before_migration_without_restart(
    tmp_path: Path,
) -> None:
    result, calls = deploy_remote(tmp_path, analysis="false", publication="false")
    assert result.returncode == 0, result.stderr
    stopped = next(
        i for i, line in enumerate(calls) if " stop publication-worker" in line
    )
    migrated = next(i for i, line in enumerate(calls) if "alembic upgrade head" in line)
    assert stopped < migrated
    assert not any("publication-worker python" in line for line in calls)
    assert not any(" up -d " in line and "publication-worker" in line for line in calls)


@pytest.mark.parametrize(
    ("target", "analysis", "publication", "profiles"),
    [
        ("staging", "false", "false", ""),
        ("staging", "true", "false", "analysis"),
        ("staging", "false", "true", "publication"),
        ("staging", "true", "true", "analysis,publication"),
        ("develop", "true", "true", ""),
    ],
)
def test_runtime_profile_matrix(
    target: str, analysis: str, publication: str, profiles: str
) -> None:
    result = runtime_environment(
        analysis=analysis, publication=publication, target=target
    )
    assert result.returncode == 0, result.stderr
    entries = dict(line.split("=", 1) for line in result.stdout.splitlines())
    assert entries["COMPOSE_PROFILES"] == profiles
    assert bool(entries["ANALYSIS__CONFIG_PATH"]) == ("analysis" in profiles)
