"""Canonical port results preserve explicit historical wire versions."""

from collections.abc import Callable
from dataclasses import asdict

from jsonschema import Draft202012Validator

from app.application.ports import ReviewChunkResult, ReviewFinding, TokenUsage
from app.domain.enums import FindingCategory, FindingSeverity, FindingSide


def result(version: int, rule_ref: str | None = None) -> ReviewChunkResult:
    return ReviewChunkResult(
        schema_version=version,
        summary="retained",
        limitations=["coverage"],
        usage=TokenUsage(17, 5),
        latency_ms=9,
        findings=[
            ReviewFinding(
                path="src/review.py",
                side=FindingSide.NEW,
                start_line=39,
                end_line=39,
                category=FindingCategory.CORRECTNESS,
                severity=FindingSeverity.HIGH,
                title="crash",
                explanation="bad input crashes",
                evidence="parse(data)",
                recommendation="handle errors",
                rule_ref=rule_ref,
                proposed_diff_fix=None,
            )
        ],
    )


def test_v1_result_wire_omits_rule_ref(
    canonical_validator: Callable[[str], Draft202012Validator],
) -> None:
    wire = result(1).to_wire()
    canonical_validator("review-result/v1.json").validate(wire)
    assert "rule_ref" not in wire["findings"][0]
    assert wire["usage"] == {"input_tokens": 17, "output_tokens": 5}


def test_v2_result_wire_requires_nullable_rule_ref(
    canonical_validator: Callable[[str], Draft202012Validator],
) -> None:
    wire = result(2).to_wire()
    canonical_validator("review-result/v2.json").validate(wire)
    assert wire["findings"][0]["rule_ref"] is None


def test_historical_finding_constructor_remains_compatible() -> None:
    fields = asdict(result(1).findings[0])
    fields.pop("rule_ref")
    finding = ReviewFinding(**fields)
    assert finding.rule_ref is None
