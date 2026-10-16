"""Reflection Pass verdicts: the second structured call's contract (issue #25).

The verifier judges only what the deterministic layer cannot: real-world
consequence, pre-existence, and style mislabeling. Coordinates, evidence,
rule references and duplicates are already deterministic checks (SD §12).
"""

from typing import Any

import pytest
from pydantic import ValidationError

from app.review.schemas.reflection import ReflectionOutput


def _verdict(**overrides: Any) -> dict[str, Any]:
    verdict: dict[str, Any] = {
        "index": 0,
        "verdict": "confirmed",
        "reason": "verified",
    }
    verdict.update(overrides)
    return verdict


def test_valid_verdicts_are_parsed() -> None:
    output = ReflectionOutput.model_validate(
        {
            "verdicts": [
                _verdict(),
                _verdict(index=1, verdict="rejected", reason="pre_existing"),
            ]
        }
    )

    assert output.verdicts[0].verdict == "confirmed"
    assert output.verdicts[1].reason == "pre_existing"


def test_verdict_and_reason_must_agree() -> None:
    mixed = [
        _verdict(verdict="rejected", reason="verified"),
        _verdict(reason="no_real_consequence"),
    ]
    for verdicts in mixed:
        with pytest.raises(ValidationError):
            ReflectionOutput.model_validate({"verdicts": [verdicts]})


def test_unknown_reason_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ReflectionOutput.model_validate(
            {"verdicts": [_verdict(verdict="rejected", reason="looks wrong")]}
        )


def test_duplicate_indexes_are_rejected() -> None:
    with pytest.raises(ValidationError):
        ReflectionOutput.model_validate({"verdicts": [_verdict(), _verdict()]})


def test_negative_index_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ReflectionOutput.model_validate({"verdicts": [_verdict(index=-1)]})


def test_strict_mode_compatible() -> None:
    """OpenAI strict mode: every object lists all properties as required
    and forbids additional properties."""
    schema = ReflectionOutput.model_json_schema()
    objects = [schema, *schema.get("$defs", {}).values()]

    for obj in objects:
        if obj.get("type") != "object":
            continue
        assert obj["additionalProperties"] is False
        assert sorted(obj["required"]) == sorted(obj["properties"])
