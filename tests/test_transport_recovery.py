"""Stage8C03 public dispatcher transport contract on real PostgreSQL and Redis."""

import asyncio
import json
import os

# ruff: noqa: PLR2004 -- transport contract limits
import uuid
from collections.abc import Iterator, MutableMapping
from datetime import timedelta
from typing import Any

import pytest
from redis.asyncio import Redis
from redis.asyncio.connection import Connection, ConnectionPool
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from redis.exceptions import TimeoutError
from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker
from structlog.testing import capture_logs
from test_postgres_integration import PostgresHarness, _create_outbox_event
from test_review_worker import SkipAnalysis

from app.adapters.outbox_dispatcher import PostgresOutboxDispatcher
from app.adapters.redis_queue import RedisQueue
from app.adapters.review_worker import PostgresReviewWorker
from app.application.ports import (
    DurableTaskOutcome,
    QueueTaskEnvelope,
    TaskLeaseSnapshot,
    TransportProbe,
)
from app.db.models import OutboxEvent, Publication, ReviewJob, TaskLease
from app.db.task_leases import PostgresTaskLeaseStore

pytest_plugins = ("test_postgres_integration",)


@pytest.fixture
def diagnostics() -> Iterator[list[MutableMapping[str, Any]]]:
    with capture_logs() as records:
        yield records


@pytest.fixture
def transport_commands(monkeypatch: pytest.MonkeyPatch) -> list[tuple[object, ...]]:
    commands: list[tuple[object, ...]] = []
    original = Connection.send_command

    async def record(self: Connection, *args: object, **kwargs: Any) -> None:
        if args and args[0] in {"XADD", "XACK", "XRANGE"}:
            commands.append(args)
        await original(self, *args, **kwargs)

    monkeypatch.setattr(Connection, "send_command", record)
    return commands


async def state(postgres: PostgresHarness, event_id: uuid.UUID) -> dict[str, Any]:
    async with postgres.engine.connect() as connection:
        return dict(
            (
                await connection.execute(
                    select(OutboxEvent.__table__).where(OutboxEvent.id == event_id)
                )
            )
            .mappings()
            .one()
        )


def dispatcher(
    postgres: PostgresHarness, client: Redis, prefix: str
) -> PostgresOutboxDispatcher:
    return PostgresOutboxDispatcher(
        async_sessionmaker(postgres.engine), RedisQueue(client, stream_prefix=prefix)
    )


async def test_initial_enqueue_has_durable_receipt_and_db_reservation(
    postgres: PostgresHarness,
    dispatcher_redis: tuple[Redis, str],
) -> None:
    event_id, _ = await _create_outbox_event(postgres)
    client, prefix = dispatcher_redis
    async with postgres.engine.connect() as connection:
        before = (await connection.execute(select(func.clock_timestamp()))).scalar_one()
    assert await dispatcher(postgres, client, prefix).dispatch_batch() == 1
    async with postgres.engine.connect() as connection:
        after = (await connection.execute(select(func.clock_timestamp()))).scalar_one()
    row = await state(postgres, event_id)
    entries = await client.xrange(f"{prefix}:analyze")
    assert entries
    assert row["broker_message_id"] == entries[0][0]
    assert before + timedelta(seconds=90) <= row["dispatch_not_before"]
    assert row["dispatch_not_before"] <= after + timedelta(seconds=90)
    assert before <= row["broker_published_at"] <= after


async def test_lost_stream_recovers_same_identity_with_new_receipt(
    postgres: PostgresHarness,
    dispatcher_redis: tuple[Redis, str],
) -> None:

    event_id, _ = await _create_outbox_event(postgres)
    client, prefix = dispatcher_redis
    service = dispatcher(postgres, client, prefix)
    assert await service.dispatch_batch() == 1
    before = await state(postgres, event_id)
    original = await client.xrange(f"{prefix}:analyze")
    await client.delete(f"{prefix}:analyze")
    async with postgres.engine.begin() as connection:
        await connection.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == event_id)
            .values(
                dispatch_not_before=func.clock_timestamp() - timedelta(seconds=1),
            )
        )
    assert await service.recover_batch() == 1
    after = await state(postgres, event_id)
    entries = await client.xrange(f"{prefix}:analyze")
    assert entries and original
    assert entries[0][1] == original[0][1]
    assert after["broker_message_id"] == entries[0][0] != before["broker_message_id"]
    assert after["broker_published_at"] >= before["broker_published_at"]
    assert await service.dispatch_batch() == 0


async def test_live_lease_blocks_lost_payload_reservation(
    postgres: PostgresHarness,
    dispatcher_redis: tuple[Redis, str],
) -> None:
    client, prefix = dispatcher_redis
    service = dispatcher(postgres, client, prefix)
    await finished_history(postgres, service)
    event_id, review_id = await _create_outbox_event(postgres)
    await service.dispatch_batch()
    await client.delete(f"{prefix}:analyze")
    async with postgres.engine.begin() as connection:
        await connection.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == event_id)
            .values(
                dispatch_not_before=func.clock_timestamp() - timedelta(seconds=1),
            )
        )
    leases = PostgresTaskLeaseStore(async_sessionmaker(postgres.engine))
    assert await leases.acquire(review_id, owner="healthy", task_kind="analyze")
    before = await state(postgres, event_id)
    try:
        assert await service.recover_batch() == 0
        assert await state(postgres, event_id) == before
        assert await client.xlen(f"{prefix}:analyze") == 0
    finally:
        async with postgres.engine.begin() as connection:
            await connection.execute(
                delete(TaskLease).where(TaskLease.review_job_id == review_id)
            )


async def test_future_retry_defers_lost_delivery_without_mutating_analysis(
    postgres: PostgresHarness,
    dispatcher_redis: tuple[Redis, str],
) -> None:
    client, prefix = dispatcher_redis
    service = dispatcher(postgres, client, prefix)
    await finished_history(postgres, service)
    event_id, review_id = await _create_outbox_event(postgres)
    await service.dispatch_batch()
    await client.delete(f"{prefix}:analyze")
    async with postgres.engine.begin() as connection:
        await connection.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == event_id)
            .values(
                dispatch_not_before=func.clock_timestamp() - timedelta(seconds=1),
            )
        )
        await connection.execute(
            update(ReviewJob)
            .where(ReviewJob.id == review_id)
            .values(
                retry_at=func.clock_timestamp() + timedelta(minutes=2),
            )
        )
        before_job = (
            (
                await connection.execute(
                    select(ReviewJob.__table__).where(ReviewJob.id == review_id)
                )
            )
            .mappings()
            .one()
        )
    before = await state(postgres, event_id)
    assert await service.recover_batch() == 0
    assert await state(postgres, event_id) == before
    async with postgres.engine.begin() as connection:
        assert (
            await connection.execute(
                select(ReviewJob.__table__).where(ReviewJob.id == review_id)
            )
        ).mappings().one() == before_job
        await connection.execute(
            update(ReviewJob)
            .where(ReviewJob.id == review_id)
            .values(retry_at=func.clock_timestamp() - timedelta(seconds=1))
        )
    assert await service.recover_batch() == 1


async def test_terminal_commit_between_probe_and_reservation_prevents_send(
    postgres: PostgresHarness,
    dispatcher_redis: tuple[Redis, str],
) -> None:

    event_id, review_id = await _create_outbox_event(postgres)
    client, prefix = dispatcher_redis
    queue = RedisQueue(client, stream_prefix=prefix)
    await dispatcher(postgres, client, prefix).dispatch_batch()
    task = QueueTaskEnvelope(
        1, str(event_id), str(review_id), "analyze", 2, "trace-outbox-test"
    )
    await client.delete(f"{prefix}:analyze")
    async with postgres.engine.begin() as connection:
        await connection.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == event_id)
            .values(dispatch_not_before=func.clock_timestamp() - timedelta(seconds=1))
        )
    entered, release = asyncio.Event(), asyncio.Event()

    class HeldProbe(RedisQueue):
        async def probe(
            self, envelope: QueueTaskEnvelope, message_id: str
        ) -> TransportProbe:
            evidence = await super().probe(envelope, message_id)
            entered.set()
            await release.wait()
            return evidence

    service = PostgresOutboxDispatcher(
        async_sessionmaker(postgres.engine), HeldProbe(client, stream_prefix=prefix)
    )
    running = asyncio.create_task(service.recover_batch())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await queue.enqueue(task)
        handler = SkipAnalysis(postgres)
        assert await PostgresReviewWorker(
            handler.factory, queue, handler, owner="terminal", task_kind="analyze"
        ).run_once()
        before = await state(postgres, event_id)
        release.set()
        assert await running == 0
        assert await state(postgres, event_id) == before
        assert await client.xlen(f"{prefix}:analyze") == 1
    finally:
        release.set()
        await asyncio.gather(running, return_exceptions=True)
        async with postgres.engine.begin() as connection:
            await connection.execute(
                delete(TaskLease).where(TaskLease.review_job_id == review_id)
            )


async def test_repeated_loss_has_no_lifetime_or_window_limit_and_keeps_cooldown(
    postgres: PostgresHarness,
    dispatcher_redis: tuple[Redis, str],
) -> None:
    event_id, _ = await _create_outbox_event(postgres)
    client, prefix = dispatcher_redis
    await dispatcher(postgres, client, prefix).dispatch_batch()
    for _ in range(4):
        await client.delete(f"{prefix}:analyze")
        before = await state(postgres, event_id)
        assert await dispatcher(postgres, client, prefix).recover_batch() == 0
        assert await state(postgres, event_id) == before
        await make_due(postgres, event_id)
        assert await dispatcher(postgres, client, prefix).recover_batch() == 1
        assert await client.xlen(f"{prefix}:analyze") == 1


async def test_recovery_tick_reserves_at_most_ten_legacy_published_rows(
    postgres: PostgresHarness,
    dispatcher_redis: tuple[Redis, str],
) -> None:

    event_id, review_id = await _create_outbox_event(postgres)
    async with postgres.engine.begin() as connection:
        await connection.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == event_id)
            .values(broker_published_at=func.clock_timestamp())
        )
        for _ in range(10):
            await connection.execute(
                insert(OutboxEvent).values(
                    id=postgres.track("outbox_event", uuid.uuid4()),
                    review_job_id=review_id,
                    schema_version=1,
                    task_kind="analyze",
                    attempt=2,
                    trace_id="legacy",
                    broker_published_at=func.clock_timestamp(),
                )
            )
    client, prefix = dispatcher_redis
    service = dispatcher(postgres, client, prefix)
    assert await service.dispatch_batch() == 0
    assert await service.recover_batch() == 10
    assert await client.xlen(f"{prefix}:analyze") == 10
    assert await dispatcher(postgres, client, prefix).recover_batch() == 1


async def test_due_order_passes_hundred_present_and_untrusted_deliveries_across_restart(
    postgres: PostgresHarness,
    dispatcher_redis: tuple[Redis, str],
) -> None:
    client, prefix = dispatcher_redis
    service = dispatcher(postgres, client, prefix)
    await finished_history(postgres, service)
    first_id, review_id = await _create_outbox_event(postgres)
    ids = [first_id]
    async with postgres.engine.begin() as connection:
        for _ in range(100):
            event_id = postgres.track("outbox_event", uuid.uuid4())
            ids.append(event_id)
            await connection.execute(
                insert(OutboxEvent).values(
                    id=event_id,
                    review_job_id=review_id,
                    schema_version=1,
                    task_kind="analyze",
                    attempt=2,
                    trace_id="backlog",
                )
            )
    assert await service.dispatch_batch() == 100
    assert await service.dispatch_batch() == 1
    async with postgres.engine.begin() as connection:
        ordered = (
            await connection.execute(
                select(OutboxEvent.id, OutboxEvent.broker_message_id)
                .where(OutboxEvent.id.in_(ids))
                .order_by(OutboxEvent.created_at, OutboxEvent.id)
            )
        ).all()
        await connection.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id.in_(ids))
            .values(dispatch_not_before=func.now() - timedelta(seconds=1))
        )
        await connection.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == ordered[0].id)
            .values(trace_id="private-source-poison")
        )
    await client.xdel(f"{prefix}:analyze", ordered[-1].broker_message_id)
    before = await client.xlen(f"{prefix}:analyze")
    assert await service.recover_batch() == 0
    assert await dispatcher(postgres, client, prefix).recover_batch() == 1
    assert await client.xlen(f"{prefix}:analyze") == before + 1
    # Persisted deferral keeps restarted ticks bounded and non-amplifying.
    for _ in range(3):
        assert await service.recover_batch() == 0
    for row in ordered[:-1]:
        assert (await state(postgres, row.id))[
            "broker_message_id"
        ] == row.broker_message_id


async def test_authority_contention_is_bounded_and_defers_only_locked_candidate(
    postgres: PostgresHarness,
    dispatcher_redis: tuple[Redis, str],
) -> None:
    locked_id, review_id = await _create_outbox_event(postgres)
    available_id, _ = await _create_outbox_event(postgres)
    client, prefix = dispatcher_redis
    service = dispatcher(postgres, client, prefix)
    async with postgres.engine.begin() as blocker:
        await blocker.execute(
            select(ReviewJob).where(ReviewJob.id == review_id).with_for_update()
        )
        assert await asyncio.wait_for(service.dispatch_batch(batch_size=2), 3) == 1
        assert (await state(postgres, locked_id))["dispatch_not_before"] is None
        assert (await state(postgres, available_id))["dispatch_not_before"] is not None
        assert await client.xlen(f"{prefix}:analyze") == 1
    assert await service.dispatch_batch() == 1
    assert await client.xlen(f"{prefix}:analyze") == 2


async def test_expired_transport_replaces_once_then_uses_latest_receipt(
    postgres: PostgresHarness,
    dispatcher_redis: tuple[Redis, str],
) -> None:

    event_id, review_id = await _create_outbox_event(postgres)
    client, prefix = dispatcher_redis
    seconds, micros = await client.time()
    old_id = await client.xadd(
        f"{prefix}:analyze",
        {
            "envelope": QueueTaskEnvelope(
                1, str(event_id), str(review_id), "analyze", 2, "trace-outbox-test"
            ).to_json()
        },
        id=f"{seconds * 1000 + micros // 1000 - 1_200_001}-0",
    )
    async with postgres.engine.begin() as connection:
        await connection.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == event_id)
            .values(
                broker_published_at=func.clock_timestamp(),
                broker_message_id=old_id,
                dispatch_not_before=func.clock_timestamp() - timedelta(seconds=1),
            )
        )
    async with postgres.engine.begin() as connection:
        await connection.execute(
            update(ReviewJob)
            .where(ReviewJob.id == review_id)
            .values(
                queue_deadline_at=func.clock_timestamp() - timedelta(seconds=1),
                retry_at=func.clock_timestamp() + timedelta(minutes=1),
            )
        )
    service = dispatcher(postgres, client, prefix)
    assert await service.recover_batch() == 1
    assert await client.xlen(f"{prefix}:analyze") == 2
    for _ in range(3):
        assert await service.recover_batch() == 0


async def make_due(postgres: PostgresHarness, event_id: uuid.UUID) -> None:
    async with postgres.engine.begin() as connection:
        await connection.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == event_id)
            .values(dispatch_not_before=func.clock_timestamp() - timedelta(seconds=1))
        )


async def finished_history(
    postgres: PostgresHarness,
    service: PostgresOutboxDispatcher,
    *,
    status: str = "SKIPPED",
) -> list[tuple[uuid.UUID, uuid.UUID]]:
    history = [await _create_outbox_event(postgres) for _ in range(101)]
    assert await service.dispatch_batch() == 100
    assert await service.dispatch_batch() == 1
    async with postgres.engine.begin() as connection:
        await connection.execute(
            update(ReviewJob)
            .where(ReviewJob.id.in_([review_id for _, review_id in history]))
            .values(status=status, finished_at=func.clock_timestamp())
        )
    return history


async def test_finished_history_cannot_hide_active_lost_delivery(
    postgres: PostgresHarness,
    dispatcher_redis: tuple[Redis, str],
) -> None:
    client, prefix = dispatcher_redis
    service = dispatcher(postgres, client, prefix)
    history = await finished_history(postgres, service)
    event_id, _ = await _create_outbox_event(postgres)
    assert await service.dispatch_batch() == 1
    before = await state(postgres, event_id)
    queue = RedisQueue(client, stream_prefix=prefix)
    entries = await client.xrange(f"{prefix}:analyze")
    assert entries
    assert len(entries) == 102
    await client.delete(f"{prefix}:analyze")
    fields = entries[-1][1]
    assert fields is not None
    task = QueueTaskEnvelope(**json.loads(fields["envelope"]))
    assert task.event_id == str(event_id)
    assert await queue.probe(task, before["broker_message_id"]) == "ABSENT"
    await make_due(postgres, event_id)

    assert await service.recover_batch() == 1

    recovered = await client.xrange(f"{prefix}:analyze")
    assert recovered
    assert len(recovered) == 1
    assert recovered[0][1] == entries[-1][1]
    assert (await state(postgres, event_id))["dispatch_not_before"] is not None
    for historic_id, _ in history:
        historic = await state(postgres, historic_id)
        assert historic["broker_message_id"] is not None


async def test_quarantined_prefix_cannot_hide_active_lost_delivery(
    postgres: PostgresHarness,
    dispatcher_redis: tuple[Redis, str],
) -> None:
    first_id, review_id = await _create_outbox_event(postgres)
    quarantined_ids = [first_id]
    async with postgres.engine.begin() as connection:
        for _ in range(100):
            event_id = postgres.track("outbox_event", uuid.uuid4())
            quarantined_ids.append(event_id)
            await connection.execute(
                insert(OutboxEvent).values(
                    id=event_id,
                    review_job_id=review_id,
                    schema_version=1,
                    task_kind="analyze",
                    attempt=2,
                    trace_id="quarantined-history",
                )
            )
        await connection.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id.in_(quarantined_ids))
            .values(
                broker_message_id="1-0",
                quarantined_at=func.clock_timestamp(),
                quarantine_reason_code="INVALID_ENVELOPE",
            )
        )
    event_id, _ = await _create_outbox_event(postgres)
    client, prefix = dispatcher_redis
    service = dispatcher(postgres, client, prefix)
    assert await service.dispatch_batch() == 1
    await client.delete(f"{prefix}:analyze")
    await make_due(postgres, event_id)
    before = [await state(postgres, row_id) for row_id in quarantined_ids]
    assert await service.recover_batch() == 1
    assert await service.recover_batch() == 0
    assert await client.xlen(f"{prefix}:analyze") == 1
    assert [await state(postgres, row_id) for row_id in quarantined_ids] == before


async def test_committed_reservation_survives_crash_before_send_and_restart(
    postgres: PostgresHarness,
    dispatcher_redis: tuple[Redis, str],
) -> None:

    event_id, _ = await _create_outbox_event(postgres)
    client, prefix = dispatcher_redis

    class CrashBeforeSend(RedisQueue):
        async def enqueue(self, envelope: QueueTaskEnvelope) -> str:
            raise TimeoutError("synthetic crash before command")

    with pytest.raises(TimeoutError):
        await PostgresOutboxDispatcher(
            async_sessionmaker(postgres.engine),
            CrashBeforeSend(client, stream_prefix=prefix),
        ).dispatch_batch()
    before = await state(postgres, event_id)
    assert before["dispatch_not_before"] is not None
    assert before["broker_published_at"] is None and before["broker_message_id"] is None
    restarted = dispatcher(postgres, client, prefix)
    assert await restarted.dispatch_batch() == 0
    assert await restarted.recover_batch() == 0
    assert await client.xlen(f"{prefix}:analyze") == 0
    assert await state(postgres, event_id) == before
    await make_due(postgres, event_id)
    assert await restarted.recover_batch() == 1


async def test_finalize_database_failure_keeps_ambiguous_delivery_and_cooldown(
    postgres: PostgresHarness,
    dispatcher_redis: tuple[Redis, str],
) -> None:

    event_id, _ = await _create_outbox_event(postgres)
    client, prefix = dispatcher_redis
    name = f"receipt_failure_{uuid.uuid4().hex}"
    async with postgres.engine.begin() as connection:
        await connection.execute(
            text(
                f"""CREATE FUNCTION {name}() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                    IF NEW.id = '{event_id}'::uuid AND NEW.broker_message_id IS NOT NULL
                    THEN RAISE EXCEPTION 'synthetic finalize failure'; END IF;
                    RETURN NEW;
                END $$"""
            )
        )
        await connection.execute(
            text(
                f"CREATE TRIGGER {name} BEFORE UPDATE ON outbox_event "
                f"FOR EACH ROW EXECUTE FUNCTION {name}()"
            )
        )
    try:
        with pytest.raises(DBAPIError):
            await dispatcher(postgres, client, prefix).dispatch_batch()
        before = await state(postgres, event_id)
        assert (
            before["dispatch_not_before"] is not None
            and before["broker_message_id"] is None
        )
        assert before["broker_published_at"] is None
        assert await client.xlen(f"{prefix}:analyze") == 1
        assert await dispatcher(postgres, client, prefix).dispatch_batch() == 0
        assert await dispatcher(postgres, client, prefix).recover_batch() == 0
        assert await state(postgres, event_id) == before
    finally:
        async with postgres.engine.begin() as connection:
            await connection.execute(text(f"DROP TRIGGER {name} ON outbox_event"))
            await connection.execute(text(f"DROP FUNCTION {name}()"))
    await make_due(postgres, event_id)
    assert await dispatcher(postgres, client, prefix).recover_batch() == 1


async def test_hundred_parked_publish_events_cannot_hide_initial_or_recovery_analyze(
    postgres: PostgresHarness,
    dispatcher_redis: tuple[Redis, str],
) -> None:

    published_id, published_job = await _create_outbox_event(
        postgres, task_kind="publish"
    )
    parked = [published_id]
    async with postgres.engine.begin() as connection:
        await connection.execute(
            update(ReviewJob)
            .where(ReviewJob.id == published_job)
            .values(status="COMPLETED", finished_at=func.clock_timestamp())
        )
        await connection.execute(
            insert(Publication).values(
                id=postgres.track("publication", uuid.uuid4()),
                review_job_id=published_job,
                status="PENDING",
                started_at=func.clock_timestamp(),
                deadline_at=func.clock_timestamp() + timedelta(minutes=5),
            )
        )
        for _ in range(101):
            event_id = postgres.track("outbox_event", uuid.uuid4())
            parked.append(event_id)
            await connection.execute(
                insert(OutboxEvent).values(
                    id=event_id,
                    review_job_id=published_job,
                    schema_version=1,
                    task_kind="publish",
                    attempt=1,
                    trace_id="parked",
                )
            )
    analyze_id, _ = await _create_outbox_event(postgres)
    client, prefix = dispatcher_redis
    service = dispatcher(postgres, client, prefix)
    assert await service.dispatch_batch() == 1
    await client.delete(f"{prefix}:analyze")
    await make_due(postgres, analyze_id)
    assert await service.recover_batch() == 1
    assert await client.xlen(f"{prefix}:publish") == 0
    async with postgres.engine.connect() as connection:
        rows = (
            (
                await connection.execute(
                    select(OutboxEvent.__table__).where(OutboxEvent.id.in_(parked))
                )
            )
            .mappings()
            .all()
        )
        assert len(rows) == 102
        assert all(
            row["broker_published_at"] is None
            and row["broker_message_id"] is None
            and row["dispatch_not_before"] is None
            for row in rows
        )
        assert (
            await connection.execute(
                select(Publication.status).where(
                    Publication.review_job_id == published_job
                )
            )
        ).scalar_one() == "PENDING"


async def test_unknown_probe_fails_without_changing_receipt_or_throttle(
    postgres: PostgresHarness,
    dispatcher_redis: tuple[Redis, str],
) -> None:

    event_id, _ = await _create_outbox_event(postgres)
    client, prefix = dispatcher_redis
    await dispatcher(postgres, client, prefix).dispatch_batch()
    await make_due(postgres, event_id)
    before = await state(postgres, event_id)
    async with Redis(
        host="127.0.0.1",
        port=1,
        retry=Retry(NoBackoff(), 0),
        socket_timeout=0.1,
        socket_connect_timeout=0.1,
    ) as offline:
        with pytest.raises(RuntimeError, match="Transport probe unavailable"):
            await dispatcher(postgres, offline, prefix).recover_batch()
    assert await state(postgres, event_id) == before
    assert await client.xlen(f"{prefix}:analyze") == 1


async def test_recovery_xadd_response_timeout_charges_once_and_survives_restart(
    postgres: PostgresHarness,
    dispatcher_redis: tuple[Redis, str],
) -> None:

    event_id, _ = await _create_outbox_event(postgres)
    client, prefix = dispatcher_redis
    await dispatcher(postgres, client, prefix).dispatch_batch()
    before = await state(postgres, event_id)
    await client.delete(f"{prefix}:analyze")
    await make_due(postgres, event_id)
    sends = []

    class LoseResponse(Connection):
        xadd = False

        async def send_command(self, *args: Any, **kwargs: Any) -> None:
            await super().send_command(*args, **kwargs)
            self.xadd = args[0] == "XADD"
            if self.xadd:
                sends.append(args)

        async def read_response(self, *args: Any, **kwargs: Any) -> Any:
            response = await super().read_response(*args, **kwargs)
            if self.xadd:
                self.xadd = False
                raise TimeoutError("synthetic lost response")
            return response

    pool = ConnectionPool.from_url(
        os.environ["DMC268_TEST_REDIS_URL"],
        connection_class=LoseResponse,
        retry=Retry(NoBackoff(), 1),
        max_connections=1,
    )
    async with Redis.from_pool(pool) as lossy:
        with pytest.raises(TimeoutError):
            await dispatcher(postgres, lossy, prefix).recover_batch()
        assert await lossy.ping()
    row = await state(postgres, event_id)
    assert len(sends) == 1
    assert row["broker_message_id"] == before["broker_message_id"]
    assert row["broker_published_at"] == before["broker_published_at"]
    assert await dispatcher(postgres, client, prefix).recover_batch() == 0
    assert await client.xlen(f"{prefix}:analyze") == 1
    await make_due(postgres, event_id)
    assert await dispatcher(postgres, client, prefix).recover_batch() == 1
    assert await client.xlen(f"{prefix}:analyze") == 2


async def test_busy_healthy_worker_and_pel_backlog_never_amplify_latest_entries(
    postgres: PostgresHarness,
    dispatcher_redis: tuple[Redis, str],
) -> None:

    first_id, review_id = await _create_outbox_event(postgres)
    ids = [first_id]
    async with postgres.engine.begin() as connection:
        for _ in range(100):
            event_id = postgres.track("outbox_event", uuid.uuid4())
            ids.append(event_id)
            await connection.execute(
                insert(OutboxEvent).values(
                    id=event_id,
                    review_job_id=review_id,
                    schema_version=1,
                    task_kind="analyze",
                    attempt=2,
                    trace_id="healthy-backlog",
                )
            )
    lost_id, _ = await _create_outbox_event(postgres)
    client, prefix = dispatcher_redis
    queue = RedisQueue(client, stream_prefix=prefix)
    service = dispatcher(postgres, client, prefix)
    assert await service.dispatch_batch() == 100
    assert await service.dispatch_batch() == 2
    lost = await state(postgres, lost_id)
    await client.xdel(f"{prefix}:analyze", lost["broker_message_id"])
    await make_due(postgres, lost_id)
    entered, release = asyncio.Event(), asyncio.Event()

    class HealthyBusy(SkipAnalysis):
        async def handle(
            self, task: QueueTaskEnvelope, lease: TaskLeaseSnapshot
        ) -> DurableTaskOutcome:
            entered.set()
            await release.wait()
            return await super().handle(task, lease)

    handler = HealthyBusy(postgres)
    worker = PostgresReviewWorker(
        handler.factory, queue, handler, owner="healthy", task_kind="analyze"
    )
    running = asyncio.create_task(worker.run_once())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        # Another delivery is pending while remaining backlog stays unread.
        assert await queue.receive(task_kind="analyze", consumer_name="crashed")
        assert (await client.xpending(f"{prefix}:analyze", "review-workers"))[
            "pending"
        ] == 2
        assert await client.xlen(f"{prefix}:analyze") == 101
        assert await service.recover_batch() == 1
        for _ in range(3):
            assert await service.recover_batch() == 0
        assert await client.xlen(f"{prefix}:analyze") == 102
        for event_id in ids:
            assert (await state(postgres, event_id))["broker_message_id"] is not None
        release.set()
        assert await running
    finally:
        release.set()
        await asyncio.gather(running, return_exceptions=True)
        async with postgres.engine.begin() as connection:
            await connection.execute(
                delete(TaskLease).where(TaskLease.review_job_id == review_id)
            )


@pytest.mark.parametrize("change", ["deleted", "identity", "quarantine"])
async def test_changed_command_after_probe_is_diagnosed_without_synthesizing(
    postgres: PostgresHarness,
    dispatcher_redis: tuple[Redis, str],
    diagnostics: list[MutableMapping[str, Any]],
    change: str,
) -> None:

    event_id, _ = await _create_outbox_event(postgres)
    client, prefix = dispatcher_redis
    await dispatcher(postgres, client, prefix).dispatch_batch()
    await client.delete(f"{prefix}:analyze")
    await make_due(postgres, event_id)
    entered, release = asyncio.Event(), asyncio.Event()

    class HeldProbe(RedisQueue):
        async def probe(
            self, envelope: QueueTaskEnvelope, message_id: str
        ) -> TransportProbe:
            evidence = await super().probe(envelope, message_id)
            entered.set()
            await release.wait()
            return evidence

    service = PostgresOutboxDispatcher(
        async_sessionmaker(postgres.engine), HeldProbe(client, stream_prefix=prefix)
    )
    running = asyncio.create_task(service.recover_batch())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        async with postgres.engine.begin() as connection:
            if change == "deleted":
                await connection.execute(
                    delete(OutboxEvent).where(OutboxEvent.id == event_id)
                )
            else:
                changes: dict[str, dict[str, Any]] = {
                    "identity": {"trace_id": "private-source"},
                    "quarantine": {
                        "quarantined_at": func.clock_timestamp(),
                        "quarantine_reason_code": "INVALID_ENVELOPE",
                        "broker_published_at": None,
                    },
                }
                values = changes[change]
                await connection.execute(
                    update(OutboxEvent)
                    .where(OutboxEvent.id == event_id)
                    .values(**values)
                )
        expected = None if change == "deleted" else await state(postgres, event_id)
        release.set()
        assert await running == 0
        if expected is not None:
            assert await state(postgres, event_id) == expected
        assert await client.xlen(f"{prefix}:analyze") == 0
        diagnostic = str(diagnostics)
        assert (
            "outbox_reservation_conflict" in diagnostic
            and "private-source" not in diagnostic
        )
    finally:
        release.set()
        await asyncio.gather(running, return_exceptions=True)


async def test_legacy_unknown_receipt_uses_recovery_allowance_and_safe_diagnostic(
    postgres: PostgresHarness,
    dispatcher_redis: tuple[Redis, str],
    diagnostics: list[MutableMapping[str, Any]],
) -> None:
    event_id, _ = await _create_outbox_event(postgres)
    async with postgres.engine.begin() as connection:
        await connection.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == event_id)
            .values(broker_published_at=func.clock_timestamp())
        )
    client, prefix = dispatcher_redis
    service = dispatcher(postgres, client, prefix)
    assert await service.dispatch_batch() == 0
    assert await service.recover_batch() == 1
    assert "outbox_receipt_unconfirmed" in {entry["event"] for entry in diagnostics}


@pytest.mark.parametrize("already_due", [False, True], ids=["null", "due"])
async def test_invalid_published_prefix_defers_across_restart_to_recover_valid_tail(
    postgres: PostgresHarness,
    dispatcher_redis: tuple[Redis, str],
    diagnostics: list[MutableMapping[str, Any]],
    already_due: bool,
) -> None:
    active_id, review_id = await _create_outbox_event(postgres)
    history_ids = [postgres.track("outbox_event", uuid.uuid4()) for _ in range(100)]
    async with postgres.engine.begin() as connection:
        now = (await connection.execute(select(func.clock_timestamp()))).scalar_one()
        await connection.execute(
            insert(OutboxEvent),
            [
                {
                    "id": event_id,
                    "review_job_id": review_id,
                    "schema_version": 2,
                    "task_kind": "analyze",
                    "attempt": 2,
                    "trace_id": "private-invalid-history",
                    "created_at": now - timedelta(days=1),
                    "broker_published_at": now - timedelta(days=1),
                    "broker_message_id": None,
                    "dispatch_not_before": (
                        now - timedelta(days=1) if already_due else None
                    ),
                }
                for event_id in history_ids
            ],
        )
        await connection.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == active_id)
            .values(
                broker_published_at=now,
                dispatch_not_before=now - timedelta(seconds=1),
            )
        )
        before = (
            (
                await connection.execute(
                    select(OutboxEvent.__table__).where(OutboxEvent.id.in_(history_ids))
                )
            )
            .mappings()
            .all()
        )
    client, prefix = dispatcher_redis
    assert await dispatcher(postgres, client, prefix).recover_batch() == 0
    assert await client.xlen(f"{prefix}:analyze") == 0

    assert await dispatcher(postgres, client, prefix).recover_batch() == 1

    entries = await client.xrange(f"{prefix}:analyze")
    assert entries is not None
    assert len(entries) == 1
    fields = entries[0][1]
    assert fields is not None
    assert json.loads(fields["envelope"])["event_id"] == str(active_id)
    async with postgres.engine.connect() as connection:
        after_tick = (
            await connection.execute(select(func.clock_timestamp()))
        ).scalar_one()
        after = {
            row["id"]: dict(row)
            for row in (
                await connection.execute(
                    select(OutboxEvent.__table__).where(OutboxEvent.id.in_(history_ids))
                )
            ).mappings()
        }
    for row in before:
        deferred = after[row["id"]].pop("dispatch_not_before")
        assert (
            now + timedelta(seconds=90)
            <= deferred
            <= after_tick + timedelta(seconds=90)
        )
        assert after[row["id"]] == {
            key: value for key, value in row.items() if key != "dispatch_not_before"
        }
    assert await dispatcher(postgres, client, prefix).recover_batch() == 0
    warnings = [row for row in diagnostics if row["event"] == "outbox_invalid_command"]
    assert len(warnings) == 100
    assert {row["event_id"] for row in warnings} == {
        str(row_id) for row_id in history_ids
    }
    assert all(
        set(row) == {"event", "event_id", "reason_code", "log_level"}
        and row["reason_code"] == "UNSUPPORTED_SCHEMA_VERSION"
        and row["log_level"] == "warning"
        for row in warnings
    )


@pytest.mark.parametrize("days", [-60, 60])
async def test_publication_marker_clock_drift_never_replaces_present_payload(
    postgres: PostgresHarness,
    dispatcher_redis: tuple[Redis, str],
    days: int,
) -> None:
    event_id, _ = await _create_outbox_event(postgres)
    client, prefix = dispatcher_redis
    service = dispatcher(postgres, client, prefix)
    await service.dispatch_batch()
    await make_due(postgres, event_id)
    async with postgres.engine.begin() as connection:
        await connection.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == event_id)
            .values(broker_published_at=func.clock_timestamp() + timedelta(days=days))
        )
    before = await state(postgres, event_id)
    assert await service.recover_batch() == 0
    after = await state(postgres, event_id)
    assert after["broker_message_id"] == before["broker_message_id"]
    assert after["broker_published_at"] == before["broker_published_at"]
    assert after["dispatch_not_before"] > before["dispatch_not_before"]
    assert await client.xlen(f"{prefix}:analyze") == 1


@pytest.mark.parametrize("untrusted", [False, True])
async def test_present_or_untrusted_probe_defers_across_restart_without_send(
    postgres: PostgresHarness,
    dispatcher_redis: tuple[Redis, str],
    transport_commands: list[tuple[object, ...]],
    untrusted: bool,
) -> None:
    event_id, _ = await _create_outbox_event(postgres)
    client, prefix = dispatcher_redis
    await dispatcher(postgres, client, prefix).dispatch_batch()
    await make_due(postgres, event_id)
    if untrusted:
        async with postgres.engine.begin() as connection:
            await connection.execute(
                update(OutboxEvent)
                .where(OutboxEvent.id == event_id)
                .values(trace_id="private-mismatch")
            )
    before = await state(postgres, event_id)
    assert await dispatcher(postgres, client, prefix).recover_batch() == 0
    after = await state(postgres, event_id)
    assert after["dispatch_not_before"] > before["dispatch_not_before"]
    assert {k: v for k, v in after.items() if k != "dispatch_not_before"} == {
        k: v for k, v in before.items() if k != "dispatch_not_before"
    }
    transport_commands.clear()
    assert await dispatcher(postgres, client, prefix).recover_batch() == 0
    assert transport_commands == []
    assert await client.xlen(f"{prefix}:analyze") == 1


@pytest.mark.parametrize("blocked_by", ["retry", "lease"])
async def test_ineligible_prefix_does_not_consume_recovery_candidate_limit(
    postgres: PostgresHarness,
    dispatcher_redis: tuple[Redis, str],
    blocked_by: str,
) -> None:
    first_id, review_id = await _create_outbox_event(postgres)
    async with postgres.engine.begin() as connection:
        await connection.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == first_id)
            .values(broker_published_at=func.clock_timestamp())
        )
        for _ in range(99):
            await connection.execute(
                insert(OutboxEvent).values(
                    id=postgres.track("outbox_event", uuid.uuid4()),
                    review_job_id=review_id,
                    schema_version=1,
                    task_kind="analyze",
                    attempt=2,
                    trace_id="blocked-prefix",
                    broker_published_at=func.clock_timestamp(),
                )
            )
        if blocked_by == "retry":
            await connection.execute(
                update(ReviewJob)
                .where(ReviewJob.id == review_id)
                .values(retry_at=func.clock_timestamp() + timedelta(minutes=2))
            )
    if blocked_by == "lease":
        leases = PostgresTaskLeaseStore(async_sessionmaker(postgres.engine))
        assert await leases.acquire(review_id, owner="live", task_kind="analyze")
    active_id, _ = await _create_outbox_event(postgres)
    async with postgres.engine.begin() as connection:
        await connection.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == active_id)
            .values(broker_published_at=func.clock_timestamp())
        )
    client, prefix = dispatcher_redis
    try:
        assert await dispatcher(postgres, client, prefix).recover_batch() == 1
        assert (await state(postgres, active_id))["broker_message_id"] is not None
        assert await client.xlen(f"{prefix}:analyze") == 1
    finally:
        async with postgres.engine.begin() as connection:
            await connection.execute(
                delete(TaskLease).where(TaskLease.review_job_id == review_id)
            )
