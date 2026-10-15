"""Restricted deployment gate checks secrets locally, separate from the API."""

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_postgres_integration import PostgresHarness
from test_runtime_manifest import PROMPTS, manifest_values, write_manifest

from app.review.configuration import load_runtime_configuration

pytest_plugins = ("test_postgres_integration",)


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
