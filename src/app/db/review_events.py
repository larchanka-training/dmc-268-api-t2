"""Authoritative, short PostgreSQL transactions for review event streams."""

from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.review_events import (
    EventReplayUnavailableError,
    PersistedReviewEvent,
    ReviewAccessDeniedError,
    ReviewEventPage,
    ReviewEventSnapshot,
)
from app.db.models import Publication, RepositoryAccess, ReviewEvent, ReviewJob
from app.domain.enums import ReviewReasonCode


def _project(event: ReviewEvent) -> PersistedReviewEvent:
    return PersistedReviewEvent(
        id=event.id,
        review_id=event.review_job_id,
        sequence=event.sequence,
        occurred_at=event.occurred_at,
        event_type=event.event_type,
        status=event.status,
        publication_status=event.publication_status,
        reason_code=event.reason_code
        if event.reason_code in ReviewReasonCode
        else None,
        retryable=event.retryable,
    )


async def append_review_event(  # noqa: PLR0913 - explicit persisted audit fields
    session: AsyncSession,
    review_id: UUID,
    *,
    event_type: str,
    publication_status: str,
    phase: str | None = None,
    reason_code: str | None = None,
    retryable: bool = False,
    attempt: int = 1,
    safe_details: dict[str, Any] | None = None,
) -> PersistedReviewEvent:
    """Append in the caller's transaction; never commit independently."""
    status = (
        await session.execute(
            select(ReviewJob.status).where(ReviewJob.id == review_id).with_for_update()
        )
    ).scalar_one()
    last = await session.scalar(
        select(func.max(ReviewEvent.sequence)).where(
            ReviewEvent.review_job_id == review_id
        )
    )
    event = ReviewEvent(
        review_job_id=review_id,
        sequence=(last or 0) + 1,
        event_type=event_type,
        status=status,
        publication_status=publication_status,
        phase=phase,
        reason_code=reason_code,
        retryable=retryable,
        attempt=attempt,
        safe_details=safe_details if safe_details is not None else {},
    )
    session.add(event)
    await session.flush()
    return _project(event)


class SqlAlchemyReviewEvents:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def read(
        self, review_id: UUID, user_id: UUID, *, after: int | None
    ) -> ReviewEventPage:
        async with self._sessions() as session:
            permitted = await session.scalar(
                select(RepositoryAccess.id)
                .join(
                    ReviewJob, ReviewJob.repository_id == RepositoryAccess.repository_id
                )
                .where(ReviewJob.id == review_id, RepositoryAccess.user_id == user_id)
            )
            if permitted is None:
                raise ReviewAccessDeniedError("Review is inaccessible")
            row = (
                await session.execute(
                    select(
                        ReviewJob.status,
                        select(Publication.status)
                        .where(Publication.review_job_id == ReviewJob.id)
                        .scalar_subquery()
                        .label("publication_status"),
                        select(func.coalesce(func.max(ReviewEvent.sequence), 0))
                        .where(ReviewEvent.review_job_id == ReviewJob.id)
                        .scalar_subquery()
                        .label("sequence"),
                    ).where(ReviewJob.id == review_id)
                )
            ).one()
            snapshot = ReviewEventSnapshot(
                review_id=review_id,
                sequence=row.sequence,
                status=row.status,
                publication_status=row.publication_status
                or ("SKIPPED" if row.status in {"FAILED", "SKIPPED"} else "NOT_READY"),
            )
            if after is None:
                return ReviewEventPage(snapshot, ())
            if after < 0 or after > snapshot.sequence:
                raise EventReplayUnavailableError("Event replay is unavailable")
            events = await session.scalars(
                select(ReviewEvent)
                .where(
                    ReviewEvent.review_job_id == review_id,
                    ReviewEvent.sequence > after,
                    ReviewEvent.sequence <= snapshot.sequence,
                )
                .order_by(ReviewEvent.sequence)
                .limit(100)
            )
            projected = tuple(_project(event) for event in events)
            if any(
                event.sequence != after + index + 1
                for index, event in enumerate(projected)
            ):
                raise EventReplayUnavailableError("Event replay contains a gap")
            return ReviewEventPage(snapshot, projected)
