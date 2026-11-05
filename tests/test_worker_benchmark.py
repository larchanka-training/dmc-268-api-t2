"""Public worker benchmark CLI, never model-quality or real provider evidence."""

# ruff: noqa: PLR2004 -- fixed schema/token protocol vectors

import asyncio
import json
import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from scripts.eval_harness.cli import run
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import models

pytestmark = pytest.mark.skipif(
    not os.environ.get("DMC268_TEST_DATABASE_URL")
    or not os.environ.get("DMC268_TEST_REDIS_URL"),
    reason="requires disposable PostgreSQL and Redis",
)


def test_worker_cli_runs_one_attested_case_through_persisted_v2(
    tmp_path: Path,
) -> None:
    output = tmp_path / "worker.json"
    report = run(
        [
            "--scope",
            "worker",
            "--mode",
            "deterministic",
            "--worker-pass",
            "baseline",
            "--fixture",
            "sec-001-sql-injection",
            "--report-out",
            str(output),
        ]
    )
    assert json.loads(output.read_text()) == report
    assert report["provenance"]["scope"] == "worker-integration"
    assert report["provenance"]["quality_measurement"] is False
    assert (
        report["provenance"]["dataset_sha256"]
        == "b7c2ad5a5ff606d4985af74191143fbf106e5aeabcd9b96f209397b1075f5396"
    )
    assert len(report["fixtures"]) == 1
    case = report["fixtures"][0]
    assert case["pass"] == "baseline"
    assert case["canonical_result_valid"] is True
    assert case["canonical_result"]["schema_version"] == 2
    assert case["ast"]["source_window_count"] + case["ast"]["whole_file_count"] > 0
    assert case["requests"] == 1
    assert case["true_positives"] == 1
    assert case["false_positives"] == case["false_negatives"] == 0


def test_worker_campaign_is_a_controlled_three_pass_comparison(tmp_path: Path) -> None:
    report = run(
        [
            "--scope",
            "worker",
            "--mode",
            "deterministic",
            "--worker-pass",
            "all",
            "--fixture",
            "sec-001-sql-injection",
            "--report-out",
            str(tmp_path / "campaign.json"),
        ]
    )
    baseline, rules, reflection = report["fixtures"]
    assert [row["pass"] for row in report["fixtures"]] == [
        "baseline",
        "rules",
        "reflection",
    ]
    assert baseline["source"]["head"] != rules["source"]["head"]
    assert rules["source"] == reflection["source"]
    assert len({row["ast"]["semantic_sha256"] for row in report["fixtures"]}) == 1
    assert [row["requests"] for row in report["fixtures"]] == [1, 1, 2]
    assert baseline["rules_source_state"] == "absent"
    assert rules["rules_source_state"] == reflection["rules_source_state"] == "valid"
    assert rules["rules_digest"] == reflection["rules_digest"]
    assert all(row["canonical_result_valid"] for row in report["fixtures"])
    assert report["controlled_comparison"]["valid"] is True
    assert report["provenance"]["quality_measurement"] is False


def test_worker_report_pins_schema_config_and_charged_receipts(tmp_path: Path) -> None:
    report = run(
        [
            "--scope",
            "worker",
            "--mode",
            "deterministic",
            "--worker-pass",
            "baseline",
            "--fixture",
            "sec-001-sql-injection",
            "--report-out",
            str(tmp_path / "receipt.json"),
        ]
    )
    provenance = report["provenance"]
    for key in (
        "provider_schema_sha256",
        "canonical_schema_sha256",
        "config_sha256",
        "lockfile_sha256",
    ):
        assert len(provenance[key]) == 64
    assert (
        provenance["matching_policy"]
        == "path-side-category-overlap-greedy-one-to-one-production-anchor/v2"
    )
    assert (
        report["usage"]["known_input_tokens"]
        == report["usage"]["charged_input_tokens"]
        == 100
    )
    assert (
        report["usage"]["known_output_tokens"]
        == report["usage"]["charged_output_tokens"]
        == 40
    )
    assert report["usage"]["unknown_attempts"] == 0
    assert report["usage"]["cost_usd"] == "0"


def test_worker_cli_cleans_only_its_campaign_records_after_reporting(
    tmp_path: Path,
) -> None:
    sentinel = uuid4()

    async def snapshot(
        *, create: bool = False, remove: bool = False
    ) -> list[set[UUID]]:
        engine = create_async_engine(os.environ["DMC268_TEST_DATABASE_URL"])
        try:
            async with async_sessionmaker(engine).begin() as session:
                if create:
                    session.add(models.User(id=sentinel))
                    await session.flush()
                if remove:
                    await session.execute(
                        delete(models.User).where(models.User.id == sentinel)
                    )
                return [
                    set(await session.scalars(select(table.id)))
                    for table in (
                        models.User,
                        models.Repository,
                        models.RepositorySettings,
                        models.ReviewJob,
                    )
                ]
        finally:
            await engine.dispose()

    before = asyncio.run(snapshot(create=True))
    try:
        report = run(
            [
                "--scope",
                "worker",
                "--mode",
                "deterministic",
                "--worker-pass",
                "baseline",
                "--fixture",
                "sec-001-sql-injection",
                "--report-out",
                str(tmp_path / "clean.json"),
            ]
        )
        assert report["fixtures"][0]["canonical_result_valid"] is True
        assert report["fixtures"][0]["usage_receipts"]
        assert asyncio.run(snapshot()) == before
    finally:
        asyncio.run(snapshot(remove=True))
