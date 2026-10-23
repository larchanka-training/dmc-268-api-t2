"""Staging analysis is explicitly activated through the existing Compose seam."""

import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).parents[1]
PREFLIGHT_FAILURE = 42


def workflow_step(name: str) -> dict[str, Any]:
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/deploy-staging.yml").read_text()
    )
    return next(s for s in workflow["jobs"]["deploy"]["steps"] if s["name"] == name)


@pytest.mark.parametrize(
    "step_name", ["Validate deployment configuration", "Write runtime environment"]
)
def test_eurorouter_key_prefers_repository_override_to_organization_fallback(
    step_name: str,
) -> None:
    assert workflow_step(step_name)["env"]["EUROROUTER_API_KEYS"] == (
        "${{ secrets.STAGING_EUROROUTER_API_KEYS || secrets.AI_DMC268_T2 }}"
    )


def test_eurorouter_endpoint_prefers_repository_to_organization_fallback() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/deploy-staging.yml").read_text()
    )
    assert workflow["jobs"]["deploy"]["env"]["EUROROUTER_BASE_URL"] == (
        "${{ vars.STAGING_EUROROUTER_BASE_URL || vars.AI_DMC268_URL }}"
    )


def writer_environment() -> dict[str, str]:
    return {
        "PATH": os.environ["PATH"],
        "API_IMAGE": "api:test",
        "UI_IMAGE": "ui:test",
        "WEB_PORT": "8080",
        "POSTGRES_PASSWORD": "synthetic-test",
        "LLM_PRIMARY_PROVIDER": "eurorouter",
        "LLM_FALLBACK_ENABLED": "false",
        "EUROROUTER_BASE_URL": "https://llm.invalid/v1",
        "EUROROUTER_MODEL": "test-model",
        "EUROROUTER_API_KEYS": "synthetic-test-key",
        "OLLAMA_BASE_URL": "http://ollama:11434",
        "OLLAMA_MODEL": "",
    }


def compose_config(
    *,
    enabled: bool = False,
    runtime_directory: str = "/opt/dmc-268-t2-runtime/analysis-runtime",
) -> dict[str, Any]:
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(ROOT / "docker-compose.staging.yml"),
            "config",
            "--format",
            "json",
        ],
        env={
            "PATH": os.environ["PATH"],
            "API_IMAGE": "api:synthetic-test",
            "UI_IMAGE": "ui:synthetic-test",
            "POSTGRES__USER": "test",
            "POSTGRES__PASSWORD": "synthetic-test-password",
            "POSTGRES__DB": "test",
            "COMPOSE_PROFILES": "analysis" if enabled else "",
            "ANALYSIS_RUNTIME_DIR": runtime_directory,
            "ANALYSIS__CONFIG_PATH": "/app/runtime/analysis-runtime.v1.json"
            if enabled
            else "",
        },
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    return json.loads(result.stdout)  # type: ignore[no-any-return]


def test_analysis_worker_is_opt_in_and_uses_existing_continuous_cli() -> None:
    services = compose_config()["services"]
    assert "worker" not in services
    assert {"api", "webhook-worker", "web", "db", "redis"} <= services.keys()
    worker = compose_config(enabled=True)["services"]["worker"]
    assert worker["command"] == ["python", "-m", "app.analysis_worker"]
    assert worker["restart"] == "on-failure:3"
    assert worker["environment"]["REDIS__URL"] == "redis://redis:6379/0"
    assert worker["depends_on"]["db"]["condition"] == "service_healthy"
    assert worker["depends_on"]["redis"]["condition"] == "service_healthy"
    raw = yaml.safe_load((ROOT / "docker-compose.staging.yml").read_text())
    assert raw["services"]["worker"]["environment"]["GITHUB__TOKEN"] == (
        "${GITHUB__TOKEN:-}"
    )
    assert "GITHUB__WRITE_TOKEN" not in worker["environment"]


def test_outbox_dispatcher_is_opt_in_and_continuously_publishes_to_same_redis() -> None:
    assert "outbox-dispatcher" not in compose_config()["services"]
    services = compose_config(enabled=True)["services"]
    dispatcher = services["outbox-dispatcher"]
    assert dispatcher["command"] == ["python", "-m", "app.outbox_dispatcher"]
    assert dispatcher["restart"] == "unless-stopped"
    for name in ("POSTGRES__HOST", "POSTGRES__DB", "REDIS__URL"):
        assert (
            dispatcher["environment"][name] == services["worker"]["environment"][name]
        )
    assert dispatcher["depends_on"]["db"]["condition"] == "service_healthy"
    assert dispatcher["depends_on"]["redis"]["condition"] == "service_healthy"
    assert "GITHUB__TOKEN" not in dispatcher["environment"]
    assert "LLM__EUROROUTER__API_KEYS" not in dispatcher["environment"]


def test_api_and_worker_share_read_only_trusted_manifest_when_enabled(
    tmp_path: Path,
) -> None:
    services = compose_config(enabled=True)["services"]
    for name in ("api", "worker"):
        service = services[name]
        assert service["environment"]["ANALYSIS__CONFIG_PATH"] == (
            "/app/runtime/analysis-runtime.v1.json"
        )
        mount = next(v for v in service["volumes"] if v["target"] == "/app/runtime")
        assert mount["type"] == "bind"
        assert mount["source"] == "/opt/dmc-268-t2-runtime/analysis-runtime"
        assert mount["read_only"] is True
    assert (
        compose_config()["services"]["api"]["environment"]["ANALYSIS__CONFIG_PATH"]
        == ""
    )
    develop = compose_config(runtime_directory="./analysis-runtime")["services"]["api"]
    mount = next(v for v in develop["volumes"] if v["target"] == "/app/runtime")
    assert mount["source"] == str(ROOT / "analysis-runtime")
    assert mount["read_only"] is True
    assert develop["environment"]["ANALYSIS__CONFIG_PATH"] == ""
    if os.environ.get("DMC268_RUN_DOCKER_TESTS") != "1":
        return
    runtime_dir = tmp_path / "analysis-runtime"
    runtime_dir.mkdir()
    (runtime_dir / "analysis-runtime.v1.json").write_text("synthetic trusted manifest")
    command = [
        "docker",
        "compose",
        "-p",
        "dmc268-analysis-manifest-probe",
        "--project-directory",
        str(tmp_path),
        "-f",
        str(ROOT / "docker-compose.staging.yml"),
        "--profile",
        "analysis",
    ]
    environment = {
        "PATH": os.environ["PATH"],
        "API_IMAGE": "python:3.14-slim",
        "UI_IMAGE": "ui:test",
        "POSTGRES__USER": "test",
        "POSTGRES__PASSWORD": "synthetic-test",
        "POSTGRES__DB": "test",
        "ANALYSIS__CONFIG_PATH": "/app/runtime/analysis-runtime.v1.json",
        "ANALYSIS_RUNTIME_DIR": str(runtime_dir),
    }
    probe = (
        "import errno, os\nfrom pathlib import Path\n"
        'path = Path(os.environ["ANALYSIS__CONFIG_PATH"])\n'
        'assert path.read_text() == "synthetic trusted manifest"\n'
        'try:\n    path.write_text("tampered")\n'
        "except OSError as error:\n    assert error.errno == errno.EROFS\n"
        'else:\n    raise AssertionError("manifest must be read-only")\n'
    )
    try:
        for name in ("api", "worker"):
            result = subprocess.run(
                [
                    *command,
                    "run",
                    "--rm",
                    "--no-deps",
                    "--entrypoint",
                    "python",
                    name,
                    "-c",
                    probe,
                ],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            assert result.returncode == 0, result.stderr
    finally:
        subprocess.run(
            [*command, "down", "--remove-orphans", "--volumes"],
            env=environment,
            capture_output=True,
            check=True,
            timeout=30,
        )


@pytest.mark.parametrize(
    ("target", "enabled", "profile"),
    [
        ("staging", "false", ""),
        ("develop", "true", ""),
        ("staging", "true", "analysis"),
    ],
)
def test_deploy_activates_analysis_only_for_explicitly_enabled_staging(
    tmp_path: Path, target: str, enabled: str, profile: str
) -> None:
    runtime = workflow_step("Write runtime environment")
    writer = runtime["run"].split("} | sshpass -e ssh", 1)[0] + "}\n"
    result = subprocess.run(
        ["bash", "-euc", writer],
        env=writer_environment()
        | {
            "ENV_NAME": target,
            "ANALYSIS_ENABLED": enabled,
        },
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    entries = dict(line.split("=", 1) for line in result.stdout.splitlines())
    assert entries["ANALYSIS_RUNTIME_DIR"] == (
        "/opt/dmc-268-t2-runtime/analysis-runtime"
        if target == "staging"
        else "./analysis-runtime"
    )
    assert entries["COMPOSE_PROFILES"] == profile
    assert entries["ANALYSIS__CONFIG_PATH"] == (
        "/app/runtime/analysis-runtime.v1.json" if profile else ""
    )
    (tmp_path / ".env.test").write_text(result.stdout)
    # Substitute only OS command boundaries; execute the deployment shell itself.
    binary = tmp_path / "bin"
    binary.mkdir()
    (binary / "docker").write_text(
        '#!/bin/bash\nprintf "%s\\n" "$*" >> "$DEPLOY_TRACE"\n'
        'if [[ "$1" == inspect ]]; then\n'
        '  if [[ "$*" == *Health* ]]; then echo healthy; else echo running; fi\n'
        "fi\n"
    )
    (binary / "curl").write_text("#!/bin/sh\nexit 0\n")
    (binary / "sudo").write_text("#!/bin/sh\nexit 0\n")
    for executable in binary.iterdir():
        executable.chmod(0o755)
    remote = workflow_step("Deploy application")["run"].split("<<'REMOTE'\n", 1)[1]
    remote = remote.split("\nREMOTE", 1)[0]
    trace = tmp_path / "trace"
    subprocess.run(
        ["bash", "-euc", remote],
        env={
            "PATH": f"{binary}:{os.environ['PATH']}",
            "DEPLOY_TRACE": str(trace),
            "MANIFEST_SHA256": "a" * 64,
            "INSTALLER_SHA256": "b" * 64,
            "DEPLOY_PATH": str(tmp_path),
            "ENV_FILE": ".env.test",
            "ENV_NAME": target,
            "WEB_PORT": "8080",
            "COMPOSE_PROJECT": f"dmc-268-t2-{target}",
        },
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    calls = trace.read_text().splitlines()
    starts = [c.split(" up -d ", 1)[1].split() for c in calls if " up -d " in c]
    assert any("webhook-worker" in c for c in starts)
    for service in ("worker", "outbox-dispatcher"):
        assert any(service in c for c in starts) == bool(profile)
    assert all(
        f"-p dmc-268-t2-{target} " in c for c in calls if c.startswith("compose ")
    )


@pytest.mark.skipif(
    shutil.which("docker") is None or os.environ.get("DMC268_RUN_DOCKER_TESTS") != "1",
    reason="set DMC268_RUN_DOCKER_TESTS=1 with Docker running",
)
def test_llm_secret_survives_runtime_writer_and_compose(tmp_path: Path) -> None:
    runtime = workflow_step("Write runtime environment")
    writer = runtime["run"].split("} | sshpass -e ssh", 1)[0] + "}\n"
    secret = "synthetic-$UNDEFINED-key"
    result = subprocess.run(
        ["bash", "-euc", writer],
        env=writer_environment()
        | {
            "EUROROUTER_API_KEYS": secret,
            "API_IMAGE": "postgres:18-alpine",
        },
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    env_file = tmp_path / ".env.test"
    env_file.write_text(result.stdout)
    command = [
        "docker",
        "compose",
        "-p",
        "dmc268-analysis-secret-probe",
        "--env-file",
        str(env_file),
        "-f",
        str(ROOT / "docker-compose.staging.yml"),
        "--profile",
        "analysis",
    ]
    try:
        rendered = subprocess.run(
            [
                *command,
                "run",
                "--rm",
                "--no-deps",
                "--entrypoint",
                "/usr/bin/env",
                "worker",
            ],
            env={"PATH": os.environ["PATH"]},
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        actual = dict(
            line.split("=", 1) for line in rendered.stdout.splitlines() if "=" in line
        )
        assert actual["LLM__EUROROUTER__API_KEYS"] == secret
    finally:
        subprocess.run(
            [*command, "down", "--remove-orphans"],
            env={"PATH": os.environ["PATH"]},
            capture_output=True,
            check=True,
            timeout=30,
        )


@pytest.mark.skipif(
    shutil.which("docker") is None or os.environ.get("DMC268_RUN_DOCKER_TESTS") != "1",
    reason="set DMC268_RUN_DOCKER_TESTS=1 with Docker running",
)
def test_deploy_preflight_executes_stdin_and_propagates_failure(tmp_path: Path) -> None:
    deployment = workflow_step("Deploy application")["run"]
    preflight_command = next(
        line for line in deployment.splitlines() if "<<'PREFLIGHT'" in line
    )
    arguments = shlex.split(preflight_command.split("<<", 1)[0])[1:]
    env_file = tmp_path / ".env.test"
    env_file.write_text(
        "API_IMAGE=python:3.14-slim\nUI_IMAGE=ui:test\nPOSTGRES__USER=test\n"
        "POSTGRES__PASSWORD=synthetic-test\nPOSTGRES__DB=test\n"
    )
    command = [
        "docker",
        "compose",
        "-p",
        "dmc268-analysis-preflight-probe",
        "--env-file",
        str(env_file),
        "-f",
        str(ROOT / "docker-compose.staging.yml"),
        "--profile",
        "analysis",
    ]
    try:
        result = subprocess.run(
            [*command, *arguments],
            input="raise SystemExit(42)\n",
            env={"PATH": os.environ["PATH"]},
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert result.returncode == PREFLIGHT_FAILURE
    finally:
        subprocess.run(
            [*command, "down", "--remove-orphans"],
            env={"PATH": os.environ["PATH"]},
            capture_output=True,
            check=True,
            timeout=30,
        )
