import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]


def test_staging_runs_intake_recovery_and_forwards_read_token() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.staging.yml").read_text())
    assert (
        compose["services"]["api"]["environment"]["GITHUB__TOKEN"]
        == "${GITHUB__TOKEN:-}"
    )
    recovery = compose["services"]["webhook-worker"]
    assert recovery["command"] == ["python", "-m", "app.webhook_worker"]
    assert recovery["environment"]["GITHUB__TOKEN"] == "${GITHUB__TOKEN:-}"
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/deploy-staging.yml").read_text()
    )
    steps = [step for job in workflow["jobs"].values() for step in job.get("steps", [])]
    runtime = next(
        step for step in steps if step.get("name") == "Write runtime environment"
    )
    assert runtime["env"]["GITHUB_API_TOKEN"] == "${{ secrets.GITHUB__TOKEN }}"
    assert "append_optional 'GITHUB__TOKEN'" in runtime["run"]


def test_staging_routes_publication_credentials_only_to_api() -> None:
    """AC-DEPLOY-01: explicit publication configuration reaches only its API host."""
    services = yaml.safe_load((ROOT / "docker-compose.staging.yml").read_text())[
        "services"
    ]
    assert (
        services["api"]["environment"]["GITHUB__WRITE_TOKEN"]
        == "${GITHUB__WRITE_TOKEN:-}"
    )
    assert (
        services["api"]["environment"]["GITHUB__BOT_USER_ID"]
        == "${GITHUB__BOT_USER_ID:-}"
    )
    for name, service in services.items():
        if name != "api":
            assert "GITHUB__WRITE_TOKEN" not in service.get("environment", {})
            assert "GITHUB__BOT_USER_ID" not in service.get("environment", {})


def test_local_sleep_worker_does_not_inherit_api_credentials() -> None:
    """AC-DEPLOY-02: a deployment stub never receives the API's dotenv secrets."""
    services = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]
    assert services["api"]["env_file"] == ".env"
    assert services["worker"]["command"] == ["sleep", "infinity"]
    assert "env_file" not in services["worker"]


def test_deploy_writes_separate_publication_credentials() -> None:
    """AC-DEPLOY-03: deployment preserves distinct read and publication identity."""
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/deploy-staging.yml").read_text()
    )
    steps = [step for job in workflow["jobs"].values() for step in job.get("steps", [])]
    runtime = next(
        step for step in steps if step.get("name") == "Write runtime environment"
    )
    assert (
        runtime["env"]["GITHUB_PUBLISH_TOKEN"] == "${{ secrets.GITHUB__WRITE_TOKEN }}"
    )
    assert runtime["env"]["GITHUB_BOT_USER_ID"] == "${{ secrets.GITHUB__BOT_USER_ID }}"
    # Execute only the environment writer, before its SSH pipe, with synthetic data.
    writer = runtime["run"].split("} | sshpass -e ssh", 1)[0] + "}\n"
    result = subprocess.run(
        ["bash", "-euc", writer],
        env={
            "PATH": os.environ["PATH"],
            "API_IMAGE": "api:test",
            "UI_IMAGE": "ui:test",
            "WEB_PORT": "8080",
            "POSTGRES_PASSWORD": "test",
            "LLM_PRIMARY_PROVIDER": "ollama",
            "LLM_FALLBACK_ENABLED": "false",
            "EUROROUTER_BASE_URL": "",
            "EUROROUTER_MODEL": "",
            "EUROROUTER_API_KEYS": "",
            "OLLAMA_BASE_URL": "http://ollama:11434",
            "OLLAMA_MODEL": "test",
            "GITHUB_API_TOKEN": "read-only-synthetic",
            "GITHUB_PUBLISH_TOKEN": "write-synthetic",
            "GITHUB_BOT_USER_ID": "12345",
        },
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    entries = dict(line.split("=", 1) for line in result.stdout.splitlines())
    assert entries["GITHUB__TOKEN"] == '"read-only-synthetic"'
    assert entries["GITHUB__WRITE_TOKEN"] == '"write-synthetic"'
    assert entries["GITHUB__BOT_USER_ID"] == '"12345"'


@pytest.mark.skipif(
    shutil.which("docker") is None or os.environ.get("DMC268_RUN_DOCKER_TESTS") != "1",
    reason="set DMC268_RUN_DOCKER_TESTS=1 with Docker running",
)
@pytest.mark.parametrize(
    "value", ["literal-$UNDEFINED", "quote'and\\slash", " spaces # comment "]
)
@pytest.mark.parametrize("variable", ["GITHUB__TOKEN", "GITHUB__WRITE_TOKEN"])
def test_runtime_secret_survives_compose_env_file(
    tmp_path: Path, value: str, variable: str
) -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/deploy-staging.yml").read_text()
    )
    steps = [step for job in workflow["jobs"].values() for step in job.get("steps", [])]
    runtime = next(
        step for step in steps if step.get("name") == "Write runtime environment"
    )
    run = runtime["run"]
    formatter = run[run.index("append_optional() {") : run.index("\n\n{")]
    result = subprocess.run(
        ["bash", "-euc", formatter + f'\nappend_optional {variable} "$TEST_SECRET"'],
        env=os.environ | {"TEST_SECRET": value},
        capture_output=True,
        text=True,
        check=True,
    )
    env_file = tmp_path / "test.env"
    env_file.write_text(
        "API_IMAGE=postgres:18-alpine\nUI_IMAGE=ui:test\nPOSTGRES__USER=test\n"
        "POSTGRES__PASSWORD=test\nPOSTGRES__DB=test\n" + result.stdout
    )
    environment = {
        key: val
        for key, val in os.environ.items()
        if key
        not in {
            "GITHUB__TOKEN",
            "GITHUB__WRITE_TOKEN",
            "GITHUB__BOT_USER_ID",
            "API_IMAGE",
            "UI_IMAGE",
            "POSTGRES__USER",
            "POSTGRES__PASSWORD",
            "POSTGRES__DB",
        }
    }
    command = [
        "docker",
        "compose",
        "-p",
        "dmc268-secret-probe",
        "--env-file",
        str(env_file),
        "-f",
        str(ROOT / "docker-compose.staging.yml"),
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
                "api",
            ],
            env=environment,
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        actual = dict(
            line.split("=", 1) for line in rendered.stdout.splitlines() if "=" in line
        )
        assert actual[variable] == value
    finally:
        subprocess.run(
            [*command, "down", "--remove-orphans"],
            env=environment,
            capture_output=True,
            check=True,
            timeout=30,
        )
