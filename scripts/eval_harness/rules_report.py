"""Reproducible paired rule-quality report, separate from Gold and QA-04A."""

import json
import os
from collections import Counter
from pathlib import Path
from typing import Any

from app.review.prompt import PromptBuilder
from app.review.validation import mask_secrets
from scripts.eval_harness.rules_dataset import load_rules_dataset
from scripts.eval_harness.rules_scoring import evaluate_rules, metrics


def build_rules_report(dataset: Path, execution: dict[str, Any]) -> dict[str, Any]:
    provenance = execution["provenance"]
    if provenance["quality_measurement"] and (
        provenance.get("mode") != "live"
        or provenance.get("provider_transport") != "real-provider"
        or execution["usage"]["requests"] <= 0
    ):
        raise ValueError(
            "Quality measurement requires confirmed real provider execution"
        )
    fixtures = load_rules_dataset(dataset)
    rows = execution["rows"]
    required = Counter(
        (fixture.id, mode) for fixture in fixtures for mode in ("baseline", "rules")
    )
    if Counter((row["fixture_id"], row["pass"]) for row in rows) != required:
        raise ValueError("Report requires complete paired results without duplicates")
    if any(
        row.get("worker_status") not in {"COMPLETED", "PARTIAL", "FAILED", "SKIPPED"}
        for row in rows
    ):
        raise ValueError("Report contains incomplete analysis Worker evidence")
    indexed = {(row["fixture_id"], row["pass"]): row for row in rows}
    for fixture in fixtures:
        baseline, enabled = (
            indexed[fixture.id, mode] for mode in ("baseline", "rules")
        )
        policies = [
            {
                key: value
                for key, value in row["frozen_policy"].items()
                if key != "repository_settings_digest"
            }
            for row in (baseline, enabled)
        ]
        if (
            not baseline["ast"]["semantic_sha256"]
            or baseline["ast"]["semantic_sha256"] != enabled["ast"]["semantic_sha256"]
            or baseline["source"]["transport_diff_sha256"]
            != enabled["source"]["transport_diff_sha256"]
            or policies[0] != policies[1]
            or baseline.get("frozen_selection") != enabled.get("frozen_selection")
        ):
            raise ValueError(
                "Controlled comparison changed code, diff or frozen policy"
            )
    schema = json.loads(
        (
            Path(__file__).resolve().parents[2] / "schemas/review-result/v2.json"
        ).read_text()
    )
    provider_schema = json.loads(
        PromptBuilder.load(
            Path(__file__).resolve().parents[2] / ".agents/reviewer", "v4"
        ).output_schema_json
    )
    labels = {f.id: f for f in fixtures}
    scored = []
    for row in rows:
        fixture = labels[row["fixture_id"]]
        provider = evaluate_rules(
            fixture.diff_text,
            fixture.expected_findings,
            row["provider_result"],
            provider_schema,
        )
        canonical = evaluate_rules(
            fixture.diff_text,
            fixture.expected_findings,
            row["canonical_result"],
            schema,
        )
        scored.append(
            {
                **row,
                "canonical_schema_errors": canonical["schema_errors"],
                "provider_quality": provider,
                "canonical_quality": canonical,
                "filtered_findings": _filtered_findings(
                    row["provider_result"], row["canonical_result"]
                ),
                "score": provider,
            }
        )
    quality = {
        target: _quality_summary(scored, labels, target)
        for target in ("provider_quality", "canonical_quality")
    }
    return {
        "fixtures": scored,
        **quality,
        # Compatibility aliases remain explicitly scoped to the provider diagnostic.
        "scoring_target": "final-provider-output-before-worker-filtering",
        **quality["provider_quality"],
        "provenance": execution["provenance"],
        "usage": execution["usage"],
        "interpretation": (
            "Limited experiment with 12 synthetic examples; no quality improvement "
            "is established. Scripted integration is not model quality. "
            "Statistical uncertainty is not resolved by this corpus."
        ),
        "zero_denominator_convention": (
            "Precision, recall and F1 are zero when their denominator is zero; "
            "clean cases are counted separately."
        ),
        "per_rule_semantics": (
            "by_rule contains finding-level rule attribution only. "
            "Every observed nonempty rule_ref, including unknown references, "
            "is retained under its literal reference; no unknown bucket is used. "
            "by_category groups complete cases by fixture category, including "
            "detection, clean controls and validation diagnostics; overall "
            "detection is independent of rule_ref."
        ),
    }


def _filtered_findings(provider: Any, canonical: Any) -> list[dict[str, Any]]:
    """Provider findings absent from persistence, including duplicate removals."""

    def findings(result: Any) -> list[dict[str, Any]]:
        if not isinstance(result, dict) or not isinstance(result.get("findings"), list):
            return []
        return [item for item in result["findings"] if isinstance(item, dict)]

    def identity(finding: dict[str, Any]) -> str:
        # Severity/evidence can be normalized by Worker without dropping a finding.
        title = finding.get("title")
        return json.dumps(
            {
                "title": mask_secrets(title) if isinstance(title, str) else title,
                **{
                    key: finding.get(key)
                    for key in (
                        "path",
                        "side",
                        "start_line",
                        "end_line",
                        "category",
                        "rule_ref",
                    )
                },
            },
            sort_keys=True,
        )

    retained = Counter(identity(item) for item in findings(canonical))
    filtered = []
    for item in findings(provider):
        key = identity(item)
        if retained[key]:
            retained[key] -= 1
        else:
            filtered.append(item)
    return filtered


def _quality_summary(
    scored: list[dict[str, Any]],
    labels: dict[str, Any],
    target: str,
) -> dict[str, Any]:
    passes = {
        mode: _aggregate([row[target] for row in scored if row["pass"] == mode])
        for mode in ("baseline", "rules")
    }
    return {
        "passes": passes,
        "by_rule": {
            reference: {
                mode: {
                    "attribution": metrics(
                        *(
                            sum(
                                row[target]["by_rule"].get(reference, {}).get(key, 0)
                                for row in scored
                                if row["pass"] == mode
                            )
                            for key in ("tp", "fp", "fn")
                        )
                    ),
                }
                for mode in ("baseline", "rules")
            }
            for reference in sorted(
                {reference for row in scored for reference in row[target]["by_rule"]}
            )
        },
        "by_category": {
            category: {
                mode: _aggregate(
                    [
                        row[target]
                        for row in scored
                        if row["pass"] == mode
                        and labels[row["fixture_id"]].category == category
                    ]
                )
                for mode in ("baseline", "rules")
            }
            for category in sorted({fixture.category for fixture in labels.values()})
        },
        "delta": {
            group: {
                key: passes["rules"][group][key] - passes["baseline"][group][key]
                for key in passes["rules"][group]
            }
            for group in ("detection", "attribution")
        },
    }


def _aggregate(scores: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for group in ("detection", "attribution"):
        result[group] = metrics(
            *(sum(score[group][key] for score in scores) for key in ("tp", "fp", "fn"))
        )
    result["attribution"].update(
        {
            key: sum(score["attribution"][key] for score in scores)
            for key in ("missing", "wrong", "unjustified")
        }
    )
    result.update(
        clean_cases=sum(score["clean"]["case"] for score in scores),
        clean_false_positive_cases=sum(
            score["clean"]["false_positive"] for score in scores
        ),
        schema_error_cases=sum(bool(score["schema_errors"]) for score in scores),
        grounding_errors=sum(score["grounding_errors"] for score in scores),
    )
    return result


def write_rules_report(report: dict[str, Any], output_dir: Path) -> None:
    """Serialize the same observed evidence in machine and human readable forms."""
    private = report["provenance"]["mode"] == "live"
    output_dir.mkdir(mode=0o700 if private else 0o777, parents=True, exist_ok=True)
    if private:
        output_dir.chmod(0o700)
    _write_report_file(
        output_dir / "rules-quality-report.json",
        json.dumps(
            report, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False
        )
        + "\n",
        private=private,
    )
    provenance = report["provenance"]
    lines = [
        "# Rule-specific Precision/Recall benchmark",
        "",
        f"mode: {provenance['mode']}; quality_measurement: "
        f"{str(provenance['quality_measurement']).lower()}",
        "",
        report["interpretation"],
        "",
    ]
    for target in ("provider_quality", "canonical_quality"):
        lines += [f"## {target}", ""]
        lines += [
            "## Detection quality",
            "",
            "| Pass | TP | FP | FN | Precision | Recall | F1 |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
        for mode, values in report[target]["passes"].items():
            score = values["detection"]
            lines.append(
                f"| {mode} | {score['tp']} | {score['fp']} | {score['fn']} | "
                f"{score['precision']:.3f} | {score['recall']:.3f} | "
                f"{score['f1']:.3f} |"
            )
        lines += [
            "",
            "## Rule attribution",
            "",
            "| Pass | TP | FP | FN | Precision | Recall | F1 | "
            "Missing | Wrong | Unjustified |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
        for mode, values in report[target]["passes"].items():
            score = values["attribution"]
            lines.append(
                f"| {mode} | {score['tp']} | {score['fp']} | {score['fn']} | "
                f"{score['precision']:.3f} | {score['recall']:.3f} | "
                f"{score['f1']:.3f} | "
                f"{score['missing']} | {score['wrong']} | {score['unjustified']} |"
            )
        lines += [
            "",
            "## Paired cases",
            "",
            "| Example | Pass | Detection TP/FP/FN | Attribution TP/FP/FN | "
            "Schema errors | Grounding errors |",
            "| --- | --- | --- | --- | ---: | ---: |",
        ]
        for row in report["fixtures"]:
            score = row[target]
            detection, attribution = (
                score[group] for group in ("detection", "attribution")
            )
            lines.append(
                f"| {row['fixture_id']} | {row['pass']} | "
                f"{detection['tp']}/{detection['fp']}/{detection['fn']} | "
                f"{attribution['tp']}/{attribution['fp']}/{attribution['fn']} | "
                f"{len(score['schema_errors'])} | {score['grounding_errors']} |"
            )
        lines += [
            "",
            report["zero_denominator_convention"],
            "",
            "## Per-rule attribution",
            "",
            report["per_rule_semantics"],
            "",
            "```json",
            json.dumps(report[target]["by_rule"], indent=2, sort_keys=True),
            "```",
            "",
            "## Per-category fixture cohorts",
            "",
            "```json",
            json.dumps(report[target]["by_category"], indent=2, sort_keys=True),
            "```",
            "",
            "## Deltas (B minus A)",
            "",
            "```json",
            json.dumps(report[target]["delta"], indent=2, sort_keys=True),
            "```",
            "",
            "## Clean controls and validation",
            "",
            "```json",
            json.dumps(
                {
                    mode: {
                        key: value
                        for key, value in values.items()
                        if key not in ("detection", "attribution")
                    }
                    for mode, values in report[target]["passes"].items()
                },
                indent=2,
                sort_keys=True,
            ),
            "```",
            "",
        ]
    lines += [
        "## Filtered findings",
        "",
        "Provider findings absent from persisted canonical results "
        "(including deduplication).",
        "",
        "```json",
        json.dumps(
            [
                {
                    "fixture_id": row["fixture_id"],
                    "pass": row["pass"],
                    "findings": row["filtered_findings"],
                }
                for row in report["fixtures"]
                if row["filtered_findings"]
            ],
            indent=2,
            sort_keys=True,
        ),
        "```",
        "",
        "## Provenance",
        "",
        "```json",
        json.dumps(provenance, indent=2, sort_keys=True),
        "```",
        "",
        "## Usage (scripted receipts are not an invoice)",
        "",
        "```json",
        json.dumps(report["usage"], indent=2, sort_keys=True),
        "```",
        "",
        "Frozen source/base/rules identities and per-case accounting receipts "
        "are retained in the companion JSON.",
        "",
    ]
    _write_report_file(
        output_dir / "rules-quality-report.md", "\n".join(lines), private=private
    )


def _write_report_file(path: Path, content: str, *, private: bool) -> None:
    if not private:
        path.write_text(content)
        return
    # Set permissions before truncation/writing, including for existing reports.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.truncate(0)
        stream.write(content)
