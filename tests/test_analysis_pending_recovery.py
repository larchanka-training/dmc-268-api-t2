"""Production composition: Redis PEL recovery preserves PostgreSQL authority."""

# ruff: noqa: PLR2004 -- explicit persisted counts

import asyncio
import json
import uuid
from dataclasses import replace
from datetime import timedelta
from typing import Any

import httpx
import pytest
from redis.asyncio import Redis
from redis.exceptions import NoPermissionError
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_analysis_gateway import OUTPUT, analysis_task
from test_analysis_handler import github_http
from test_analysis_recovery import paid_response, production_handler
from test_analysis_terminal import freeze, ledger_cleanup  # noqa: F401 -- fixture
from test_postgres_integration import PostgresHarness
from test_review_worker import redis_without_command

from app.adapters.redis_queue import RedisQueue
from app.adapters.review_worker import PostgresReviewWorker
from app.application.ports import LeaseLostError
from app.db.models import (
    ChunkResult,
    OutboxEvent,
    Publication,
    ReviewJob,
    ReviewLlmAttempt,
    TaskLease,
)
from app.db.task_leases import PostgresTaskLeaseStore

pytest_plugins = ("test_analysis_handler",)


async def snapshot(
    factory: async_sessionmaker[AsyncSession], review_id: uuid.UUID
) -> dict[str, Any]:
    """Independent committed state, including immutable ledger/settlement fields."""
    async with factory() as session:
        result = {}
        for model in (
            ReviewJob,
            ReviewLlmAttempt,
            ChunkResult,
            Publication,
            OutboxEvent,
        ):
            table = model.__table__
            key = table.c.id if model is ReviewJob else table.c.review_job_id
            rows = (
                await session.execute(
                    select(model.__table__).where(key == review_id).order_by(model.id)
                )
            ).all()
            result[model.__tablename__] = [dict(row._mapping) for row in rows]
        return result


@pytest.mark.parametrize("guard", ["lease", "retry", "identity", "quarantine"])
async def test_recovery_guards_prevent_begin_http_ledger_and_ack(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    review_id: uuid.UUID,
    guard: str,
) -> None:
    factory = async_sessionmaker(postgres.engine, expire_on_commit=False)
    await freeze(factory, review_id)
    task = await analysis_task(factory, review_id)
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    message_id = await queue.enqueue(
        replace(task, trace_id="forged") if guard == "identity" else task
    )
    assert await queue.receive(task_kind="analyze", consumer_name="crashed")
    leases = PostgresTaskLeaseStore(factory)
    lease = None
    if guard == "lease":
        lease = await leases.acquire(review_id, owner="live", task_kind="analyze")
        assert lease is not None
    elif guard == "retry":
        async with factory.begin() as session:
            await session.execute(
                update(ReviewJob)
                .where(ReviewJob.id == review_id)
                .values(
                    started_at=func.clock_timestamp(),
                    analysis_deadline_at=func.clock_timestamp() + timedelta(minutes=2),
                    retry_at=func.clock_timestamp() + timedelta(minutes=1),
                )
            )
    elif guard == "quarantine":
        async with factory.begin() as session:
            await session.execute(
                update(OutboxEvent)
                .where(OutboxEvent.id == uuid.UUID(task.event_id))
                .values(
                    quarantined_at=func.clock_timestamp(),
                    quarantine_reason_code="INVALID_ENVELOPE",
                )
            )
    before = await snapshot(factory, review_id)
    calls = []

    def forbidden(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise AssertionError("PostgreSQL guard must precede all external HTTP")

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as http:
            worker = PostgresReviewWorker(
                factory,
                queue,
                production_handler(factory, http, http),
                owner="recovery",
                task_kind="analyze",
            )
            assert await worker.recover_once(cursor="0-0", min_idle_time=0) == (
                "0-0",
                False,
            )
        assert calls == []
        assert await snapshot(factory, review_id) == before
        pending = await redis_client.xpending_range(
            f"{stream_prefix}:analyze", "review-workers", "-", "+", 10
        )
        assert [(row["message_id"], row["consumer"]) for row in pending] == [
            (message_id, "recovery")
        ]
        assert await redis_client.xlen(f"{stream_prefix}:dead") == 0
    finally:
        if lease is not None:
            assert await leases.release(lease)


async def test_future_then_due_retry_uses_pg_attempt_not_redis_delivery_count(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    review_id: uuid.UUID,
) -> None:
    factory = async_sessionmaker(postgres.engine, expire_on_commit=False)
    await freeze(factory, review_id)
    task = await analysis_task(factory, review_id)
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    message_id = await queue.enqueue(task)
    assert await queue.receive(task_kind="analyze", consumer_name="crashed")
    async with factory.begin() as session:
        await session.execute(
            update(ReviewJob)
            .where(ReviewJob.id == review_id)
            .values(
                started_at=func.clock_timestamp(),
                analysis_deadline_at=func.clock_timestamp() + timedelta(minutes=2),
                retry_at=func.clock_timestamp() + timedelta(minutes=1),
            )
        )
    before = await snapshot(factory, review_id)
    deadline = before["review_job"][0]["analysis_deadline_at"]
    calls: list[httpx.Request] = []
    async with (
        httpx.AsyncClient(transport=github_http()) as github,
        httpx.AsyncClient(transport=paid_response(calls)) as http,
    ):
        worker = PostgresReviewWorker(
            factory,
            queue,
            production_handler(factory, github, http),
            owner="retry",
            task_kind="analyze",
        )
        for _ in range(3):
            assert await worker.recover_once(cursor="0-0", min_idle_time=0) == (
                "0-0",
                False,
            )
        assert await snapshot(factory, review_id) == before
        assert calls == []
        async with factory.begin() as session:
            await session.execute(
                update(ReviewJob)
                .where(ReviewJob.id == review_id)
                .values(retry_at=func.clock_timestamp() - timedelta(seconds=1))
            )
        assert await worker.recover_once(cursor="0-0", min_idle_time=0) == ("0-0", True)
    after = await snapshot(factory, review_id)
    assert after["review_job"][0]["analysis_attempts"] == 1
    assert after["review_job"][0]["analysis_deadline_at"] == deadline
    assert len(calls) == 1 and len(after["review_llm_attempt"]) == 1
    remaining = await redis_client.xrange(f"{stream_prefix}:analyze")
    assert remaining is not None and remaining[0][0] == message_id
    assert (await redis_client.xpending(f"{stream_prefix}:analyze", "review-workers"))[
        "pending"
    ] == 0


async def test_expired_pg_deadline_precedes_future_retry_with_zero_http(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    review_id: uuid.UUID,
) -> None:
    factory = async_sessionmaker(postgres.engine, expire_on_commit=False)
    await freeze(factory, review_id)
    task = await analysis_task(factory, review_id)
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    await queue.enqueue(task)
    assert await queue.receive(task_kind="analyze", consumer_name="crashed")
    async with factory.begin() as session:
        await session.execute(
            update(ReviewJob)
            .where(ReviewJob.id == review_id)
            .values(
                started_at=func.clock_timestamp() - timedelta(minutes=3),
                analysis_deadline_at=func.clock_timestamp() - timedelta(seconds=1),
                retry_at=func.clock_timestamp() + timedelta(minutes=1),
            )
        )
    calls = []

    def forbidden(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise AssertionError("deadline terminalization must use zero HTTP")

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as http:
        worker = PostgresReviewWorker(
            factory,
            queue,
            production_handler(factory, http, http),
            owner="deadline",
            task_kind="analyze",
        )
        assert await worker.recover_once(cursor="0-0", min_idle_time=0) == ("0-0", True)
    state = await snapshot(factory, review_id)
    assert calls == [] and state["review_llm_attempt"] == []
    assert state["review_job"][0]["status"] == "FAILED"
    assert state["review_job"][0]["finished_at"] is not None
    assert state["review_job"][0]["analysis_attempts"] == 0


@pytest.mark.parametrize("lose_lease", [False, True])
async def test_reclaim_during_paid_http_obeys_live_and_replaced_pg_fence(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    review_id: uuid.UUID,
    lose_lease: bool,
) -> None:
    factory = async_sessionmaker(postgres.engine, expire_on_commit=False)
    await freeze(factory, review_id, run_max_requests=2)
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    message_id = await queue.enqueue(await analysis_task(factory, review_id))
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            entered.set()
            await release.wait()
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
        first = PostgresReviewWorker(
            factory,
            queue,
            production_handler(factory, github, http),
            owner="a",
            task_kind="analyze",
        )
        second = PostgresReviewWorker(
            factory,
            queue,
            production_handler(factory, github, http),
            owner="b",
            task_kind="analyze",
        )
        running = asyncio.create_task(first.run_once())
        try:
            await asyncio.wait_for(entered.wait(), 5)
            assert await second.recover_once(cursor="0-0", min_idle_time=0) == (
                "0-0",
                False,
            )
            assert len(calls) == 1
            pending = await redis_client.xpending_range(
                f"{stream_prefix}:analyze", "review-workers", "-", "+", 10
            )
            assert [(row["message_id"], row["consumer"]) for row in pending] == [
                (message_id, "b")
            ]
            if lose_lease:
                async with factory.begin() as session:
                    await session.execute(
                        update(TaskLease)
                        .where(TaskLease.review_job_id == review_id)
                        .values(
                            expires_at=func.clock_timestamp() - timedelta(seconds=1)
                        )
                    )
                assert await second.recover_once(cursor="0-0", min_idle_time=0) == (
                    "0-0",
                    True,
                )
                completed = await snapshot(factory, review_id)
                release.set()
                with pytest.raises(LeaseLostError):
                    await running
                assert await snapshot(factory, review_id) == completed
                assert len(calls) == 2
                assert sorted(
                    row["state"] for row in completed["review_llm_attempt"]
                ) == ["RESERVED", "SUCCEEDED"]
                assert len(completed["chunk_result"]) == 1
            else:
                release.set()
                assert await running
                assert len(calls) == 1
        finally:
            release.set()
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
    assert (await redis_client.xpending(f"{stream_prefix}:analyze", "review-workers"))[
        "pending"
    ] == 0


@pytest.mark.parametrize(
    "handoff",
    ["complete", "publication_missing", "outbox_missing", "outbox_quarantined"],
)
async def test_commit_before_ack_recovery_uses_only_durable_completion_proof(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    review_id: uuid.UUID,
    handoff: str,
) -> None:
    factory = async_sessionmaker(postgres.engine, expire_on_commit=False)
    await freeze(factory, review_id)
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    message_id = await queue.enqueue(await analysis_task(factory, review_id))
    calls: list[httpx.Request] = []
    async with (
        httpx.AsyncClient(transport=github_http()) as github,
        httpx.AsyncClient(transport=paid_response(calls)) as http,
    ):
        async with redis_without_command(redis_client, stream_prefix, "xack") as denied:
            first = PostgresReviewWorker(
                factory,
                RedisQueue(denied, stream_prefix=stream_prefix),
                production_handler(factory, github, http),
                owner="a",
                task_kind="analyze",
            )
            with pytest.raises(NoPermissionError):
                await first.run_once()
        assert len(calls) == 1
    committed = await snapshot(factory, review_id)
    assert committed["review_job"][0]["status"] == "COMPLETED"
    assert committed["review_job"][0]["finished_at"] is not None
    assert committed["publication"][0]["status"] == "PENDING"
    assert len(committed["chunk_result"]) == len(committed["review_llm_attempt"]) == 1
    assert committed["review_llm_attempt"][0]["state"] == "SUCCEEDED"
    assert (
        len([row for row in committed["outbox_event"] if row["task_kind"] == "publish"])
        == 1
    )
    if handoff != "complete":
        async with factory.begin() as session:
            if handoff == "publication_missing":
                await session.execute(
                    update(Publication)
                    .where(Publication.review_job_id == review_id)
                    .values(started_at=None)
                )
            elif handoff == "outbox_missing":
                await session.execute(
                    delete(OutboxEvent).where(
                        OutboxEvent.review_job_id == review_id,
                        OutboxEvent.task_kind == "publish",
                    )
                )
            else:
                await session.execute(
                    update(OutboxEvent)
                    .where(
                        OutboxEvent.review_job_id == review_id,
                        OutboxEvent.task_kind == "publish",
                    )
                    .values(
                        quarantined_at=func.clock_timestamp(),
                        quarantine_reason_code="INVALID_ENVELOPE",
                    )
                )
    before_recovery = await snapshot(factory, review_id)
    pending = await redis_client.xpending_range(
        f"{stream_prefix}:analyze", "review-workers", "-", "+", 10
    )
    assert [(row["message_id"], row["consumer"]) for row in pending] == [
        (message_id, "a")
    ]
    recovered_http = []

    def forbidden(request: httpx.Request) -> httpx.Response:
        recovered_http.append(request)
        raise AssertionError("terminal duplicate must never repeat handler HTTP")

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as http:
        async with redis_without_command(redis_client, stream_prefix, "xack") as denied:
            second = PostgresReviewWorker(
                factory,
                RedisQueue(denied, stream_prefix=stream_prefix),
                production_handler(factory, http, http),
                owner="b",
                task_kind="analyze",
            )
            if handoff == "complete":
                with pytest.raises(NoPermissionError):
                    await second.recover_once(cursor="0-0", min_idle_time=0)
            else:
                assert await second.recover_once(cursor="0-0", min_idle_time=0) == (
                    "0-0",
                    False,
                )
        pending = await redis_client.xpending_range(
            f"{stream_prefix}:analyze", "review-workers", "-", "+", 10
        )
        assert [
            (row["message_id"], row["consumer"], row["times_delivered"])
            for row in pending
        ] == [(message_id, "b", 2)]
        third = PostgresReviewWorker(
            factory,
            queue,
            production_handler(factory, http, http),
            owner="c",
            task_kind="analyze",
        )
        assert await third.recover_once(cursor="0-0", min_idle_time=0) == (
            "0-0",
            handoff == "complete",
        )
    assert recovered_http == []
    assert await snapshot(factory, review_id) == before_recovery
    assert (await redis_client.xpending(f"{stream_prefix}:analyze", "review-workers"))[
        "pending"
    ] == (0 if handoff == "complete" else 1)
