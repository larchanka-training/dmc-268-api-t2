"""Durable worker retry using the original Redis delivery and PostgreSQL."""

# ruff: noqa: PLR2004 -- literal execution counts

import asyncio
import uuid
from datetime import timedelta
from pathlib import Path

import httpx
from redis.asyncio import Redis
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_analysis_handler import DeterministicLlm
from test_postgres_integration import PostgresHarness

from app.adapters.analysis_handler import PostgresAnalysisHandler
from app.adapters.redis_queue import RedisQueue
from app.adapters.review_worker import PostgresReviewWorker
from app.application.ports import QueueDelivery, QueueTaskEnvelope, TaskKind
from app.db.models import OutboxEvent, ReviewEvent, ReviewJob
from app.vcs.github import GithubClient

pytest_plugins = ("test_analysis_handler",)


async def test_retryable_failure_defers_original_redis_delivery_without_ack(  # noqa: PLR0915 -- original message lifecycle
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    review_id: uuid.UUID,
) -> None:
    factory = async_sessionmaker(postgres.engine)
    async with factory() as session:
        event = (
            await session.scalars(
                select(OutboxEvent).where(OutboxEvent.review_job_id == review_id)
            )
        ).one()
        task = QueueTaskEnvelope(
            1, str(event.id), str(review_id), "analyze", 1, event.trace_id
        )
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    message_id = await queue.enqueue(task)
    calls = []
    llm = DeterministicLlm()

    def unavailable(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as http:
        handler = PostgresAnalysisHandler(
            factory, GithubClient(http), llm, schemas_dir=Path("schemas")
        )
        for owner in ("first", "redelivery"):
            if owner != "first":
                await redis_client.xgroup_setid(
                    f"{stream_prefix}:analyze", "review-workers", "0"
                )
            assert not await PostgresReviewWorker(
                factory, queue, handler, owner=owner, task_kind="analyze"
            ).run_once()
            async with factory() as session:
                job = await session.get(ReviewJob, review_id)
                assert job is not None and job.retry_at is not None
                assert job.analysis_attempts == 1
                assert job.finished_at is None
                deadline = job.analysis_deadline_at
            assert len(calls) == 1
        received, resume = asyncio.Event(), asyncio.Event()

        class HeldDelivery(RedisQueue):
            async def receive(
                self, *, task_kind: TaskKind, consumer_name: str
            ) -> QueueDelivery | None:
                delivery = await super().receive(
                    task_kind=task_kind, consumer_name=consumer_name
                )
                assert delivery is not None and delivery.message_id == message_id
                received.set()
                await resume.wait()
                return delivery

        await redis_client.xgroup_setid(
            f"{stream_prefix}:analyze", "review-workers", "0"
        )
        first = asyncio.create_task(
            PostgresReviewWorker(
                factory,
                HeldDelivery(redis_client, stream_prefix=stream_prefix),
                handler,
                owner="held",
                task_kind="analyze",
            ).run_once()
        )
        second = None
        try:
            await asyncio.wait_for(received.wait(), 5)
            await redis_client.xgroup_setid(
                f"{stream_prefix}:analyze", "review-workers", "0"
            )
            second = asyncio.create_task(
                PostgresReviewWorker(
                    factory, queue, handler, owner="overlapping", task_kind="analyze"
                ).run_once()
            )
            resume.set()
            first_result, second_result = await asyncio.gather(first, second)
            assert first_result is False and second_result is False
        finally:
            resume.set()
            first.cancel()
            if second is not None:
                second.cancel()
                await asyncio.gather(first, second, return_exceptions=True)
            else:
                await asyncio.gather(first, return_exceptions=True)
        assert len(calls) == 1
        async with factory.begin() as session:
            await session.execute(
                update(ReviewJob)
                .where(ReviewJob.id == review_id)
                .values(retry_at=func.clock_timestamp() - timedelta(seconds=1))
            )
        await redis_client.xgroup_setid(
            f"{stream_prefix}:analyze", "review-workers", "0"
        )
        assert not await PostgresReviewWorker(
            factory, queue, handler, owner="due", task_kind="analyze"
        ).run_once()
        async with factory() as session:
            job = await session.get(ReviewJob, review_id)
            assert job is not None and job.analysis_attempts == 2
            assert job.analysis_deadline_at == deadline
            events = (
                await session.scalars(
                    select(ReviewEvent).where(
                        ReviewEvent.review_job_id == review_id,
                        ReviewEvent.event_type == "FAILURE",
                    )
                )
            ).all()
            assert len(events) == 2 and all(event.retryable for event in events)
    pending = await redis_client.xpending_range(
        f"{stream_prefix}:analyze", "review-workers", "-", "+", 10
    )
    assert [entry["message_id"] for entry in pending] == [message_id]
    assert len(calls) == 2 and llm.payloads == []
