"""Public rule-specific scorer contracts; labels are independent literals."""

import json
from pathlib import Path
from typing import Any

import pytest

# ruff: noqa: PLR2004 -- explicit worked-example counts
from scripts.eval_harness.rules_scoring import evaluate_rules, score_rules


def finding(**changes: Any) -> dict[str, Any]:
    return {
        "path": "src/handler.py",
        "side": "NEW",
        "start_line": 3,
        "end_line": 3,
        "category": "security",
        "rule_ref": "SEC-1",
        **changes,
    }


def test_rule_detection_and_attribution_are_independent() -> None:
    result = score_rules([finding()], [finding()])
    assert result["detection"] == {
        "tp": 1,
        "fp": 0,
        "fn": 0,
        "precision": 1.0,
        "recall": 1.0,
        "f1": 1.0,
    }
    assert result["attribution"] == {
        "tp": 1,
        "fp": 0,
        "fn": 0,
        "precision": 1.0,
        "recall": 1.0,
        "f1": 1.0,
        "missing": 0,
        "wrong": 0,
        "unjustified": 0,
    }


@pytest.mark.parametrize("reference", [{}, {"rule_ref": None}, {"rule_ref": ""}])
def test_missing_reference_preserves_detection_only(reference: dict[str, Any]) -> None:
    predicted = finding()
    predicted.pop("rule_ref")
    result = score_rules([{**predicted, **reference}], [finding()])
    assert result["detection"]["tp"] == 1
    assert result["attribution"]["tp"] == 0
    assert result["attribution"]["missing"] == 1
    assert result["attribution"]["fp"] == 0
    assert result["attribution"]["fn"] == 1
    assert result["by_rule"]["SEC-1"] == {
        "tp": 0,
        "fp": 0,
        "fn": 1,
        "precision": 0.0,
        "recall": 0.0,
        "f1": 0.0,
    }


def test_wrong_reference_is_not_a_rule_true_positive() -> None:
    result = score_rules([finding(rule_ref="PERF-1")], [finding()])
    assert result["detection"]["tp"] == 1
    assert result["attribution"]["tp"] == 0
    assert result["attribution"]["wrong"] == 1
    assert result["attribution"]["fp"] == result["attribution"]["fn"] == 1


def test_duplicates_do_not_increase_true_positives() -> None:
    result = score_rules([finding(), finding()], [finding()])
    assert result["detection"]["tp"] == 1
    assert result["detection"]["fp"] == 1
    assert result["attribution"]["tp"] == 1
    assert result["attribution"]["unjustified"] == 1


def test_matching_maximizes_detection_independent_of_input_order() -> None:
    result = score_rules(
        [finding(start_line=3, end_line=5), finding()],
        [finding(), finding(start_line=5, end_line=5)],
    )
    assert result["detection"]["tp"] == result["attribution"]["tp"] == 2


def test_attribution_matching_prefers_correct_reference() -> None:
    result = score_rules([finding(rule_ref="PERF-1"), finding()], [finding()])
    assert result["attribution"]["tp"] == 1
    assert result["attribution"]["unjustified"] == 1
    assert result["attribution"]["wrong"] == 0


def test_clean_controls_are_counted_separately() -> None:
    result = score_rules([finding()], [])
    assert result["detection"]["fp"] == 1
    assert result["attribution"]["unjustified"] == 1
    assert result["clean"] == {"case": True, "false_positive": True}
    empty = score_rules([], [])
    assert empty["clean"] == {"case": True, "false_positive": False}
    assert empty["detection"]["precision"] == empty["detection"]["recall"] == 0.0


def test_schema_errors_are_not_scored_as_valid_predictions() -> None:

    result = evaluate_rules(
        "diff --git a/src/handler.py b/src/handler.py\n"
        "--- a/src/handler.py\n+++ b/src/handler.py\n@@ -0,0 +1,3 @@\n"
        "+a = 1\n+b = 2\n+c = 3\n",
        [finding()],
        {"findings": "bad"},
        {
            "type": "object",
            "properties": {"findings": {"type": "array"}},
            "required": ["findings"],
        },
    )
    assert result["schema_errors"] == ["schema:type"]
    assert result["detection"]["fn"] == 1


def test_ungrounded_findings_are_false_positives() -> None:

    schema = json.loads(Path("schemas/review-result/v2.json").read_text())
    predicted = finding(
        start_line=99,
        end_line=99,
        severity="high",
        title="Tenant isolation",
        side="NEW",
        explanation="Missing tenant filter.",
        evidence="query",
        recommendation="Filter by tenant_id.",
        proposed_diff_fix=None,
    )
    response = {
        "schema_version": 2,
        "summary": "Review",
        "limitations": [],
        "usage": None,
        "latency_ms": 0,
        "findings": [predicted],
    }
    result = evaluate_rules(
        "diff --git a/src/handler.py b/src/handler.py\n"
        "--- a/src/handler.py\n+++ b/src/handler.py\n@@ -0,0 +1,3 @@\n"
        "+a = 1\n+b = 2\n+c = 3\n",
        [finding(start_line=99, end_line=99)],
        response,
        schema,
    )
    assert not result["schema_errors"]
    assert result["grounding_errors"] == 1
    assert result["detection"]["tp"] == 0
    assert result["detection"]["fp"] == result["detection"]["fn"] == 1
    assert result["attribution"]["unjustified"] == 1


def test_attribution_ties_preserve_missing_reference_diagnostics() -> None:
    result = score_rules(
        [finding(end_line=5), finding(rule_ref=None)],
        [finding(), finding(start_line=5, end_line=5)],
    )
    assert result["detection"]["tp"] == 2
    assert result["attribution"]["tp"] == 1
    assert result["attribution"]["missing"] == 1


@pytest.mark.parametrize("reverse", [False, True])
def test_multiple_rule_labels_share_one_matching_without_reusing_predictions(
    reverse: bool,
) -> None:
    predicted = [finding(end_line=5), finding(rule_ref="PERF-1")]
    if reverse:
        predicted.reverse()
    result = score_rules(
        predicted,
        [finding(rule_ref="PERF-1"), finding(start_line=5, end_line=5)],
    )
    assert result["detection"]["tp"] == result["attribution"]["tp"] == 2
    for reference in ("SEC-1", "PERF-1"):
        assert result["by_rule"][reference] == {
            "tp": 1,
            "fp": 0,
            "fn": 0,
            "precision": 1.0,
            "recall": 1.0,
            "f1": 1.0,
        }
