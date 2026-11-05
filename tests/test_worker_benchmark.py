"""Public worker benchmark CLI, never model-quality or real provider evidence."""

# ruff: noqa: PLR2004 -- fixed schema/token protocol vectors

import asyncio
import json
import os
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from scripts.eval_harness.cli import run
from scripts.eval_harness.dataset import load_dataset
from scripts.eval_harness.llm_client import LlmConfigurationError
from scripts.eval_harness.worker import cleanup_campaign, runtime_values
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import models
from app.review.configuration import load_runtime_configuration

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


def live_arguments(tmp_path: Path) -> list[str]:
    manifest = tmp_path / "runtime.json"
    manifest.write_text(json.dumps(runtime_values()))
    return [
        "--scope",
        "worker",
        "--mode",
        "live",
        "--allow-live",
        "--runtime-manifest",
        str(manifest),
        "--max-cost-usd",
        "1",
        "--input-usd-per-million",
        "0.25",
        "--output-usd-per-million",
        "1",
        "--campaign-journal",
        str(tmp_path / "campaign.json"),
        "--report-out",
        str(tmp_path / "live.json"),
    ]


def cleanup_live_test(journal: Path) -> None:
    if not journal.exists():
        return
    values = json.loads(journal.read_text())

    async def cleanup() -> None:
        engine = create_async_engine(os.environ["DMC268_TEST_DATABASE_URL"])
        try:
            await cleanup_campaign(
                async_sessionmaker(engine),
                list(map(UUID, values["repositories"])),
                list(map(UUID, values["actors"])),
            )
        finally:
            await engine.dispose()

    asyncio.run(cleanup())


def test_live_worker_cli_uses_real_worker_with_injected_provider_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arguments = live_arguments(tmp_path)
    journal = tmp_path / "campaign.json"
    fixtures = load_dataset(Path("test-prs-dataset"))
    fixtures_by_id = {fixture.id: fixture for fixture in fixtures}
    calls: list[dict[str, Any]] = []
    monkeypatch.setenv("LLM__EUROROUTER__API_KEYS", "synthetic-provider-test")

    def provider(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.eurouter.ai/api/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer synthetic-provider-test"
        state = json.loads(journal.read_text())
        assert state["reserved_upper_bound_usd"] == "2.592"
        assert state["state"] == "running" and state["jobs"]
        job = state["jobs"][-1]
        current = fixtures_by_id[job["fixture_id"]]
        body = json.loads(request.content)
        calls.append(body)
        answer: dict[str, Any]
        if "verdicts" in body["response_format"]["json_schema"]["schema"]["properties"]:
            answer = {
                "verdicts": [
                    {"index": i, "verdict": "confirmed", "reason": "verified"}
                    for i in range(len(current.golden_response["findings"]))
                ]
            }
        else:
            answer = (
                {"invalid": True}
                if current.id == fixtures[0].id and job["pass"] == "baseline"
                else current.golden_response
            )
        return httpx.Response(
            200,
            json={
                "model": "mistral-small-4",
                "choices": [{"message": {"content": json.dumps(answer)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 40},
            },
        )

    try:
        report = run(arguments, transport=httpx.MockTransport(provider))
        assert len(calls) == report["usage"]["requests"] == 88
        assert len(report["fixtures"]) == 72
        assert report["provenance"]["mode"] == "live"
        assert report["provenance"]["quality_measurement"] is False
        assert report["provenance"]["provider_transport"] == "injected-test"
        assert (
            report["provenance"]["runtime_manifest_sha256"]
            == load_runtime_configuration(tmp_path / "runtime.json").identity
        )
        assert report["usage"]["cost_usd"] == "0.00572"
        assert report["usage"]["unknown_attempts"] == 0
        assert json.loads(journal.read_text())["state"] == "complete"
        assert journal.stat().st_mode & 0o777 == 0o600
        assert report["aggregate"]["shape_valid_count"] == 71
        assert report["aggregate"]["false_negatives"] == 1
        failed = report["fixtures"][0]
        assert failed["canonical_result_valid"] is False
        assert failed["worker_status"] == "FAILED"
        assert len(failed["usage_receipts"]) == 2
        for offset in range(0, 72, 3):
            baseline, rules, reflection = report["fixtures"][offset : offset + 3]
            assert rules["source"] == reflection["source"]
            assert (
                len(
                    {
                        row["ast"]["semantic_sha256"]
                        for row in (baseline, rules, reflection)
                    }
                )
                == 1
            )
            assert baseline["rules_source_state"] == "absent"
            assert (
                rules["rules_source_state"]
                == reflection["rules_source_state"]
                == "valid"
            )
    finally:
        cleanup_live_test(journal)


@pytest.mark.parametrize(
    ("option", "value"), [("--max-cost-usd", "1.01"), ("--input-usd-per-million", "0")]
)
def test_live_worker_rejects_unapproved_cost_ceilings_before_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, option: str, value: str
) -> None:
    arguments = live_arguments(tmp_path)
    arguments[arguments.index(option) + 1] = value
    journal = tmp_path / "campaign.json"
    monkeypatch.setenv("LLM__EUROROUTER__API_KEYS", "synthetic-provider-test")
    calls = []

    def provider(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise AssertionError("Unapproved provider dispatch")

    try:
        with pytest.raises(LlmConfigurationError, match="approval ceilings"):
            run(arguments, transport=httpx.MockTransport(provider))
        assert not calls and not journal.exists()
    finally:
        cleanup_live_test(journal)


def test_live_worker_keeps_unknown_receipts_and_refuses_uncertain_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arguments = live_arguments(tmp_path)
    journal = tmp_path / "campaign.json"
    monkeypatch.setenv("LLM__EUROROUTER__API_KEYS", "synthetic-provider-test")
    calls = []

    def provider(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert len(json.loads(journal.read_text())["jobs"]) == 1
        raise httpx.ReadTimeout("sensitive-provider-exception-text", request=request)

    try:
        with pytest.raises(LlmConfigurationError, match="uncertain"):
            run(arguments, transport=httpx.MockTransport(provider))
        state = json.loads(journal.read_text())
        assert state["state"] == "uncertain" and len(state["jobs"]) == 1
        assert state["reserved_upper_bound_usd"] == "2.592"
        assert state["uncertain_usage_receipts"]
        assert all(
            receipt["actual_input_tokens"] is None
            for receipt in state["uncertain_usage_receipts"]
        )
        assert "sensitive-provider-exception-text" not in journal.read_text()
        previous = len(calls)
        with pytest.raises(LlmConfigurationError, match="journal exists"):
            run(arguments, transport=httpx.MockTransport(provider))
        assert len(calls) == previous
    finally:
        cleanup_live_test(journal)
