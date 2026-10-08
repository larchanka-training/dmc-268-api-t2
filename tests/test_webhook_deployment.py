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
    assert compose["services"]["worker"]["command"] == ["sleep", "infinity"]
    assert "GITHUB__TOKEN" not in compose["services"]["worker"]["environment"]
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/deploy-staging.yml").read_text()
    )
    steps = [step for job in workflow["jobs"].values() for step in job.get("steps", [])]
    runtime = next(
        step for step in steps if step.get("name") == "Write runtime environment"
    )
    assert runtime["env"]["GITHUB_API_TOKEN"] == "${{ secrets.GITHUB__TOKEN }}"
    assert "append_optional 'GITHUB__TOKEN'" in runtime["run"]


@pytest.mark.skipif(
    shutil.which("docker") is None or os.environ.get("DMC268_RUN_DOCKER_TESTS") != "1",
    reason="set DMC268_RUN_DOCKER_TESTS=1 with Docker running",
)
@pytest.mark.parametrize(
    "value", ["literal-$UNDEFINED", "quote'and\\slash", " spaces # comment "]
)
def test_runtime_secret_survives_compose_env_file(tmp_path: Path, value: str) -> None:
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
        ["bash", "-euc", formatter + '\nappend_optional GITHUB__TOKEN "$TEST_SECRET"'],
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
        assert actual["GITHUB__TOKEN"] == value
    finally:
        subprocess.run(
            [*command, "down", "--remove-orphans"],
            env=environment,
            capture_output=True,
            check=True,
            timeout=30,
        )
