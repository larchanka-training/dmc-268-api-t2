"""AC28: production internal composition, real PG/Redis and mocked HTTP edges."""

# ruff: noqa: PLR2004 -- known integration counts

import json
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_analysis_gateway import OUTPUT
from test_analysis_handler import github_http
from test_analysis_terminal import (  # noqa: F401 -- local fixture
    PROFILES,
    PROMPTS,
    detail,
    freeze,
    ledger_cleanup,
)
from test_frozen_configuration import save_policy
from test_postgres_integration import PostgresHarness

from app.adapters.analysis_composition import build_postgres_analysis_worker
from app.adapters.outbox_dispatcher import PostgresOutboxDispatcher
from app.adapters.redis_queue import RedisQueue
from app.db.models import (
    ChunkResult,
    ContextPayload,
    Finding,
    OutboxEvent,
    Publication,
    ReviewEvent,
    ReviewJob,
    ReviewLlmAttempt,
)
from app.db.worker_tasks import PostgresWorkerTasks
from app.vcs.github import GithubClient

pytest_plugins = ("test_analysis_handler",)


async def test_production_composition_dispatches_frozen_review_and_acks_after_handoff(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    review_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    factory = async_sessionmaker(postgres.engine, expire_on_commit=False)
    await freeze(factory, review_id)
    monkeypatch.setenv("LLM__EUROROUTER__MODEL", "wrong-runtime-model")
    monkeypatch.setenv("LLM__PROMPT_VERSION", "wrong-runtime-version")
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    calls: list[dict[str, Any]] = []

    async def reply(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        # Every paid request has a committed durable debit before the wire call.
        async with factory() as session:
            row = (
                await session.scalars(
                    select(ReviewLlmAttempt).where(
                        ReviewLlmAttempt.review_job_id == review_id
                    )
                )
            ).one()
            assert row.state == "RESERVED" and row.model_ref == "test-model"
        return httpx.Response(
            200,
            json={
                "model": "test-model",
                "choices": [{"message": {"content": json.dumps(OUTPUT)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 40},
            },
        )

    assert await PostgresOutboxDispatcher(factory, queue).dispatch_batch() == 1

    class ProofQueue(RedisQueue):
        async def ack(self, delivery: Any) -> bool:
            assert await PostgresWorkerTasks(factory).completed(delivery.envelope)
            return await super().ack(delivery)

    async with (
        httpx.AsyncClient(transport=github_http()) as github,
        httpx.AsyncClient(transport=httpx.MockTransport(reply)) as http,
    ):
        worker = build_postgres_analysis_worker(
            factory,
            ProofQueue(redis_client, stream_prefix=stream_prefix),
            github=GithubClient(github),
            http=http,
            profiles=PROFILES,
            prompt_root=PROMPTS,
            schemas_dir=Path("schemas"),
            owner="production-composition",
        )
        assert await worker.run_once()
    view = await detail(factory, review_id)
    assert view["status"] == "COMPLETED" and view["publication_status"] == "PENDING"
    assert view["progress"] == {"completed_chunks": 1, "planned_chunks": 1}
    assert view["summary"] == OUTPUT["summary"]
    assert calls[0]["model"] == "test-model" and calls[0]["temperature"] == 0
    assert calls[0]["response_format"]["type"] == "json_schema"
    async with factory() as session:
        context = (
            await session.scalars(
                select(ContextPayload).where(ContextPayload.review_job_id == review_id)
            )
        ).one()
        assert context.payload_body["budget"]["limit_input_tokens"] == 10000
        assert context.payload_body["budget"]["reserved_output_tokens"] == 2000
        chunk = (
            await session.scalars(
                select(ChunkResult).where(ChunkResult.review_job_id == review_id)
            )
        ).one()
        assert chunk.usage == {"input_tokens": 100, "output_tokens": 40}
        assert (
            await session.scalar(
                select(func.count())
                .select_from(Finding)
                .where(Finding.chunk_result_id == chunk.id)
            )
            == 1
        )
        ledger = (
            await session.scalars(
                select(ReviewLlmAttempt).where(
                    ReviewLlmAttempt.review_job_id == review_id
                )
            )
        ).one()
        assert ledger.state == "SUCCEEDED" and ledger.actual_input_tokens == 100
        publication = (
            await session.scalars(
                select(Publication).where(Publication.review_job_id == review_id)
            )
        ).one()
        assert publication.finished_at is None
        assert (
            await session.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(
                    OutboxEvent.review_job_id == review_id,
                    OutboxEvent.task_kind == "publish",
                )
            )
            == 1
        )
    assert (await redis_client.xpending(f"{stream_prefix}:analyze", "review-workers"))[
        "pending"
    ] == 0


async def test_missing_runtime_profile_terminalizes_before_http(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    review_id: uuid.UUID,
) -> None:

    factory = async_sessionmaker(postgres.engine, expire_on_commit=False)
    await freeze(factory, review_id)
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    assert await PostgresOutboxDispatcher(factory, queue).dispatch_batch() == 1
    calls = []

    def forbidden(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise AssertionError("configuration failure must precede external HTTP")

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as http:
        worker = build_postgres_analysis_worker(
            factory,
            queue,
            github=GithubClient(http),
            http=http,
            profiles={},
            prompt_root=PROMPTS,
            schemas_dir=Path("schemas"),
            owner="missing-profile",
        )
        assert await worker.run_once()
    view = await detail(factory, review_id)
    assert view["status"] == "FAILED" and view["finished_at"] is not None
    assert view["error"]["reason_code"] == "LLM_UNAVAILABLE"
    assert view["publication_status"] == "NOT_READY"
    async with factory() as session:
        event = (
            await session.scalars(
                select(ReviewEvent).where(
                    ReviewEvent.review_job_id == review_id,
                    ReviewEvent.status == "FAILED",
                )
            )
        ).one()
        assert event.safe_details == {"error_code": "unsupported_model_profile"}
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ReviewLlmAttempt)
                .where(ReviewLlmAttempt.review_job_id == review_id)
            )
            == 0
        )
    assert calls == []


@pytest.mark.parametrize(
    "defect,code",
    [
        ("prompt_version", "unsupported_prompt_bundle"),
        ("prompt_digest", "unsupported_prompt_bundle"),
        ("model_pin", "model_digest_unattestable"),
        ("policy_version", "unsupported_model_settings"),
        ("config_digest", "config_digest_mismatch"),
    ],
)
@pytest.mark.parametrize("saved", [False, True])
async def test_production_configuration_failures_are_terminal_before_external_io(  # noqa: PLR0913, PLR0917 -- failure matrix
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    review_id: uuid.UUID,
    defect: str,
    code: str,
    saved: bool,
) -> None:

    factory = async_sessionmaker(postgres.engine, expire_on_commit=False)
    await freeze(factory, review_id)
    if defect == "prompt_version":
        await save_policy(postgres, review_id, prompt_version="v99")
    elif defect == "prompt_digest":
        await save_policy(postgres, review_id, prompt_digest="0" * 64)
    elif defect == "model_pin":
        await save_policy(postgres, review_id, model_digest="a" * 64)
    elif defect == "policy_version":
        async with factory() as session:
            job = await session.get(ReviewJob, review_id)
            assert job is not None
            policy = dict(job.model_settings)
        policy["token_estimator_version"] = "review-conservative-v99"
        await save_policy(postgres, review_id, model_settings=policy)
    else:
        async with factory.begin() as session:
            await session.execute(
                update(ReviewJob)
                .where(ReviewJob.id == review_id)
                .values(config_digest="0" * 64)
            )
    if saved:
        async with factory.begin() as session:
            session.add(
                ChunkResult(
                    id=uuid.uuid4(),
                    review_job_id=review_id,
                    schema_version=1,
                    summary="Verified retained result",
                    limitations=[],
                    latency_ms=1,
                )
            )
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    assert await PostgresOutboxDispatcher(factory, queue).dispatch_batch() == 1
    calls = []

    def forbidden(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise AssertionError("configuration failure must precede external I/O")

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as http:
        assert await build_postgres_analysis_worker(
            factory,
            queue,
            github=GithubClient(http),
            http=http,
            profiles=PROFILES,
            prompt_root=PROMPTS,
            schemas_dir=Path("schemas"),
            owner="unsupported",
        ).run_once()
    view = await detail(factory, review_id)
    assert view["status"] == ("PARTIAL" if saved else "FAILED")
    assert view["error"]["reason_code"] == "LLM_UNAVAILABLE"
    assert view["publication_status"] == ("PENDING" if saved else "NOT_READY")
    async with factory() as session:
        event = (
            await session.scalars(
                select(ReviewEvent).where(
                    ReviewEvent.review_job_id == review_id,
                    ReviewEvent.status == view["status"],
                )
            )
        ).one()
        assert (
            event.safe_details is not None and event.safe_details["error_code"] == code
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ReviewLlmAttempt)
                .where(ReviewLlmAttempt.review_job_id == review_id)
            )
            == 0
        )
    assert calls == []


async def test_production_retry_and_duplicate_delivery_share_ledger_and_ack_proof(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    review_id: uuid.UUID,
) -> None:

    factory = async_sessionmaker(postgres.engine, expire_on_commit=False)
    await freeze(factory, review_id)
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    assert await PostgresOutboxDispatcher(factory, queue).dispatch_batch() == 1
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) <= 2:
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={
                "model": "test-model",
                "choices": [{"message": {"content": json.dumps(OUTPUT)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 40},
            },
        )

    async with (
        httpx.AsyncClient(transport=github_http()) as github,
        httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http,
    ):

        def worker(owner: str) -> Any:
            return build_postgres_analysis_worker(
                factory,
                queue,
                github=GithubClient(github),
                http=http,
                profiles=PROFILES,
                prompt_root=PROMPTS,
                schemas_dir=Path("schemas"),
                owner=owner,
            )

        assert not await worker("retry-first").run_once()
        pending = await redis_client.xpending_range(
            f"{stream_prefix}:analyze", "review-workers", "-", "+", 10
        )
        original_id = pending[0]["message_id"]
        async with factory() as session:
            job = await session.get(ReviewJob, review_id)
            assert (
                job is not None and job.retry_at is not None and job.finished_at is None
            )
            deadline = job.analysis_deadline_at
        await redis_client.xgroup_setid(
            f"{stream_prefix}:analyze", "review-workers", "0"
        )
        assert not await worker("retry-early").run_once()
        assert len(calls) == 2
        async with factory.begin() as session:
            await session.execute(
                update(ReviewJob)
                .where(ReviewJob.id == review_id)
                .values(retry_at=func.clock_timestamp() - timedelta(seconds=1))
            )
        await redis_client.xgroup_setid(
            f"{stream_prefix}:analyze", "review-workers", "0"
        )
        assert await worker("retry-due").run_once()
        await redis_client.xgroup_setid(
            f"{stream_prefix}:analyze", "review-workers", "0"
        )
        assert await worker("terminal-duplicate").run_once()
    assert len(calls) == 3 and original_id
    async with factory() as session:
        rows = (
            await session.scalars(
                select(ReviewLlmAttempt)
                .where(ReviewLlmAttempt.review_job_id == review_id)
                .order_by(ReviewLlmAttempt.reserved_at)
            )
        ).all()
        assert [row.state for row in rows] == ["ERROR", "ERROR", "SUCCEEDED"]
        assert [row.analysis_attempt for row in rows] == [1, 1, 2]
        job = await session.get(ReviewJob, review_id)
        assert (
            job is not None
            and job.status == "COMPLETED"
            and job.analysis_deadline_at == deadline
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(
                    OutboxEvent.review_job_id == review_id,
                    OutboxEvent.task_kind == "publish",
                )
            )
            == 1
        )
    assert (await redis_client.xpending(f"{stream_prefix}:analyze", "review-workers"))[
        "pending"
    ] == 0
