"""Send persisted publication commands without activating analysis transport."""

import uuid
from datetime import timedelta

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.ports import (
    QueueEnvelopeValidationError,
    QueuePort,
    QueueTaskEnvelope,
)
from app.db.models import OutboxEvent, Publication, ReviewJob, TaskLease
from app.db.publication_models import PublicationOperation
from app.db.task_leases import (
    lock_publication,
    lock_review_job,
    lock_task_lease,
    publish_eligibility_sql,
    task_is_eligible,
)


class PostgresPublicationDispatcher:
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], queue: QueuePort
    ) -> None:
        self._sessions = sessions
        self._queue = queue

    async def dispatch_batch(self, *, batch_size: int = 100) -> int:
        async with self._sessions() as session:
            events = (
                await session.scalars(
                    select(OutboxEvent)
                    .join(ReviewJob, ReviewJob.id == OutboxEvent.review_job_id)
                    .join(
                        Publication,
                        Publication.review_job_id == OutboxEvent.review_job_id,
                    )
                    .where(
                        OutboxEvent.task_kind == "publish",
                        OutboxEvent.broker_published_at.is_(None),
                        OutboxEvent.quarantined_at.is_(None),
                        or_(
                            OutboxEvent.dispatch_not_before.is_(None),
                            OutboxEvent.dispatch_not_before <= func.clock_timestamp(),
                        ),
                        Publication.status == "PENDING",
                        Publication.deadline_at > func.clock_timestamp(),
                        ReviewJob.status.in_(("COMPLETED", "PARTIAL")),
                        ReviewJob.finished_at.is_not(None),
                        ~select(TaskLease.id)
                        .where(
                            TaskLease.review_job_id == ReviewJob.id,
                            TaskLease.expires_at > func.clock_timestamp(),
                        )
                        .exists(),
                        ~select(PublicationOperation.id)
                        .where(
                            PublicationOperation.change_request_id
                            == ReviewJob.change_request_id,
                            PublicationOperation.state != "RESOLVED",
                        )
                        .exists(),
                    )
                    .order_by(OutboxEvent.created_at, OutboxEvent.id)
                    .limit(batch_size)
                )
            ).all()
            commands = [
                QueueTaskEnvelope(
                    event.schema_version,
                    str(event.id),
                    str(event.review_job_id),
                    "publish",
                    event.attempt,
                    event.trace_id,
                )
                for event in events
            ]
        confirmed = 0
        for command in commands:
            if not await self._reserve(command):
                continue
            message_id = await self._queue.enqueue(command)
            async with self._sessions.begin() as session:
                await session.execute(
                    update(OutboxEvent)
                    .where(OutboxEvent.id == command.event_id)
                    .values(
                        broker_message_id=message_id,
                        broker_published_at=func.clock_timestamp(),
                    )
                )
            confirmed += 1
        return confirmed

    async def _reserve(self, command: QueueTaskEnvelope) -> bool:
        async with self._sessions.begin() as session:
            review = await lock_review_job(session, uuid.UUID(command.review_id))
            if review is None:
                return False
            lease = await lock_task_lease(session, review.id)
            publication = await lock_publication(session, review.id)
            event = await session.scalar(
                select(OutboxEvent)
                .where(OutboxEvent.id == uuid.UUID(command.event_id))
                .with_for_update()
            )
            now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
            if (
                not task_is_eligible(review, "publish", publication, now)
                or (lease is not None and lease.expires_at > now)
                or event is None
                or event.quarantined_at is not None
                or (
                    event.review_job_id,
                    event.task_kind,
                    event.schema_version,
                    event.attempt,
                    event.trace_id,
                )
                != (
                    review.id,
                    command.task_kind,
                    command.schema_version,
                    command.attempt,
                    command.trace_id,
                )
            ):
                return False
            unresolved = await session.scalar(
                select(PublicationOperation.id).where(
                    PublicationOperation.change_request_id == review.change_request_id,
                    PublicationOperation.state != "RESOLVED",
                )
            )
            if unresolved is not None:
                return False
            try:
                self._queue.validate(command)
            except QueueEnvelopeValidationError as error:
                event.quarantined_at = now
                event.quarantine_reason_code = error.reason_code
                return False
            reserved = await session.scalar(
                update(OutboxEvent)
                .where(
                    OutboxEvent.id == uuid.UUID(command.event_id),
                    OutboxEvent.broker_published_at.is_(None),
                    publish_eligibility_sql(review.id),
                    or_(
                        OutboxEvent.dispatch_not_before.is_(None),
                        OutboxEvent.dispatch_not_before <= func.clock_timestamp(),
                    ),
                )
                .values(
                    dispatch_not_before=func.clock_timestamp() + timedelta(seconds=90)
                )
                .returning(OutboxEvent.id)
            )
            return reserved is not None
