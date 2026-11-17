"""Execute deployment shell with fakes only at OS command boundaries."""

# ruff: noqa: PLR2004 -- literal exit codes and private file mode are the contract

import os
import subprocess
from pathlib import Path

import pytest
import yaml
from deploy_remote_fakes import install_deploy_remote_fakes
from test_analysis_deployment import ROOT, workflow_step, writer_environment


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
    if not active.exists():
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
        docker.read_text()
        .replace(
            "# docker compose ... ps -q <service>",
            """# Persist service state so failure tests observe the final containers.
for service in api webhook-worker worker outbox-dispatcher publication-worker; do
  state_file="${DEPLOY_SERVICE_STATE}/${service}"
  if [[ "$1" == compose && " $* " == *" stop "* && " $* " == *" ${service} "* ]]; then
    echo exited > "$state_file"
  elif [[ "$1" == start && " $* " == *" cid-${service} "* ]]; then
    if [[ "$service" == webhook-worker ]]; then
      case "${WORKER_RECOVERY_FAILURE:-}" in
        start) exit 47 ;;
        not-running) exit 0 ;;
      esac
    fi
    echo running > "$state_file"
  elif [[ "$1" == compose && " $* " == *" up -d "* &&
          " $* " == *" ${service} "* ]]; then
    echo running > "$state_file"
  elif [[ "$1" == inspect && "$*" == *State.Status* &&
          "${@: -1}" == "cid-${service}" ]]; then
    if [[ "${WORKER_SNAPSHOT_INSPECT_FAILURE:-0}" == 1 &&
          "$service" == webhook-worker &&
          ! -f "${DEPLOY_SERVICE_STATE}/inspect-failed" ]]; then
      touch "${DEPLOY_SERVICE_STATE}/inspect-failed"
      exit 46
    fi
    if [[ "${ROLLBACK_INSPECT_FAILURE:-0}" == 1 && "$service" == api &&
          "$(cat "$state_file")" == exited ]]; then exit 45; fi
    cat "$state_file"
    exit 0
  fi
done
# docker compose ... ps -q <service>""",
        )
        .replace(
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
    service_state = tmp_path / "services"
    service_state.mkdir(exist_ok=True)
    for service in (
        "api",
        "webhook-worker",
        "worker",
        "outbox-dispatcher",
        "publication-worker",
    ):
        state_file = service_state / service
        if not state_file.exists():
            state_file.write_text("running\n")
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-euc", remote],
        env={
            "PATH": f"{binary}:{os.environ['PATH']}",
            "DEPLOY_TRACE": str(trace),
            "DEPLOY_SERVICE_STATE": str(service_state),
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
    assert not (tmp_path / ".env.staging.simulation-rollback").exists()
    for service in (
        "webhook-worker",
        "worker",
        "outbox-dispatcher",
        "publication-worker",
    ):
        assert (tmp_path / "services" / service).read_text() == "running\n"


@pytest.mark.parametrize("failure", ["start", "not-running"])
def test_failed_worker_recovery_retains_rollback_and_blocks_next_activation(
    tmp_path: Path, failure: str
) -> None:
    previous_env = (
        "API_IMAGE=api:previous\nUI_IMAGE=ui:previous\n"
        "SIMULATION__ENABLED=false\nCOMPOSE_PROFILES=\n"
    )
    result, calls = deploy(
        tmp_path,
        SIMULATION_PREFLIGHT_FAILURE="42",
        WORKER_RECOVERY_FAILURE=failure,
    )
    assert result.returncode != 0
    assert any("scripts.simulation_readiness" in call for call in calls)
    assert any(" stop webhook-worker" in call for call in calls)
    assert "start cid-webhook-worker" in calls
    assert "failed to restore previous worker cid-webhook-worker" in result.stderr
    assert (tmp_path / "services/webhook-worker").read_text() == "exited\n"
    assert (tmp_path / ".env.staging").read_text() == previous_env
    assert not (tmp_path / ".env.staging.simulation-candidate").exists()
    assert not any("--force-recreate --no-deps api" in call for call in calls)
    rollback = tmp_path / ".env.staging.simulation-rollback"
    assert rollback.is_dir()
    assert (rollback / "previous-env").read_text() == previous_env

    retry, all_calls = deploy(tmp_path)
    retry_calls = all_calls[len(calls) :]
    assert retry.returncode != 0
    assert ".env.staging.simulation-rollback" in retry.stderr
    assert "File exists" in retry.stderr
    assert not any(" stop " in call or " up " in call for call in retry_calls)
    assert not any(call.startswith("start ") for call in retry_calls)
    assert not any("scripts.simulation_readiness" in call for call in retry_calls)
    assert (tmp_path / "services/api").read_text() == "running\n"
    assert (tmp_path / "services/webhook-worker").read_text() == "exited\n"
    assert (tmp_path / ".env.staging").read_text() == previous_env
    assert (rollback / "previous-env").read_text() == previous_env


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


def test_unreadable_worker_state_aborts_before_stopping_consumers(
    tmp_path: Path,
) -> None:
    result, calls = deploy(
        tmp_path,
        WORKER_SNAPSHOT_INSPECT_FAILURE="1",
        SIMULATION_PREFLIGHT_FAILURE="42",
    )
    for service in (
        "webhook-worker",
        "worker",
        "outbox-dispatcher",
        "publication-worker",
    ):
        assert (tmp_path / "services" / service).read_text() == "running\n"
    assert not any(" stop " in call for call in calls)
    assert result.returncode == 46


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


def complete_workflow(tmp_path: Path) -> list[subprocess.CompletedProcess[str]]:
    binary = tmp_path / "bin"
    docker = binary / "docker"
    docker.write_text(
        docker.read_text().replace(
            "trace_file=",
            'if [[ "$*" == *"caddy validate"* ]]; then exit 44; fi\ntrace_file=',
            1,
        )
    )
    caddy = tmp_path / "caddy"
    caddy.mkdir()
    (caddy / "Caddyfile.new").write_text("synthetic config")
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/deploy-staging.yml").read_text()
    )
    after_deploy = False
    failed = False
    outcomes = []
    for step in workflow["jobs"]["deploy"]["steps"]:
        if step["name"] == "Deploy application":
            after_deploy = True
            continue
        if not after_deploy or (failed and "always()" not in step.get("if", "")):
            continue
        remote = step["run"].split("<<'REMOTE'\n", 1)[1].split("\nREMOTE", 1)[0]
        outcome = subprocess.run(
            [
                "bash",
                "--noprofile",
                "--norc",
                "-euc",
                remote.replace("/opt/caddy", str(caddy)),
            ],
            env={
                "PATH": f"{binary}:{os.environ['PATH']}",
                "DEPLOY_PATH": str(tmp_path),
                "ENV_FILE": ".env.staging",
                "ENV_NAME": "staging",
                "COMPOSE_PROJECT": "dmc-268-t2-staging",
                "DEPLOY_TRACE": str(tmp_path / "trace"),
                "DEPLOY_SERVICE_STATE": str(tmp_path / "services"),
                "DEPLOY_SUCCEEDED": "true",
                "WORKFLOW_SUCCEEDED": "false" if failed else "true",
                "ROLLBACK_INSPECT_FAILURE": os.environ.get(
                    "ROLLBACK_INSPECT_FAILURE", "0"
                ),
            },
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        failed = failed or outcome.returncode != 0
        outcomes.append(outcome)
    return outcomes


def test_late_caddy_failure_rolls_back_successfully_started_api(tmp_path: Path) -> None:
    result, _ = deploy(tmp_path)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "services/api").read_text() == "running\n"
    outcomes = complete_workflow(tmp_path)
    assert any(outcome.returncode != 0 for outcome in outcomes)
    assert outcomes[-1].returncode == 0, outcomes[-1].stderr
    assert (tmp_path / "services/api").read_text() == "exited\n"
    assert (tmp_path / ".env.staging").read_text() == (
        "API_IMAGE=api:previous\nUI_IMAGE=ui:previous\nSIMULATION__ENABLED=false\nCOMPOSE_PROFILES=\n"
    )


@pytest.mark.parametrize("late_failure", [False, True])
def test_unverifiable_rollback_retains_recovery_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, late_failure: bool
) -> None:
    monkeypatch.setenv("ROLLBACK_INSPECT_FAILURE", "1")
    result, _ = deploy(
        tmp_path,
        ROLLBACK_INSPECT_FAILURE="1",
        API_START_FAILURE="0" if late_failure else "43",
    )
    if late_failure:
        assert result.returncode == 0, result.stderr
        outcomes = complete_workflow(tmp_path)
        assert outcomes[-1].returncode != 0
    else:
        assert result.returncode != 0
    assert (tmp_path / ".env.staging.simulation-rollback/previous-env").is_file()
