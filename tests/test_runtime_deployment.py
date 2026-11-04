"""Restricted deployment gate checks secrets locally, separate from the API."""

import os
import subprocess
import sys
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_analysis_deployment import workflow_step
from test_analysis_terminal import freeze
from test_frozen_configuration import save_policy
from test_postgres_integration import PostgresHarness
from test_runtime_manifest import PROMPTS, manifest_values, write_manifest

from app.db.models import ReviewJob
from app.review.configuration import load_runtime_configuration

pytest_plugins = ("test_analysis_handler",)


def deployment_preflight_script() -> str:
    deployment: str = workflow_step("Deploy application")["run"]
    assert "<<'PREFLIGHT'" in deployment, "deployment must gate worker activation"
    return deployment.split("<<'PREFLIGHT'\n", 1)[1].split("\nPREFLIGHT", 1)[0]


async def test_deploy_preflight_refuses_missing_execution_key_without_leaking(
    postgres: PostgresHarness, tmp_path: Path
) -> None:
    del postgres  # Require a migrated, reachable database at the deployment boundary.
    values = manifest_values()
    del values["profiles"]["ollama/review-v1"]
    environment = os.environ | {
        "ANALYSIS__CONFIG_PATH": str(write_manifest(tmp_path, values)),
        "GITHUB__TOKEN": "synthetic-read-token",
        "LLM__EUROROUTER__API_KEYS": "synthetic-test-key",
        "LLM__EUROROUTER__MODEL": "test-model",
        "LLM__EUROROUTER__BASE_URL": "https://llm.invalid/v1",
    }
    ready = subprocess.run(
        [sys.executable, "-c", deployment_preflight_script()],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert ready.returncode == 0
    assert ready.stdout + ready.stderr == "Analysis preflight passed\n"
    environment["LLM__EUROROUTER__API_KEYS"] = ""
    result = subprocess.run(
        [sys.executable, "-c", deployment_preflight_script()],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 1
    assert result.stdout + result.stderr == "Analysis preflight failed\n"


@pytest.mark.parametrize(
    "defect", ["token", "model", "endpoint", "fallback", "provider", "inventory"]
)
async def test_mvp_preflight_refuses_unapproved_or_incomplete_provider_configuration(
    postgres: PostgresHarness, tmp_path: Path, defect: str
) -> None:
    del postgres
    values = manifest_values()
    del values["profiles"]["ollama/review-v1"]
    if defect == "inventory":
        values["profiles"]["ollama/review-v1"] = manifest_values()["profiles"][
            "ollama/review-v1"
        ]
    if defect == "fallback":
        values["admission"]["policy"]["routing"]["fallback"] = [
            values["admission"]["primary"]
        ]
    if defect == "provider":
        values["profiles"] = {
            "ollama/review-v1": {
                "provider": "ollama",
                "base_url": "https://llm.invalid/v1",
                "allowed_models": ["test-model"],
                "credential_env": None,
            }
        }
        values["admission"]["primary"]["profile_ref"] = "ollama/review-v1"
        values["admission"]["policy"]["routing"]["primary_profile_ref"] = (
            "ollama/review-v1"
        )
    environment = os.environ | {
        "ANALYSIS__CONFIG_PATH": str(write_manifest(tmp_path, values)),
        "GITHUB__TOKEN": "" if defect == "token" else "synthetic-read-token",
        "LLM__EUROROUTER__API_KEYS": "synthetic-test-key",
        "LLM__EUROROUTER__MODEL": "unapproved" if defect == "model" else "test-model",
        "LLM__EUROROUTER__BASE_URL": "https://other.invalid/v1"
        if defect == "endpoint"
        else "https://llm.invalid/v1",
    }
    result = subprocess.run(
        [sys.executable, "-c", deployment_preflight_script()],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 1
    assert result.stdout + result.stderr == "Analysis preflight failed\n"


async def test_mvp_preflight_refuses_saved_same_provider_fallback_without_rewriting_job(
    postgres: PostgresHarness, review_id: UUID, tmp_path: Path
) -> None:
    factory = async_sessionmaker(postgres.engine, expire_on_commit=False)
    await freeze(factory, review_id)
    async with factory() as session:
        job = await session.get(ReviewJob, review_id)
        assert job is not None
        policy = dict(job.model_settings)
    policy["routing"]["fallback"] = [
        {
            "profile_ref": "eurorouter/review-v1",
            "model_ref": "fallback-model",
            "model_digest": None,
        }
    ]
    await save_policy(postgres, review_id, model_settings=policy)
    values = manifest_values()
    del values["profiles"]["ollama/review-v1"]
    path = write_manifest(tmp_path, values)
    environment = os.environ | {
        "ANALYSIS__CONFIG_PATH": str(path),
        "GITHUB__TOKEN": "synthetic-read-token",
        "LLM__EUROROUTER__API_KEYS": "synthetic-test-key",
        "LLM__EUROROUTER__MODEL": "test-model",
        "LLM__EUROROUTER__BASE_URL": "https://llm.invalid/v1",
    }
    # The saved policy is valid for the backend; only MVP activation refuses it.
    runtime = load_runtime_configuration(path)
    report = await runtime.deployment_preflight(factory, PROMPTS, environ=environment)
    assert report.ready
    result = subprocess.run(
        [sys.executable, "-c", deployment_preflight_script()],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 1
    assert result.stdout + result.stderr == "Analysis preflight failed\n"
    async with factory() as session:
        saved = await session.get(ReviewJob, review_id)
        assert saved is not None and saved.model_settings == policy
        assert saved.status == "QUEUED"


@pytest.mark.parametrize("location", ["primary", "fallback"])
@pytest.mark.parametrize(
    "credential",
    [None, "", "  ", "synthetic-deployment-test-key", "synthetic-test-key\n"],
)
async def test_restricted_preflight_checks_required_credentials(
    postgres: PostgresHarness, tmp_path: Path, location: str, credential: str | None
) -> None:
    values = manifest_values()
    if location == "fallback":
        values["admission"]["primary"]["profile_ref"] = "ollama/review-v1"
        values["admission"]["policy"]["routing"] = {
            "primary_profile_ref": "ollama/review-v1",
            "fallback": [
                {
                    "profile_ref": "eurorouter/review-v1",
                    "model_ref": "fallback-model",
                    "model_digest": None,
                }
            ],
        }
    runtime = load_runtime_configuration(write_manifest(tmp_path, values))
    env = {} if credential is None else {"LLM__EUROROUTER__API_KEYS": credential}
    report = await runtime.deployment_preflight(
        async_sessionmaker(postgres.engine), PROMPTS, environ=env
    )
    assert report.ready == (credential == "synthetic-deployment-test-key")
    assert report.manifest_identity == runtime.identity
    assert "synthetic-deployment-test-key" not in repr(report)
    assert "synthetic-deployment-test-key" not in repr(runtime)
    if not report.ready:
        assert report.issues[-1].code == "invalid_execution_credentials"
    assert runtime.profiles["eurorouter/review-v1"].api_keys == ()
