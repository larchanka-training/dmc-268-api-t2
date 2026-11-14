"""Execute deployment shell with fakes only at OS command boundaries."""

# ruff: noqa: PLR2004 -- literal exit codes and private file mode are the contract

import os
import subprocess
from pathlib import Path

import pytest
from deploy_remote_fakes import install_deploy_remote_fakes
from test_analysis_deployment import workflow_step, writer_environment


def test_enabled_writer_keeps_active_configuration_unchanged(tmp_path: Path) -> None:
    active = tmp_path / ".env.staging"
    active.write_text("API_IMAGE=api:previous\nSIMULATION__ENABLED=false\n")
    binary = tmp_path / "bin"
    binary.mkdir()
    sshpass = binary / "sshpass"
    sshpass.write_text('#!/bin/bash\nexec bash --noprofile --norc -c "${@: -1}"\n')
    sshpass.chmod(0o755)
    result = subprocess.run(
        [
            "bash",
            "--noprofile",
            "--norc",
            "-euc",
            workflow_step("Write runtime environment")["run"],
        ],
        env=writer_environment()
        | {
            "PATH": f"{binary}:{os.environ['PATH']}",
            "HOME": os.environ["HOME"],
            "DEPLOY_PATH": str(tmp_path),
            "ENV_FILE": active.name,
            "ENV_NAME": "staging",
            "SIMULATION_ENABLED": "true",
            "SSH_USER": "synthetic",
            "SSH_HOST": "synthetic.invalid",
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert active.read_text() == "API_IMAGE=api:previous\nSIMULATION__ENABLED=false\n"
    candidate = tmp_path / ".env.staging.simulation-candidate"
    assert "SIMULATION__ENABLED=true\n" in candidate.read_text()
    assert candidate.stat().st_mode & 0o777 == 0o600


def deploy(
    tmp_path: Path, **overrides: str
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    active = tmp_path / ".env.staging"
    active.write_text(
        "API_IMAGE=api:previous\nUI_IMAGE=ui:previous\nSIMULATION__ENABLED=false\nCOMPOSE_PROFILES=\n"
    )
    (tmp_path / ".env.staging.simulation-candidate").write_text(
        "API_IMAGE=api:selected\nUI_IMAGE=ui:selected\nSIMULATION__ENABLED=true\nCOMPOSE_PROFILES=\n"
    )
    binary = tmp_path / "bin"
    install_deploy_remote_fakes(binary)
    docker = binary / "docker"
    docker.write_text(
        docker.read_text().replace(
            'if [[ "$1" == compose &&',
            'if [[ "$1 $2" == "volume inspect" ]]; then\n'
            'exit "${VOLUME_FAILURE:-0}"; fi\n'
            'if [[ "$*" == *"scripts.simulation_readiness"* ]]; then\n'
            'exit "${SIMULATION_PREFLIGHT_FAILURE:-0}"; fi\n'
            'if [[ "$*" == *"--force-recreate --no-deps api"* ]]; then\n'
            'exit "${API_START_FAILURE:-0}"; fi\n'
            'if [[ "$1" == compose &&',
            1,
        )
    )
    for name in ("sudo",):
        (binary / name).write_text("#!/bin/sh\nexit 0\n")
        (binary / name).chmod(0o755)
    backup = tmp_path / "backup-postgres.sh"
    backup.write_text("#!/bin/sh\nexit 0\n")
    backup.chmod(0o755)
    remote = (
        workflow_step("Deploy application")["run"]
        .split("<<'REMOTE'\n", 1)[1]
        .split("\nREMOTE", 1)[0]
    )
    trace = tmp_path / "trace"
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-euc", remote],
        env={
            "PATH": f"{binary}:{os.environ['PATH']}",
            "DEPLOY_TRACE": str(trace),
            "DEPLOY_PATH": str(tmp_path),
            "ENV_FILE": active.name,
            "ENV_NAME": "staging",
            "COMPOSE_PROJECT": "dmc-268-t2-staging",
            "WEB_PORT": "80",
            "SIMULATION_ENABLED": "true",
            "SIMULATION_ACTIVATION_APPROVED": "true",
            "MANIFEST_SHA256": "a" * 64,
            "INSTALLER_SHA256": "b" * 64,
        }
        | overrides,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return result, trace.read_text().splitlines() if trace.exists() else []


@pytest.mark.parametrize("defect", ["approval", "project", "volume"])
def test_activation_requires_prepared_staging_storage_and_authorization(
    tmp_path: Path, defect: str
) -> None:
    result, calls = deploy(
        tmp_path,
        **{
            "approval": {"SIMULATION_ACTIVATION_APPROVED": "false"},
            "project": {"COMPOSE_PROJECT": "dmc-268-t2-develop"},
            "volume": {"VOLUME_FAILURE": "1"},
        }[defect],
    )
    assert result.returncode != 0
    assert not any(" up " in call for call in calls)
    assert not any("volume create" in call for call in calls)
    assert "SIMULATION__ENABLED=false" in (tmp_path / ".env.staging").read_text()


def test_readiness_failure_never_promotes_candidate_or_starts_api(
    tmp_path: Path,
) -> None:
    result, calls = deploy(tmp_path, SIMULATION_PREFLIGHT_FAILURE="42")
    assert result.returncode == 42
    assert not any("--force-recreate --no-deps api" in call for call in calls)
    assert "SIMULATION__ENABLED=false" in (tmp_path / ".env.staging").read_text()
    assert not (tmp_path / ".env.staging.simulation-candidate").exists()


def test_success_promotes_only_after_readiness_then_uses_active_env(
    tmp_path: Path,
) -> None:
    result, calls = deploy(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "SIMULATION__ENABLED=true" in (tmp_path / ".env.staging").read_text()
    assert not (tmp_path / ".env.staging.simulation-candidate").exists()
    preflight = next(
        i for i, call in enumerate(calls) if "scripts.simulation_readiness" in call
    )
    api = next(
        i for i, call in enumerate(calls) if "--force-recreate --no-deps api" in call
    )
    assert preflight < api
    assert "--env-file .env.staging " in calls[api]


def test_failure_after_promotion_restores_previous_configuration(
    tmp_path: Path,
) -> None:
    result, calls = deploy(tmp_path, API_START_FAILURE="43")
    assert result.returncode == 43
    assert any("scripts.simulation_readiness" in call for call in calls)
    active = (tmp_path / ".env.staging").read_text()
    assert "SIMULATION__ENABLED=false" in active
    assert "API_IMAGE=api:previous" in active
    assert not (tmp_path / ".env.staging.simulation-candidate").exists()
