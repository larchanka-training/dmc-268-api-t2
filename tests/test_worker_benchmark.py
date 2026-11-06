"""Public worker benchmark CLI, never model-quality or real provider evidence."""

# ruff: noqa: PLR2004 -- fixed schema/token protocol vectors

import asyncio
import hashlib
import json
import os
import shutil
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from scripts.eval_harness.cli import run
from scripts.eval_harness.dataset import load_dataset
from scripts.eval_harness.llm_client import LlmConfigurationError
from scripts.eval_harness.worker import (
    CaseTransport,
    cleanup_campaign,
    git_object,
    load_sources,
    run_worker,
    runtime_values,
    write_journal,
)
from sqlalchemy import delete, event, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_analysis_accounting import Execution, reserve

from app.db import models
from app.db.publication_models import PublicationOperation
from app.review.configuration import load_runtime_configuration

pytest_plugins = ("test_analysis_handler", "test_analysis_accounting")

pytestmark = pytest.mark.skipif(
    not os.environ.get("DMC268_TEST_DATABASE_URL")
    or not os.environ.get("DMC268_TEST_REDIS_URL"),
    reason="requires disposable PostgreSQL and Redis",
)


def test_campaign_journal_keeps_checkpoint_when_new_write_cannot_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = tmp_path / "campaign.json"
    previous = {"state": "running", "jobs": ["previous-owned-job"]}
    write_journal(journal, previous, create=True)

    def failed_sync(_descriptor: int) -> None:
        raise OSError("controlled journal sync failure")

    monkeypatch.setattr(os, "fsync", failed_sync)
    with pytest.raises(OSError, match="controlled journal sync failure"):
        write_journal(journal, {"state": "complete", "jobs": ["new-owned-job"]})
    assert json.loads(journal.read_text()) == previous
    assert journal.stat().st_mode & 0o777 == 0o600
    assert list(tmp_path.iterdir()) == [journal]


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


def test_worker_campaign_never_dispatches_another_review(
    tmp_path: Path, review_id: UUID
) -> None:
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
            str(tmp_path / "isolated.json"),
        ]
    )
    assert report["fixtures"][0]["canonical_result_valid"] is True

    async def foreign_state() -> None:
        engine = create_async_engine(os.environ["DMC268_TEST_DATABASE_URL"])
        try:
            async with async_sessionmaker(engine)() as session:
                job = await session.get(models.ReviewJob, review_id)
                event = (
                    await session.scalars(
                        select(models.OutboxEvent).where(
                            models.OutboxEvent.review_job_id == review_id,
                            models.OutboxEvent.task_kind == "analyze",
                        )
                    )
                ).one()
                assert job is not None and job.status == "QUEUED"
                assert event.broker_message_id is None
                assert event.broker_published_at is None
                assert event.dispatch_not_before is None
        finally:
            await engine.dispose()

    asyncio.run(foreign_state())


async def test_scripted_provider_rejects_foreign_job_debit(
    execution: Execution, review_id: UUID
) -> None:
    assert (
        await execution.store().begin(task=execution.task, lease=execution.lease)
    ).accepted
    await reserve(execution, execution.budget())
    dataset = Path("test-prs-dataset")
    bundle, _ = load_sources(dataset / "worker-sources", dataset)
    fixture = next(f for f in load_dataset(dataset) if f.id == "sec-001-sql-injection")
    scenario = CaseTransport(
        fixture,
        bundle["fixtures"][fixture.id],
        dataset / "worker-sources",
        "123",
        execution.factory,
    )
    scenario.review_id = review_id
    request = httpx.Request(
        "POST",
        "https://api.eurouter.ai/api/v1/chat/completions",
        json={
            "messages": [],
            "response_format": {
                "json_schema": {"schema": {"properties": {"findings": {}}}}
            },
        },
    )
    with pytest.raises(AssertionError, match="without committed debit"):
        await scenario.respond(request)
    assert scenario.calls == []


@pytest.mark.parametrize(
    ("field", "sentinel"),
    [
        ("title", '"summary":"'),
        ("description", "## Что искать\n\n"),
    ],
)
async def test_campaign_checks_decoded_model_messages_for_ground_truth(
    field: str, sentinel: str
) -> None:
    dataset = Path("test-prs-dataset")
    fixture = next(f for f in load_dataset(dataset) if f.id == "sec-001-sql-injection")
    fixture = (
        replace(fixture, title=sentinel)
        if field == "title"
        else replace(fixture, description=sentinel)
    )
    with pytest.raises(ValueError, match="Ground-truth metadata leaked"):
        await run_worker([fixture], dataset, dataset / "worker-sources")


async def test_worker_campaign_reports_missing_context_and_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset = Path("test-prs-dataset")
    selected = [
        f
        for f in load_dataset(dataset)
        if f.id in {"sec-001-sql-injection", "clean-001-well-factored-handler"}
    ]
    original = CaseTransport.respond

    async def stale_pull(self: CaseTransport, request: httpx.Request) -> httpx.Response:
        if (
            self.fixture.id == "sec-001-sql-injection"
            and self.review_id is not None
            and request.url.path.endswith("/pulls/1")
        ):
            return httpx.Response(404)
        return await original(self, request)

    monkeypatch.setattr(CaseTransport, "respond", stale_pull)
    report = await run_worker(selected, dataset, dataset / "worker-sources")
    rows = {row["id"]: row for row in report["fixtures"]}
    missing = rows["sec-001-sql-injection"]
    assert missing["canonical_result_valid"] is False
    assert missing["canonical_result"] is None
    assert missing["ast"]["semantic_sha256"] is None
    assert missing["requests"] == 0
    assert rows["clean-001-well-factored-handler"]["canonical_result_valid"] is True
    assert report["controlled_comparison"]["valid"] is False


async def test_worker_campaign_exports_all_chunks_with_stable_finding_order(
    tmp_path: Path,
) -> None:
    dataset = Path("test-prs-dataset")
    source_root = tmp_path / "sources"
    shutil.copytree(dataset / "worker-sources", source_root)
    manifest_path = source_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    fixture = next(f for f in load_dataset(dataset) if f.id == "sec-001-sql-injection")
    source = manifest["fixtures"][fixture.id]["new"]
    source_path = source_root / source["stored_path"]
    raw = source_path.read_text() + "".join(
        f"padding_{i} = '{'x' * 600}'\n" for i in range(40)
    )
    source_path.write_text(raw)
    source.update(
        git_blob_sha1=git_object("blob", raw.encode()),
        sha256=hashlib.sha256(raw.encode()).hexdigest(),
        byte_count=len(raw.encode()),
    )
    manifest["files"][source["stored_path"]] = source["sha256"]
    manifest_path.write_text(json.dumps(manifest))
    path = source["path"]
    lines = raw.splitlines()
    diff = (
        f"diff --git a/{path} b/{path}\nnew file mode 100644\n"
        f"index 0000000..1111111\n--- /dev/null\n+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n" + "".join(f"+{line}\n" for line in lines)
    )
    finding = fixture.golden_response["findings"][0]
    fixture = replace(
        fixture,
        diff_text=diff,
        golden_response=fixture.golden_response
        | {
            "findings": [
                finding | {"title": "Z stable finding"},
                finding | {"title": "A stable finding"},
            ],
        },
    )
    first = (await run_worker([fixture], dataset, source_root))["fixtures"][0]
    second = (await run_worker([fixture], dataset, source_root))["fixtures"][0]
    assert first["chunk_count"] > 1
    assert first["canonical_result_valid"] is True
    assert first["requests"] == first["chunk_count"]
    assert [f["title"] for f in first["canonical_result"]["findings"]] == [
        "A stable finding",
        "Z stable finding",
    ]
    assert (
        first["canonical_result"]["findings"] == second["canonical_result"]["findings"]
    )
    assert first["ast"]["semantic_sha256"] == second["ast"]["semantic_sha256"]


async def test_worker_campaign_scores_retained_partial_usage_without_aborting() -> None:
    dataset = Path("test-prs-dataset")
    fixture = next(f for f in load_dataset(dataset) if f.id == "sec-001-sql-injection")

    def retained_partial_usage(
        _mapper: Any, connection: Any, row: models.ChunkResult
    ) -> None:
        # Emulate a retained JSONB shape. The current canonical writer emits
        # both measured dimensions or whole null, never this legacy partial row.
        connection.execute(
            update(models.ChunkResult)
            .where(models.ChunkResult.id == row.id)
            .values(usage={"input_tokens": None, "output_tokens": 40})
        )

    event.listen(models.ChunkResult, "after_insert", retained_partial_usage)
    try:
        report = await run_worker([fixture], dataset, dataset / "worker-sources")
    finally:
        event.remove(models.ChunkResult, "after_insert", retained_partial_usage)
    row = report["fixtures"][0]
    assert row["canonical_result_valid"] is False
    assert row["canonical_result"]["usage"] == {
        "input_tokens": None,
        "output_tokens": 40,
    }
    assert report["usage"]["known_input_tokens"] == 100
    assert report["usage"]["known_output_tokens"] == 40


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


async def test_cleanup_campaign_removes_its_publication_operation(
    review_id: UUID,
) -> None:
    engine = create_async_engine(os.environ["DMC268_TEST_DATABASE_URL"])
    factory = async_sessionmaker(engine)
    try:
        async with factory.begin() as session:
            job = await session.get(models.ReviewJob, review_id)
            assert job is not None
            repository = job.repository_id
            session.add(
                PublicationOperation(
                    review_job_id=review_id,
                    change_request_id=job.change_request_id,
                    owner="synthetic-campaign",
                    fencing_token=1,
                    marker="synthetic",
                    payload_hash="0" * 64,
                    state="RESOLVED",
                )
            )
        await cleanup_campaign(factory, [repository], [])
        async with factory() as session:
            assert await session.get(models.ReviewJob, review_id) is None
            assert (
                await session.scalar(
                    select(PublicationOperation.id).where(
                        PublicationOperation.review_job_id == review_id
                    )
                )
                is None
            )
    finally:
        async with factory.begin() as session:
            await session.execute(
                delete(PublicationOperation).where(
                    PublicationOperation.review_job_id == review_id
                )
            )
        await engine.dispose()


async def test_cleanup_campaign_removes_only_its_actor_sessions(
    review_id: UUID,
) -> None:
    engine = create_async_engine(os.environ["DMC268_TEST_DATABASE_URL"])
    factory = async_sessionmaker(engine)
    actor, foreign = uuid4(), uuid4()
    try:
        async with factory.begin() as session:
            job = await session.get(models.ReviewJob, review_id)
            assert job is not None
            repository = job.repository_id
            session.add_all([models.User(id=actor), models.User(id=foreign)])
            await session.flush()
            session.add_all(
                [
                    models.AuthSession(
                        id=user,
                        user_id=user,
                        csrf_token="synthetic",
                        expires_at=datetime.now(UTC) + timedelta(minutes=30),
                    )
                    for user in (actor, foreign)
                ]
            )
        await cleanup_campaign(factory, [repository], [actor])
        async with factory() as session:
            assert await session.get(models.User, actor) is None
            assert await session.get(models.AuthSession, actor) is None
            assert await session.get(models.User, foreign) is not None
            assert await session.get(models.AuthSession, foreign) is not None
    finally:
        async with factory.begin() as session:
            await session.execute(
                delete(models.AuthSession).where(
                    models.AuthSession.user_id.in_([actor, foreign])
                )
            )
            await session.execute(
                delete(models.User).where(models.User.id.in_([actor, foreign]))
            )
        await engine.dispose()


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
