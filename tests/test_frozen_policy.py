"""Persisted policy shape and explicit new-admission defaults (AC 6)."""

import math
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.domain.frozen_configuration import ModelSettings, admission_policy
from app.domain.settings import compute_repository_settings_digest


def test_new_admission_materializes_every_policy_field() -> None:
    policy = admission_policy("0" * 64).model_dump(mode="json")
    assert policy == {
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
    assert ModelSettings.from_stored(policy).model_dump(mode="json") == policy


@pytest.mark.parametrize("temperature", [-0.1, 2.1, math.inf, math.nan, True, "0.3"])
def test_admission_rejects_invalid_temperature(temperature: Any) -> None:
    with pytest.raises((ValidationError, ValueError)):
        admission_policy("0" * 64, temperature=temperature)


@pytest.mark.parametrize(
    "defect", ["missing", "extra", "limit", "repair", "estimator", "routing", "boolean"]
)
def test_persisted_policy_never_receives_implicit_defaults(defect: str) -> None:
    values: dict[str, Any] = admission_policy("0" * 64).model_dump(mode="json")
    if defect == "missing":
        del values["generation"]
    elif defect == "extra":
        values["top_p"] = 0.5
    elif defect == "limit":
        values["limits"]["max_input_tokens"] = 16384
    elif defect == "repair":
        values["retry"]["max_format_repairs"] = 2
    elif defect == "estimator":
        values["token_estimator_version"] = "current"
    elif defect == "routing":
        values["routing"]["primary_profile_ref"] = "unknown/current"
    else:
        values["limits"]["max_chunks"] = True
    with pytest.raises(ValidationError):
        ModelSettings.from_stored(values)


@pytest.mark.parametrize(
    "path,value",
    [
        (("schema_version",), True),
        (("schema_version",), 1.0),
        (("retry", "max_format_repairs"), True),
        (("retry", "max_format_repairs"), 1.0),
        (("limits", "analysis_duration_ms"), 600000.0),
    ],
)
def test_fixed_integer_policy_values_reject_coercible_scalars(
    path: tuple[str, ...],
    value: Any,
) -> None:
    values: dict[str, Any] = admission_policy("0" * 64).model_dump(mode="json")
    target = values
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValidationError):
        ModelSettings.from_stored(values)


def test_raw_historical_settings_digest_has_independent_vectors() -> None:

    raw = compute_repository_settings_digest(
        id=UUID("00000000-0000-0000-0000-000000000001"),
        repository_id=UUID("00000000-0000-0000-0000-000000000002"),
        rules={},
        ignores={},
        output_language="en",
        surrounding_lines=3,
    )
    canonical = compute_repository_settings_digest(
        id=UUID("00000000-0000-0000-0000-000000000001"),
        repository_id=UUID("00000000-0000-0000-0000-000000000002"),
        rules={"instructions": []},
        ignores={"globs": []},
        output_language="en",
        surrounding_lines=3,
    )
    assert raw == "e5fb935b55b4ba152cc8e78266f2e8932e35b123be0073b793ff8ca69229de17"
    assert (
        canonical == "21c071ac9afbf4b3b5992a5c34c5b43e03bd8d8c169f1af3b9b5fb5574b66574"
    )
    assert raw != canonical
