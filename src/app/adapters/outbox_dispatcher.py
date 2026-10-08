from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.ports import QueuePort, QueueTaskEnvelope, TaskKind
from app.db.models import OutboxEvent


class PostgresOutboxDispatcher:
    """Send one bounded outbox batch using at-least-once delivery.

    SKIP LOCKED applies only during selection, not across Redis I/O. Concurrent
    runs or a crash after enqueue may send the same event more than once, always
    with its original identity. No PostgreSQL transaction stays open in Redis.
    """

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], queue: QueuePort
    ) -> None:
        self._session_factory = session_factory
        self._queue = queue

    async def dispatch_batch(self, *, batch_size: int = 100) -> int:
        """Return confirmed enqueues; propagate errors and retain unsent work.

        Each publication marker commits separately. An error stops the batch,
        preserving earlier progress; the failed event remains eligible for retry.
        The count includes duplicates sent by concurrent runs, even if another
        run has already set the marker by the time this run updates it.
        """

        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        async with self._session_factory.begin() as session:
            events = (
                await session.scalars(
                    select(OutboxEvent)
                    .where(OutboxEvent.broker_published_at.is_(None))
                    .order_by(OutboxEvent.created_at, OutboxEvent.id)
                    .limit(batch_size)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            tasks = [
                (
                    event.id,
                    QueueTaskEnvelope(
                        schema_version=event.schema_version,
                        event_id=str(event.id),
                        review_id=str(event.review_job_id),
                        task_kind=cast(TaskKind, event.task_kind),
                        attempt=event.attempt,
                        trace_id=event.trace_id,
                    ),
                )
                for event in events
            ]
        for event_id, envelope in tasks:
            await self._queue.enqueue(envelope)
            async with self._session_factory.begin() as session:
                await session.execute(
                    update(OutboxEvent)
                    .where(
                        OutboxEvent.id == event_id,
                        OutboxEvent.broker_published_at.is_(None),
                    )
                    .values(broker_published_at=datetime.now(UTC))
                )
        return len(tasks)
