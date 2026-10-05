"""Unit tests for the eval harness: validator, scoring, and an end-to-end CLI run."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from scripts.eval_harness.cli import run
from scripts.eval_harness.llm_client import LlmConfigurationError
from scripts.eval_harness.scoring import MatchCounts, match_findings
from scripts.eval_harness.validator import is_grounded, parse_diff, validate_shape

_DIFF = (
    "diff --git a/app/example.py b/app/example.py\n"
    "new file mode 100644\n"
    "index 0000000..1111111\n"
    "--- /dev/null\n"
    "+++ b/app/example.py\n"
    "@@ -0,0 +1,3 @@\n"
    "+line one\n"
    "+line two\n"
    "+line three\n"
)

_SCHEMA_PATH = (
    Path(__file__).parents[1] / "scripts/eval_harness/schema/review_result.schema.json"
)
_SCHEMA = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
_FULL_PERCENT = 100.0


def _counts(result: MatchCounts) -> tuple[int, int, int]:
    return result.true_positives, result.false_positives, result.false_negatives


def _finding(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "path": "app/example.py",
        "side": "NEW",
        "start_line": 2,
        "end_line": 2,
        "category": "security",
        "severity": "high",
        "title": "t",
        "explanation": "e",
        "evidence": "ev",
        "recommendation": "r",
    }
    base.update(overrides)
    return base


class TestParseDiff:
    def test_records_added_lines_on_the_new_side(self) -> None:
        changed = parse_diff(_DIFF)

        assert changed["app/example.py"].new_lines == {1, 2, 3}
        assert changed["app/example.py"].old_lines == set()

    def test_unknown_path_has_no_entry(self) -> None:
        changed = parse_diff(_DIFF)

        assert "app/other.py" not in changed


class TestIsGrounded:
    def test_accepts_a_finding_fully_within_changed_lines(self) -> None:
        changed = parse_diff(_DIFF)

        assert is_grounded(_finding(start_line=1, end_line=2), changed) is True

    def test_rejects_a_finding_outside_the_diff(self) -> None:
        changed = parse_diff(_DIFF)

        assert is_grounded(_finding(start_line=10, end_line=10), changed) is False

    def test_rejects_a_finding_on_an_unknown_path(self) -> None:
        changed = parse_diff(_DIFF)

        assert is_grounded(_finding(path="app/other.py"), changed) is False

    def test_rejects_the_wrong_side(self) -> None:
        changed = parse_diff(_DIFF)

        finding = _finding(side="OLD", start_line=1, end_line=1)

        assert is_grounded(finding, changed) is False


class TestValidateShape:
    def test_valid_response_has_no_errors(self) -> None:
        response = {"summary": "ok", "findings": [_finding()], "limitations": []}

        assert validate_shape(response, _SCHEMA) == []

    def test_unknown_category_is_rejected(self) -> None:
        response = {
            "summary": "ok",
            "findings": [_finding(category="style")],
            "limitations": [],
        }

        assert validate_shape(response, _SCHEMA) != []

    def test_missing_required_field_is_rejected(self) -> None:
        response = {"summary": "ok", "findings": [], "limitations": []}
        del response["summary"]

        assert validate_shape(response, _SCHEMA) != []

    def test_unknown_top_level_field_is_rejected(self) -> None:
        response = {
            "summary": "ok",
            "findings": [],
            "limitations": [],
            "extra": "not allowed",
        }

        assert validate_shape(response, _SCHEMA) != []


class TestMatchFindings:
    def test_exact_match_is_a_true_positive(self) -> None:
        predicted = [_finding(start_line=2, end_line=2)]
        expected = [_finding(start_line=2, end_line=2)]

        counts = match_findings(predicted, expected)

        assert _counts(counts) == (
            1,
            0,
            0,
        )

    def test_overlapping_range_still_counts_as_a_match(self) -> None:
        predicted = [_finding(start_line=1, end_line=3)]
        expected = [_finding(start_line=2, end_line=2)]

        counts = match_findings(predicted, expected)

        assert counts.true_positives == 1

    def test_different_category_is_not_a_match(self) -> None:
        predicted = [_finding(category="correctness")]
        expected = [_finding(category="security")]

        counts = match_findings(predicted, expected)

        assert _counts(counts) == (
            0,
            1,
            1,
        )

    def test_extra_predicted_finding_is_a_false_positive(self) -> None:
        predicted = [_finding(), _finding(start_line=1, end_line=1)]
        expected = [_finding()]

        counts = match_findings(predicted, expected)

        assert _counts(counts) == (
            1,
            1,
            0,
        )

    def test_missing_prediction_is_a_false_negative(self) -> None:
        counts = match_findings([], [_finding()])

        assert _counts(counts) == (
            0,
            0,
            1,
        )

    def test_duplicate_predictions_consume_at_most_one_expected_finding(self) -> None:
        predicted = [_finding(), _finding()]
        expected = [_finding()]

        counts = match_findings(predicted, expected)

        assert _counts(counts) == (
            1,
            1,
            0,
        )


class TestCliEndToEnd:
    def test_offline_run_against_a_tiny_dataset_reports_perfect_scores(
        self, tmp_path: Path
    ) -> None:
        dataset_dir = tmp_path / "dataset"
        fixture_dir = dataset_dir / "fixtures" / "sec-001-demo"
        fixture_dir.mkdir(parents=True)

        manifest = [{"id": "sec-001-demo", "category": "security", "title": "Demo"}]
        (dataset_dir / "manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        (fixture_dir / "pr.diff").write_text(_DIFF, encoding="utf-8")
        meta = {
            "title": "Demo",
            "description": "demo",
            "language": "python",
            "category": "security",
        }
        (fixture_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        expected = [
            {
                "path": "app/example.py",
                "side": "NEW",
                "start_line": 2,
                "end_line": 2,
                "category": "security",
                "severity": "high",
            }
        ]
        (fixture_dir / "expected_findings.json").write_text(
            json.dumps(expected), encoding="utf-8"
        )
        golden = {
            "summary": "one issue",
            "findings": [_finding()],
            "limitations": [],
        }
        (fixture_dir / "golden_response.json").write_text(
            json.dumps(golden), encoding="utf-8"
        )

        report_out = tmp_path / "report.json"
        report = run(
            [
                "--mode",
                "offline",
                "--dataset-dir",
                str(dataset_dir),
                "--report-out",
                str(report_out),
            ]
        )

        assert report["aggregate"]["fixtures_total"] == 1
        assert report["aggregate"]["shape_valid_percent"] == _FULL_PERCENT
        assert report["aggregate"]["precision"] == 1.0
        assert report["aggregate"]["recall"] == 1.0
        assert json.loads(report_out.read_text(encoding="utf-8")) == report

    def test_live_mode_without_env_vars_raises_configuration_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("EVAL_LLM_BASE_URL", raising=False)
        monkeypatch.delenv("EVAL_LLM_API_KEY", raising=False)
        monkeypatch.delenv("EVAL_LLM_MODEL", raising=False)

        dataset_dir = tmp_path / "dataset"
        (dataset_dir / "fixtures").mkdir(parents=True)
        (dataset_dir / "manifest.json").write_text("[]", encoding="utf-8")

        with pytest.raises(LlmConfigurationError):
            run(["--mode", "live", "--dataset-dir", str(dataset_dir)])
