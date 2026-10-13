from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.ports import (
    QueueEnvelopeValidationError,
    QueueTaskEnvelope,
    TaskLeaseSnapshot,
)
from app.db.models import OutboxEvent, ReviewJob
from app.db.task_leases import (
    lock_owned_lease,
    lock_publication,
    lock_review_job,
    task_is_eligible,
)


class QueueIdentityError(LookupError):
    """Canonical wire message does not identify an authoritative command."""


class PostgresWorkerTasks:
    """Read authoritative commands and decisions in short locked transactions."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def validate_identity(self, task: QueueTaskEnvelope) -> bool:
        async with self._session_factory.begin() as session:
            return await validate_task_identity(session, task)

    async def ready(self, task: QueueTaskEnvelope, lease: TaskLeaseSnapshot) -> bool:
        async with self._session_factory.begin() as session:
            locked = await lock_owned_lease(session, lease)
            if locked is None or not await validate_task_identity(session, task):
                return False
            publication = (
                await lock_publication(session, lease.review_job_id)
                if lease.task_kind == "publish"
                else None
            )
            now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
            return locked[1].expires_at > now and task_is_eligible(
                locked[0], lease.task_kind, publication, now
            )

    async def completed(
        self, task: QueueTaskEnvelope, lease: TaskLeaseSnapshot | None = None
    ) -> bool:
        if lease is not None and (
            lease.review_job_id != uuid.UUID(task.review_id)
            or lease.task_kind != task.task_kind
        ):
            return False
        async with self._session_factory.begin() as session:
            review: ReviewJob | None
            if lease is not None:
                locked = await lock_owned_lease(session, lease)
                review = None if locked is None else locked[0]
            else:
                review = await lock_review_job(session, uuid.UUID(task.review_id))
            if (
                review is None
                or not await validate_task_identity(session, task)
                or review.finished_at is None
            ):
                return False
            if task.task_kind == "analyze" and review.status in {"FAILED", "SKIPPED"}:
                return True
            publication = await lock_publication(session, review.id)
            if (
                publication is None
                or publication.status == "NOT_READY"
                or publication.started_at is None
                or publication.deadline_at is None
            ):
                return False
            if task.task_kind == "publish":
                return (
                    publication.status in {"PUBLISHED", "PARTIAL", "SKIPPED"}
                    and publication.finished_at is not None
                )
            return await self._has_publish_work(session, review.id)

    async def _has_publish_work(
        self, session: AsyncSession, review_id: uuid.UUID
    ) -> bool:
        candidates = await session.scalars(
            select(OutboxEvent)
            .where(
                OutboxEvent.review_job_id == review_id,
                OutboxEvent.task_kind == "publish",
                OutboxEvent.schema_version == 1,
                OutboxEvent.attempt > 0,
                OutboxEvent.trace_id != "",
                OutboxEvent.quarantined_at.is_(None),
            )
            .with_for_update(read=True)
        )
        for event in candidates:
            successor = QueueTaskEnvelope(
                schema_version=event.schema_version,
                event_id=str(event.id),
                review_id=str(event.review_job_id),
                task_kind="publish",
                attempt=event.attempt,
                trace_id=event.trace_id,
            )
            try:
                successor.to_json()
            except QueueEnvelopeValidationError:
                continue
            return True
        return False


async def validate_task_identity(
    session: AsyncSession, task: QueueTaskEnvelope
) -> bool:
    try:
        event_id, review_id = uuid.UUID(task.event_id), uuid.UUID(task.review_id)
    except ValueError:
        raise QueueIdentityError("Queue identity is not authoritative") from None
    event = await session.scalar(
        select(OutboxEvent).where(OutboxEvent.id == event_id).with_for_update(read=True)
    )
    review = await session.get(ReviewJob, review_id)
    if (
        event is None
        or review is None
        or event.review_job_id != review_id
        or event.schema_version != task.schema_version
        or event.task_kind != task.task_kind
        or event.attempt != task.attempt
        or event.trace_id != task.trace_id
    ):
        raise QueueIdentityError("Queue identity is not authoritative")
    return event.quarantined_at is None
