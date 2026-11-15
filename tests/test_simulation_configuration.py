"""Offline public contract for trusted, opt-in Playground preparation."""

import json
import os
import traceback
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError
from redis.asyncio import Redis
from starlette.requests import Request

from app.api.rule_simulations import get_simulation_coordinator, get_simulation_executor
from app.core.config import LLM, Settings, Simulation
from app.domain.frozen_configuration import FrozenConfigurationError
from app.llm.openai_compatible import OpenAICompatibleClient
from app.main import create_app
from app.review.simulation_configuration import prepare_simulation_configuration

INPUT_CAP = 8192
OUTPUT_CAP = 2000
CONTEXT_WINDOW = 16384
MANIFEST = Path(__file__).resolve().parents[1] / "deploy/analysis-runtime.staging.json"


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    for name in tuple(os.environ):
        if name.startswith(("LLM__", "SIMULATION__", "ANALYSIS__")):
            monkeypatch.delenv(name)


def test_playground_is_disabled_without_flag_or_llm_settings() -> None:
    settings = Settings()
    assert settings.simulation.enabled is False
    assert settings.llm.eurorouter.api_keys == []


@pytest.mark.parametrize("flag", ["", "garbage", "2", "null"])
def test_invalid_flag_cannot_enable_playground(
    flag: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SIMULATION__ENABLED", flag)
    with pytest.raises(ValidationError):
        Settings()


def test_disabled_preparation_does_not_read_manifest_credentials_or_create_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings()

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("disabled preparation touched runtime resources")

    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(os.environ, "get", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "__init__", forbidden)
    monkeypatch.setattr(Redis, "__init__", forbidden)
    monkeypatch.setattr(OpenAICompatibleClient, "__init__", forbidden)
    assert prepare_simulation_configuration(settings) is None


def enabled_settings(**llm_overrides: Any) -> Settings:
    return Settings(
        simulation=Simulation(enabled=True),
        llm=LLM.model_validate(
            {
                "primary_provider": "eurorouter",
                "fallback_enabled": False,
                "eurorouter": {
                    "base_url": "https://api.eurouter.ai/api/v1/",
                    "model": "mistral-small-4",
                },
            }
            | llm_overrides
        ),
    )


def test_preparation_selects_existing_trusted_manifest_without_analysis_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM__EUROROUTER__API_KEYS", "synthetic-first-key")
    config = prepare_simulation_configuration(
        enabled_settings(), manifest_path=MANIFEST
    )
    assert config is not None
    assert config.provider == "eurorouter"
    assert config.model == "mistral-small-4"
    assert config.base_url == "https://api.eurouter.ai/api/v1"


def manifest_with(tmp_path: Path, change: str) -> Path:
    values = json.loads(MANIFEST.read_text())
    route = values["admission"]["primary"]
    profile = values["profiles"]["eurorouter/review-v1"]
    if change == "manifest-model":
        route["model_ref"] = "unapproved-model"
        profile["allowed_models"] = ["unapproved-model"]
    elif change == "manifest-provider":
        values["profiles"] = {
            "ollama/review-v1": {
                "provider": "ollama",
                "base_url": "https://api.eurouter.ai/api/v1",
                "allowed_models": ["mistral-small-4"],
                "credential_env": None,
            }
        }
        route["profile_ref"] = "ollama/review-v1"
        values["admission"]["policy"]["routing"]["primary_profile_ref"] = (
            "ollama/review-v1"
        )
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps(values))
    return path


@pytest.mark.parametrize("change", ["manifest-model", "manifest-provider"])
def test_unapproved_manifest_selection_is_rejected(tmp_path: Path, change: str) -> None:
    with pytest.raises(FrozenConfigurationError, match="simulation_provider_mismatch"):
        prepare_simulation_configuration(
            enabled_settings(), manifest_path=manifest_with(tmp_path, change)
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("model", "unapproved-model"),
        ("model", ""),
        ("base_url", "https://attacker.invalid/api/v1"),
        ("base_url", "https://api.eurouter.ai/api/v1/other"),
        ("base_url", ""),
    ],
)
def test_effective_provider_settings_must_match_manifest(
    field: str, value: str
) -> None:
    settings = enabled_settings()
    setattr(settings.llm.eurorouter, field, value)
    with pytest.raises(FrozenConfigurationError, match="simulation_provider_mismatch"):
        prepare_simulation_configuration(settings, manifest_path=MANIFEST)


def test_other_effective_primary_provider_is_rejected() -> None:
    with pytest.raises(FrozenConfigurationError, match="simulation_provider_mismatch"):
        prepare_simulation_configuration(
            enabled_settings(primary_provider="ollama"), manifest_path=MANIFEST
        )


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        " ",
        "synthetic key",
        "synthetic\n",
        "synthetic\x7f",
        "synthetic-é",
        "synthetic,",
        ",synthetic",
    ],
)
def test_missing_or_invalid_credentials_fail_without_secret_diagnostics(
    raw: str | None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    if raw is not None:
        monkeypatch.setenv("LLM__EUROROUTER__API_KEYS", raw)
    with pytest.raises(
        FrozenConfigurationError, match="invalid_execution_credentials"
    ) as error:
        prepare_simulation_configuration(enabled_settings(), manifest_path=MANIFEST)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    if raw and raw.strip():
        assert raw not in repr(error.value) + str(error.value) + caplog.text + "".join(
            traceback.format_exception(error.value)
        )


def test_only_first_validated_key_is_exposed_without_rotation_or_repr_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "LLM__EUROROUTER__API_KEYS", "synthetic-first-key,synthetic-second-key"
    )
    config = prepare_simulation_configuration(
        enabled_settings(), manifest_path=MANIFEST
    )
    assert config is not None
    assert config.api_key.get_secret_value() == "synthetic-first-key"
    assert "synthetic-first-key" not in repr(config)
    assert "synthetic-second-key" not in repr(config)


def test_manifest_cannot_redirect_credential_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = json.loads(MANIFEST.read_text())
    values["profiles"]["eurorouter/review-v1"]["credential_env"] = "OTHER_API_KEYS"
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps(values))
    monkeypatch.setenv("OTHER_API_KEYS", "synthetic-other-key")
    with pytest.raises(
        FrozenConfigurationError, match="simulation_credentials_source_mismatch"
    ):
        prepare_simulation_configuration(enabled_settings(), manifest_path=path)


def test_effective_fallback_is_forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM__EUROROUTER__API_KEYS", "synthetic-first-key")
    with pytest.raises(FrozenConfigurationError, match="simulation_fallback_forbidden"):
        prepare_simulation_configuration(
            enabled_settings(fallback_enabled=True), manifest_path=MANIFEST
        )


def test_manifest_fallback_route_is_forbidden(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = json.loads(MANIFEST.read_text())
    values["profiles"]["ollama/review-v1"] = {
        "provider": "ollama",
        "base_url": "http://ollama.invalid:11434",
        "allowed_models": ["mistral-small-4"],
        "credential_env": None,
    }
    values["admission"]["policy"]["routing"]["fallback"] = [
        {
            "profile_ref": "ollama/review-v1",
            "model_ref": "mistral-small-4",
            "model_digest": None,
        }
    ]
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps(values))
    monkeypatch.setenv("LLM__EUROROUTER__API_KEYS", "synthetic-first-key")
    with pytest.raises(FrozenConfigurationError, match="simulation_fallback_forbidden"):
        prepare_simulation_configuration(enabled_settings(), manifest_path=path)


def test_staging_token_policy_retains_stricter_service_input_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM__EUROROUTER__API_KEYS", "synthetic-first-key")
    config = prepare_simulation_configuration(
        enabled_settings(), manifest_path=MANIFEST
    )
    assert config is not None
    assert config.max_input_tokens == INPUT_CAP
    assert config.max_output_tokens == OUTPUT_CAP
    assert config.max_input_tokens + config.max_output_tokens <= CONTEXT_WINDOW


@pytest.mark.parametrize(
    "request_ms,duration_ms,effective",
    [
        (120000, 600000, 30.0),
        (5000, 600000, 5.0),
        (2000, 600000, 2.0),
    ],
)
def test_effective_execution_deadline_obeys_service_and_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request_ms: int,
    duration_ms: int,
    effective: float,
) -> None:
    values = json.loads(MANIFEST.read_text())
    values["admission"]["policy"]["limits"]["request_timeout_ms"] = request_ms
    values["admission"]["policy"]["limits"]["analysis_duration_ms"] = duration_ms
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps(values))
    monkeypatch.setenv("LLM__EUROROUTER__API_KEYS", "synthetic-first-key")
    config = prepare_simulation_configuration(enabled_settings(), manifest_path=path)
    assert config is not None
    assert config.execution_timeout_s == effective


def test_enabled_preparation_requires_explicit_path_even_with_analysis_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANALYSIS__CONFIG_PATH", str(MANIFEST))
    monkeypatch.setenv("LLM__EUROROUTER__API_KEYS", "synthetic-first-key")
    with pytest.raises(
        FrozenConfigurationError, match="simulation_manifest_path_required"
    ):
        prepare_simulation_configuration(enabled_settings())


@pytest.mark.parametrize(
    "defect",
    [
        "missing",
        "directory",
        "unreadable",
        "json",
        "duplicate",
        "schema",
        "secret",
        "context",
    ],
)
def test_missing_unavailable_or_invalid_manifest_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    defect: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "runtime.json"
    values = json.loads(MANIFEST.read_text())
    if defect == "directory":
        path.mkdir()
    elif defect == "unreadable":

        def denied(self: Path) -> bytes:
            raise PermissionError("synthetic-manifest-private")

        monkeypatch.setattr(Path, "read_bytes", denied)
    elif defect == "json":
        path.write_text("synthetic-manifest-private")
    elif defect == "duplicate":
        path.write_text('{"schema_version":1,"schema_version":1}')
    elif defect != "missing":
        if defect == "schema":
            values["schema_version"] = 2
        elif defect == "secret":
            values["profiles"]["eurorouter/review-v1"]["api_keys"] = [
                "synthetic-manifest-private"
            ]
        else:
            values["admission"]["policy"]["limits"]["context_window"] = 10000
        path.write_text(json.dumps(values))
    with pytest.raises(
        FrozenConfigurationError, match="invalid_runtime_manifest"
    ) as error:
        prepare_simulation_configuration(enabled_settings(), manifest_path=path)
    assert "synthetic-manifest-private" not in str(error.value) + repr(
        error.value
    ) + caplog.text + "".join(traceback.format_exception(error.value))


@pytest.mark.parametrize("flag", ["false", "0", "off"])
def test_explicit_false_does_not_prepare_simulation(
    flag: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SIMULATION__ENABLED", flag)
    assert prepare_simulation_configuration(Settings()) is None


@pytest.mark.parametrize(
    "input_cap,output_cap,expected",
    [
        (10000, 2000, (8192, 2000)),
        (4000, 1000, (4000, 1000)),
        (12000, 4000, (8192, 2000)),
    ],
)
def test_manifest_token_budgets_cannot_expand_playground_caps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    input_cap: int,
    output_cap: int,
    expected: tuple[int, int],
) -> None:
    values = json.loads(MANIFEST.read_text())
    limits = values["admission"]["policy"]["limits"]
    limits["max_input_tokens"] = input_cap
    limits["max_output_tokens"] = output_cap
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps(values))
    monkeypatch.setenv("LLM__EUROROUTER__API_KEYS", "synthetic-first-key")
    config = prepare_simulation_configuration(enabled_settings(), manifest_path=path)
    assert config is not None
    assert (config.max_input_tokens, config.max_output_tokens) == expected
    assert (
        config.max_input_tokens + config.max_output_tokens <= limits["context_window"]
    )


async def test_feature_flag_does_not_activate_http_dependencies_before_lifespan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SIMULATION__ENABLED", "true")
    assert Settings().simulation.enabled is True
    request = Request({"type": "http", "app": create_app()})
    assert await get_simulation_executor(request) is None
    assert await get_simulation_coordinator(request) is None


def test_single_operation_cannot_exceed_manifest_run_token_caps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = json.loads(MANIFEST.read_text())
    values["admission"]["policy"]["limits"]["run_max_input_tokens"] = 1000
    values["admission"]["policy"]["limits"]["run_max_output_tokens"] = 100
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps(values))
    monkeypatch.setenv("LLM__EUROROUTER__API_KEYS", "synthetic-first-key")
    config = prepare_simulation_configuration(enabled_settings(), manifest_path=path)
    assert config is not None
    assert (config.max_input_tokens, config.max_output_tokens) == (1000, 100)
