"""Select review cycles whose GitHub commit status is due and record outcomes."""

from datetime import timedelta
from uuid import UUID

from sqlalchemy import and_, distinct, exists, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from app.application.commit_status_ports import (
    MAX_FAILED_ATTEMPTS,
    CommitStatusTarget,
)
from app.db.commit_status_models import CommitStatus
from app.db.models import ChunkResult, Finding, Repository, ReviewJob

# Statuses are not backfilled for history older than this on first deploy.
_WINDOW = timedelta(hours=24)


class PostgresCommitStatusStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def due(self, *, limit: int) -> list[CommitStatusTarget]:
        newer = aliased(ReviewJob)
        critical = (
            select(func.count(distinct(Finding.fingerprint)))
            .join(ChunkResult, ChunkResult.id == Finding.chunk_result_id)
            .where(
                ChunkResult.review_job_id == ReviewJob.id,
                Finding.severity == "critical",
            )
            .scalar_subquery()
        )
        statement = (
            select(
                ReviewJob.id,
                Repository.owner,
                Repository.repo_name,
                ReviewJob.requested_head_sha,
                ReviewJob.status,
                critical,
                CommitStatus.posted_state,
                func.coalesce(CommitStatus.failed_attempts, 0),
            )
            .join(Repository, Repository.id == ReviewJob.repository_id)
            .outerjoin(CommitStatus, CommitStatus.review_job_id == ReviewJob.id)
            .where(
                Repository.provider_name == "github",
                Repository.integration_status == "healthy",
                ReviewJob.created_at > func.clock_timestamp() - _WINDOW,
                # Only pending can still change; a final status is never revisited.
                or_(
                    CommitStatus.posted_state.is_(None),
                    and_(
                        CommitStatus.posted_state == "pending",
                        ReviewJob.finished_at.is_not(None),
                    ),
                ),
                or_(
                    CommitStatus.retry_at.is_(None),
                    CommitStatus.retry_at <= func.clock_timestamp(),
                ),
                # Parked cycles (permanent rejection or exhausted retries) stay out.
                func.coalesce(CommitStatus.failed_attempts, 0) < MAX_FAILED_ATTEMPTS,
                # A later cycle on the same head owns the status context.
                ~exists().where(
                    newer.change_request_id == ReviewJob.change_request_id,
                    newer.requested_head_sha == ReviewJob.requested_head_sha,
                    newer.created_at > ReviewJob.created_at,
                ),
            )
            .order_by(ReviewJob.created_at, ReviewJob.id)
            .limit(limit)
        )
        async with self._sessions() as session:
            rows = (await session.execute(statement)).all()
        return [CommitStatusTarget(*row) for row in rows]

    async def record_posted(self, review_id: UUID, state: str) -> None:
        values = {"posted_state": state, "failed_attempts": 0, "retry_at": None}
        async with self._sessions.begin() as session:
            await session.execute(
                insert(CommitStatus)
                .values(review_job_id=review_id, **values)
                .on_conflict_do_update(
                    index_elements=[CommitStatus.review_job_id],
                    set_={**values, "updated_at": func.clock_timestamp()},
                )
            )

    async def record_failure(self, review_id: UUID, *, retry_after_s: float) -> None:
        retry_at = func.clock_timestamp() + timedelta(seconds=retry_after_s)
        async with self._sessions.begin() as session:
            await session.execute(
                insert(CommitStatus)
                .values(review_job_id=review_id, failed_attempts=1, retry_at=retry_at)
                .on_conflict_do_update(
                    index_elements=[CommitStatus.review_job_id],
                    set_={
                        "failed_attempts": CommitStatus.failed_attempts + 1,
                        "retry_at": retry_at,
                        "updated_at": func.clock_timestamp(),
                    },
                )
            )

    async def park(self, review_id: UUID) -> None:
        values = {"failed_attempts": MAX_FAILED_ATTEMPTS, "retry_at": None}
        async with self._sessions.begin() as session:
            await session.execute(
                insert(CommitStatus)
                .values(review_job_id=review_id, **values)
                .on_conflict_do_update(
                    index_elements=[CommitStatus.review_job_id],
                    set_={**values, "updated_at": func.clock_timestamp()},
                )
            )
