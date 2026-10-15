from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import cast

import structlog
from sqlalchemy import and_, func, not_, or_, select, text, tuple_, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from app.application.ports import (
    QueueEnvelopeValidationError,
    QueuePort,
    QueueTaskEnvelope,
    TaskKind,
)
from app.db.models import OutboxEvent, Publication, ReviewJob

_MAX_CANDIDATES = 100
_MAX_RECOVERY_SENDS = 10
_RECOVERY_WINDOW = timedelta(seconds=300)
_RECOVERY_LIMIT = 3
_COOLDOWN = timedelta(seconds=60)


@dataclass(frozen=True)
class _Candidate:
    envelope: QueueTaskEnvelope
    generation: int
    message_id: str | None
    created_at: datetime
    published_at: datetime | None


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
    """Reserve in PostgreSQL, send outside locks, finalize the same generation."""

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], queue: QueuePort
    ) -> None:
        self._session_factory = session_factory
        self._queue = queue
        self._recovery_cursor: tuple[datetime, uuid.UUID] | None = None
        self._terminal_cursor: tuple[datetime, uuid.UUID] | None = None

    async def dispatch_batch(self, *, batch_size: int = 100) -> int:
        """Dispatch genuine initial analyze commands; publish remains parked."""
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        async with asyncio.timeout(5), self._session_factory.begin() as session:
            await session.execute(text("SET LOCAL lock_timeout = '500ms'"))
            await session.execute(text("SET LOCAL statement_timeout = '2s'"))
            await session.execute(
                text("SET LOCAL idle_in_transaction_session_timeout = '5s'")
            )
            events = (
                await session.scalars(
                    select(OutboxEvent)
                    .where(
                        OutboxEvent.task_kind == "analyze",
                        OutboxEvent.dispatch_generation == 0,
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
                _Candidate(
                    _envelope(event),
                    event.dispatch_generation,
                    event.broker_message_id,
                    event.created_at,
                    event.broker_published_at,
                )
                for event in events
            ]
        confirmed = 0
        for candidate in candidates:
            generation = await self._reserve(candidate)
            if generation is not None:
                message_id = await self._queue.enqueue(candidate.envelope)
                await self._finalize(candidate, generation, message_id)
                confirmed += 1
        return confirmed

    async def recover_batch(self) -> int:
        """Probe at most 100 active commands; separately bound terminal alerts."""
        async with asyncio.timeout(5), self._session_factory.begin() as session:
            await session.execute(text("SET LOCAL lock_timeout = '500ms'"))
            await session.execute(text("SET LOCAL statement_timeout = '2s'"))
            await session.execute(
                text("SET LOCAL idle_in_transaction_session_timeout = '5s'")
            )
            query = (
                select(OutboxEvent)
                .join(ReviewJob)
                .where(
                    OutboxEvent.task_kind == "analyze",
                    OutboxEvent.quarantined_at.is_(None),
                    not_(
                        and_(
                            OutboxEvent.dispatch_generation == 0,
                            OutboxEvent.broker_published_at.is_(None),
                            OutboxEvent.broker_message_id.is_(None),
                        )
                    ),
                )
            )
            # Structural gaps only; the worker still owns full completion proof.
            prepared_publication = (
                select(Publication.id)
                .where(
                    Publication.review_job_id == ReviewJob.id,
                    Publication.status != "NOT_READY",
                    Publication.started_at.is_not(None),
                    Publication.deadline_at.is_not(None),
                )
                .exists()
            )
            successor = aliased(OutboxEvent)
            publish_work = (
                select(successor.id)
                .where(
                    successor.review_job_id == ReviewJob.id,
                    successor.task_kind == "publish",
                    successor.quarantined_at.is_(None),
                )
                .exists()
            )
            terminal_query = query.with_only_columns(
                ReviewJob.finished_at, OutboxEvent.id
            ).where(
                ReviewJob.finished_at.is_not(None),
                ReviewJob.status.in_(("COMPLETED", "PARTIAL")),
                or_(~prepared_publication, ~publish_work),
            )
            if self._terminal_cursor is not None:
                terminal_query = terminal_query.where(
                    tuple_(ReviewJob.finished_at, OutboxEvent.id)
                    > self._terminal_cursor
                )
            terminal_events = (
                await session.execute(
                    terminal_query.order_by(
                        ReviewJob.finished_at, OutboxEvent.id
                    ).limit(_MAX_CANDIDATES)
                )
            ).all()
            query = query.where(ReviewJob.finished_at.is_(None))
            if self._recovery_cursor is not None:
                query = query.where(
                    tuple_(OutboxEvent.created_at, OutboxEvent.id)
                    > self._recovery_cursor
                )
            events = (
                await session.scalars(
                    query.order_by(OutboxEvent.created_at, OutboxEvent.id).limit(
                        _MAX_CANDIDATES
                    )
                )
            ).all()
            candidates = [
                _Candidate(
                    _envelope(event),
                    event.dispatch_generation,
                    event.broker_message_id,
                    event.created_at,
                    event.broker_published_at,
                )
                for event in events
            ]
        confirmed = 0
        for candidate in candidates:
            if confirmed >= _MAX_RECOVERY_SENDS:
                break
            self._recovery_cursor = (
                candidate.created_at,
                uuid.UUID(candidate.envelope.event_id),
            )
            evidence = None
            if candidate.message_id is not None:
                evidence = await self._queue.probe(
                    candidate.envelope, candidate.message_id
                )
                if evidence in {"UNKNOWN", "UNTRUSTED"}:
                    structlog.get_logger().warning(
                        "outbox_transport_deferred",
                        event_id=candidate.envelope.event_id,
                        state=evidence,
                    )
                if evidence not in {"ABSENT", "EXPIRED"}:
                    continue
            generation = await self._reserve(candidate, expired=evidence == "EXPIRED")
            if generation is not None:
                if candidate.message_id is None:
                    structlog.get_logger().warning(
                        "outbox_receipt_unconfirmed",
                        event_id=candidate.envelope.event_id,
                    )
                message_id = await self._queue.enqueue(candidate.envelope)
                await self._finalize(candidate, generation, message_id)
                confirmed += 1
        if not candidates or (
            len(candidates) < _MAX_CANDIDATES
            and self._recovery_cursor
            == (candidates[-1].created_at, uuid.UUID(candidates[-1].envelope.event_id))
        ):
            self._recovery_cursor = None
        for finished_at, event_id in terminal_events:
            structlog.get_logger().warning(
                "outbox_terminal_deferred", event_id=str(event_id)
            )
            self._terminal_cursor = (finished_at, event_id)
        return confirmed

    async def _reserve(  # noqa: PLR0911, PLR0912 -- explicit authority rejection/rollback gates
        self, candidate: _Candidate, *, expired: bool = False
    ) -> int | None:
        # Keep CLI argument parsing/settings-error sanitization independent of
        # the settings-dependent default engine imported by the lease store.
        from app.db.task_leases import lock_review_job, lock_task_lease  # noqa: PLC0415

        try:
            async with asyncio.timeout(5), self._session_factory.begin() as session:
                await session.execute(text("SET LOCAL lock_timeout = '500ms'"))
                await session.execute(text("SET LOCAL statement_timeout = '2s'"))
                await session.execute(
                    text("SET LOCAL idle_in_transaction_session_timeout = '5s'")
                )
                # Both initial and recovery acquire authority in the worker order.
                review = await lock_review_job(
                    session, uuid.UUID(candidate.envelope.review_id)
                )
                if review is None:
                    return None
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
                    or event.dispatch_generation != candidate.generation
                    or event.broker_message_id != candidate.message_id
                    or event.broker_published_at != candidate.published_at
                ):
                    structlog.get_logger().warning(
                        "outbox_reservation_conflict",
                        event_id=candidate.envelope.event_id,
                    )
                    return None
                now = (
                    await session.execute(select(func.clock_timestamp()))
                ).scalar_one()
                if review.finished_at is not None:
                    structlog.get_logger().warning(
                        "outbox_terminal_deferred", event_id=str(event.id)
                    )
                    return None
                if lease is not None and lease.expires_at > now:
                    return None
                deadline_expired = (
                    review.analysis_deadline_at is not None
                    and review.analysis_deadline_at <= now
                ) or (review.started_at is None and review.queue_deadline_at <= now)
                if expired and deadline_expired:
                    return None
                if (
                    review.retry_at is not None
                    and review.retry_at > now
                    and not deadline_expired
                ):
                    return None
                try:
                    self._queue.validate(candidate.envelope)
                except QueueEnvelopeValidationError as error:
                    if event.broker_published_at is None:
                        event.quarantined_at = now
                        event.quarantine_reason_code = error.reason_code
                    structlog.get_logger().warning(
                        "outbox_invalid_command",
                        event_id=str(event.id),
                        reason_code=error.reason_code,
                    )
                    return None
                if (
                    event.dispatch_not_before is not None
                    and event.dispatch_not_before > now
                ):
                    return None
                initial = (
                    event.dispatch_generation == 0
                    and event.broker_published_at is None
                    and event.broker_message_id is None
                )
                if not initial:
                    if (
                        event.recovery_window_started_at is None
                        or now >= event.recovery_window_started_at + _RECOVERY_WINDOW
                    ):
                        event.recovery_window_started_at = now
                        event.recovery_send_count = 0
                    if event.recovery_send_count >= _RECOVERY_LIMIT:
                        structlog.get_logger().warning(
                            "outbox_recovery_window_exhausted", event_id=str(event.id)
                        )
                        return None
                    event.recovery_send_count += 1
                event.dispatch_generation += 1
                event.dispatch_not_before = now + _COOLDOWN
                event.broker_message_id = None
                return event.dispatch_generation
        except DBAPIError as error:
            # Only lock contention is candidate-local. Other SQL/connection
            # failures retain the existing fail-closed dispatcher behavior.
            if getattr(error.orig, "sqlstate", None) != "55P03":
                raise
            structlog.get_logger().warning(
                "outbox_authority_busy", event_id=candidate.envelope.event_id
            )
            return None

    async def _finalize(
        self, candidate: _Candidate, generation: int, message_id: str
    ) -> None:
        async with asyncio.timeout(5), self._session_factory.begin() as session:
            await session.execute(text("SET LOCAL lock_timeout = '500ms'"))
            await session.execute(text("SET LOCAL statement_timeout = '2s'"))
            await session.execute(
                text("SET LOCAL idle_in_transaction_session_timeout = '5s'")
            )
            await session.execute(
                update(OutboxEvent)
                .where(
                    OutboxEvent.id == uuid.UUID(candidate.envelope.event_id),
                    OutboxEvent.task_kind == "analyze",
                    OutboxEvent.dispatch_generation == generation,
                    OutboxEvent.quarantined_at.is_(None),
                )
                .values(
                    broker_message_id=message_id,
                    broker_published_at=func.clock_timestamp(),
                )
            )
