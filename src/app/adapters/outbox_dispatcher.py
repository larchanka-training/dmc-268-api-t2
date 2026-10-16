from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import cast

import structlog
from sqlalchemy import and_, func, not_, or_, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.ports import (
    QueueEnvelopeValidationError,
    QueuePort,
    QueueTaskEnvelope,
    TaskKind,
)
from app.db.models import OutboxEvent, ReviewJob, TaskLease

_MAX_CANDIDATES = 100
_MAX_RECOVERY_SENDS = 10
_COOLDOWN = timedelta(seconds=90)


@dataclass(frozen=True)
class _Candidate:
    envelope: QueueTaskEnvelope
    message_id: str | None


def _envelope(event: OutboxEvent) -> QueueTaskEnvelope:
    return QueueTaskEnvelope(
        schema_version=event.schema_version,
        event_id=str(event.id),
        review_id=str(event.review_job_id),
        task_kind=cast(TaskKind, event.task_kind),
        attempt=event.attempt,
        trace_id=event.trace_id,
    )


class PostgresOutboxDispatcher:
    """Singleton sender with committed throttle and exact confirmed-ID probes."""

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], queue: QueuePort
    ) -> None:
        self._session_factory = session_factory
        self._queue = queue

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[AsyncSession]:
        async with asyncio.timeout(5), self._session_factory.begin() as session:
            await session.execute(text("SET LOCAL lock_timeout = '500ms'"))
            await session.execute(text("SET LOCAL statement_timeout = '2s'"))
            await session.execute(
                text("SET LOCAL idle_in_transaction_session_timeout = '5s'")
            )
            yield session

    async def dispatch_batch(self, *, batch_size: int = 100) -> int:
        """Dispatch genuine initial analyze commands; publish remains parked."""
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        async with self._transaction() as session:
            events = (
                await session.scalars(
                    select(OutboxEvent)
                    .where(
                        OutboxEvent.task_kind == "analyze",
                        OutboxEvent.dispatch_not_before.is_(None),
                        OutboxEvent.broker_published_at.is_(None),
                        OutboxEvent.broker_message_id.is_(None),
                        OutboxEvent.quarantined_at.is_(None),
                    )
                    .order_by(OutboxEvent.created_at, OutboxEvent.id)
                    .limit(min(batch_size, _MAX_CANDIDATES))
                    .with_for_update(skip_locked=True)
                )
            ).all()
            candidates = [
                _Candidate(_envelope(event), event.broker_message_id)
                for event in events
            ]
        confirmed = 0
        for candidate in candidates:
            if await self._reserve(candidate):
                message_id = await self._queue.enqueue(candidate.envelope)
                await self._finalize(candidate, message_id)
                confirmed += 1
        return confirmed

    async def recover_batch(self) -> int:
        """Probe at most 100 due active commands; reserve at most 10 resends."""
        async with self._transaction() as session:
            query = (
                select(OutboxEvent)
                .join(ReviewJob)
                .where(
                    OutboxEvent.task_kind == "analyze",
                    OutboxEvent.quarantined_at.is_(None),
                    or_(
                        OutboxEvent.dispatch_not_before.is_(None),
                        OutboxEvent.dispatch_not_before <= func.clock_timestamp(),
                    ),
                    not_(
                        and_(
                            OutboxEvent.dispatch_not_before.is_(None),
                            OutboxEvent.broker_published_at.is_(None),
                            OutboxEvent.broker_message_id.is_(None),
                        )
                    ),
                )
            )
            now = func.clock_timestamp()
            deadline_expired = or_(
                ReviewJob.analysis_deadline_at <= now,
                and_(
                    ReviewJob.started_at.is_(None), ReviewJob.queue_deadline_at <= now
                ),
            )
            live_lease = (
                select(TaskLease.id)
                .where(
                    TaskLease.review_job_id == ReviewJob.id, TaskLease.expires_at > now
                )
                .exists()
            )
            query = query.where(
                ReviewJob.finished_at.is_(None),
                or_(
                    ReviewJob.retry_at.is_(None),
                    ReviewJob.retry_at <= now,
                    deadline_expired,
                ),
                ~live_lease,
            )
            events = (
                await session.scalars(
                    query.order_by(
                        OutboxEvent.dispatch_not_before.nullsfirst(),
                        OutboxEvent.created_at,
                        OutboxEvent.id,
                    ).limit(_MAX_CANDIDATES)
                )
            ).all()
            candidates = [
                _Candidate(_envelope(event), event.broker_message_id)
                for event in events
            ]
        confirmed = 0
        for candidate in candidates:
            if confirmed >= _MAX_RECOVERY_SENDS:
                break
            evidence = None
            if candidate.message_id is not None:
                evidence = await self._queue.probe(
                    candidate.envelope, candidate.message_id
                )
                if evidence == "UNKNOWN":
                    raise RuntimeError("Transport probe unavailable")
                if evidence == "UNTRUSTED":
                    structlog.get_logger().warning(
                        "outbox_transport_deferred",
                        event_id=candidate.envelope.event_id,
                        state=evidence,
                    )
                if evidence not in {"ABSENT", "EXPIRED"}:
                    async with self._transaction() as session:
                        await session.execute(
                            update(OutboxEvent)
                            .where(
                                OutboxEvent.id == uuid.UUID(candidate.envelope.event_id)
                            )
                            .values(
                                dispatch_not_before=func.clock_timestamp() + _COOLDOWN
                            )
                        )
                    continue
            if await self._reserve(candidate):
                if candidate.message_id is None:
                    structlog.get_logger().warning(
                        "outbox_receipt_unconfirmed",
                        event_id=candidate.envelope.event_id,
                    )
                message_id = await self._queue.enqueue(candidate.envelope)
                await self._finalize(candidate, message_id)
                confirmed += 1
        return confirmed

    async def _reserve(  # noqa: PLR0911 -- explicit authority rejection/rollback gates
        self, candidate: _Candidate
    ) -> bool:
        # Keep CLI argument parsing/settings-error sanitization independent of
        # the settings-dependent default engine imported by the lease store.
        from app.db.task_leases import lock_review_job, lock_task_lease  # noqa: PLC0415

        try:
            async with self._transaction() as session:
                # Both initial and recovery acquire authority in the worker order.
                review = await lock_review_job(
                    session, uuid.UUID(candidate.envelope.review_id)
                )
                if review is None:
                    return False
                lease = await lock_task_lease(session, review.id)
                event = await session.scalar(
                    select(OutboxEvent)
                    .where(
                        OutboxEvent.id == uuid.UUID(candidate.envelope.event_id),
                        OutboxEvent.task_kind == "analyze",
                    )
                    .with_for_update()
                )
                if (
                    event is None
                    or event.quarantined_at is not None
                    or _envelope(event) != candidate.envelope
                ):
                    structlog.get_logger().warning(
                        "outbox_reservation_conflict",
                        event_id=candidate.envelope.event_id,
                    )
                    return False
                now = (
                    await session.execute(select(func.clock_timestamp()))
                ).scalar_one()
                if review.finished_at is not None:
                    structlog.get_logger().warning(
                        "outbox_terminal_deferred", event_id=str(event.id)
                    )
                    return False
                if lease is not None and lease.expires_at > now:
                    return False
                deadline_expired = (
                    review.analysis_deadline_at is not None
                    and review.analysis_deadline_at <= now
                ) or (review.started_at is None and review.queue_deadline_at <= now)
                if (
                    review.retry_at is not None
                    and review.retry_at > now
                    and not deadline_expired
                ):
                    return False
                try:
                    self._queue.validate(candidate.envelope)
                except QueueEnvelopeValidationError as error:
                    if event.broker_published_at is None:
                        event.quarantined_at = now
                        event.quarantine_reason_code = error.reason_code
                    else:
                        event.dispatch_not_before = now + _COOLDOWN
                    structlog.get_logger().warning(
                        "outbox_invalid_command",
                        event_id=str(event.id),
                        reason_code=error.reason_code,
                    )
                    return False
                if (
                    event.dispatch_not_before is not None
                    and event.dispatch_not_before > now
                ):
                    return False
                event.dispatch_not_before = now + _COOLDOWN
                return True
        except DBAPIError as error:
            # Only lock contention is candidate-local. Other SQL/connection
            # failures retain the existing fail-closed dispatcher behavior.
            if getattr(error.orig, "sqlstate", None) != "55P03":
                raise
            structlog.get_logger().warning(
                "outbox_authority_busy", event_id=candidate.envelope.event_id
            )
            return False

    async def _finalize(self, candidate: _Candidate, message_id: str) -> None:
        async with self._transaction() as session:
            await session.execute(
                update(OutboxEvent)
                .where(
                    OutboxEvent.id == uuid.UUID(candidate.envelope.event_id),
                    OutboxEvent.task_kind == "analyze",
                    OutboxEvent.quarantined_at.is_(None),
                )
                .values(
                    broker_message_id=message_id,
                    broker_published_at=func.clock_timestamp(),
                )
            )
