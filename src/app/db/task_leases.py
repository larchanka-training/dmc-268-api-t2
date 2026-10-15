from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import cast

from sqlalchemy import exists, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from app.application.ports import TaskKind, TaskLeaseSnapshot
from app.db.models import Publication, ReviewJob, TaskLease
from app.db.session import async_session_factory

LEASE_DURATION = timedelta(seconds=90)
HEARTBEAT_INTERVAL = timedelta(seconds=15)


async def lock_review_job(
    session: AsyncSession, review_job_id: uuid.UUID
) -> ReviewJob | None:
    result = await session.scalars(
        select(ReviewJob)
        .where(ReviewJob.id == review_job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.one_or_none()


async def lock_task_lease(
    session: AsyncSession, review_job_id: uuid.UUID
) -> TaskLease | None:
    result = await session.scalars(
        select(TaskLease)
        .where(TaskLease.review_job_id == review_job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.one_or_none()


async def lock_owned_lease(
    session: AsyncSession, claim: TaskLeaseSnapshot
) -> tuple[ReviewJob, TaskLease, datetime] | None:
    review = await lock_review_job(session, claim.review_job_id)
    if review is None:
        return None
    lease = await lock_task_lease(session, claim.review_job_id)
    now: datetime = (await session.execute(select(func.clock_timestamp()))).scalar_one()
    if (
        lease is None
        or lease.task_kind != claim.task_kind
        or lease.owner != claim.owner
        or lease.fencing_token != claim.fencing_token
        or lease.expires_at <= now
    ):
        return None
    return review, lease, now


async def lock_publication(
    session: AsyncSession, review_job_id: uuid.UUID
) -> Publication | None:
    return (
        await session.scalars(
            select(Publication)
            .where(Publication.review_job_id == review_job_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).one_or_none()


def task_is_eligible(
    review: ReviewJob,
    task_kind: TaskKind,
    publication: Publication | None,
    now: datetime,
) -> bool:
    if task_kind == "analyze":
        return review.finished_at is None
    return (
        task_kind == "publish"
        and review.status in {"COMPLETED", "PARTIAL"}
        and review.finished_at is not None
        and publication is not None
        and publication.status == "PENDING"
        and publication.deadline_at is not None
        and publication.deadline_at > now
    )


def publish_eligibility_sql(review_job_id: uuid.UUID) -> ColumnElement[bool]:
    return exists().where(
        ReviewJob.id == review_job_id,
        ReviewJob.status.in_(("COMPLETED", "PARTIAL")),
        ReviewJob.finished_at.is_not(None),
    ).correlate(None) & exists().where(
        Publication.review_job_id == review_job_id,
        Publication.status == "PENDING",
        Publication.deadline_at > func.clock_timestamp(),
    ).correlate(None)


def _snapshot(lease: TaskLease, sampled_at: datetime) -> TaskLeaseSnapshot:
    return TaskLeaseSnapshot(
        review_job_id=lease.review_job_id,
        owner=lease.owner,
        expires_at=lease.expires_at,
        fencing_token=lease.fencing_token,
        task_kind=cast(TaskKind, lease.task_kind),
        sampled_at=sampled_at,
    )


class PostgresTaskLeaseStore:
    """Each lease operation owns a short transaction, without external I/O."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] = async_session_factory,
    ) -> None:
        self._session_factory = session_factory

    async def acquire(
        self, review_job_id: uuid.UUID, *, owner: str, task_kind: TaskKind
    ) -> TaskLeaseSnapshot | None:
        async with self._session_factory.begin() as session:
            review = await lock_review_job(session, review_job_id)
            if review is None:
                return None
            lease = await lock_task_lease(session, review_job_id)
            publication = (
                await lock_publication(session, review_job_id)
                if task_kind == "publish"
                else None
            )
            # A SHARE relation lock permits row locking but delays INSERT/UPDATE.
            # Obtain the mutation lock before sampling eligibility and lease time.
            await session.execute(text("LOCK TABLE task_lease IN ROW EXCLUSIVE MODE"))
            now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
            if not task_is_eligible(review, task_kind, publication, now):
                return None
            if lease is None:
                lease = TaskLease(
                    review_job_id=review_job_id,
                    owner=owner,
                    task_kind=task_kind,
                    expires_at=now + LEASE_DURATION,
                    fencing_token=1,
                )
                session.add(lease)
            else:
                if lease.expires_at > now:
                    return None
                lease.owner = owner
                lease.task_kind = task_kind
                lease.expires_at = now + LEASE_DURATION
                lease.fencing_token += 1
            await session.flush()
            return _snapshot(lease, now)

    async def renew(self, lease: TaskLeaseSnapshot) -> TaskLeaseSnapshot | None:
        async with self._session_factory.begin() as session:
            locked = await lock_owned_lease(session, lease)
            if locked is None:
                return None
            publication = (
                await lock_publication(session, lease.review_job_id)
                if lease.task_kind == "publish"
                else None
            )
            now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
            if not task_is_eligible(locked[0], lease.task_kind, publication, now):
                return None
            current = (
                await session.execute(
                    update(TaskLease)
                    .where(
                        TaskLease.review_job_id == lease.review_job_id,
                        TaskLease.owner == lease.owner,
                        TaskLease.task_kind == lease.task_kind,
                        TaskLease.fencing_token == lease.fencing_token,
                        TaskLease.expires_at > func.clock_timestamp(),
                        publish_eligibility_sql(lease.review_job_id)
                        if lease.task_kind == "publish"
                        else exists().where(
                            ReviewJob.id == lease.review_job_id,
                            ReviewJob.finished_at.is_(None),
                        ),
                    )
                    .values(expires_at=func.clock_timestamp() + LEASE_DURATION)
                    .returning(TaskLease, func.clock_timestamp())
                    .execution_options(populate_existing=True)
                )
            ).one_or_none()
            return None if current is None else _snapshot(current[0], current[1])

    async def release(self, lease: TaskLeaseSnapshot) -> bool:
        async with self._session_factory.begin() as session:
            locked = await lock_owned_lease(session, lease)
            if locked is None:
                return False
            released = await session.scalar(
                update(TaskLease)
                .where(
                    TaskLease.review_job_id == lease.review_job_id,
                    TaskLease.owner == lease.owner,
                    TaskLease.task_kind == lease.task_kind,
                    TaskLease.fencing_token == lease.fencing_token,
                    TaskLease.expires_at > func.clock_timestamp(),
                )
                .values(expires_at=func.clock_timestamp())
                .returning(TaskLease.id)
            )
            return released is not None
