"""CLI entrypoint: ``python -m scripts.eval_harness``."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

import httpx

from app.review.prompt import PromptBuilder
from scripts.eval_harness.dataset import Fixture, load_dataset
from scripts.eval_harness.llm_client import LlmConfig, LlmConfigurationError, call_llm
from scripts.eval_harness.provenance import digest, replay_provenance
from scripts.eval_harness.report import (
    build_report,
    evaluate_fixture,
    print_console_summary,
)
from scripts.eval_harness.runtime import deterministic_response

ROOT = Path(__file__).resolve().parents[2]
PROMPTS = ROOT / ".agents/reviewer"


def _offline_response(fixture: Fixture) -> Any:
    return fixture.golden_response


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.eval_harness",
        description="Run the gold-benchmark eval harness against test-prs-dataset.",
    )
    parser.add_argument("--scope", choices=["gateway", "worker"], default="gateway")
    parser.add_argument(
        "--worker-pass",
        choices=["baseline", "rules", "reflection", "all"],
        default="all",
    )
    parser.add_argument("--fixture")
    parser.add_argument(
        "--mode",
        choices=["offline", "deterministic", "live"],
        default="offline",
        help="offline (default): replay golden_response.json, no network/API key. "
        "deterministic: verified prompt/Gateway with local HTTP replay. "
        "live: explicitly approved runtime manifest and spend ceilings.",
    )
    parser.add_argument(
        "--dataset-dir",
        type=Path,
        default=Path("test-prs-dataset"),
        help="Path to the dataset directory (default: ./test-prs-dataset).",
    )
    parser.add_argument(
        "--report-out",
        type=Path,
        default=Path("eval_report.json"),
        help="Where to write the machine-readable JSON report.",
    )
    parser.add_argument(
        "--filter",
        default=None,
        help="Only evaluate fixtures whose category equals this value.",
    )
    parser.add_argument("--runtime-manifest", type=Path)
    parser.add_argument("--allow-live", action="store_true")
    parser.add_argument("--max-cost-usd")
    parser.add_argument("--input-usd-per-million")
    parser.add_argument("--output-usd-per-million")
    parser.add_argument("--campaign-journal", type=Path)
    return parser.parse_args(argv)


def run(
    argv: list[str] | None = None, *, transport: httpx.AsyncBaseTransport | None = None
) -> dict[str, Any]:
    args = parse_args(argv)
    if args.mode == "live" and not all(
        (
            args.allow_live,
            args.runtime_manifest,
            args.max_cost_usd,
            args.input_usd_per_million,
            args.output_usd_per_million,
        )
    ):
        raise LlmConfigurationError(
            "Live mode requires an explicit manifest, approval, cost cap "
            "and input/output price ceilings"
        )
    fixtures = load_dataset(args.dataset_dir, category_filter=args.filter)
    if not fixtures:
        raise ValueError("No fixtures selected; check the manifest and category filter")
    if args.fixture:
        fixtures = [fixture for fixture in fixtures if fixture.id == args.fixture]
        if not fixtures:
            raise ValueError("No fixture selected")
    if args.scope == "worker":
        if args.mode not in {"deterministic", "live"}:
            raise LlmConfigurationError(
                "Worker scope requires deterministic or explicitly approved live mode"
            )
        from scripts.eval_harness.worker import run_worker  # noqa: PLC0415

        live_config = (
            LlmConfig.approved(
                args.runtime_manifest,
                args.max_cost_usd,
                args.input_usd_per_million,
                args.output_usd_per_million,
                len(fixtures),
            )
            if args.mode == "live"
            else None
        )
        if live_config and (
            args.fixture
            or args.filter
            or args.worker_pass != "all"
            or not args.campaign_journal
        ):
            raise LlmConfigurationError(
                "Live worker requires the full three-pass corpus "
                "and a new campaign journal"
            )
        report = asyncio.run(
            run_worker(
                fixtures,
                args.dataset_dir,
                args.dataset_dir / "worker-sources",
                passes=("baseline", "rules", "reflection")
                if args.worker_pass == "all"
                else (args.worker_pass,),
                live_config=live_config,
                journal_path=args.campaign_journal,
                provider_transport=transport,
            )
        )
        args.report_out.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        )
        print_console_summary(report)
        return report

    llm_config = (
        LlmConfig.approved(
            args.runtime_manifest,
            args.max_cost_usd,
            args.input_usd_per_million,
            args.output_usd_per_million,
            len(fixtures),
        )
        if args.mode == "live"
        else None
    )
    builder = (
        PromptBuilder.load(
            PROMPTS,
            llm_config.runtime.admission.prompt_version,
            expected_digest=llm_config.runtime.admission.prompt_digest,
        )
        if llm_config
        else PromptBuilder.load(PROMPTS, "v3")
    )
    schema = json.loads(builder.output_schema_json)
    requests = input_tokens = output_tokens = 0
    reports = []
    receipts: list[dict[str, Any]] = []
    for fixture in fixtures:
        response = _offline_response(fixture)
        receipt: dict[str, Any] = {
            "provider": None,
            "model": None,
            "error_code": None,
            "requests": 0,
            "input_tokens": 0,
            "output_tokens": 0,
        }
        if args.mode != "offline":
            observation = (
                asyncio.run(call_llm(fixture, builder, llm_config, transport=transport))
                if llm_config
                else asyncio.run(deterministic_response(fixture, builder))
            )
            response = observation.response
            requests += observation.requests
            input_tokens += observation.input_tokens
            output_tokens += observation.output_tokens
            receipt = {
                "provider": observation.provider,
                "model": observation.model,
                "error_code": observation.error_code,
                "requests": observation.requests,
                "input_tokens": observation.input_tokens,
                "output_tokens": observation.output_tokens,
            }
        receipts.append(receipt)
        reports.append(evaluate_fixture(fixture, response, schema))

    report = build_report(reports)
    for row, receipt in zip(report["fixtures"], receipts, strict=True):
        row.update(receipt)
    report["provenance"] = replay_provenance(args.dataset_dir, builder, args.filter)
    if args.mode == "deterministic":
        report["provenance"].update(
            mode="deterministic",
            provider="mock",
            model="gold-replay",
            scope="verified-initial-prompt/Gateway with HTTP replay; no model quality",
        )
    if llm_config:
        report["provenance"].update(
            mode="live",
            provider=llm_config.runtime.admission.primary.profile_ref,
            model=llm_config.runtime.admission.primary.model_ref,
            runtime_manifest_sha256=llm_config.runtime.identity,
            quality_measurement=True,
            scope="verified initial prompt/Gateway; no worker admission or repair",
            cost_basis=(
                "price ceilings times charged token receipts/reservations; "
                "estimate, not invoice"
            ),
            max_cost_usd=str(llm_config.cap),
            input_usd_per_million=str(llm_config.input_price),
            output_usd_per_million=str(llm_config.output_price),
        )
    report["provenance"]["config_sha256"] = digest(
        {
            key: value
            for key, value in report["provenance"].items()
            if key not in {"config_sha256", "dataset_sha256"}
        }
    )
    report["usage"] = {
        "requests": requests,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": str(llm_config.cost(input_tokens, output_tokens))
        if llm_config
        else "0",
    }
    print_console_summary(report)

    args.report_out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


def main(argv: list[str] | None = None) -> int:
    report = run(argv)
    summary = report["aggregate"]
    return int(
        summary["shape_valid_count"] != summary["fixtures_total"]
        or (
            report["provenance"]["mode"] != "live"
            and (summary["false_positives"] or summary["false_negatives"])
        )
        or summary["findings_grounded"] != summary["findings_predicted"]
    )
