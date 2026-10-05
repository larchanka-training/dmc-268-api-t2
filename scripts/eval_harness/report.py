"""Per-fixture evaluation and aggregate report (console + JSON artifact)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from scripts.eval_harness.dataset import Fixture
from scripts.eval_harness.scoring import match_findings
from scripts.eval_harness.validator import is_grounded, parse_diff, validate_shape


@dataclass(frozen=True, slots=True)
class FixtureReport:
    fixture_id: str
    category: str
    shape_valid: bool
    shape_errors: list[str]
    findings_predicted: int
    findings_grounded: int
    true_positives: int
    false_positives: int
    false_negatives: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.fixture_id,
            "category": self.category,
            "shape_valid": self.shape_valid,
            "shape_errors": self.shape_errors,
            "findings_predicted": self.findings_predicted,
            "findings_grounded": self.findings_grounded,
            "true_positives": self.true_positives,
            "false_positives": self.false_positives,
            "false_negatives": self.false_negatives,
        }


def evaluate_fixture(
    fixture: Fixture, response: Any, schema: dict[str, Any]
) -> FixtureReport:
    """Score one fixture's (parsed) response against the schema and its labels."""

    if not isinstance(response, dict):
        return FixtureReport(
            fixture_id=fixture.id,
            category=fixture.category,
            shape_valid=False,
            shape_errors=["response is not a JSON object"],
            findings_predicted=0,
            findings_grounded=0,
            true_positives=0,
            false_positives=0,
            false_negatives=len(fixture.expected_findings),
        )

    shape_errors = validate_shape(response, schema)
    shape_valid = not shape_errors
    findings = response.get("findings", []) if shape_valid else []

    changed_lines = parse_diff(fixture.diff_text)
    grounded_count = sum(
        1 for finding in findings if is_grounded(finding, changed_lines)
    )

    counts = match_findings(findings, fixture.expected_findings)

    return FixtureReport(
        fixture_id=fixture.id,
        category=fixture.category,
        shape_valid=shape_valid,
        shape_errors=shape_errors,
        findings_predicted=len(findings),
        findings_grounded=grounded_count,
        true_positives=counts.true_positives,
        false_positives=counts.false_positives,
        false_negatives=counts.false_negatives,
    )


def _safe_ratio(numerator: int, denominator: int, *, default: float) -> float:
    return numerator / denominator if denominator else default


def aggregate(reports: list[FixtureReport]) -> dict[str, Any]:
    """Aggregate per-fixture reports into overall metrics."""

    total = len(reports)
    shape_valid_count = sum(1 for report in reports if report.shape_valid)
    findings_predicted = sum(report.findings_predicted for report in reports)
    findings_grounded = sum(report.findings_grounded for report in reports)
    true_positives = sum(report.true_positives for report in reports)
    false_positives = sum(report.false_positives for report in reports)
    false_negatives = sum(report.false_negatives for report in reports)

    precision = _safe_ratio(
        true_positives, true_positives + false_positives, default=1.0
    )
    recall = _safe_ratio(true_positives, true_positives + false_negatives, default=1.0)
    denominator = precision + recall
    f1 = (2 * precision * recall / denominator) if denominator else 0.0
    shape_valid_ratio = _safe_ratio(shape_valid_count, total, default=0.0)
    shape_valid_percent = round(100 * shape_valid_ratio, 1)

    return {
        "fixtures_total": total,
        "shape_valid_count": shape_valid_count,
        "shape_valid_percent": shape_valid_percent,
        "findings_predicted": findings_predicted,
        "findings_grounded": findings_grounded,
        "grounding_valid_percent": round(
            100 * _safe_ratio(findings_grounded, findings_predicted, default=100.0), 1
        ),
        "true_positives": true_positives,
        "false_positives": false_positives,
        "false_negatives": false_negatives,
        "precision": round(precision, 3),
        "recall": round(recall, 3),
        "f1": round(f1, 3),
    }


def aggregate_by_category(reports: list[FixtureReport]) -> dict[str, dict[str, Any]]:
    categories = sorted({report.category for report in reports})
    return {
        category: aggregate(
            [report for report in reports if report.category == category]
        )
        for category in categories
    }


def build_report(reports: list[FixtureReport]) -> dict[str, Any]:
    return {
        "fixtures": [report.to_dict() for report in reports],
        "aggregate": aggregate(reports),
        "by_category": aggregate_by_category(reports),
    }


def print_console_summary(report: dict[str, Any]) -> None:
    header = f"{'fixture':45} {'shape':>6} {'pred':>5} {'grnd':>5}"
    print(f"{header} {'tp':>4} {'fp':>4} {'fn':>4}")
    for fixture_report in report["fixtures"]:
        print(
            f"{fixture_report['id']:45} "
            f"{'ok' if fixture_report['shape_valid'] else 'BAD':>6} "
            f"{fixture_report['findings_predicted']:>5} "
            f"{fixture_report['findings_grounded']:>5} "
            f"{fixture_report['true_positives']:>4} "
            f"{fixture_report['false_positives']:>4} "
            f"{fixture_report['false_negatives']:>4}"
        )

    aggregate_result = report["aggregate"]
    print()
    print(f"Fixtures:              {aggregate_result['fixtures_total']}")
    print(
        f"% shape-valid:         {aggregate_result['shape_valid_percent']}% "
        f"({aggregate_result['shape_valid_count']}/{aggregate_result['fixtures_total']})"
    )
    print(f"% grounding-valid:     {aggregate_result['grounding_valid_percent']}%")
    print(f"Precision:             {aggregate_result['precision']}")
    print(f"Recall:                {aggregate_result['recall']}")
    print(f"F1:                    {aggregate_result['f1']}")

    print()
    print("By category:")
    for category, category_aggregate in report["by_category"].items():
        print(
            f"  {category:16} shape-valid={category_aggregate['shape_valid_percent']}% "
            f"precision={category_aggregate['precision']} "
            f"recall={category_aggregate['recall']}"
        )
