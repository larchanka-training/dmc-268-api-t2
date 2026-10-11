from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import asdict, replace
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_contracts import SCHEMAS, _validator
from test_postgres_integration import (
    PostgresHarness,
    _create_change_request,
    _create_repository,
    _create_settings,
)

from app.adapters.analysis_handler import PostgresAnalysisHandler
from app.adapters.outbox_dispatcher import PostgresOutboxDispatcher
from app.adapters.redis_queue import RedisQueue
from app.adapters.review_worker import PostgresReviewWorker
from app.application.ports import (
    BuiltContextPayload,
    LeaseLostError,
    QueueTaskEnvelope,
    ReviewChunkResult,
    ReviewFinding,
    StartReviewCommand,
    TokenUsage,
)
from app.application.use_cases.start_review import start_review
from app.db.models import (
    ChangeRequest,
    ChunkResult,
    ContextPayload,
    Finding,
    OutboxEvent,
    Publication,
    Repository,
    RepositorySettings,
    ReviewEvent,
    ReviewJob,
    TaskLease,
)
from app.db.uow import SqlAlchemyUnitOfWork
from app.domain.enums import FindingCategory, FindingSeverity, FindingSide
from app.domain.settings import compute_rules_digest
from app.vcs.github import GithubClient

pytest_plugins = ("test_postgres_integration", "test_redis_queue")
SHA256_HEX_LENGTH = 64
GITHUB_SNAPSHOT_REQUESTS = 4
HEAD, BASE, MERGE = "a" * 40, "b" * 40, "c" * 40
DIFF = """diff --git a/src/a.py b/src/a.py
index 123abcd..456abcd 100644
--- a/src/a.py
+++ b/src/a.py
@@ -1 +1 @@
-return value
+return value / 0
"""


class DeterministicLlm:
    def __init__(self) -> None:
        self.payloads: list[BuiltContextPayload] = []
        self.result = ReviewChunkResult(
            schema_version=1,
            summary="Division by zero must be fixed.",
            limitations=[],
            usage=TokenUsage(100, 40),
            latency_ms=7,
            findings=[
                ReviewFinding(
                    path="src/a.py",
                    side=FindingSide.NEW,
                    start_line=1,
                    end_line=1,
                    category=FindingCategory.CORRECTNESS,
                    severity=FindingSeverity.HIGH,
                    title="Division by zero",
                    explanation="This expression always raises.",
                    evidence="return value / 0",
                    recommendation="Remove the division.",
                    proposed_diff_fix=None,
                )
            ],
        )

    async def review(self, payload: BuiltContextPayload) -> ReviewChunkResult:
        self.payloads.append(payload)
        return self.result


def github_http(raw_diff: str = DIFF) -> httpx.MockTransport:
    def respond(request: httpx.Request) -> httpx.Response:
        if "/pulls/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "title": "Fix division",
                    "body": "Description",
                    "state": "open",
                    "head": {"repo": {"id": 43}, "sha": HEAD, "ref": "feature"},
                    "base": {"repo": {"id": 42}, "sha": BASE, "ref": "main"},
                },
            )
        if request.headers["accept"] == "application/vnd.github.diff":
            return httpx.Response(200, text=raw_diff)
        return httpx.Response(200, json={"merge_base_commit": {"sha": MERGE}})

    return httpx.MockTransport(respond)


@pytest_asyncio.fixture
async def review_id(postgres: PostgresHarness) -> AsyncIterator[uuid.UUID]:
    rules_digest = compute_rules_digest({"instructions": []})
    async with postgres.engine.begin() as connection:
        repo = await _create_repository(postgres, connection, external_id="42")
        settings = await _create_settings(
            postgres, connection, repo, rules_digest=rules_digest
        )
        change = await _create_change_request(postgres, connection, repo)
        await connection.execute(
            update(Repository)
            .where(Repository.id == repo)
            .values(owner="team", repo_name="api")
        )
        await connection.execute(
            update(ChangeRequest)
            .where(ChangeRequest.id == change)
            .values(
                external_number="13",
                head_sha=HEAD,
                base_sha=BASE,
                source_repository_external_id="43",
            )
        )
    factory = async_sessionmaker(postgres.engine)
    review = await start_review(
        StartReviewCommand(
            change,
            settings,
            HEAD,
            rules_digest,
            "manual",
            None,
            "test-model",
            None,
            {"temperature": 0},
            "v1",
            "prompt-digest",
            "trace-analysis",
        ),
        unit_of_work=SqlAlchemyUnitOfWork(factory),
    )
    postgres.track("review_job", review)
    try:
        yield review
    finally:
        async with factory.begin() as session:
            chunks = select(ChunkResult.id).where(ChunkResult.review_job_id == review)
            await session.execute(
                delete(Finding).where(Finding.chunk_result_id.in_(chunks))
            )
            for table in (
                ChunkResult,
                ContextPayload,
                ReviewEvent,
                OutboxEvent,
                Publication,
                TaskLease,
            ):
                await session.execute(
                    delete(table).where(table.review_job_id == review)
                )


async def test_pipeline_persists_valid_analysis_and_publication_handoff(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    review_id: uuid.UUID,
) -> None:

    factory = async_sessionmaker(postgres.engine)
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    assert await PostgresOutboxDispatcher(factory, queue).dispatch_batch() == 1
    llm = DeterministicLlm()
    async with httpx.AsyncClient(transport=github_http()) as client:
        handler = PostgresAnalysisHandler(
            factory, GithubClient(client), llm, schemas_dir=Path(SCHEMAS)
        )
        worker = PostgresReviewWorker(
            factory, queue, handler, owner="analysis", task_kind="analyze"
        )
        assert await worker.run_once() is True
    async with factory() as session:
        review = await session.get(ReviewJob, review_id)
        context = (
            await session.scalars(
                select(ContextPayload).where(ContextPayload.review_job_id == review_id)
            )
        ).one()
        chunk = (
            await session.scalars(
                select(ChunkResult).where(ChunkResult.review_job_id == review_id)
            )
        ).one()
        findings = (
            await session.scalars(
                select(Finding).where(Finding.chunk_result_id == chunk.id)
            )
        ).all()
        events = (
            await session.scalars(
                select(ReviewEvent).where(ReviewEvent.review_job_id == review_id)
            )
        ).all()
        publication = (
            await session.scalars(
                select(Publication).where(Publication.review_job_id == review_id)
            )
        ).one()
        publish = (
            await session.scalars(
                select(OutboxEvent).where(
                    OutboxEvent.review_job_id == review_id,
                    OutboxEvent.task_kind == "publish",
                )
            )
        ).one()
    assert (
        review is not None
        and review.status == "COMPLETED"
        and review.finished_at is not None
    )
    assert review.final_summary == "Division by zero must be fixed."
    assert (
        review.snapshot_provider,
        review.base_sha,
        review.merge_base_sha,
        review.requested_head_sha,
    ) == ("github", BASE, MERGE, HEAD)
    assert (
        review.source_repository_external_id == "43"
        and review.base_repository_external_id == "42"
    )
    assert review.captured_at is not None and review.provider_diff_version is not None
    assert (
        context.id == llm.payloads[0].chunk_id
        and chunk.context_payload_id == context.id
    )
    assert llm.payloads[0].review_id == review_id
    _validator(SCHEMAS / "context/v1.json").validate(llm.payloads[0].to_wire())
    _validator(SCHEMAS / "review-result/v1.json").validate(asdict(llm.result))
    assert context.payload_body == llm.payloads[0].payload_body
    assert review.coverage == {
        "included": [
            {"path": "src/a.py", "ranges": [{"start_line": 1, "end_line": 1}]}
        ],
        "skipped": [],
        "context_quality": "diff_only",
    }
    assert len(findings) == 1 and len(findings[0].fingerprint) == SHA256_HEX_LENGTH
    assert chunk.summary == review.final_summary and chunk.usage == {
        "input_tokens": 100,
        "output_tokens": 40,
    }
    assert {event.status for event in events} == {
        "FETCHING_DIFF",
        "PARSING_CONTEXT",
        "LLM_PROCESSING",
        "COMPLETED",
    }
    assert publication.status == "PENDING" and publication.finished_at is None
    assert publication.started_at is not None and publication.deadline_at is not None
    assert publish.trace_id == "trace-analysis"
    assert (await redis_client.xpending(f"{stream_prefix}:analyze", "review-workers"))[
        "pending"
    ] == 0


@pytest.mark.parametrize(
    "mutation",
    ["path", "traversal", "coordinate", "range", "schema", "usage", "summary"],
)
async def test_invalid_llm_location_never_persists_findings(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    review_id: uuid.UUID,
    mutation: str,
) -> None:

    factory = async_sessionmaker(postgres.engine)
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    await PostgresOutboxDispatcher(factory, queue).dispatch_batch()
    llm = DeterministicLlm()
    if mutation == "schema":
        llm.result = replace(llm.result, schema_version=2)
    elif mutation == "usage":
        llm.result = replace(llm.result, usage=TokenUsage(-1, 0))
    elif mutation == "summary":
        llm.result = replace(
            llm.result, summary=cast(str, {"invalid": "summary must be a string"})
        )
    else:
        changes: dict[str, dict[str, Any]] = {
            "path": {"path": "src/other.py"},
            "traversal": {"path": "../a.py"},
            "coordinate": {"start_line": 9, "end_line": 9},
            "range": {"start_line": 2, "end_line": 1},
        }
        llm.result = replace(
            llm.result, findings=[replace(llm.result.findings[0], **changes[mutation])]
        )
    async with httpx.AsyncClient(transport=github_http()) as client:
        handler = PostgresAnalysisHandler(
            factory, GithubClient(client), llm, schemas_dir=SCHEMAS
        )
        worker = PostgresReviewWorker(
            factory, queue, handler, owner="analysis", task_kind="analyze"
        )
        assert await worker.run_once() is True
    async with factory() as session:
        review = await session.get(ReviewJob, review_id)
        chunks = (
            await session.scalars(
                select(ChunkResult).where(ChunkResult.review_job_id == review_id)
            )
        ).all()
        events = (
            await session.scalars(
                select(ReviewEvent).where(
                    ReviewEvent.review_job_id == review_id,
                    ReviewEvent.reason_code == "LLM_OUTPUT_INVALID",
                    ReviewEvent.status == "FAILED",
                )
            )
        ).all()
    assert (
        review is not None
        and review.status == "FAILED"
        and review.finished_at is not None
    )
    assert chunks == [] and review.final_summary is None
    assert len(events) == 1 and events[0].safe_details is None
    assert len(llm.payloads) == 2  # noqa: PLR2004 — one initial call plus one repair


@pytest.mark.parametrize("policy", ["empty", "default", "frozen"])
async def test_empty_diff_skips_without_fictitious_context_or_llm(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    review_id: uuid.UUID,
    policy: str,
) -> None:

    factory = async_sessionmaker(postgres.engine)
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    await PostgresOutboxDispatcher(factory, queue).dispatch_batch()
    llm = DeterministicLlm()
    raw_diff = "" if policy == "empty" else DIFF
    if policy == "default":
        raw_diff = DIFF.replace("src/a.py", "vendor/a.py")
    if policy == "frozen":
        async with postgres.engine.begin() as connection:
            await connection.execute(
                update(RepositorySettings)
                .where(
                    RepositorySettings.id.in_(
                        select(ReviewJob.repository_settings_id).where(
                            ReviewJob.id == review_id
                        )
                    )
                )
                .values(ignores={"globs": ["src/**"]})
            )
    async with httpx.AsyncClient(transport=github_http(raw_diff)) as client:
        handler = PostgresAnalysisHandler(
            factory, GithubClient(client), llm, schemas_dir=SCHEMAS
        )
        worker = PostgresReviewWorker(
            factory, queue, handler, owner="analysis", task_kind="analyze"
        )
        assert await worker.run_once() is True
    async with factory() as session:
        review = await session.get(ReviewJob, review_id)
        contexts = (
            await session.scalars(
                select(ContextPayload).where(ContextPayload.review_job_id == review_id)
            )
        ).all()
        chunks = (
            await session.scalars(
                select(ChunkResult).where(ChunkResult.review_job_id == review_id)
            )
        ).all()
        publication = (
            await session.scalars(
                select(Publication).where(Publication.review_job_id == review_id)
            )
        ).one()
    assert (
        review is not None
        and review.status == "SKIPPED"
        and review.finished_at is not None
    )
    assert review.snapshot_provider == "github" and review.merge_base_sha == MERGE
    expected_skipped = (
        []
        if policy == "empty"
        else [
            {
                "path": "vendor/a.py" if policy == "default" else "src/a.py",
                "ranges": [{"start_line": 1, "end_line": 1}],
                "reason": "policy",
            }
        ]
    )
    assert review.coverage == {
        "included": [],
        "skipped": expected_skipped,
        "context_quality": "diff_only",
    }
    assert contexts == [] and chunks == [] and llm.payloads == []
    assert publication.status == "NOT_READY"


@pytest.mark.parametrize("invalid", [True, False])
async def test_stale_fence_blocks_result_and_second_provider_call(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    review_id: uuid.UUID,
    invalid: bool,
) -> None:

    class LosingLlm(DeterministicLlm):
        async def review(self, payload: BuiltContextPayload) -> ReviewChunkResult:
            result = await super().review(payload)
            async with postgres.engine.begin() as connection:
                await connection.execute(
                    update(TaskLease)
                    .where(TaskLease.review_job_id == review_id)
                    .values(
                        fencing_token=TaskLease.fencing_token + 1, owner="successor"
                    )
                )
            return (
                replace(
                    result, findings=[replace(result.findings[0], path="outside.py")]
                )
                if invalid
                else result
            )

    factory = async_sessionmaker(postgres.engine)
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    await PostgresOutboxDispatcher(factory, queue).dispatch_batch()
    llm = LosingLlm()
    async with httpx.AsyncClient(transport=github_http()) as client:
        handler = PostgresAnalysisHandler(
            factory, GithubClient(client), llm, schemas_dir=SCHEMAS
        )
        worker = PostgresReviewWorker(
            factory, queue, handler, owner="analysis", task_kind="analyze"
        )
        with pytest.raises(LeaseLostError):
            await worker.run_once()
    assert len(llm.payloads) == 1
    async with factory() as session:
        review = await session.get(ReviewJob, review_id)
        chunks = (
            await session.scalars(
                select(ChunkResult).where(ChunkResult.review_job_id == review_id)
            )
        ).all()
    assert chunks == [] and review is not None and review.finished_at is None
    assert (await redis_client.xpending(f"{stream_prefix}:analyze", "review-workers"))[
        "pending"
    ] == 1


async def test_expired_queue_deadline_fails_without_github_call(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    review_id: uuid.UUID,
) -> None:

    async with postgres.engine.begin() as connection:
        await connection.execute(
            update(ReviewJob)
            .where(ReviewJob.id == review_id)
            .values(queue_deadline_at=func.clock_timestamp() - timedelta(seconds=1))
        )
    requests: list[httpx.Request] = []

    async def observe(request: httpx.Request) -> None:
        requests.append(request)

    factory = async_sessionmaker(postgres.engine)
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    await PostgresOutboxDispatcher(factory, queue).dispatch_batch()
    llm = DeterministicLlm()
    async with httpx.AsyncClient(
        transport=github_http(), event_hooks={"request": [observe]}
    ) as client:
        handler = PostgresAnalysisHandler(
            factory, GithubClient(client), llm, schemas_dir=SCHEMAS
        )
        worker = PostgresReviewWorker(
            factory, queue, handler, owner="analysis", task_kind="analyze"
        )
        assert await worker.run_once() is True
    async with factory() as session:
        review = await session.get(ReviewJob, review_id)
        reasons = (
            await session.scalars(
                select(ReviewEvent.reason_code).where(
                    ReviewEvent.review_job_id == review_id
                )
            )
        ).all()
    assert requests == [] and llm.payloads == []
    assert (
        review is not None
        and review.status == "FAILED"
        and review.finished_at is not None
    )
    assert "QUEUE_DEADLINE_EXCEEDED" in reasons


async def test_analysis_deadline_after_capture_blocks_llm(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    review_id: uuid.UUID,
) -> None:

    requests: list[httpx.Request] = []

    async def observe(request: httpx.Request) -> None:
        requests.append(request)
        if len(requests) == GITHUB_SNAPSHOT_REQUESTS:
            async with postgres.engine.begin() as connection:
                await connection.execute(
                    update(ReviewJob)
                    .where(ReviewJob.id == review_id)
                    .values(
                        analysis_deadline_at=func.clock_timestamp()
                        - timedelta(seconds=1)
                    )
                )

    factory = async_sessionmaker(postgres.engine)
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    await PostgresOutboxDispatcher(factory, queue).dispatch_batch()
    llm = DeterministicLlm()
    async with httpx.AsyncClient(
        transport=github_http(), event_hooks={"request": [observe]}
    ) as client:
        handler = PostgresAnalysisHandler(
            factory, GithubClient(client), llm, schemas_dir=SCHEMAS
        )
        worker = PostgresReviewWorker(
            factory, queue, handler, owner="analysis", task_kind="analyze"
        )
        assert await worker.run_once() is True
    async with factory() as session:
        review = await session.get(ReviewJob, review_id)
        reasons = (
            await session.scalars(
                select(ReviewEvent.reason_code).where(
                    ReviewEvent.review_job_id == review_id
                )
            )
        ).all()
    assert llm.payloads == []
    assert (
        review is not None
        and review.status == "FAILED"
        and review.finished_at is not None
    )
    assert "ANALYSIS_DEADLINE_EXCEEDED" in reasons


@pytest.mark.parametrize("incompatible", ["head", "closed", "provider", "rules"])
async def test_changed_authoritative_pr_is_skipped_before_fetch(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    review_id: uuid.UUID,
    incompatible: str,
) -> None:

    async with postgres.engine.begin() as connection:
        if incompatible == "provider":
            await connection.execute(
                update(Repository)
                .where(
                    Repository.id.in_(
                        select(ReviewJob.repository_id).where(ReviewJob.id == review_id)
                    )
                )
                .values(provider_name="gitlab")
            )
        elif incompatible == "rules":
            await connection.execute(
                update(ReviewJob)
                .where(ReviewJob.id == review_id)
                .values(rules_digest="incompatible")
            )
        else:
            changes = (
                {"head_sha": "d" * 40}
                if incompatible == "head"
                else {"current_status": "closed"}
            )
            await connection.execute(
                update(ChangeRequest)
                .where(
                    ChangeRequest.id.in_(
                        select(ReviewJob.change_request_id).where(
                            ReviewJob.id == review_id
                        )
                    )
                )
                .values(**changes)
            )
    requests: list[httpx.Request] = []

    async def observe(request: httpx.Request) -> None:
        requests.append(request)

    factory = async_sessionmaker(postgres.engine)
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    await PostgresOutboxDispatcher(factory, queue).dispatch_batch()
    llm = DeterministicLlm()
    async with httpx.AsyncClient(
        transport=github_http(), event_hooks={"request": [observe]}
    ) as client:
        handler = PostgresAnalysisHandler(
            factory, GithubClient(client), llm, schemas_dir=SCHEMAS
        )
        worker = PostgresReviewWorker(
            factory, queue, handler, owner="analysis", task_kind="analyze"
        )
        assert await worker.run_once() is True
    assert requests == [] and llm.payloads == []
    async with factory() as session:
        review = await session.get(ReviewJob, review_id)
    assert (
        review is not None
        and review.status
        == ("SKIPPED" if incompatible in {"head", "closed"} else "FAILED")
        and review.finished_at is not None
    )


async def test_context_budget_does_not_send_over_limit_payload(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    review_id: uuid.UUID,
) -> None:

    factory = async_sessionmaker(postgres.engine)
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    await PostgresOutboxDispatcher(factory, queue).dispatch_batch()
    llm = DeterministicLlm()
    async with httpx.AsyncClient(transport=github_http()) as client:
        handler = PostgresAnalysisHandler(
            factory,
            GithubClient(client),
            llm,
            schemas_dir=SCHEMAS,
            limit_input_tokens=1,
        )
        worker = PostgresReviewWorker(
            factory, queue, handler, owner="analysis", task_kind="analyze"
        )
        assert await worker.run_once() is True
    assert llm.payloads == []
    async with factory() as session:
        review = await session.get(ReviewJob, review_id)
        contexts = (
            await session.scalars(
                select(ContextPayload).where(ContextPayload.review_job_id == review_id)
            )
        ).all()
    assert review is not None and review.status == "SKIPPED" and contexts == []
    assert review.coverage == {
        "included": [],
        "skipped": [
            {
                "path": "src/a.py",
                "ranges": [{"start_line": 1, "end_line": 1}],
                "reason": "token_budget",
            }
        ],
        "context_quality": "diff_only",
    }


async def test_github_access_denied_is_safe_terminal_failure(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    review_id: uuid.UUID,
) -> None:

    factory = async_sessionmaker(postgres.engine)
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    await PostgresOutboxDispatcher(factory, queue).dispatch_batch()
    llm = DeterministicLlm()
    transport = httpx.MockTransport(
        lambda request: httpx.Response(403, text="secret provider data")
    )
    async with httpx.AsyncClient(transport=transport) as client:
        handler = PostgresAnalysisHandler(
            factory, GithubClient(client), llm, schemas_dir=SCHEMAS
        )
        worker = PostgresReviewWorker(
            factory, queue, handler, owner="analysis", task_kind="analyze"
        )
        assert await worker.run_once() is True
    async with factory() as session:
        review = await session.get(ReviewJob, review_id)
        events = (
            await session.scalars(
                select(ReviewEvent).where(
                    ReviewEvent.review_job_id == review_id,
                    ReviewEvent.reason_code == "VCS_ACCESS_DENIED",
                )
            )
        ).all()
    assert (
        review is not None
        and review.status == "FAILED"
        and review.finished_at is not None
    )
    assert len(events) == 1 and events[0].safe_details is None and llm.payloads == []


async def test_untrustworthy_diff_is_safe_terminal_failure(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    review_id: uuid.UUID,
) -> None:

    factory = async_sessionmaker(postgres.engine)
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    await PostgresOutboxDispatcher(factory, queue).dispatch_batch()
    llm = DeterministicLlm()
    raw_diff = DIFF.replace("src/a.py", "../secret.py")
    async with httpx.AsyncClient(transport=github_http(raw_diff)) as client:
        handler = PostgresAnalysisHandler(
            factory, GithubClient(client), llm, schemas_dir=SCHEMAS
        )
        worker = PostgresReviewWorker(
            factory, queue, handler, owner="analysis", task_kind="analyze"
        )
        assert await worker.run_once() is True
    async with factory() as session:
        review = await session.get(ReviewJob, review_id)
        reasons = (
            await session.scalars(
                select(ReviewEvent.reason_code).where(
                    ReviewEvent.review_job_id == review_id
                )
            )
        ).all()
    assert review is not None and review.status == "FAILED" and llm.payloads == []
    assert "DIFF_UNTRUSTWORTHY" in reasons


async def test_useful_result_with_limitations_is_partial(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    review_id: uuid.UUID,
) -> None:

    factory = async_sessionmaker(postgres.engine)
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    await PostgresOutboxDispatcher(factory, queue).dispatch_batch()
    llm = DeterministicLlm()
    llm.result = replace(
        llm.result, limitations=["Unable to establish all caller invariants."]
    )
    async with httpx.AsyncClient(transport=github_http()) as client:
        handler = PostgresAnalysisHandler(
            factory, GithubClient(client), llm, schemas_dir=SCHEMAS
        )
        worker = PostgresReviewWorker(
            factory, queue, handler, owner="analysis", task_kind="analyze"
        )
        assert await worker.run_once() is True
    async with factory() as session:
        review = await session.get(ReviewJob, review_id)
        chunk = (
            await session.scalars(
                select(ChunkResult).where(ChunkResult.review_job_id == review_id)
            )
        ).one()
    assert (
        review is not None
        and review.status == "PARTIAL"
        and review.finished_at is not None
    )
    assert (
        review.final_summary == llm.result.summary
        and chunk.limitations == llm.result.limitations
    )


async def test_commit_failure_retains_delivery_and_rolls_back_results(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    review_id: uuid.UUID,
) -> None:

    factory = async_sessionmaker(postgres.engine)
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    await PostgresOutboxDispatcher(factory, queue).dispatch_batch()
    llm = DeterministicLlm()
    trigger = f"analysis_commit_failure_{uuid.uuid4().hex}"
    async with postgres.engine.begin() as connection:
        await connection.execute(
            text(f"""CREATE FUNCTION {trigger}() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.review_job_id = '{review_id}'::uuid
     AND NEW.status IN ('COMPLETED', 'PARTIAL') THEN
    RAISE EXCEPTION 'injected durable commit failure';
  END IF;
  RETURN NEW;
END; $$""")
        )
        await connection.execute(
            text(
                f"CREATE CONSTRAINT TRIGGER {trigger} AFTER INSERT ON review_event "
                f"DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
                f"EXECUTE FUNCTION {trigger}()"
            )
        )
    try:
        async with httpx.AsyncClient(transport=github_http()) as client:
            handler = PostgresAnalysisHandler(
                factory, GithubClient(client), llm, schemas_dir=SCHEMAS
            )
            worker = PostgresReviewWorker(
                factory, queue, handler, owner="analysis", task_kind="analyze"
            )
            with pytest.raises(DBAPIError, match="injected durable commit failure"):
                await worker.run_once()
        assert (
            await redis_client.xpending(f"{stream_prefix}:analyze", "review-workers")
        )["pending"] == 1
        async with factory() as session:
            review = await session.get(ReviewJob, review_id)
            chunks = (
                await session.scalars(
                    select(ChunkResult).where(ChunkResult.review_job_id == review_id)
                )
            ).all()
            publication = (
                await session.scalars(
                    select(Publication).where(Publication.review_job_id == review_id)
                )
            ).one()
        assert (
            review is not None
            and review.status == "LLM_PROCESSING"
            and review.finished_at is None
        )
        assert chunks == [] and publication.status == "NOT_READY"
    finally:
        async with postgres.engine.begin() as connection:
            await connection.execute(text(f"DROP TRIGGER {trigger} ON review_event"))
            await connection.execute(text(f"DROP FUNCTION {trigger}()"))


async def test_duplicate_delivery_keeps_one_context_result_and_finding(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    review_id: uuid.UUID,
) -> None:

    factory = async_sessionmaker(postgres.engine)
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    await PostgresOutboxDispatcher(factory, queue).dispatch_batch()
    async with factory() as session:
        event = (
            await session.scalars(
                select(OutboxEvent).where(OutboxEvent.review_job_id == review_id)
            )
        ).one()
        task = QueueTaskEnvelope(
            event.schema_version,
            str(event.id),
            str(review_id),
            "analyze",
            event.attempt,
            event.trace_id,
        )
    llm = DeterministicLlm()
    async with httpx.AsyncClient(transport=github_http()) as client:
        handler = PostgresAnalysisHandler(
            factory, GithubClient(client), llm, schemas_dir=SCHEMAS
        )
        worker = PostgresReviewWorker(
            factory, queue, handler, owner="analysis", task_kind="analyze"
        )
        assert await worker.run_once() is True
        await queue.enqueue(task)
        assert await worker.run_once() is True
    assert len(llm.payloads) == 1
    async with factory() as session:
        contexts = (
            await session.scalars(
                select(ContextPayload).where(ContextPayload.review_job_id == review_id)
            )
        ).all()
        chunks = (
            await session.scalars(
                select(ChunkResult).where(ChunkResult.review_job_id == review_id)
            )
        ).all()
        findings = (
            await session.scalars(
                select(Finding)
                .join(ChunkResult)
                .where(ChunkResult.review_job_id == review_id)
            )
        ).all()
    assert len(contexts) == len(chunks) == len(findings) == 1


async def test_format_repair_budget_survives_failed_provider_call_and_redelivery(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    review_id: uuid.UUID,
) -> None:

    class InterruptedRepair(DeterministicLlm):
        async def review(self, payload: BuiltContextPayload) -> ReviewChunkResult:
            result = await super().review(payload)
            if len(self.payloads) == 2:  # noqa: PLR2004 — interrupt reserved repair
                raise RuntimeError("provider private data")
            return replace(
                result, findings=[replace(result.findings[0], path="outside.py")]
            )

    factory = async_sessionmaker(postgres.engine)
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    await PostgresOutboxDispatcher(factory, queue).dispatch_batch()
    async with factory() as session:
        event = (
            await session.scalars(
                select(OutboxEvent).where(OutboxEvent.review_job_id == review_id)
            )
        ).one()
        task = QueueTaskEnvelope(
            1, str(event.id), str(review_id), "analyze", event.attempt, event.trace_id
        )
    llm = InterruptedRepair()
    async with httpx.AsyncClient(transport=github_http()) as client:
        handler = PostgresAnalysisHandler(
            factory, GithubClient(client), llm, schemas_dir=SCHEMAS
        )
        worker = PostgresReviewWorker(
            factory, queue, handler, owner="analysis", task_kind="analyze"
        )
        with pytest.raises(RuntimeError, match="provider private data"):
            await worker.run_once()
        await queue.enqueue(task)
        assert await worker.run_once() is True
    assert len(llm.payloads) == 3  # noqa: PLR2004 — repair is not repeated on redelivery
    async with factory() as session:
        events = (
            await session.scalars(
                select(ReviewEvent).where(
                    ReviewEvent.review_job_id == review_id,
                    ReviewEvent.event_type == "FORMAT_REPAIR",
                )
            )
        ).all()
    assert len(events) == 1 and events[0].safe_details is None


async def test_redelivery_reuses_context_after_provider_failure(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    review_id: uuid.UUID,
) -> None:

    class InterruptedLlm(DeterministicLlm):
        async def review(self, payload: BuiltContextPayload) -> ReviewChunkResult:
            result = await super().review(payload)
            if len(self.payloads) == 1:
                raise RuntimeError("provider private data")
            return result

    factory = async_sessionmaker(postgres.engine)
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    await PostgresOutboxDispatcher(factory, queue).dispatch_batch()
    async with factory() as session:
        event = (
            await session.scalars(
                select(OutboxEvent).where(OutboxEvent.review_job_id == review_id)
            )
        ).one()
        task = QueueTaskEnvelope(
            1, str(event.id), str(review_id), "analyze", event.attempt, event.trace_id
        )
    llm = InterruptedLlm()
    async with httpx.AsyncClient(transport=github_http()) as client:
        handler = PostgresAnalysisHandler(
            factory, GithubClient(client), llm, schemas_dir=SCHEMAS
        )
        worker = PostgresReviewWorker(
            factory, queue, handler, owner="analysis", task_kind="analyze"
        )
        with pytest.raises(RuntimeError):
            await worker.run_once()
        await queue.enqueue(task)
        assert await worker.run_once() is True
    assert llm.payloads[0] == llm.payloads[1]
    async with factory() as session:
        contexts = (
            await session.scalars(
                select(ContextPayload).where(ContextPayload.review_job_id == review_id)
            )
        ).all()
    assert len(contexts) == 1


async def test_providers_run_without_open_postgres_transactions(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    review_id: uuid.UUID,
) -> None:

    async def check_transactions() -> None:
        async with postgres.engine.connect() as connection:
            active = (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname = current_database() "
                        "AND pid <> pg_backend_pid() "
                        "AND state = 'idle in transaction'"
                    )
                )
            ).scalar_one()
        assert active == 0

    async def observe(request: httpx.Request) -> None:
        await check_transactions()

    class ObservingLlm(DeterministicLlm):
        async def review(self, payload: BuiltContextPayload) -> ReviewChunkResult:
            await check_transactions()
            async with postgres.engine.connect() as connection:
                context_id = (
                    await connection.execute(
                        select(ContextPayload.id).where(
                            ContextPayload.review_job_id == review_id
                        )
                    )
                ).scalar_one()
            assert context_id == payload.chunk_id
            return await super().review(payload)

    factory = async_sessionmaker(postgres.engine)
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    await PostgresOutboxDispatcher(factory, queue).dispatch_batch()
    llm = ObservingLlm()
    async with httpx.AsyncClient(
        transport=github_http(), event_hooks={"request": [observe]}
    ) as client:
        handler = PostgresAnalysisHandler(
            factory, GithubClient(client), llm, schemas_dir=SCHEMAS
        )
        worker = PostgresReviewWorker(
            factory, queue, handler, owner="analysis", task_kind="analyze"
        )
        assert await worker.run_once() is True


async def test_snapshot_with_wrong_source_repository_is_skipped(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    review_id: uuid.UUID,
) -> None:
    async with postgres.engine.begin() as connection:
        await connection.execute(
            update(ChangeRequest)
            .where(
                ChangeRequest.id.in_(
                    select(ReviewJob.change_request_id).where(ReviewJob.id == review_id)
                )
            )
            .values(source_repository_external_id="99")
        )
    factory = async_sessionmaker(postgres.engine)
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    await PostgresOutboxDispatcher(factory, queue).dispatch_batch()
    llm = DeterministicLlm()
    async with httpx.AsyncClient(transport=github_http()) as client:
        handler = PostgresAnalysisHandler(
            factory, GithubClient(client), llm, schemas_dir=SCHEMAS
        )
        worker = PostgresReviewWorker(
            factory, queue, handler, owner="analysis", task_kind="analyze"
        )
        assert await worker.run_once() is True
    assert llm.payloads == []
    async with factory() as session:
        review = await session.get(ReviewJob, review_id)
        contexts = (
            await session.scalars(
                select(ContextPayload).where(ContextPayload.review_job_id == review_id)
            )
        ).all()
    assert review is not None and review.status == "SKIPPED" and contexts == []


async def test_rate_limited_github_preserves_active_job_and_pending_delivery(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    review_id: uuid.UUID,
) -> None:
    factory = async_sessionmaker(postgres.engine)
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    await PostgresOutboxDispatcher(factory, queue).dispatch_batch()
    llm = DeterministicLlm()
    transport = httpx.MockTransport(
        lambda request: httpx.Response(429, headers={"retry-after": "60"})
    )
    async with httpx.AsyncClient(transport=transport) as client:
        handler = PostgresAnalysisHandler(
            factory, GithubClient(client), llm, schemas_dir=SCHEMAS
        )
        worker = PostgresReviewWorker(
            factory, queue, handler, owner="analysis", task_kind="analyze"
        )
        assert await worker.run_once() is False
    async with factory() as session:
        review = await session.get(ReviewJob, review_id)
        events = (
            await session.scalars(
                select(ReviewEvent).where(
                    ReviewEvent.review_job_id == review_id,
                    ReviewEvent.reason_code == "VCS_RATE_LIMITED",
                )
            )
        ).all()
    assert (
        review is not None
        and review.status == "FETCHING_DIFF"
        and review.finished_at is None
    )
    assert len(events) == 1 and events[0].retryable and events[0].safe_details is None
    assert llm.payloads == []
    assert (await redis_client.xpending(f"{stream_prefix}:analyze", "review-workers"))[
        "pending"
    ] == 1


async def test_valid_output_after_analysis_deadline_is_partial_with_audit(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    review_id: uuid.UUID,
) -> None:
    class LateLlm(DeterministicLlm):
        async def review(self, payload: BuiltContextPayload) -> ReviewChunkResult:
            result = await super().review(payload)
            async with postgres.engine.begin() as connection:
                await connection.execute(
                    update(ReviewJob)
                    .where(ReviewJob.id == review_id)
                    .values(
                        analysis_deadline_at=func.clock_timestamp()
                        - timedelta(seconds=1)
                    )
                )
            return result

    factory = async_sessionmaker(postgres.engine)
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    await PostgresOutboxDispatcher(factory, queue).dispatch_batch()
    llm = LateLlm()
    async with httpx.AsyncClient(transport=github_http()) as client:
        handler = PostgresAnalysisHandler(
            factory, GithubClient(client), llm, schemas_dir=SCHEMAS
        )
        worker = PostgresReviewWorker(
            factory, queue, handler, owner="analysis", task_kind="analyze"
        )
        assert await worker.run_once() is True
    async with factory() as session:
        review = await session.get(ReviewJob, review_id)
        reasons = (
            await session.scalars(
                select(ReviewEvent.reason_code).where(
                    ReviewEvent.review_job_id == review_id
                )
            )
        ).all()
    assert (
        review is not None
        and review.status == "PARTIAL"
        and review.finished_at is not None
    )
    assert (
        review.final_summary == llm.result.summary
        and "ANALYSIS_DEADLINE_EXCEEDED" in reasons
    )


async def test_queue_deadline_crossing_db_lock_blocks_analysis_start(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    review_id: uuid.UUID,
) -> None:

    requests: list[httpx.Request] = []

    async def observe(request: httpx.Request) -> None:
        requests.append(request)

    factory = async_sessionmaker(postgres.engine)
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    await PostgresOutboxDispatcher(factory, queue).dispatch_batch()
    llm = DeterministicLlm()
    async with httpx.AsyncClient(
        transport=github_http(), event_hooks={"request": [observe]}
    ) as client:
        handler = PostgresAnalysisHandler(
            factory, GithubClient(client), llm, schemas_dir=SCHEMAS
        )
        worker = PostgresReviewWorker(
            factory, queue, handler, owner="analysis", task_kind="analyze"
        )
        async with postgres.engine.begin() as blocker:
            await blocker.execute(
                text("LOCK TABLE context_payload IN ACCESS EXCLUSIVE MODE")
            )
            execution = asyncio.create_task(worker.run_once())

            async def wait_for_context_lock() -> None:
                while True:
                    async with postgres.engine.connect() as observer:
                        blocked = (
                            await observer.execute(
                                text(
                                    "SELECT count(*) FROM pg_stat_activity "
                                    "WHERE datname = current_database() "
                                    "AND wait_event_type = 'Lock' "
                                    "AND query LIKE '%context_payload%' "
                                    "AND pid <> pg_backend_pid()"
                                )
                            )
                        ).scalar_one()
                    if blocked:
                        return
                    await asyncio.sleep(0.01)

            try:
                await asyncio.wait_for(wait_for_context_lock(), timeout=5)
                await blocker.execute(
                    update(ReviewJob)
                    .where(ReviewJob.id == review_id)
                    .values(
                        queue_deadline_at=func.clock_timestamp() - timedelta(seconds=1)
                    )
                )
            except BaseException:
                execution.cancel()
                raise
        assert await execution is True
    assert requests == [] and llm.payloads == []
    async with factory() as session:
        review = await session.get(ReviewJob, review_id)
        reasons = (
            await session.scalars(
                select(ReviewEvent.reason_code).where(
                    ReviewEvent.review_job_id == review_id
                )
            )
        ).all()
    assert (
        review is not None and review.status == "FAILED" and review.started_at is None
    )
    assert "QUEUE_DEADLINE_EXCEEDED" in reasons


async def test_worker_uses_frozen_historical_empty_settings(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    review_id: uuid.UUID,
) -> None:
    factory = async_sessionmaker(postgres.engine)
    async with factory.begin() as session:
        review = await session.get(ReviewJob, review_id)
        assert review is not None
        frozen_id = review.repository_settings_id
        identity = (frozen_id, review.rules_digest, review.config_digest)
        await session.execute(
            update(RepositorySettings)
            .where(RepositorySettings.id == frozen_id)
            .values(rules={}, ignores={}, output_language="en")
        )
        async with postgres.engine.begin() as conn:
            current_id = await _create_settings(postgres, conn, review.repository_id)
            await conn.execute(
                update(RepositorySettings)
                .where(RepositorySettings.id == current_id)
                .values(ignores={"globs": ["src/a.py"]})
            )
            await conn.execute(
                update(Repository)
                .where(Repository.id == review.repository_id)
                .values(current_settings_id=current_id)
            )
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    assert await PostgresOutboxDispatcher(factory, queue).dispatch_batch() == 1
    llm = DeterministicLlm()
    async with httpx.AsyncClient(transport=github_http()) as client:
        worker = PostgresReviewWorker(
            factory,
            queue,
            PostgresAnalysisHandler(
                factory, GithubClient(client), llm, schemas_dir=SCHEMAS
            ),
            owner="analysis",
            task_kind="analyze",
        )
        assert await worker.run_once() is True
    assert len(llm.payloads) == 1
    metadata = cast(dict[str, Any], llm.payloads[0].payload_body["metadata"])
    assert metadata["rules_version"] == str(frozen_id)
    assert metadata["output_language"] == "en"
    async with factory() as session:
        review = await session.get(ReviewJob, review_id)
        settings = await session.get(RepositorySettings, frozen_id)
        assert review is not None and settings is not None
        assert review.status == "COMPLETED" and review.finished_at is not None
        assert (
            review.repository_settings_id,
            review.rules_digest,
            review.config_digest,
        ) == identity
        assert (settings.rules, settings.ignores) == ({}, {})
        assert settings.rules_digest == identity[1] and settings.output_language == "en"
        assert (
            await session.scalar(
                select(func.count())
                .select_from(Finding)
                .join(ChunkResult)
                .where(ChunkResult.review_job_id == review_id)
            )
            == 1
        )
        publication = (
            await session.scalars(
                select(Publication).where(Publication.review_job_id == review_id)
            )
        ).one()
        assert publication.status == "PENDING"
        publish = (
            await session.scalars(
                select(OutboxEvent).where(
                    OutboxEvent.review_job_id == review_id,
                    OutboxEvent.task_kind == "publish",
                )
            )
        ).one()
        assert publish.broker_published_at is None
    pending = await redis_client.xpending(f"{stream_prefix}:analyze", "review-workers")
    assert pending["pending"] == 0


async def test_backend_limitation_preserves_valid_result_without_repair(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    review_id: uuid.UUID,
) -> None:
    factory = async_sessionmaker(postgres.engine)
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    assert await PostgresOutboxDispatcher(factory, queue).dispatch_batch() == 1
    llm = DeterministicLlm()
    model_limitations = [f"Model limitation {number}" for number in range(10)]
    raw = {
        "summary": llm.result.summary,
        "findings": [asdict(finding) for finding in llm.result.findings],
        "limitations": model_limitations,
    }
    _validator(SCHEMAS / "llm-output/v1.json").validate(raw)
    llm.result = replace(
        llm.result,
        limitations=[*model_limitations, "Backend note: PR metadata was shortened."],
    )
    _validator(SCHEMAS / "review-result/v1.json").validate(asdict(llm.result))
    async with httpx.AsyncClient(transport=github_http()) as client:
        worker = PostgresReviewWorker(
            factory,
            queue,
            PostgresAnalysisHandler(
                factory, GithubClient(client), llm, schemas_dir=SCHEMAS
            ),
            owner="analysis",
            task_kind="analyze",
        )
        assert await worker.run_once() is True
    async with factory() as session:
        review = await session.get(ReviewJob, review_id)
        assert review is not None and review.status == "PARTIAL"
        assert (
            review.finished_at is not None
            and review.final_summary == llm.result.summary
        )
        chunk = (
            await session.scalars(
                select(ChunkResult).where(ChunkResult.review_job_id == review_id)
            )
        ).one()
        assert chunk.limitations == llm.result.limitations
        finding = (
            await session.scalars(
                select(Finding).where(Finding.chunk_result_id == chunk.id)
            )
        ).one()
        assert finding.title == llm.result.findings[0].title
        assert finding.explanation == llm.result.findings[0].explanation
        assert finding.recommendation == llm.result.findings[0].recommendation
        publication = (
            await session.scalars(
                select(Publication).where(Publication.review_job_id == review_id)
            )
        ).one()
        assert publication.status == "PENDING"
        publish = (
            await session.scalars(
                select(OutboxEvent).where(
                    OutboxEvent.review_job_id == review_id,
                    OutboxEvent.task_kind == "publish",
                )
            )
        ).one()
        assert publish.broker_published_at is None
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ReviewEvent)
                .where(
                    ReviewEvent.review_job_id == review_id,
                    ReviewEvent.reason_code == "LLM_OUTPUT_INVALID",
                )
            )
            == 0
        )
    assert len(llm.payloads) == 1
    pending = await redis_client.xpending(f"{stream_prefix}:analyze", "review-workers")
    assert pending["pending"] == 0
