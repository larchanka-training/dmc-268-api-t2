from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.redis_queue import InvalidQueueMessageError, RedisQueue
from app.application.ports import (
    DurableTaskOutcome,
    QueueDelivery,
    TaskHandlerPort,
    TaskKind,
    TaskLeaseSnapshot,
)
from app.db.task_leases import PostgresTaskLeaseStore
from app.db.worker_tasks import PostgresWorkerTasks


class PostgresReviewWorker:
    """One bounded execution; deliberately has no production entrypoint."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        queue: RedisQueue,
        handler: TaskHandlerPort,
        *,
        owner: str,
        task_kind: TaskKind,
    ) -> None:
        self._queue = queue
        self._handler = handler
        self._owner = owner
        self._task_kind = task_kind
        self._leases = PostgresTaskLeaseStore(session_factory)
        self._tasks = PostgresWorkerTasks(session_factory)

    async def run_once(self) -> bool:
        """Return True only for an acknowledged delivery; False retains work."""
        try:
            delivery = await self._queue.receive(
                task_kind=self._task_kind, consumer_name=self._owner
            )
        except InvalidQueueMessageError as rejected:
            await self._queue.quarantine(rejected)
            return True
        if delivery is None:
            return False
        if not await self._tasks.validate_identity(delivery.envelope):
            return False
        if await self._tasks.completed(delivery.envelope):
            return await self._queue.ack(delivery)
        lease = await self._leases.acquire(
            uuid.UUID(delivery.envelope.review_id),
            owner=self._owner,
            task_kind=self._task_kind,
        )
        if lease is None:
            return False
        return await self._run_claimed(delivery, lease)

    async def _run_claimed(
        self, delivery: QueueDelivery, lease: TaskLeaseSnapshot
    ) -> bool:
        try:
            if not await self._tasks.ready(delivery.envelope, lease):
                return False
            outcome = await self._handler.handle(delivery.envelope, lease)
            completed = (
                outcome is DurableTaskOutcome.COMPLETED
                and await self._tasks.completed(delivery.envelope, lease)
            )
        finally:
            released = await self._leases.release(lease)
        if not completed or not released:
            return False
        return await self._queue.ack(delivery)
