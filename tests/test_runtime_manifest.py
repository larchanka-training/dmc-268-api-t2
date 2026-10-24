"""Trusted nonsecret runtime inventory, never provider inference."""

import json
from pathlib import Path
from typing import Any

import pytest

from app.domain.frozen_configuration import FrozenConfigurationError, admission_policy
from app.review import configuration
from app.review.prompt import PromptBuilder

PROMPTS = Path(__file__).resolve().parents[1] / ".agents/reviewer"


def test_versioned_staging_manifest_preserves_approved_configuration() -> None:
    runtime = configuration.load_runtime_configuration(
        PROMPTS.parents[1] / "deploy/analysis-runtime.staging.json"
    )
    assert tuple(runtime.profiles) == ("eurorouter/review-v1",)
    profile = runtime.profiles["eurorouter/review-v1"]
    assert (profile.provider, profile.base_url, profile.allowed_models) == (
        "eurorouter",
        "https://api.eurouter.ai/api/v1",
        ("mistral-small-4",),
    )
    assert profile.api_keys == ()
    assert dict(runtime.credential_env) == {
        "eurorouter/review-v1": "LLM__EUROROUTER__API_KEYS"
    }
    admission = runtime.admission
    assert admission.primary.model_dump() == {
        "profile_ref": "eurorouter/review-v1",
        "model_ref": "mistral-small-4",
        "model_digest": None,
    }
    assert admission.prompt_version == "v2"
    bundle = PromptBuilder.load(
        PROMPTS, admission.prompt_version, expected_digest=admission.prompt_digest
    )
    bundle.ensure_verified()
    assert bundle.prompt_digest == (
        "a09dffb11fdd1481888eab77d2530e135e998765e22f9fc254a80cc94faef50b"
    )
    assert admission.policy.model_dump(mode="json") == {
        "schema_version": 1,
        "generation": {"temperature": 0.0},
        "routing": {"primary_profile_ref": "eurorouter/review-v1", "fallback": []},
        "limits": {
            "context_window": 16384,
            "max_input_tokens": 10000,
            "max_output_tokens": 2000,
            "request_timeout_ms": 120000,
            "run_max_requests": 8,
            "run_max_input_tokens": 80000,
            "run_max_output_tokens": 16000,
            "max_chunks": 5,
            "analysis_duration_ms": 600000,
        },
        "retry": {
            "worker_max_attempts": 3,
            "provider_max_attempts_per_operation": 3,
            "max_transient_per_provider": 2,
            "max_format_repairs": 1,
            "max_overflow_rebuilds_per_chunk": 1,
            "backoff_policy": "review-exponential-v1",
        },
        "token_estimator_version": "review-conservative-v1",
        "repository_settings_digest": "0" * 64,
    }


def manifest_values() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "profiles": {
            "eurorouter/review-v1": {
                "provider": "eurorouter",
                "base_url": "https://llm.invalid/v1",
                "allowed_models": ["test-model", "fallback-model"],
                "credential_env": "LLM__EUROROUTER__API_KEYS",
            },
            "ollama/review-v1": {
                "provider": "ollama",
                "base_url": "http://ollama.invalid:11434",
                "allowed_models": ["test-model", "fallback-model"],
                "credential_env": None,
            },
        },
        "admission": {
            "primary": {
                "profile_ref": "eurorouter/review-v1",
                "model_ref": "test-model",
                "model_digest": None,
            },
            "policy": admission_policy("0" * 64).model_dump(mode="json"),
            "prompt_version": "v2",
            "prompt_digest": PromptBuilder.load(PROMPTS, "v2").prompt_digest,
        },
    }


def write_manifest(tmp_path: Path, values: dict[str, Any]) -> Path:
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps(values), encoding="utf-8")
    return path


def test_manifest_loads_exact_nonsecret_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM__EUROROUTER__API_KEYS", "must-not-be-read")
    path = write_manifest(tmp_path, manifest_values())
    runtime = configuration.load_runtime_configuration(path)
    assert runtime.profiles["eurorouter/review-v1"].api_keys == ()
    assert runtime.admission.primary.model_ref == "test-model"
    digest = runtime.identity
    assert len(digest) == 64  # noqa: PLR2004
    path.write_text(json.dumps(manifest_values(), indent=4, sort_keys=True))
    assert configuration.load_runtime_configuration(path).identity == digest
    monkeypatch.setenv("ANALYSIS__CONFIG_PATH", str(path))
    assert configuration.load_runtime_configuration().identity == digest


@pytest.mark.parametrize(
    "defect",
    [
        "unknown_profile",
        "provider",
        "model",
        "url",
        "policy",
        "extra",
        "version",
        "pin",
        "secret",
    ],
)
def test_manifest_rejects_invalid_inventory(tmp_path: Path, defect: str) -> None:
    values = manifest_values()
    profile = values["profiles"]["eurorouter/review-v1"]
    if defect == "unknown_profile":
        values["admission"]["primary"]["profile_ref"] = "eurorouter/unknown"
    elif defect == "provider":
        profile["provider"] = "ollama"
    elif defect == "model":
        values["admission"]["primary"]["model_ref"] = "untrusted"
    elif defect == "url":
        profile["base_url"] = "http://llm.invalid/v1"
    elif defect == "policy":
        del values["admission"]["policy"]["retry"]
    elif defect == "extra":
        values["unexpected"] = True
    elif defect == "version":
        values["schema_version"] = True
    elif defect == "pin":
        values["admission"]["primary"]["model_digest"] = "a" * 64
    else:
        profile["api_keys"] = ["never-in-manifest"]
    with pytest.raises(FrozenConfigurationError):
        configuration.load_runtime_configuration(write_manifest(tmp_path, values))


@pytest.mark.parametrize(
    "url",
    [
        "https://llm.invalid/v1\n",
        "https://llm.invalid/\tpath",
        " https://llm.invalid/v1",
        "https://llm.invalid/\x7f",
    ],
)
def test_manifest_rejects_raw_url_controls(tmp_path: Path, url: str) -> None:
    values = manifest_values()
    values["profiles"]["eurorouter/review-v1"]["base_url"] = url
    with pytest.raises(FrozenConfigurationError, match="invalid_runtime_manifest"):
        configuration.load_runtime_configuration(write_manifest(tmp_path, values))


def test_execution_rejects_control_characters_before_csv_normalization(
    tmp_path: Path,
) -> None:
    runtime = configuration.load_runtime_configuration(
        write_manifest(tmp_path, manifest_values())
    )
    with pytest.raises(FrozenConfigurationError, match="invalid_execution_credentials"):
        runtime.execution_profiles(
            ("eurorouter/review-v1",),
            environ={"LLM__EUROROUTER__API_KEYS": "synthetic-test-key\n"},
        )


def test_manifest_admits_verified_v3_without_changing_historical_defaults(
    tmp_path: Path,
) -> None:
    values = manifest_values()
    values["admission"]["prompt_version"] = "v3"
    values["admission"]["prompt_digest"] = PromptBuilder.load(
        PROMPTS, "v3"
    ).prompt_digest
    runtime = configuration.load_runtime_configuration(write_manifest(tmp_path, values))
    assert runtime.admission.prompt_version == "v3"
    assert manifest_values()["admission"]["prompt_version"] == "v2"
