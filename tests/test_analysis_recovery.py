"""AC26: paid HTTP survives rollback/crash; Redis ACK requires durable proof."""

# ruff: noqa: PLR2004 -- explicit ledger and delivery counts

import asyncio
import json
import uuid
from datetime import timedelta
from pathlib import Path
from types import TracebackType

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    AsyncSessionTransaction,
    async_sessionmaker,
)
from test_analysis_gateway import OUTPUT, analysis_task
from test_analysis_handler import github_http
from test_analysis_terminal import (  # noqa: F401 -- local fixture
    PROFILES,
    PROMPTS,
    freeze,
    ledger_cleanup,
)
from test_postgres_integration import PostgresHarness

from app.adapters.analysis_handler import PostgresAnalysisHandler
from app.adapters.outbox_dispatcher import PostgresOutboxDispatcher
from app.adapters.redis_queue import RedisQueue
from app.adapters.review_worker import PostgresReviewWorker
from app.application.ports import (
    LeaseLostError,
    QueueDelivery,
)
from app.db.models import (
    ChunkResult,
    OutboxEvent,
    Publication,
    ReviewJob,
    ReviewLlmAttempt,
    TaskLease,
)
from app.db.task_leases import PostgresTaskLeaseStore
from app.db.worker_tasks import PostgresWorkerTasks
from app.review.execution import FrozenAnalysisExecutionFactory
from app.vcs.github import GithubClient

pytest_plugins = ("test_analysis_handler",)


class SimulatedCrashError(RuntimeError):
    pass


def production_handler(
    factory: async_sessionmaker[AsyncSession],
    github: httpx.AsyncClient,
    http: httpx.AsyncClient,
) -> PostgresAnalysisHandler:
    return PostgresAnalysisHandler(
        factory,
        GithubClient(github),
        None,
        schemas_dir=Path("schemas"),
        execution_factory=FrozenAnalysisExecutionFactory(
            factory, PROMPTS, PROFILES, http
        ),
    )


def paid_response(calls: list[httpx.Request]) -> httpx.MockTransport:
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


@pytest.mark.parametrize("failure", ["rollback", "crash"])
@pytest.mark.parametrize("request_cap", [1, 2])
async def test_paid_settlement_survives_result_failure_and_fresh_worker(  # noqa: PLR0913, PLR0917, PLR0915 -- transaction barrier and restart
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    review_id: uuid.UUID,
    failure: str,
    request_cap: int,
) -> None:
    factory = async_sessionmaker(postgres.engine, expire_on_commit=False)
    await freeze(factory, review_id, run_max_requests=request_cap)
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    assert await PostgresOutboxDispatcher(factory, queue).dispatch_batch() == 1
    entered, release = asyncio.Event(), asyncio.Event()

    class FailingCommit(AsyncSessionTransaction):
        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            traceback: TracebackType | None,
        ) -> None:
            if exc_type is None and any(
                isinstance(row, OutboxEvent) and row.task_kind == "publish"
                for row in self.session.new
            ):
                entered.set()
                await release.wait()
                try:
                    if failure == "crash":
                        raise SimulatedCrashError(
                            "after settlement, before result commit"
                        )
                    await self.session.flush()
                    await self.session.execute(text("SELECT 1 / 0"))
                except BaseException:
                    await self.rollback()
                    raise
            await super().__aexit__(exc_type, exc, traceback)

    class FailingSession(AsyncSession):
        def begin(self) -> AsyncSessionTransaction:
            return FailingCommit(self)

    failing = async_sessionmaker[AsyncSession](
        postgres.engine, class_=FailingSession, expire_on_commit=False
    )
    calls: list[httpx.Request] = []
    task = await analysis_task(factory, review_id)
    async with (
        httpx.AsyncClient(transport=github_http()) as github,
        httpx.AsyncClient(transport=paid_response(calls)) as http,
    ):
        running = asyncio.create_task(
            PostgresReviewWorker(
                factory,
                queue,
                production_handler(failing, github, http),
                owner="will-crash",
                task_kind="analyze",
            ).run_once()
        )
        try:
            await asyncio.wait_for(entered.wait(), 5)
            async with factory() as session:
                first_charge = (
                    await session.scalars(
                        select(ReviewLlmAttempt).where(
                            ReviewLlmAttempt.review_job_id == review_id
                        )
                    )
                ).one()
                assert (
                    first_charge.state,
                    first_charge.actual_input_tokens,
                    first_charge.actual_output_tokens,
                ) == ("SUCCEEDED", 100, 40)
                job = await session.get(ReviewJob, review_id)
                assert job is not None and job.finished_at is None
            release.set()
            with pytest.raises(
                SimulatedCrashError if failure == "crash" else DBAPIError
            ):
                await running
        finally:
            release.set()
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        async with factory() as session:
            job = await session.get(ReviewJob, review_id)
            assert job is not None and job.finished_at is None
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(ChunkResult)
                    .where(ChunkResult.review_job_id == review_id)
                )
                == 0
            )
            publication = (
                await session.scalars(
                    select(Publication).where(Publication.review_job_id == review_id)
                )
            ).one()
            assert publication.status == "NOT_READY"
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(OutboxEvent)
                    .where(
                        OutboxEvent.review_job_id == review_id,
                        OutboxEvent.task_kind == "publish",
                    )
                )
                == 0
            )
        pending = await redis_client.xpending_range(
            f"{stream_prefix}:analyze", "review-workers", "-", "+", 10
        )
        assert len(pending) == 1
        original_id = pending[0]["message_id"]
        # Controlled original-ID redelivery; this does not implement XAUTOCLAIM.
        await redis_client.xgroup_setid(
            f"{stream_prefix}:analyze", "review-workers", "0"
        )
        assert await PostgresReviewWorker(
            factory,
            queue,
            production_handler(factory, github, http),
            owner="recreated",
            task_kind="analyze",
        ).run_once()
    assert len(calls) == request_cap
    async with factory() as session:
        ledger = (
            await session.scalars(
                select(ReviewLlmAttempt)
                .where(ReviewLlmAttempt.review_job_id == review_id)
                .order_by(ReviewLlmAttempt.analysis_attempt)
            )
        ).all()
        assert len(ledger) == request_cap
        assert all(
            (row.actual_input_tokens, row.actual_output_tokens) == (100, 40)
            for row in ledger
        )
        if request_cap == 2:
            assert ledger[0].id != ledger[1].id
            assert ledger[0].fencing_token < ledger[1].fencing_token
        job = await session.get(ReviewJob, review_id)
        assert job is not None and job.status == (
            "COMPLETED" if request_cap == 2 else "FAILED"
        )
        assert job.analysis_attempts == 2 and job.finished_at is not None
    assert (
        original_id
        and (await redis_client.xpending(f"{stream_prefix}:analyze", "review-workers"))[
            "pending"
        ]
        == 0
    )
    assert await PostgresWorkerTasks(factory).completed(task)


async def test_terminal_commit_before_failed_ack_redelivers_without_new_http(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    review_id: uuid.UUID,
) -> None:
    factory = async_sessionmaker(postgres.engine, expire_on_commit=False)
    await freeze(factory, review_id)
    proof_seen = asyncio.Event()

    class AckFails(RedisQueue):
        async def ack(self, delivery: QueueDelivery) -> bool:
            assert await PostgresWorkerTasks(factory).completed(delivery.envelope)
            proof_seen.set()
            raise ConnectionError(
                "simulated broker disconnect after durable completion"
            )

    queue = AckFails(redis_client, stream_prefix=stream_prefix)
    assert await PostgresOutboxDispatcher(factory, queue).dispatch_batch() == 1
    calls: list[httpx.Request] = []
    async with (
        httpx.AsyncClient(transport=github_http()) as github,
        httpx.AsyncClient(transport=paid_response(calls)) as http,
    ):
        with pytest.raises(ConnectionError):
            await PostgresReviewWorker(
                factory,
                queue,
                production_handler(factory, github, http),
                owner="ack-fails",
                task_kind="analyze",
            ).run_once()
        assert proof_seen.is_set() and len(calls) == 1
        assert (
            await redis_client.xpending(f"{stream_prefix}:analyze", "review-workers")
        )["pending"] == 1
        await redis_client.xgroup_setid(
            f"{stream_prefix}:analyze", "review-workers", "0"
        )
        assert await PostgresReviewWorker(
            factory,
            RedisQueue(redis_client, stream_prefix=stream_prefix),
            production_handler(factory, github, http),
            owner="ack-recovery",
            task_kind="analyze",
        ).run_once()
    assert len(calls) == 1
    async with factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ReviewLlmAttempt)
                .where(ReviewLlmAttempt.review_job_id == review_id)
            )
            == 1
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


async def test_stale_worker_paid_http_cannot_persist_or_ack_and_recovery_pays_again(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    review_id: uuid.UUID,
) -> None:
    factory = async_sessionmaker(postgres.engine, expire_on_commit=False)
    await freeze(factory, review_id, run_max_requests=2)
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    assert await PostgresOutboxDispatcher(factory, queue).dispatch_batch() == 1
    entered, release = asyncio.Event(), asyncio.Event()
    calls: list[httpx.Request] = []

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
        running = asyncio.create_task(
            PostgresReviewWorker(
                factory,
                queue,
                production_handler(factory, github, http),
                owner="stale-http",
                task_kind="analyze",
            ).run_once()
        )
        try:
            await asyncio.wait_for(entered.wait(), 5)
            async with factory.begin() as session:
                await session.execute(
                    update(TaskLease)
                    .where(TaskLease.review_job_id == review_id)
                    .values(expires_at=func.clock_timestamp() - timedelta(seconds=1))
                )
            leases = PostgresTaskLeaseStore(factory)
            successor = await leases.acquire(
                review_id, owner="new-fence", task_kind="analyze"
            )
            assert successor is not None and successor.fencing_token == 2
            release.set()
            with pytest.raises(LeaseLostError):
                await running
            async with factory() as session:
                first = (
                    await session.scalars(
                        select(ReviewLlmAttempt).where(
                            ReviewLlmAttempt.review_job_id == review_id
                        )
                    )
                ).one()
                assert first.state == "RESERVED" and first.actual_input_tokens is None
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(ChunkResult)
                        .where(ChunkResult.review_job_id == review_id)
                    )
                    == 0
                )
            assert (
                await redis_client.xpending(
                    f"{stream_prefix}:analyze", "review-workers"
                )
            )["pending"] == 1
            assert await leases.release(successor)
            await redis_client.xgroup_setid(
                f"{stream_prefix}:analyze", "review-workers", "0"
            )
            assert await PostgresReviewWorker(
                factory,
                queue,
                production_handler(factory, github, http),
                owner="recover-after-loss",
                task_kind="analyze",
            ).run_once()
        finally:
            release.set()
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
    assert len(calls) == 2
    async with factory() as session:
        rows = (
            await session.scalars(
                select(ReviewLlmAttempt)
                .where(ReviewLlmAttempt.review_job_id == review_id)
                .order_by(ReviewLlmAttempt.analysis_attempt)
            )
        ).all()
        assert [row.state for row in rows] == ["RESERVED", "SUCCEEDED"]
        job = await session.get(ReviewJob, review_id)
        assert job is not None and job.status == "COMPLETED"
