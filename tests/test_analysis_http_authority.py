"""Worker/PG heartbeat + real Gateway/MockTransport concurrency regressions."""

import asyncio
import uuid
from datetime import timedelta

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select, update
from test_analysis_accounting import Execution, wait_until_expired
from test_frozen_configuration import PROMPTS, runtime_profiles

from app.adapters.redis_queue import RedisQueue
from app.adapters.review_worker import PostgresReviewWorker
from app.application.ports import QueueTaskEnvelope, TaskLeaseSnapshot
from app.db.llm_accounting import PostgresReviewRunBudget
from app.db.models import ReviewJob, TaskLease
from app.db.task_leases import PostgresTaskLeaseStore
from app.llm.client import ChatMessage, LLMRequest
from app.llm.errors import BudgetExceededError
from app.review.configuration import FrozenConfigurationReader, build_frozen_gateway
from app.review.schemas.result import LLMReviewOutput

pytest_plugins = ("test_analysis_accounting", "test_redis_queue")


@pytest.mark.parametrize("deadline_expires", [False, True])
async def test_healthy_heartbeat_extends_lease_but_never_http_deadline(
    execution: Execution,
    redis_client: Redis,
    stream_prefix: str,
    monkeypatch: pytest.MonkeyPatch,
    deadline_expires: bool,
) -> None:
    # Scale only the lease for an integration barrier crossing; HTTP keeps its
    # actual frozen 120s setting. The numeric 90/120 clock case is also tested.
    monkeypatch.setattr(
        "app.db.task_leases.LEASE_DURATION", timedelta(milliseconds=500)
    )
    assert await PostgresTaskLeaseStore(execution.factory).release(execution.lease)
    entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []
    claims = []
    timed_out = []

    async def response(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return httpx.Response(
            200,
            json={
                "model": "test-model",
                "choices": [
                    {
                        "message": {
                            "content": '{"summary":"ok","findings":[],"limitations":[]}'
                        }
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as http:

        class Analysis:
            async def handle(
                self, task: QueueTaskEnvelope, lease: TaskLeaseSnapshot
            ) -> None:
                claims.append(lease)
                decision = await execution.store().begin(task=task, lease=lease)
                assert decision.accepted
                if deadline_expires:
                    async with execution.factory.begin() as session:
                        await session.execute(
                            update(ReviewJob)
                            .where(ReviewJob.id == lease.review_job_id)
                            .values(
                                analysis_deadline_at=func.clock_timestamp()
                                + timedelta(milliseconds=500)
                            )
                        )
                config = await FrozenConfigurationReader(
                    execution.factory, PROMPTS, runtime_profiles()
                ).resolve(lease.review_job_id)
                budget = PostgresReviewRunBudget(
                    execution.factory,
                    task=task,
                    lease=lease,
                    analysis_attempt=decision.analysis_attempt,
                )
                try:
                    result = await build_frozen_gateway(
                        config, runtime_profiles(), http
                    ).generate_structured(
                        LLMRequest(
                            messages=[ChatMessage(role="user", content="review")],
                            max_output_tokens=2000,
                            timeout_s=120,
                        ),
                        LLMReviewOutput,
                        budget=budget,
                        input_tokens=100,
                        chunk_id=execution.chunk_id,
                        operation_id=uuid.uuid4(),
                    )
                    assert result.parsed.summary == "ok"
                except BudgetExceededError:
                    timed_out.append(True)

        queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
        await queue.enqueue(execution.task)
        worker = PostgresReviewWorker(
            execution.factory,
            queue,
            Analysis(),
            owner="rolling",
            task_kind="analyze",
            heartbeat_interval=0.02,
        )
        running = asyncio.create_task(worker.run_once())
        try:
            await asyncio.wait_for(entered.wait(), 5)
            if not deadline_expires:
                await wait_until_expired(execution.factory, claims[0].expires_at)
                async with execution.factory() as session:
                    assert await session.scalar(
                        select(TaskLease.expires_at > func.clock_timestamp()).where(
                            TaskLease.review_job_id == claims[0].review_job_id
                        )
                    )
                assert not running.done()
                assert calls[0].extensions["timeout"]["read"] > 119  # noqa: PLR2004 -- frozen120 minus DB round trip
                release.set()
            assert await asyncio.wait_for(running, 5) is False
            assert len(calls) == 1
            assert cancelled.is_set() == deadline_expires
            assert bool(timed_out) == deadline_expires
            assert (
                await redis_client.xpending(
                    f"{stream_prefix}:analyze", "review-workers"
                )
            )["pending"] == 1
        finally:
            release.set()
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
