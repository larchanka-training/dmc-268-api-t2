"""Lost transport returns through the existing fenced zero-HTTP handler path."""

import asyncio
import uuid
from datetime import timedelta
from types import TracebackType

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    AsyncSessionTransaction,
    async_sessionmaker,
)
from test_analysis_gateway import analysis_task
from test_analysis_handler import github_http
from test_analysis_pending_recovery import snapshot
from test_analysis_recovery import (
    SimulatedCrashError,
    paid_response,
    production_handler,
)
from test_analysis_terminal import freeze, ledger_cleanup  # noqa: F401 -- fixture
from test_postgres_integration import PostgresHarness

from app.adapters.outbox_dispatcher import PostgresOutboxDispatcher
from app.adapters.redis_queue import RedisQueue
from app.adapters.review_worker import PostgresReviewWorker
from app.db.models import ChunkResult, ContextPayload, OutboxEvent, ReviewJob, TaskLease
from app.db.worker_tasks import PostgresWorkerTasks

pytest_plugins = ("test_analysis_handler",)


@pytest.mark.parametrize("clock", ["queue", "analysis"])
@pytest.mark.parametrize("saved", [False, True])
async def test_lost_expired_future_retry_terminalizes_without_begin_or_http(  # noqa: PLR0913, PLR0917 -- deadline and saved-result fixture matrix
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    review_id: uuid.UUID,
    payload_data: dict[str, object],
    clock: str,
    saved: bool,
) -> None:
    factory = async_sessionmaker(postgres.engine, expire_on_commit=False)
    await freeze(factory, review_id)
    task = await analysis_task(factory, review_id)
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    service = PostgresOutboxDispatcher(factory, queue)
    assert await service.dispatch_batch() == 1
    await redis_client.delete(f"{stream_prefix}:analyze")
    async with factory.begin() as session:
        if saved:
            context = ContextPayload(
                id=uuid.uuid4(),
                review_job_id=review_id,
                schema_version=1,
                payload_body={
                    key: value
                    for key, value in payload_data.items()
                    if key not in {"schema_version", "review_id", "chunk_id"}
                },
            )
            session.add(context)
            await session.flush()
            session.add(
                ChunkResult(
                    id=uuid.uuid4(),
                    review_job_id=review_id,
                    context_payload_id=context.id,
                    schema_version=1,
                    summary="Saved useful review",
                    limitations=[],
                    usage=None,
                    latency_ms=1,
                )
            )
            await session.flush()
        values = {"retry_at": func.clock_timestamp() + timedelta(minutes=1)}
        if clock == "queue":
            values["queue_deadline_at"] = func.clock_timestamp() - timedelta(seconds=1)
        else:
            values["started_at"] = func.clock_timestamp() - timedelta(minutes=3)
            values["analysis_deadline_at"] = func.clock_timestamp() - timedelta(
                seconds=1
            )
        await session.execute(
            update(ReviewJob).where(ReviewJob.id == review_id).values(**values)
        )
        await session.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == uuid.UUID(task.event_id))
            .values(
                dispatch_not_before=func.clock_timestamp() - timedelta(seconds=1),
            )
        )
    assert await service.recover_batch() == 1
    calls = []

    def forbidden(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise AssertionError("expired task must not use provider HTTP")

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as http:
        worker = PostgresReviewWorker(
            factory,
            queue,
            production_handler(factory, http, http),
            owner="deadline",
            task_kind="analyze",
        )
        assert await worker.run_once()
    after = await snapshot(factory, review_id)
    job = after["review_job"][0]
    assert job["status"] == ("PARTIAL" if saved else "FAILED")
    assert job["finished_at"] is not None
    if clock == "queue":
        assert job["started_at"] is None and job["analysis_deadline_at"] is None
    else:
        assert job["started_at"] is not None and job["analysis_deadline_at"] is not None
    assert job["analysis_attempts"] == 0
    assert after["review_llm_attempt"] == [] and calls == []
    if saved:
        assert after["publication"][0]["status"] == "PENDING"
        assert len(after["chunk_result"]) == 1
        assert (
            len([row for row in after["outbox_event"] if row["task_kind"] == "publish"])
            == 1
        )
    # Terminal duplicate still ACKs without publisher or a second durable effect.
    await queue.enqueue(task)
    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as http:
        duplicate = PostgresReviewWorker(
            factory,
            queue,
            production_handler(factory, http, http),
            owner="duplicate",
            task_kind="analyze",
        )
        assert await duplicate.run_once()
    assert await snapshot(factory, review_id) == after
    assert await redis_client.xlen(f"{stream_prefix}:publish") == 0


async def test_lost_transport_after_paid_result_crash_preserves_charge_and_handoff(  # noqa: PLR0915 -- end-to-end paid crash and durable recovery proof
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    review_id: uuid.UUID,
) -> None:
    factory = async_sessionmaker(postgres.engine, expire_on_commit=False)
    await freeze(factory, review_id, run_max_requests=2)
    task = await analysis_task(factory, review_id)
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    dispatcher = PostgresOutboxDispatcher(factory, queue)
    assert await dispatcher.dispatch_batch() == 1
    entered, release = asyncio.Event(), asyncio.Event()

    class CrashBeforeResultCommit(AsyncSessionTransaction):
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
                await self.rollback()
                raise SimulatedCrashError("paid settlement before result commit")
            await super().__aexit__(exc_type, exc, traceback)

    class FailingSession(AsyncSession):
        def begin(self) -> AsyncSessionTransaction:
            return CrashBeforeResultCommit(self)

    failing = async_sessionmaker[AsyncSession](
        postgres.engine, class_=FailingSession, expire_on_commit=False
    )
    calls: list[httpx.Request] = []
    async with (
        httpx.AsyncClient(transport=github_http()) as github,
        httpx.AsyncClient(transport=paid_response(calls)) as http,
    ):
        running = asyncio.create_task(
            PostgresReviewWorker(
                factory,
                queue,
                production_handler(failing, github, http),
                owner="paid-transport-crash",
                task_kind="analyze",
            ).run_once()
        )
        try:
            await asyncio.wait_for(entered.wait(), 5)
            before = await snapshot(factory, review_id)
            first_charge = before["review_llm_attempt"][0]
            assert (
                first_charge["state"],
                first_charge["actual_input_tokens"],
                first_charge["actual_output_tokens"],
            ) == ("SUCCEEDED", 100, 40)
            assert before["chunk_result"] == []
            assert before["publication"][0]["status"] == "NOT_READY"
            assert len(before["outbox_event"]) == 1
            release.set()
            with pytest.raises(SimulatedCrashError):
                await running
        finally:
            release.set()
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)

        after_crash = await snapshot(factory, review_id)
        assert after_crash == before
        receipt = before["outbox_event"][0]["broker_message_id"]
        assert await redis_client.xrange(f"{stream_prefix}:analyze", receipt, receipt)
        assert await redis_client.delete(f"{stream_prefix}:analyze") == 1
        async with factory.begin() as session:
            await session.execute(
                update(ReviewJob)
                .where(ReviewJob.id == review_id)
                .values(retry_at=func.clock_timestamp() - timedelta(seconds=1))
            )
            await session.execute(
                update(OutboxEvent)
                .where(OutboxEvent.id == uuid.UUID(task.event_id))
                .values(
                    dispatch_not_before=func.clock_timestamp() - timedelta(seconds=1)
                )
            )
        assert await dispatcher.recover_batch() == 1
        assert await PostgresReviewWorker(
            factory,
            queue,
            production_handler(factory, github, http),
            owner="paid-transport-retry",
            task_kind="analyze",
        ).run_once()
        assert len(calls) == 2  # noqa: PLR2004 -- frozen run cap
        after = await snapshot(factory, review_id)
        job = after["review_job"][0]
        assert job["status"] == "COMPLETED" and job["finished_at"] is not None
        assert job["analysis_attempts"] == 2  # noqa: PLR2004 -- two paid executions
        unchanged = set(job) - {
            "status",
            "finished_at",
            "final_summary",
            "analysis_attempts",
            "analysis_attempt_fence",
            "retry_at",
        }
        assert {key: job[key] for key in unchanged} == {
            key: before["review_job"][0][key] for key in unchanged
        }
        ledger = after["review_llm_attempt"]
        assert len(ledger) == 2  # noqa: PLR2004 -- no paid request refunded
        assert next(row for row in ledger if row["id"] == first_charge["id"]) == (
            first_charge
        )
        second_charge = next(row for row in ledger if row["id"] != first_charge["id"])
        assert second_charge["state"] == "SUCCEEDED"
        assert second_charge["fencing_token"] > first_charge["fencing_token"]
        assert second_charge["analysis_attempt"] == job["analysis_attempts"]
        async with factory() as session:
            assert (
                await session.scalar(
                    select(TaskLease.fencing_token).where(
                        TaskLease.review_job_id == review_id
                    )
                )
                == second_charge["fencing_token"]
            )
        assert len(after["chunk_result"]) == 1
        assert len(after["publication"]) == 1
        assert after["publication"][0]["status"] == "PENDING"
        publish = [
            row for row in after["outbox_event"] if row["task_kind"] == "publish"
        ]
        assert len(publish) == 1 and publish[0]["broker_published_at"] is None
        analyze = next(
            row for row in after["outbox_event"] if row["task_kind"] == "analyze"
        )
        assert analyze["broker_message_id"] != receipt
        assert analyze["recovery_send_count"] == 1
        assert await PostgresWorkerTasks(factory).completed(task)
        await queue.enqueue(task)
        duplicate_calls: list[httpx.Request] = []

        def forbidden(request: httpx.Request) -> httpx.Response:
            duplicate_calls.append(request)
            raise AssertionError("terminal duplicate must not use provider HTTP")

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(forbidden)
        ) as closed:
            assert await PostgresReviewWorker(
                factory,
                queue,
                production_handler(factory, closed, closed),
                owner="paid-transport-duplicate",
                task_kind="analyze",
            ).run_once()
        assert duplicate_calls == []
        assert len(calls) == 2  # noqa: PLR2004 -- terminal duplicate makes no HTTP
        assert await snapshot(factory, review_id) == after
    assert await redis_client.xlen(f"{stream_prefix}:publish") == 0
