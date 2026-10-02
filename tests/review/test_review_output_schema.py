"""LLMReviewOutput: the only schema sent to the model (spec §3.2)."""

from typing import Any

import pytest
from pydantic import ValidationError

from app.review.schemas.result import LLMReviewOutput

_LINE = 39


def _finding(**overrides: Any) -> dict[str, Any]:
    finding: dict[str, Any] = {
        "location": {
            "path": "src/review.py",
            "side": "NEW",
            "line": 39,
            "end_line": None,
        },
        "category": "correctness",
        "severity": "high",
        "title": "Ошибка валидации не обрабатывается",
        "explanation": "ValidationError уходит наружу и роняет обработчик.",
        "evidence": "src/review.py:39 вызывает model_validate без try.",
        "recommendation": "Перехватить ValidationError и вернуть 422.",
    }
    finding.update(overrides)
    return finding


def test_valid_output_is_parsed() -> None:
    raw = {"summary": "Одна проблема", "findings": [_finding()], "limitations": []}

    output = LLMReviewOutput.model_validate(raw)

    assert output.findings[0].location.line == _LINE


@pytest.mark.parametrize(
    "finding",
    [
        _finding(severity="info"),
        _finding(category="style"),
        _finding(location={"path": "a.py", "side": "NEW", "line": 5, "end_line": 4}),
        _finding(location={"path": "a.py", "side": "NEW", "line": 0, "end_line": None}),
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
