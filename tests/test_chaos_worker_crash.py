"""Chaos: worker dies mid-LLM-call — PEL reclaim re-drives to exactly-once."""

# ruff: noqa: PLR2004 -- literal ledger and attempt counts

import asyncio
import json
import uuid
from datetime import timedelta

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_analysis_gateway import OUTPUT, analysis_task
from test_analysis_handler import github_http
from test_analysis_pending_recovery import snapshot
from test_analysis_recovery import production_handler
from test_analysis_terminal import freeze, ledger_cleanup  # noqa: F401 -- fixture
from test_postgres_integration import PostgresHarness

from app.adapters.outbox_dispatcher import PostgresOutboxDispatcher
from app.adapters.redis_queue import RedisQueue
from app.adapters.review_worker import PostgresReviewWorker
from app.db.models import TaskLease

pytest_plugins = ("test_analysis_handler",)

pytestmark = pytest.mark.chaos


async def test_worker_killed_mid_llm_call_is_reclaimed_and_completed_exactly_once(  # noqa: PLR0915 -- one coherent crash-and-recover scenario
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    review_id: uuid.UUID,
) -> None:
    factory = async_sessionmaker(postgres.engine, expire_on_commit=False)
    await freeze(factory, review_id)
    task = await analysis_task(factory, review_id)
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    assert await PostgresOutboxDispatcher(factory, queue).dispatch_batch() == 1

    entered = asyncio.Event()
    calls: list[httpx.Request] = []

    def dying_provider(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        entered.set()
        raise asyncio.CancelledError

    async with (
        httpx.AsyncClient(transport=github_http()) as github,
        httpx.AsyncClient(transport=httpx.MockTransport(dying_provider)) as http,
    ):
        victim = asyncio.create_task(
            PostgresReviewWorker(
                factory,
                queue,
                production_handler(factory, github, http),
                owner="chaos-kill-victim",
                task_kind="analyze",
            ).run_once()
        )
        try:
            await asyncio.wait_for(entered.wait(), 5)
        finally:
            # Process death: no ack, no defer, no settlement — hard cancel.
            victim.cancel()
            await asyncio.gather(victim, return_exceptions=True)

    mid = await snapshot(factory, review_id)
    job = mid["review_job"][0]
    assert job["finished_at"] is None
    assert job["analysis_attempts"] == 1
    assert mid["chunk_result"] == []
    pending = await redis_client.xpending(f"{stream_prefix}:analyze", "review-workers")
    assert pending["pending"] == 1  # delivery stuck in the PEL

    # Time passes: the dead worker's lease expires; a recovery pass reclaims
    # the abandoned delivery through the explicit pending-recovery seam.
    async with factory.begin() as session:
        await session.execute(
            update(TaskLease)
            .where(TaskLease.review_job_id == review_id)
            .values(expires_at=func.clock_timestamp() - timedelta(seconds=1))
        )
    async with (
        httpx.AsyncClient(transport=github_http()) as github,
        httpx.AsyncClient(transport=paid_provider(calls)) as http,
    ):
        rescuer = PostgresReviewWorker(
            factory,
            queue,
            production_handler(factory, github, http),
            owner="chaos-kill-rescuer",
            task_kind="analyze",
        )
        _, acknowledged = await rescuer.recover_once(cursor="0", min_idle_time=0)
        assert acknowledged is True

    after = await snapshot(factory, review_id)
    job = after["review_job"][0]
    assert job["status"] == "COMPLETED" and job["finished_at"] is not None
    assert job["analysis_attempts"] == 2
    assert len(after["chunk_result"]) == 1
    assert after["publication"][0]["status"] == "PENDING"
    assert (
        len([row for row in after["outbox_event"] if row["task_kind"] == "publish"])
        == 1
    )
    ledger = after["review_llm_attempt"]
    # The killed attempt never settled: its reservation is left dangling and
    # the rescuer re-drives under a new fencing token.
    assert sorted(row["state"] for row in ledger) == ["RESERVED", "SUCCEEDED"]
    failed_token = next(
        row["fencing_token"] for row in ledger if row["state"] == "RESERVED"
    )
    succeeded = next(row for row in ledger if row["state"] == "SUCCEEDED")
    assert succeeded["fencing_token"] > failed_token
    assert len(calls) == 2  # one killed call, one paid retry

    # Terminal duplicate: redelivery ACKs with zero provider HTTP.
    before_duplicate = await snapshot(factory, review_id)
    await queue.enqueue(task)
    forbidden: list[httpx.Request] = []

    def forbidden_provider(request: httpx.Request) -> httpx.Response:
        forbidden.append(request)
        raise AssertionError("terminal duplicate must not use provider HTTP")

    async with (
        httpx.AsyncClient(transport=github_http()) as github,
        httpx.AsyncClient(transport=httpx.MockTransport(forbidden_provider)) as http,
    ):
        duplicate = PostgresReviewWorker(
            factory,
            queue,
            production_handler(factory, github, http),
            owner="chaos-kill-duplicate",
            task_kind="analyze",
        )
        assert await duplicate.run_once() is True
    assert forbidden == []
    assert await snapshot(factory, review_id) == before_duplicate
    assert await redis_client.xlen(f"{stream_prefix}:publish") == 0


def paid_provider(calls: list[httpx.Request]) -> httpx.MockTransport:
    def reply(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "model": "test-model",
                "choices": [{"message": {"content": json.dumps(OUTPUT)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 40},
            },
        )

    return httpx.MockTransport(reply)
