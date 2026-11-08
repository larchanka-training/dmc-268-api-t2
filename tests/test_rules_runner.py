"""Safety and real Worker seams for the separate QA-04B campaign."""

# ruff: noqa: PLR2004 -- fixed corpus/protocol counts

import hashlib
import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from scripts.eval_harness import rules_runner
from scripts.eval_harness.provenance import dataset_digest, digest
from scripts.eval_harness.rules_dataset import load_rules_dataset
from scripts.eval_harness.rules_runner import (
    run_rules_benchmark,
    validate_frozen_identity,
    validate_live_approval,
)
from scripts.eval_harness.worker import cleanup_campaign, load_sources, runtime_values
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.domain.settings import compute_rules_digest


async def test_live_requires_new_explicit_approval_before_any_resources() -> None:
    with pytest.raises(ValueError, match="approval"):
        await run_rules_benchmark(Path("missing"), live=True, disposable=True)


async def test_runner_requires_explicit_disposable_environment() -> None:
    with pytest.raises(ValueError, match="disposable"):
        await run_rules_benchmark(Path("missing"))


async def test_deterministic_campaign_processes_twelve_paired_real_worker_jobs() -> (
    None
):

    if not os.environ.get("DMC268_TEST_DATABASE_URL"):
        pytest.skip("requires disposable PostgreSQL/Redis")
    result = await run_rules_benchmark(
        Path("tests/fixtures/rules-benchmark"), disposable=True
    )
    assert len(result["rows"]) == 24
    assert result["provenance"]["quality_measurement"] is False
    assert result["usage"]["requests"] == 24
    assert all(row["canonical_result"] is not None for row in result["rows"])


async def test_runner_rejects_remote_database_before_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "DMC268_TEST_DATABASE_URL", "postgresql+asyncpg://x:x@staging.invalid/db"
    )
    monkeypatch.setenv("DMC268_TEST_REDIS_URL", "redis://127.0.0.1:56379/0")

    with pytest.raises(ValueError, match="localhost"):
        rules_runner.disposable_urls()


def test_frozen_identity_rejects_digest_that_disagrees_with_expected_rules() -> None:
    with pytest.raises(ValueError, match="Frozen rules identity"):
        rules_runner.validate_frozen_identity(
            {"rules_digest": "0" * 64},
            base="a" * 40,
            head="b" * 40,
            rules=None,
        )


def test_frozen_identity_rejects_rules_from_another_base_revision() -> None:
    with pytest.raises(ValueError, match="base revision"):
        validate_frozen_identity(
            {
                "rules_digest": compute_rules_digest({"instructions": []}),
                "rules_source": {"file": {"revision": "c" * 40}},
            },
            base="a" * 40,
            head="b" * 40,
            rules=None,
        )


def test_frozen_identity_rejects_analysis_of_different_code_snapshot() -> None:
    with pytest.raises(ValueError, match="code snapshot"):
        validate_frozen_identity(
            {
                "rules_digest": compute_rules_digest({"instructions": []}),
                "rules_source": {
                    "file": {
                        "revision": "a" * 40,
                        "state": "absent",
                        "content_digest": None,
                    }
                },
                "context_snapshots": [{"base_sha": "a" * 40, "head_sha": "c" * 40}],
            },
            base="a" * 40,
            head="b" * 40,
            rules=None,
        )


async def test_worker_reports_provider_findings_before_unknown_refs_are_filtered() -> (
    None
):

    if not os.environ.get("DMC268_TEST_DATABASE_URL"):
        pytest.skip("requires disposable PostgreSQL/Redis")
    result = await run_rules_benchmark(
        Path("tests/fixtures/rules-benchmark"), disposable=True
    )
    baseline = result["rows"][0]
    assert (
        baseline["provider_observations"][0]["response"]["findings"][0]["rule_ref"]
        == "SEC-1"
    )
    assert baseline["canonical_result"]["findings"] == []


def approval_document(dataset: Path, journal: Path) -> dict[str, object]:

    _, source_digest = load_sources(dataset / "sources", dataset)
    return {
        "allow_live": True,
        "dataset_sha256": dataset_digest(dataset),
        "source_bundle_sha256": source_digest,
        "rules_sha256": hashlib.sha256((dataset / "rules.md").read_bytes()).hexdigest(),
        "runtime_sha256": digest(runtime_values()),
        "model": "mistral-small-4",
        "destination": "https://api.eurouter.ai/api/v1/chat/completions",
        "input_usd_per_million": "0.25",
        "output_usd_per_million": "1",
        "prices_verified_at": datetime.now(UTC).isoformat(),
        "price_evidence": "Synthetic test evidence; no real provider prices claimed",
        "max_cost_usd": "1",
        "max_requests": 24,
        "journal_path": str(journal.resolve()),
    }


def test_live_approval_refuses_another_corpus(tmp_path: Path) -> None:
    dataset = Path("tests/fixtures/rules-benchmark")
    journal = tmp_path / "once.json"
    document = approval_document(dataset, journal)
    document["dataset_sha256"] = "0" * 64
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="approval identity"):
        rules_runner.validate_live_approval(approval, dataset, journal)


@pytest.mark.parametrize("value", ["0", "-1", "NaN", "Infinity"])
def test_live_approval_requires_positive_finite_prices(
    tmp_path: Path, value: str
) -> None:
    dataset = Path("tests/fixtures/rules-benchmark")
    journal = tmp_path / "once.json"
    document = approval_document(dataset, journal)
    document["input_usd_per_million"] = value
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="positive finite"):
        validate_live_approval(approval, dataset, journal)


def test_live_approval_cap_must_cover_entire_pair_campaign(tmp_path: Path) -> None:
    dataset = Path("tests/fixtures/rules-benchmark")
    journal = tmp_path / "once.json"
    document = approval_document(dataset, journal)
    document["max_cost_usd"] = "0.001"
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="reserved upper bound"):
        validate_live_approval(approval, dataset, journal)


@pytest.mark.parametrize("value", [0, -1, 193, True, 1.5])
def test_live_approval_requires_bounded_integer_request_count(
    tmp_path: Path, value: object
) -> None:
    dataset = Path("tests/fixtures/rules-benchmark")
    journal = tmp_path / "once.json"
    document = approval_document(dataset, journal)
    document["max_requests"] = value
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="request count"):
        validate_live_approval(approval, dataset, journal)


def test_live_approval_requires_recent_price_verification_evidence(
    tmp_path: Path,
) -> None:
    dataset = Path("tests/fixtures/rules-benchmark")
    journal = tmp_path / "once.json"
    document = approval_document(dataset, journal)
    document["prices_verified_at"] = "2000-01-01T00:00:00+00:00"
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="price evidence"):
        validate_live_approval(approval, dataset, journal)


async def test_live_worker_uses_injected_provider_and_durable_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not os.environ.get("DMC268_TEST_DATABASE_URL"):
        pytest.skip("requires disposable PostgreSQL/Redis")
    dataset = Path("tests/fixtures/rules-benchmark")
    journal = tmp_path / "once.json"
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps(approval_document(dataset, journal)))
    monkeypatch.setenv("LLM__EUROROUTER__API_KEYS", "synthetic-provider-test")
    calls: list[dict[str, Any]] = []

    def provider(request: httpx.Request) -> httpx.Response:
        state = json.loads(journal.read_text())
        assert state["state"] == "running" and state["jobs"]
        assert state["requests"] == len(calls) + 1
        body = json.loads(request.content)
        assert body["model"] == "mistral-small-4"
        calls.append(body)
        return httpx.Response(
            200,
            json={
                "model": "mistral-small-4",
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "summary": "No findings",
                                    "findings": [],
                                    "limitations": [],
                                }
                            )
                        }
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 40},
            },
        )

    try:
        result = await run_rules_benchmark(
            dataset,
            live=True,
            disposable=True,
            approval=approval,
            journal=journal,
            provider_transport=httpx.MockTransport(provider),
        )
        assert len(calls) == result["usage"]["requests"] == 24
        assert result["provenance"]["quality_measurement"] is False
        assert result["provenance"]["provider_transport"] == "injected-test"
        assert json.loads(journal.read_text())["state"] == "complete"
        with pytest.raises(ValueError, match="journal exists"):
            await run_rules_benchmark(
                dataset,
                live=True,
                disposable=True,
                approval=approval,
                journal=journal,
                provider_transport=httpx.MockTransport(provider),
            )
        assert len(calls) == 24
    finally:
        await cleanup_injected_campaign(journal)


@pytest.mark.parametrize("value", [1, "true"])
def test_live_approval_requires_literal_true(tmp_path: Path, value: object) -> None:
    dataset = Path("tests/fixtures/rules-benchmark")
    journal = tmp_path / "once.json"
    document = approval_document(dataset, journal)
    document["allow_live"] = value
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="approval identity"):
        validate_live_approval(approval, dataset, journal)


def test_cli_live_refuses_without_approval() -> None:
    with pytest.raises(ValueError, match="approval"):
        rules_runner.run(["--mode", "live", "--disposable"])


def test_frozen_identity_rejects_multiple_chunks_for_single_result_scoring() -> None:
    with pytest.raises(ValueError, match="single chunk"):
        validate_frozen_identity(
            {
                "rules_digest": compute_rules_digest({"instructions": []}),
                "rules_source": {
                    "file": {
                        "revision": "a" * 40,
                        "state": "absent",
                        "content_digest": None,
                    }
                },
                "context_snapshots": [],
                "chunk_count": 2,
            },
            base="a" * 40,
            head="b" * 40,
            rules=None,
        )


def test_provider_request_refuses_fixture_labels_in_metadata() -> None:
    fixture = load_rules_dataset(Path("tests/fixtures/rules-benchmark"))[0]
    with pytest.raises(ValueError, match="Ground-truth"):
        rules_runner.validate_model_input({"metadata": {"case": fixture.id}}, fixture)


@pytest.mark.parametrize("field", ["title", "description"])
@pytest.mark.parametrize("value", ['Synthetic "quoted" label', "Synthetic\nlabel"])
def test_provider_request_refuses_escaped_fixture_metadata(
    field: str, value: str
) -> None:
    metadata: dict[str, Any] = {field: value}
    fixture = replace(
        load_rules_dataset(Path("tests/fixtures/rules-benchmark"))[0],
        **metadata,
    )
    body = {"metadata": {"labels": [{"text": f"Prefix: {value}; suffix"}]}}
    with pytest.raises(ValueError, match="Ground-truth"):
        rules_runner.validate_model_input(body, fixture)


def test_frozen_identity_requires_actual_rules_file_content_and_parser_state() -> None:
    with pytest.raises(ValueError, match="rules file"):
        validate_frozen_identity(
            {
                "rules_digest": compute_rules_digest({"instructions": []}),
                "rules_source": {
                    "file": {
                        "revision": "a" * 40,
                        "state": "valid",
                        "content_digest": "0" * 64,
                    }
                },
                "context_snapshots": [],
            },
            base="a" * 40,
            head="b" * 40,
            rules=None,
        )


def test_live_price_requires_decimal_string_not_boolean(tmp_path: Path) -> None:
    dataset = Path("tests/fixtures/rules-benchmark")
    journal = tmp_path / "once.json"
    document = approval_document(dataset, journal)
    document["output_usd_per_million"] = True
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="positive finite"):
        validate_live_approval(approval, dataset, journal)


def test_frozen_selection_must_match_exact_runtime_model_and_prompt() -> None:
    expected = runtime_values()["admission"]
    with pytest.raises(ValueError, match="model or prompt"):
        rules_runner.validate_frozen_selection(
            {
                "model_ref": "different-model",
                "model_digest": None,
                "prompt_version": expected["prompt_version"],
                "prompt_digest": expected["prompt_digest"],
            },
            expected,
        )


async def cleanup_injected_campaign(journal: Path) -> None:
    state = json.loads(journal.read_text())
    engine = create_async_engine(os.environ["DMC268_TEST_DATABASE_URL"])
    try:
        await cleanup_campaign(
            async_sessionmaker(engine),
            [UUID(value) for value in state["repositories"]],
            [UUID(value) for value in state["actors"]],
        )
    finally:
        await engine.dispose()


async def test_live_request_cap_stops_before_second_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not os.environ.get("DMC268_TEST_DATABASE_URL"):
        pytest.skip("requires disposable PostgreSQL/Redis")
    dataset = Path("tests/fixtures/rules-benchmark")
    journal = tmp_path / "once.json"
    approval = tmp_path / "approval.json"
    document = approval_document(dataset, journal)
    document["max_requests"] = 1
    approval.write_text(json.dumps(document))
    monkeypatch.setenv("LLM__EUROROUTER__API_KEYS", "synthetic-provider-test")
    calls: list[httpx.Request] = []

    def provider(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "model": "mistral-small-4",
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "summary": "No findings",
                                    "findings": [],
                                    "limitations": [],
                                }
                            )
                        }
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 40},
            },
        )

    try:
        with pytest.raises(ValueError, match="request count"):
            await run_rules_benchmark(
                dataset,
                live=True,
                disposable=True,
                approval=approval,
                journal=journal,
                provider_transport=httpx.MockTransport(provider),
            )
        assert len(calls) == 1
        assert json.loads(journal.read_text())["state"] == "uncertain"
    finally:
        await cleanup_injected_campaign(journal)


async def test_live_unknown_receipts_remain_charged_and_refuse_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not os.environ.get("DMC268_TEST_DATABASE_URL"):
        pytest.skip("requires disposable PostgreSQL/Redis")
    dataset = Path("tests/fixtures/rules-benchmark")
    journal = tmp_path / "once.json"
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps(approval_document(dataset, journal)))
    monkeypatch.setenv("LLM__EUROROUTER__API_KEYS", "synthetic-provider-test")
    calls: list[httpx.Request] = []

    def provider(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ReadTimeout("controlled unknown usage", request=request)

    try:
        with pytest.raises(ValueError, match="Uncertain provider usage"):
            await run_rules_benchmark(
                dataset,
                live=True,
                disposable=True,
                approval=approval,
                journal=journal,
                provider_transport=httpx.MockTransport(provider),
            )
        state = json.loads(journal.read_text())
        assert state["state"] == "uncertain"
        assert state["unknown_usage_receipts"]
        assert all(
            receipt["actual_input_tokens"] is None
            and receipt["reserved_input_tokens"] > 0
            for receipt in state["unknown_usage_receipts"]
        )
        previous = len(calls)
        with pytest.raises(ValueError, match="journal exists"):
            await run_rules_benchmark(
                dataset,
                live=True,
                disposable=True,
                approval=approval,
                journal=journal,
                provider_transport=httpx.MockTransport(provider),
            )
        assert len(calls) == previous
    finally:
        await cleanup_injected_campaign(journal)


def test_nonterminal_analysis_cannot_complete_campaign() -> None:
    with pytest.raises(ValueError, match="incomplete Worker"):
        rules_runner.validate_analysis_complete({"worker_status": "LLM_PROCESSING"})
