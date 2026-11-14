"""Rule Playground uses the existing staging deployment contract."""

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml
from test_analysis_deployment import (
    ROOT,
    compose_config,
    workflow_step,
    writer_environment,
)


def runtime_environment(
    *,
    target: str = "staging",
    flag: str | None = None,
    analysis: str = "false",
    publication: str = "false",
) -> subprocess.CompletedProcess[str]:
    writer = workflow_step("Write runtime environment")["run"]
    writer = writer.split("} | sshpass -e ssh", 1)[0] + "}\n"
    environment = writer_environment() | {
        "ENV_NAME": target,
        "ANALYSIS_ENABLED": analysis,
        "PUBLICATION_ENABLED": publication,
    }
    if flag is not None:
        environment["SIMULATION_ENABLED"] = flag
    return subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-euc", writer],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


@pytest.mark.parametrize("target", ["staging", "develop"])
@pytest.mark.parametrize("flag", [None, "false"])
def test_absent_or_false_flag_is_explicitly_delivered_disabled(
    target: str,
    flag: str | None,
) -> None:
    result = runtime_environment(target=target, flag=flag)
    assert result.returncode == 0, result.stderr
    entries = dict(line.split("=", 1) for line in result.stdout.splitlines())
    assert entries.get("SIMULATION__ENABLED") == "false"


def test_simulation_variable_is_independent_and_default_disabled() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/deploy-staging.yml").read_text()
    )
    assert workflow["jobs"]["deploy"]["env"].get("SIMULATION_ENABLED") == (
        "${{ vars.STAGING_SIMULATION_ENABLED || 'false' }}"
    )


@pytest.mark.parametrize("target,expected", [("staging", "true"), ("develop", "false")])
def test_true_flag_is_delivered_only_to_staging(target: str, expected: str) -> None:
    result = runtime_environment(target=target, flag="true")
    assert result.returncode == 0, result.stderr
    entries = dict(line.split("=", 1) for line in result.stdout.splitlines())
    assert entries["SIMULATION__ENABLED"] == expected


@pytest.mark.parametrize("target", ["staging", "develop"])
@pytest.mark.parametrize("flag", ["invalid-synthetic-flag", "TRUE", "1", "false\ntrue"])
@pytest.mark.parametrize(
    "step_name", ["Validate deployment configuration", "Write runtime environment"]
)
def test_invalid_simulation_flag_fails_before_output_without_echoing_value(
    target: str,
    flag: str,
    step_name: str,
) -> None:
    if step_name == "Write runtime environment":
        result = runtime_environment(target=target, flag=flag)
    else:
        result = subprocess.run(
            [
                "/bin/bash",
                "--noprofile",
                "--norc",
                "-euc",
                workflow_step(step_name)["run"],
            ],
            env=writer_environment()
            | {
                "ENV_NAME": target,
                "SIMULATION_ENABLED": flag,
                "SSH_HOST": "synthetic.invalid",
                "SSH_USER": "synthetic-user",
                "SSH_PASSWORD": "synthetic-password",
                "API_SHA": "a" * 40,
                "UI_SHA": "",
            },
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    assert result.returncode != 0
    assert result.stdout == ""
    assert (
        result.stderr == "::error::STAGING_SIMULATION_ENABLED must be true or false\n"
    )


def test_compose_delivers_explicit_default_disabled_flag_only_to_api() -> None:
    services = compose_config(profiles="analysis,publication")["services"]
    assert services["api"]["environment"].get("SIMULATION__ENABLED") == "false"
    assert all(
        "SIMULATION__ENABLED" not in service.get("environment", {})
        for name, service in services.items()
        if name != "api"
    )


@pytest.mark.parametrize("analysis", ["false", "true"])
def test_staging_delivers_existing_manifest_path_independently_of_analysis(
    analysis: str,
) -> None:
    result = runtime_environment(analysis=analysis)
    assert result.returncode == 0, result.stderr
    entries = dict(line.split("=", 1) for line in result.stdout.splitlines())
    assert (
        entries.get("SIMULATION__MANIFEST_PATH")
        == "/app/runtime/analysis-runtime.v1.json"
    )


def test_compose_receives_explicit_simulation_path_with_analysis_disabled(
    tmp_path: Path,
) -> None:
    result = runtime_environment(flag="true")
    assert result.returncode == 0, result.stderr
    env_file = tmp_path / ".env.test"
    env_file.write_text(result.stdout)
    rendered = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(env_file),
            "-f",
            str(ROOT / "docker-compose.staging.yml"),
            "config",
            "--format",
            "json",
        ],
        env={"PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    api = json.loads(rendered.stdout)["services"]["api"]
    assert (
        api["environment"].get("SIMULATION__MANIFEST_PATH")
        == "/app/runtime/analysis-runtime.v1.json"
    )
    assert api["environment"]["ANALYSIS__CONFIG_PATH"] == ""
    assert api["environment"]["SIMULATION__ENABLED"] == "true"
    mount = next(v for v in api["volumes"] if v["target"] == "/app/runtime")
    assert mount["source"] == "/opt/dmc-268-t2-runtime/analysis-runtime"
    assert mount["read_only"] is True


@pytest.mark.parametrize("target", ["staging", "develop"])
@pytest.mark.parametrize("flag", [None, "false", "true"])
@pytest.mark.parametrize(
    "analysis,publication",
    [("false", "false"), ("true", "false"), ("false", "true"), ("true", "true")],
)
def test_environment_matrix_preserves_profiles_and_never_delivers_hmac(
    target: str,
    flag: str | None,
    analysis: str,
    publication: str,
) -> None:
    # Preservation coverage after the individual delivery RED/GREEN slices.
    result = runtime_environment(
        target=target, flag=flag, analysis=analysis, publication=publication
    )
    assert result.returncode == 0, result.stderr
    entries = dict(line.split("=", 1) for line in result.stdout.splitlines())
    assert entries["SIMULATION__ENABLED"] == (
        "true" if target == "staging" and flag == "true" else "false"
    )
    expected_profiles = {
        ("false", "false"): "",
        ("true", "false"): "analysis",
        ("false", "true"): "publication",
        ("true", "true"): "analysis,publication",
    }
    assert entries["COMPOSE_PROFILES"] == (
        expected_profiles[analysis, publication] if target == "staging" else ""
    )
    assert ("SIMULATION__MANIFEST_PATH" in entries) == (target == "staging")
    assert "SIMULATION__HMAC_SECRET_B64" not in entries


def test_default_services_stay_unchanged_and_hmac_is_never_forwarded() -> None:
    raw = yaml.safe_load((ROOT / "docker-compose.staging.yml").read_text())
    assert set(compose_config(profiles="analysis,publication")["services"]) == {
        "api",
        "web",
        "worker",
        "outbox-dispatcher",
        "publication-worker",
        "webhook-worker",
        "db",
        "redis",
    }
    assert set(compose_config()["volumes"]) == {"db-data"}
    for name, service in raw["services"].items():
        assert "SIMULATION__HMAC_SECRET_B64" not in service.get("environment", {})
        if name != "api":
            assert not any(
                key.startswith("SIMULATION__") for key in service.get("environment", {})
            )
    assert "HMAC" not in (ROOT / ".github/workflows/deploy-staging.yml").read_text()


def test_pr_and_feature_branch_cannot_publish_or_deploy() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/deploy-staging.yml").read_text()
    )
    # PyYAML's YAML 1.1 parser reads the GitHub Actions `on` key as True.
    assert workflow[True]["workflow_run"]["branches"] == ["main", "develop"]
    gate = workflow["jobs"]["deploy"]["if"]
    assert "github.event.workflow_run.event == 'push'" in gate
    assert "github.ref == 'refs/heads/main'" in gate
    ci = yaml.safe_load((ROOT / ".github/workflows/api-ci.yml").read_text())
    assert (
        ci["jobs"]["publish"]["if"] == "github.event_name == 'push' && "
        "(github.ref == 'refs/heads/main' || github.ref == 'refs/heads/develop')"
    )
