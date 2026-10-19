"""Publish-command transport through real PostgreSQL and Redis boundaries."""

import asyncio
import uuid
from datetime import timedelta
from typing import Any

import pytest
from redis.asyncio import Redis
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from test_publications_postgres import seed_review

from app.adapters.publication_dispatcher import PostgresPublicationDispatcher
from app.adapters.redis_queue import RedisQueue
from app.application.ports import QueueTaskEnvelope
from app.application.publication_ports import PublicationAuthority
from app.db.models import OutboxEvent, Publication, ReviewJob
from app.db.publications import PostgresPublicationStore
from app.db.task_leases import PostgresTaskLeaseStore

pytest_plugins = ("test_publications_postgres", "test_redis_queue")


async def publication_command(
    engine: AsyncEngine, payload: dict[str, Any], *, change_id: uuid.UUID | None = None
) -> QueueTaskEnvelope:
    review_id, _ = await seed_review(engine, payload, change_id=change_id)
    event_id = uuid.uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            insert(OutboxEvent).values(
                id=event_id,
                review_job_id=review_id,
                task_kind="publish",
                schema_version=1,
                attempt=1,
                trace_id="publication-runtime",
            )
        )
    return QueueTaskEnvelope(
        1, str(event_id), str(review_id), "publish", 1, "publication-runtime"
    )


async def test_dispatcher_delivers_canonical_publish_command_once(
    publication_engine: AsyncEngine,
    payload_data: dict[str, Any],
    redis_client: Redis,
    stream_prefix: str,
) -> None:
    command = await publication_command(publication_engine, payload_data)
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    dispatcher = PostgresPublicationDispatcher(
        async_sessionmaker(publication_engine), queue
    )

    assert await dispatcher.dispatch_batch(batch_size=1) == 1
    received = await queue.receive(task_kind="publish", consumer_name="publication")
    assert received is not None and received.envelope == command
    assert await dispatcher.dispatch_batch(batch_size=1) == 0
    assert await queue.receive(task_kind="publish", consumer_name="publication") is None
    assert await redis_client.xlen(f"{stream_prefix}:analyze") == 0


async def test_expired_cycle_never_creates_a_publish_delivery(
    publication_engine: AsyncEngine,
    payload_data: dict[str, Any],
    redis_client: Redis,
    stream_prefix: str,
) -> None:
    command = await publication_command(publication_engine, payload_data)
    async with publication_engine.begin() as connection:
        await connection.execute(
            update(Publication)
            .where(Publication.review_job_id == uuid.UUID(command.review_id))
            .values(deadline_at=func.clock_timestamp())
        )
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    dispatcher = PostgresPublicationDispatcher(
        async_sessionmaker(publication_engine), queue
    )

    assert await dispatcher.dispatch_batch() == 0
    assert await redis_client.xlen(f"{stream_prefix}:publish") == 0


async def test_unknown_publication_remains_parked_without_transport_retry(
    publication_engine: AsyncEngine,
    payload_data: dict[str, Any],
    redis_client: Redis,
    stream_prefix: str,
) -> None:
    command = await publication_command(publication_engine, payload_data)
    async with publication_engine.begin() as connection:
        await connection.execute(
            update(Publication)
            .where(Publication.review_job_id == uuid.UUID(command.review_id))
            .values(status="UNKNOWN")
        )
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    dispatcher = PostgresPublicationDispatcher(
        async_sessionmaker(publication_engine), queue
    )

    assert await dispatcher.dispatch_batch() == 0
    assert await redis_client.xlen(f"{stream_prefix}:publish") == 0


async def test_quarantined_publish_command_never_leaves_postgres(
    publication_engine: AsyncEngine,
    payload_data: dict[str, Any],
    redis_client: Redis,
    stream_prefix: str,
) -> None:
    command = await publication_command(publication_engine, payload_data)
    async with publication_engine.begin() as connection:
        await connection.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == uuid.UUID(command.event_id))
            .values(
                quarantined_at=func.clock_timestamp(),
                quarantine_reason_code="INVALID_ENVELOPE",
            )
        )
    dispatcher = PostgresPublicationDispatcher(
        async_sessionmaker(publication_engine),
        RedisQueue(redis_client, stream_prefix=stream_prefix),
    )

    assert await dispatcher.dispatch_batch() == 0
    assert await redis_client.xlen(f"{stream_prefix}:publish") == 0


async def test_active_analysis_cannot_dispatch_a_publication_command(
    publication_engine: AsyncEngine,
    payload_data: dict[str, Any],
    redis_client: Redis,
    stream_prefix: str,
) -> None:
    command = await publication_command(publication_engine, payload_data)
    async with publication_engine.begin() as connection:
        await connection.execute(
            update(ReviewJob)
            .where(ReviewJob.id == uuid.UUID(command.review_id))
            .values(status="LLM_PROCESSING", finished_at=None)
        )
    dispatcher = PostgresPublicationDispatcher(
        async_sessionmaker(publication_engine),
        RedisQueue(redis_client, stream_prefix=stream_prefix),
    )

    assert await dispatcher.dispatch_batch() == 0
    assert await redis_client.xlen(f"{stream_prefix}:publish") == 0


async def test_live_publication_lease_blocks_dispatch_before_batch_limit(
    publication_engine: AsyncEngine,
    payload_data: dict[str, Any],
    redis_client: Redis,
    stream_prefix: str,
) -> None:
    command = await publication_command(publication_engine, payload_data)
    sessions = async_sessionmaker(publication_engine)
    lease = await PostgresTaskLeaseStore(sessions).acquire(
        uuid.UUID(command.review_id), owner="active-publisher", task_kind="publish"
    )
    assert lease is not None
    async with sessions() as session:
        review = await session.get(ReviewJob, uuid.UUID(command.review_id))
        assert review is not None
        change_id = review.change_request_id
    due = await publication_command(
        publication_engine, payload_data, change_id=change_id
    )
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    dispatcher = PostgresPublicationDispatcher(sessions, queue)

    assert await dispatcher.dispatch_batch(batch_size=1) == 1
    received = await queue.receive(task_kind="publish", consumer_name="publisher")
    assert received is not None and received.envelope == due
    assert await queue.receive(task_kind="publish", consumer_name="publisher") is None


async def test_lost_redis_receipt_retains_durable_cooldown_across_restart(
    publication_engine: AsyncEngine,
    payload_data: dict[str, Any],
    redis_client: Redis,
    stream_prefix: str,
) -> None:
    await publication_command(publication_engine, payload_data)
    sessions = async_sessionmaker(publication_engine)

    class LostReceiptQueue(RedisQueue):
        async def enqueue(self, envelope: QueueTaskEnvelope) -> str:
            await super().enqueue(envelope)
            raise ConnectionError("private Redis response was lost")

    dispatcher = PostgresPublicationDispatcher(
        sessions, LostReceiptQueue(redis_client, stream_prefix=stream_prefix)
    )
    with pytest.raises(ConnectionError):
        await dispatcher.dispatch_batch()
    restarted = PostgresPublicationDispatcher(
        sessions, RedisQueue(redis_client, stream_prefix=stream_prefix)
    )

    assert await restarted.dispatch_batch() == 0
    assert await redis_client.xlen(f"{stream_prefix}:publish") == 1


async def test_publication_change_during_lock_wait_prevents_external_send(
    publication_engine: AsyncEngine,
    payload_data: dict[str, Any],
    redis_client: Redis,
    stream_prefix: str,
) -> None:
    command = await publication_command(publication_engine, payload_data)
    sessions = async_sessionmaker(publication_engine)
    dispatcher = PostgresPublicationDispatcher(
        sessions, RedisQueue(redis_client, stream_prefix=stream_prefix)
    )
    async with sessions.begin() as competing:
        await competing.scalar(
            select(ReviewJob.id)
            .where(ReviewJob.id == uuid.UUID(command.review_id))
            .with_for_update()
        )
        sending = asyncio.create_task(dispatcher.dispatch_batch())
        await asyncio.sleep(0.1)
        await competing.execute(
            update(Publication)
            .where(Publication.review_job_id == uuid.UUID(command.review_id))
            .values(status="UNKNOWN")
        )

    assert await sending == 0
    assert await redis_client.xlen(f"{stream_prefix}:publish") == 0


async def test_unresolved_operation_parks_every_review_of_the_same_pull_request(
    publication_engine: AsyncEngine,
    payload_data: dict[str, Any],
    redis_client: Redis,
    stream_prefix: str,
) -> None:
    command = await publication_command(publication_engine, payload_data)
    sessions = async_sessionmaker(publication_engine)
    leases = PostgresTaskLeaseStore(sessions)
    lease = await leases.acquire(
        uuid.UUID(command.review_id), owner="old-publisher", task_kind="publish"
    )
    assert lease is not None
    store = PostgresPublicationStore(sessions)
    claim = await store.begin(
        lease.review_job_id,
        authority=PublicationAuthority(
            lease.review_job_id, lease.owner, lease.fencing_token
        ),
        marker="existing-operation",
        payload_hash="payload-hash",
    )
    assert claim is not None
    assert await leases.release(lease)
    await publication_command(
        publication_engine, payload_data, change_id=claim.change_request_id
    )
    dispatcher = PostgresPublicationDispatcher(
        sessions, RedisQueue(redis_client, stream_prefix=stream_prefix)
    )

    assert await dispatcher.dispatch_batch() == 0
    assert await redis_client.xlen(f"{stream_prefix}:publish") == 0


async def test_invalid_canonical_envelope_is_quarantined_without_redis_write(
    publication_engine: AsyncEngine,
    payload_data: dict[str, Any],
    redis_client: Redis,
    stream_prefix: str,
) -> None:
    command = await publication_command(publication_engine, payload_data)
    async with publication_engine.begin() as connection:
        await connection.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == uuid.UUID(command.event_id))
            .values(schema_version=2)
        )
    dispatcher = PostgresPublicationDispatcher(
        async_sessionmaker(publication_engine),
        RedisQueue(redis_client, stream_prefix=stream_prefix),
    )

    assert await dispatcher.dispatch_batch() == 0
    assert await dispatcher.dispatch_batch() == 0
    assert await redis_client.xlen(f"{stream_prefix}:publish") == 0


async def test_deadline_expiry_during_relation_lock_wait_prevents_xadd(
    publication_engine: AsyncEngine,
    payload_data: dict[str, Any],
    redis_client: Redis,
    stream_prefix: str,
) -> None:
    command = await publication_command(publication_engine, payload_data)
    async with publication_engine.begin() as connection:
        await connection.execute(
            update(Publication)
            .where(Publication.review_job_id == uuid.UUID(command.review_id))
            .values(deadline_at=func.clock_timestamp() + timedelta(milliseconds=300))
        )
    dispatcher = PostgresPublicationDispatcher(
        async_sessionmaker(publication_engine),
        RedisQueue(redis_client, stream_prefix=stream_prefix),
    )
    async with publication_engine.begin() as blocker:
        await blocker.execute(text("LOCK TABLE outbox_event IN SHARE MODE"))
        sending = asyncio.create_task(dispatcher.dispatch_batch())
        await asyncio.sleep(0.4)
        assert not sending.done()

    assert await sending == 0
    assert await redis_client.xlen(f"{stream_prefix}:publish") == 0
