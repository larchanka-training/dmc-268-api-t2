from __future__ import annotations

import asyncio
import time
import uuid

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.redis_queue import InvalidQueueMessageError, RedisQueue
from app.application.analysis_authority import (
    LiveAnalysisAuthority,
    current_analysis_authority,
)
from app.application.ports import (
    DurableTaskOutcome,
    LeaseLostError,
    QueueDelivery,
    TaskHandlerPort,
    TaskKind,
    TaskLeaseSnapshot,
)
from app.db.task_leases import HEARTBEAT_INTERVAL, PostgresTaskLeaseStore
from app.db.worker_tasks import PostgresWorkerTasks, QueueIdentityError


class PostgresReviewWorker:
    """One bounded execution; deliberately has no production entrypoint."""

    def __init__(  # noqa: PLR0913 — dependencies, claim identity and heartbeat timing
        self,
        session_factory: async_sessionmaker[AsyncSession],
        queue: RedisQueue,
        handler: TaskHandlerPort,
        *,
        owner: str,
        task_kind: TaskKind,
        heartbeat_interval: float = HEARTBEAT_INTERVAL.total_seconds(),
    ) -> None:
        self._slot = asyncio.Lock()
        self._queue = queue
        self._handler = handler
        self._owner = owner
        self._task_kind = task_kind
        self._heartbeat_interval = heartbeat_interval
        self._leases = PostgresTaskLeaseStore(session_factory)
        self._tasks = PostgresWorkerTasks(session_factory)

    async def run_once(self) -> bool:
        """Return True only for an acknowledged delivery; False retains work."""
        async with self._slot:
            try:
                delivery = await self._queue.receive(
                    task_kind=self._task_kind, consumer_name=self._owner
                )
            except InvalidQueueMessageError as rejected:
                await self._queue.quarantine(rejected)
                return True
            if delivery is None:
                return False
            return await self._process_delivery(delivery)

    async def recover_once(
        self, *, cursor: str, min_idle_time: int = 90_000
    ) -> tuple[str, bool]:
        """One explicit analyze recovery attempt; caller owns cursor and scheduling."""
        async with self._slot:
            if self._task_kind != "analyze":
                raise ValueError("Pending recovery supports analyze only")
            try:
                claimed = await self._queue.reclaim(
                    consumer_name=self._owner,
                    cursor=cursor,
                    min_idle_time=min_idle_time,
                )
            except InvalidQueueMessageError as rejected:
                await self._queue.quarantine(rejected)
                assert rejected.next_cursor is not None
                return rejected.next_cursor, True
            try:
                acknowledged = (
                    await self._process_delivery(claimed.delivery)
                    if claimed.delivery is not None
                    else False
                )
            except (QueueIdentityError, LeaseLostError) as error:
                acknowledged = False
                structlog.get_logger().warning(
                    "analysis_worker_recovery_deferred",
                    owner=self._owner,
                    error_type=type(error).__name__,
                )
            return claimed.cursor, acknowledged

    async def _process_delivery(self, delivery: QueueDelivery) -> bool:
        if not await self._tasks.validate_identity(delivery.envelope):
            return False
        if await self._tasks.completed(delivery.envelope):
            return await self._queue.ack(delivery)
        claim_started = time.monotonic()
        lease = await self._leases.acquire(
            uuid.UUID(delivery.envelope.review_id),
            owner=self._owner,
            task_kind=self._task_kind,
        )
        if lease is None:
            return False
        authority = LiveAnalysisAuthority(lease, started=claim_started)
        token = current_analysis_authority.set(authority)
        try:
            return await self._run_claimed(delivery, lease)
        finally:
            authority.cancel()
            current_analysis_authority.reset(token)

    async def _run_claimed(
        self, delivery: QueueDelivery, lease: TaskLeaseSnapshot
    ) -> bool:
        try:
            if not await self._tasks.ready(delivery.envelope, lease):
                return False
            authority = current_analysis_authority.get()
            assert authority is not None
            watchdog = asyncio.create_task(authority.watch())
            heartbeat = asyncio.create_task(self._heartbeat(delivery, lease))
            handling = asyncio.create_task(
                self._handler.handle(delivery.envelope, lease)
            )
            try:
                done, _ = await asyncio.wait(
                    (handling, heartbeat, watchdog), return_when=asyncio.FIRST_COMPLETED
                )
                if heartbeat in done:
                    await heartbeat
                if watchdog in done:
                    await watchdog
                outcome = await handling
            finally:
                authority.cancel()
                watchdog.cancel()
                heartbeat.cancel()
                handling.cancel()
                await asyncio.gather(
                    heartbeat, handling, watchdog, return_exceptions=True
                )
            # A failure racing handler completion must still prevent ACK.
            if not heartbeat.cancelled():
                heartbeat.result()
            completed = (
                outcome is DurableTaskOutcome.COMPLETED
                and await self._tasks.completed(delivery.envelope, lease)
            )
        finally:
            released = await self._leases.release(lease)
        if not completed or not released:
            return False
        return await self._queue.ack(delivery)

    async def _heartbeat(
        self, delivery: QueueDelivery, lease: TaskLeaseSnapshot
    ) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_interval)
            authority = current_analysis_authority.get()
            assert authority is not None
            authority.check()
            started = time.monotonic()
            try:
                renewed = await self._leases.renew(lease)
            except BaseException:
                authority.cancel()
                raise
            if renewed is None:
                # Terminal commit can make renewal ineligible before handle returns.
                # Only a durable decision under this same live claim explains it.
                if await self._tasks.completed(delivery.envelope, lease):
                    return
                authority.cancel()
                raise LeaseLostError("Task lease could not be renewed")
            authority.confirm(renewed, started=started)
