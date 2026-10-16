"""LLMReviewOutput: the only schema sent to the model (spec §3.2)."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from app.review.schemas.result import LLMReviewOutput

CanonicalValidator = Callable[[str], Draft202012Validator]

_LINE = 39
_EXAMPLE = (
    Path(__file__).resolve().parents[2] / "schemas/examples/review-result.v1.json"
)


def _finding(**overrides: Any) -> dict[str, Any]:
    finding: dict[str, Any] = {
        "path": "src/review.py",
        "side": "NEW",
        "start_line": 39,
        "end_line": 39,
        "category": "correctness",
        "severity": "high",
        "title": "Ошибка валидации не обрабатывается",
        "explanation": "ValidationError уходит наружу и роняет обработчик.",
        "evidence": "src/review.py:39 вызывает model_validate без try.",
        "recommendation": "Перехватить ValidationError и вернуть 422.",
        "rule_ref": None,
        "proposed_diff_fix": None,
    }
    finding.update(overrides)
    return finding


def test_finding_may_reference_a_rule_label() -> None:
    raw = {
        "summary": "Одна проблема",
        "findings": [_finding(rule_ref="SEC-1")],
        "limitations": [],
    }

    output = LLMReviewOutput.model_validate(raw)

    assert output.findings[0].rule_ref == "SEC-1"


def test_valid_output_is_parsed() -> None:
    raw = {"summary": "Одна проблема", "findings": [_finding()], "limitations": []}

    output = LLMReviewOutput.model_validate(raw)

    assert output.findings[0].start_line == _LINE
    assert output.findings[0].rule_ref is None  # rule refs are optional


@pytest.mark.parametrize(
    "finding",
    [
        _finding(severity="info"),
        _finding(category="style"),
        _finding(start_line=5, end_line=4),
        _finding(start_line=0, end_line=1),
        _finding(end_line=None),
        _finding(evidence=""),
    ],
)
def test_invalid_finding_is_rejected(finding: dict[str, Any]) -> None:
    raw = {"summary": "", "findings": [finding], "limitations": []}

    with pytest.raises(ValidationError):
        LLMReviewOutput.model_validate(raw)


def test_json_schema_is_strict_mode_compatible() -> None:
    """OpenAI strict mode: every object lists all properties as required
    and forbids additional properties."""
    schema = LLMReviewOutput.model_json_schema()
    objects = [schema, *schema.get("$defs", {}).values()]

    for obj in objects:
        if obj.get("type") != "object":
            continue
        assert obj["additionalProperties"] is False
        assert sorted(obj["required"]) == sorted(obj["properties"])


def test_canonical_example_model_fields_are_parsed() -> None:
    """The shared v2 fixture (schemas/examples) fits the model-facing contract."""
    example = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "schemas/examples/review-result.v2.json"
        ).read_text(encoding="utf-8")
    )
    raw = {key: example[key] for key in ("summary", "findings", "limitations")}

    output = LLMReviewOutput.model_validate(raw)

    assert output.findings[0].rule_ref == "COR-1"


def test_finding_without_proposed_diff_fix_is_rejected() -> None:
    finding = _finding()
    del finding["proposed_diff_fix"]

    with pytest.raises(ValidationError):
        LLMReviewOutput.model_validate(
            {"summary": "", "findings": [finding], "limitations": []}
        )


def test_parsed_output_matches_canonical_llm_output_schema(
    canonical_validator: CanonicalValidator,
) -> None:
    output = LLMReviewOutput.model_validate(
        {"summary": "Сводка", "findings": [_finding()], "limitations": []}
    )

    canonical_validator("llm-output/v2.json").validate(output.model_dump(mode="json"))
