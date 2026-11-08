"""Public report consumes complete paired Worker evidence only."""

import json
import os
import stat
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from scripts.eval_harness.rules_dataset import load_rules_dataset
from scripts.eval_harness.rules_report import build_rules_report, write_rules_report

# ruff: noqa: PLR2004 -- hand-labeled six violations and six controls

ROOT = Path("tests/fixtures/rules-benchmark")


def execution() -> dict[str, Any]:
    rows = []
    for fixture in load_rules_dataset(ROOT):
        for mode in ("baseline", "rules"):
            rows.append(
                {
                    "worker_status": "COMPLETED",
                    "fixture_id": fixture.id,
                    "pass": mode,
                    "provider_result": fixture.golden_response,
                    "canonical_result": {
                        "schema_version": 2,
                        **fixture.golden_response,
                        "usage": None,
                        "latency_ms": 0,
                    },
                    "source": {
                        "base": mode,
                        "head": mode,
                        "transport_diff_sha256": "same-diff",
                    },
                    "ast": {"semantic_sha256": "same-code"},
                    "frozen_policy": {
                        "repository_settings_digest": mode,
                        "generation": {"temperature": 0.0},
                    },
                    "rules_digest": mode,
                    "requests": 1,
                }
            )
    return {
        "rows": rows,
        "provenance": {"mode": "deterministic", "quality_measurement": False},
        "usage": {"requests": 24, "cost_usd": "0"},
    }


def test_report_refuses_incomplete_or_duplicate_pairs() -> None:
    sample = execution()
    sample["rows"].pop()
    with pytest.raises(ValueError, match="complete paired"):
        build_rules_report(ROOT, sample)
    sample = execution()
    sample["rows"][-1] = sample["rows"][0]
    with pytest.raises(ValueError, match="complete paired"):
        build_rules_report(ROOT, sample)


def test_report_aggregates_quality_without_claiming_scripted_improvement() -> None:
    report = build_rules_report(ROOT, execution())
    assert report["passes"]["baseline"]["detection"]["tp"] == 6
    assert report["passes"]["rules"]["detection"]["precision"] == 1.0
    assert report["passes"]["rules"]["attribution"]["tp"] == 6
    assert report["passes"]["rules"]["clean_cases"] == 6
    assert report["delta"]["detection"]["f1"] == 0.0
    assert report["provenance"]["quality_measurement"] is False
    assert "no quality improvement" in report["interpretation"]


@pytest.mark.parametrize("changed", ["ast", "source", "frozen_policy"])
def test_report_rejects_uncontrolled_pairs(changed: str) -> None:
    sample = execution()
    if changed == "ast":
        sample["rows"][1]["ast"]["semantic_sha256"] = "different-code"
    elif changed == "source":
        sample["rows"][1]["source"]["transport_diff_sha256"] = "different-diff"
    else:
        sample["rows"][1]["frozen_policy"]["generation"]["temperature"] = 0.5
    with pytest.raises(ValueError, match="Controlled comparison"):
        build_rules_report(ROOT, sample)


def test_scripted_evidence_cannot_claim_provider_quality() -> None:
    sample = execution()
    sample["provenance"]["quality_measurement"] = True
    with pytest.raises(ValueError, match="real provider"):
        build_rules_report(ROOT, sample)


def test_json_and_markdown_reports_are_reproducible(tmp_path: Path) -> None:

    report = build_rules_report(ROOT, execution())
    write_rules_report(report, tmp_path)
    assert json.loads((tmp_path / "rules-quality-report.json").read_text()) == report
    markdown = (tmp_path / "rules-quality-report.md").read_text()
    assert "quality_measurement: false" in markdown
    assert "| baseline | 6 | 0 | 0 | 1.000 | 1.000 | 1.000 |" in markdown
    assert "security-a" in markdown
    assert "no quality improvement" in markdown
    write_rules_report(report, tmp_path)
    assert (tmp_path / "rules-quality-report.md").read_text() == markdown


def test_provider_tp_filtered_by_worker_is_not_a_canonical_tp(tmp_path: Path) -> None:
    sample = execution()
    row = sample["rows"][0]
    row["provider_result"] = deepcopy(load_rules_dataset(ROOT)[0].golden_response)
    row["provider_result"]["findings"][0]["rule_ref"] = "UNKNOWN-1"
    row["canonical_result"]["findings"] = []
    report = build_rules_report(ROOT, sample)
    scored = report["fixtures"][0]
    assert scored["score"]["detection"]["tp"] == 1
    assert scored["canonical_schema_errors"] == []
    assert report["scoring_target"] == "final-provider-output-before-worker-filtering"
    assert scored["provider_quality"]["detection"]["tp"] == 1
    assert scored["provider_quality"]["attribution"]["wrong"] == 1
    assert scored["canonical_quality"]["detection"]["tp"] == 0
    assert scored["canonical_quality"]["detection"]["fn"] == 1
    assert scored["canonical_quality"]["attribution"]["tp"] == 0
    assert scored["canonical_quality"]["attribution"]["fn"] == 1
    assert scored["filtered_findings"] == row["provider_result"]["findings"]
    assert report["provider_quality"]["passes"]["baseline"]["detection"]["tp"] == 6
    canonical = report["canonical_quality"]
    assert canonical["passes"]["baseline"]["detection"]["tp"] == 5
    assert canonical["passes"]["baseline"]["attribution"]["tp"] == 5
    assert canonical["by_rule"]["SEC-1"]["baseline"]["detection"]["fn"] == 1
    assert canonical["delta"]["detection"]["tp"] == 1
    assert canonical["delta"]["attribution"]["tp"] == 1
    write_rules_report(report, tmp_path)
    saved = json.loads((tmp_path / "rules-quality-report.json").read_text())
    assert saved["canonical_quality"] == canonical
    markdown = (tmp_path / "rules-quality-report.md").read_text()
    assert "## provider_quality" in markdown
    assert "## canonical_quality" in markdown
    assert "| baseline | 5 | 0 | 1 |" in markdown
    assert "Filtered findings" in markdown
    assert "UNKNOWN-1" in markdown


def test_report_breaks_down_each_repository_rule() -> None:
    report = build_rules_report(ROOT, execution())
    assert set(report["by_rule"]) == {"SEC-1", "PERF-1", "STYLE-1"}
    security = report["by_rule"]["SEC-1"]
    assert security["baseline"]["detection"]["tp"] == 2
    assert security["rules"]["clean_cases"] == 2


def test_persisted_finding_with_masked_title_is_not_reported_as_filtered() -> None:
    sample = execution()
    row = sample["rows"][0]
    row["provider_result"] = deepcopy(row["provider_result"])
    row["canonical_result"] = deepcopy(row["canonical_result"])
    row["provider_result"]["findings"][0]["title"] = "Avoid password=synthetic-value"
    row["canonical_result"]["findings"][0]["title"] = "Avoid password=***"
    row["canonical_result"]["findings"][0]["severity"] = "high"
    report = build_rules_report(ROOT, sample)
    scored = report["fixtures"][0]
    assert scored["provider_quality"]["detection"]["tp"] == 1
    assert scored["canonical_quality"]["detection"]["tp"] == 1
    assert scored["filtered_findings"] == []


@pytest.mark.parametrize("existing", [False, True], ids=["new", "existing"])
def test_live_reports_are_private_with_public_umask(
    tmp_path: Path, existing: bool
) -> None:
    report = build_rules_report(ROOT, execution())
    report["provenance"]["mode"] = "live"
    output = tmp_path / "live-report"
    files = [output / f"rules-quality-report.{suffix}" for suffix in ("json", "md")]
    if existing:
        output.mkdir(mode=0o755)
        output.chmod(0o755)
        for path in files:
            path.write_text("previous report")
            path.chmod(0o644)
    previous = os.umask(0o022)
    try:
        write_rules_report(report, output)
    finally:
        os.umask(previous)
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert [stat.S_IMODE(path.stat().st_mode) for path in files] == [0o600, 0o600]
    assert json.loads(files[0].read_text()) == report
    assert "canonical_quality" in files[1].read_text()


def test_report_refuses_frozen_model_selection_drift() -> None:
    sample = execution()
    sample["rows"][0]["frozen_selection"] = {"model_ref": "first-model"}
    sample["rows"][1]["frozen_selection"] = {"model_ref": "different-model"}
    with pytest.raises(ValueError, match="Controlled comparison"):
        build_rules_report(ROOT, sample)


def test_nonterminal_worker_evidence_is_incomplete() -> None:
    sample = execution()
    sample["rows"][0]["worker_status"] = "LLM_PROCESSING"
    sample["rows"][0]["chunk_count"] = 0
    with pytest.raises(ValueError, match="incomplete analysis"):
        build_rules_report(ROOT, sample)
