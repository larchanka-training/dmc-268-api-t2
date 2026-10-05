"""CLI entrypoint: ``python -m scripts.eval_harness``."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, cast

from scripts.eval_harness.dataset import Fixture, load_dataset
from scripts.eval_harness.llm_client import LlmConfig, call_llm
from scripts.eval_harness.prompt import SYSTEM_PROMPT, build_user_message
from scripts.eval_harness.report import (
    build_report,
    evaluate_fixture,
    print_console_summary,
)

_SCHEMA_PATH = Path(__file__).parent / "schema" / "review_result.schema.json"


def _load_schema() -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(_SCHEMA_PATH.read_text(encoding="utf-8")))


def _offline_response(fixture: Fixture) -> Any:
    return fixture.golden_response


def _live_response(fixture: Fixture, config: LlmConfig) -> Any:
    raw_text = call_llm(
        config,
        system_prompt=SYSTEM_PROMPT,
        user_message=build_user_message(
            title=fixture.title,
            description=fixture.description,
            diff_text=fixture.diff_text,
        ),
    )
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        return raw_text


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.eval_harness",
        description="Run the gold-benchmark eval harness against test-prs-dataset.",
    )
    parser.add_argument(
        "--mode",
        choices=["offline", "live"],
        default="offline",
        help="offline (default): replay golden_response.json, no network/API key. "
        "live: call a real LLM configured via EVAL_LLM_* env vars.",
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
    return parser.parse_args(argv)


def run(argv: list[str] | None = None) -> dict[str, Any]:
    args = parse_args(argv)
    schema = _load_schema()
    fixtures = load_dataset(args.dataset_dir, category_filter=args.filter)

    llm_config = LlmConfig.from_env() if args.mode == "live" else None

    reports = []
    for fixture in fixtures:
        response = (
            _offline_response(fixture)
            if llm_config is None
            else _live_response(fixture, llm_config)
        )
        reports.append(evaluate_fixture(fixture, response, schema))

    report = build_report(reports)
    print_console_summary(report)

    args.report_out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


def main(argv: list[str] | None = None) -> int:
    run(argv)
    return 0
