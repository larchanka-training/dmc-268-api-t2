from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.ports import TaskLeaseSnapshot
from app.db.models import ReviewJob, TaskLease
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
        or lease.owner != claim.owner
        or lease.fencing_token != claim.fencing_token
        or lease.expires_at <= now
    ):
        return None
    return review, lease, now


def _snapshot(lease: TaskLease) -> TaskLeaseSnapshot:
    return TaskLeaseSnapshot(
        review_job_id=lease.review_job_id,
        owner=lease.owner,
        expires_at=lease.expires_at,
        fencing_token=lease.fencing_token,
    )


class PostgresTaskLeaseStore:
    """Each lease operation owns a short transaction, without external I/O."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] = async_session_factory,
    ) -> None:
        self._session_factory = session_factory

    async def acquire(
        self, review_job_id: uuid.UUID, *, owner: str
    ) -> TaskLeaseSnapshot | None:
        async with self._session_factory.begin() as session:
            review = await lock_review_job(session, review_job_id)
            if review is None or review.finished_at is not None:
                return None
            lease = await lock_task_lease(session, review_job_id)
            now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
            if lease is None:
                lease = TaskLease(
                    review_job_id=review_job_id,
                    owner=owner,
                    expires_at=now + LEASE_DURATION,
                    fencing_token=1,
                )
                session.add(lease)
            else:
                if lease.expires_at > now:
                    return None
                lease.owner = owner
                lease.expires_at = now + LEASE_DURATION
                lease.fencing_token += 1
            await session.flush()
            return _snapshot(lease)

    async def renew(self, lease: TaskLeaseSnapshot) -> TaskLeaseSnapshot | None:
        async with self._session_factory.begin() as session:
            locked = await lock_owned_lease(session, lease)
            if locked is None:
                return None
            current = await session.scalar(
                update(TaskLease)
                .where(
                    TaskLease.review_job_id == lease.review_job_id,
                    TaskLease.owner == lease.owner,
                    TaskLease.fencing_token == lease.fencing_token,
                    TaskLease.expires_at > func.clock_timestamp(),
                )
                .values(expires_at=func.clock_timestamp() + LEASE_DURATION)
                .returning(TaskLease)
                .execution_options(populate_existing=True)
            )
            return None if current is None else _snapshot(current)

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
                    TaskLease.fencing_token == lease.fencing_token,
                    TaskLease.expires_at > func.clock_timestamp(),
                )
                .values(expires_at=func.clock_timestamp())
                .returning(TaskLease.id)
            )
            return released is not None
