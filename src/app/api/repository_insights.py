"""Repository-scoped projections for review history and operational insights."""

import base64
import binascii
import json
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import and_, func, literal, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import AuthenticatedPrincipal, get_authenticated_principal
from app.api.errors import ApiError
from app.db.models import (
    ChangeRequest,
    ChunkResult,
    Finding,
    GithubIdentity,
    Publication,
    QuotaUsage,
    Repository,
    RepositoryAccess,
    RepositorySettings,
    ReviewJob,
    ReviewLlmAttempt,
)
from app.db.session import get_session
from app.domain.enums import FindingCategory, ReviewStatus

router = APIRouter(prefix="/api/v1/repositories")
Principal = Annotated[AuthenticatedPrincipal, Depends(get_authenticated_principal)]
Session = Annotated[AsyncSession, Depends(get_session)]
_MAX_PAGE = 100
_MAX_PERIOD_DAYS = 366
_MAX_CURSOR_LENGTH = 1024
_CURSOR_FIELDS = 4


def _invalid() -> ApiError:
    return ApiError(422, "VALIDATION_FAILED", "Request is invalid.")


def _period(
    days: str, date_from: str | None, date_to: str | None
) -> tuple[datetime, datetime]:
    try:
        if days not in {"7", "30", "90"}:
            raise ValueError("Invalid days")
        end = date.fromisoformat(date_to) if date_to else datetime.now(UTC).date()
        start = (
            date.fromisoformat(date_from)
            if date_from
            else end - timedelta(days=int(days) - 1)
        )
        if not 0 <= (end - start).days < _MAX_PERIOD_DAYS:
            raise ValueError("Invalid period")
        return datetime.combine(start, time.min, UTC), datetime.combine(
            end + timedelta(days=1), time.min, UTC
        )
    except ValueError, OverflowError:
        raise _invalid() from None


def _cursor(cursor: str, scope: list[Any]) -> tuple[datetime, UUID]:
    try:
        if not cursor or len(cursor) > _MAX_CURSOR_LENGTH:
            raise ValueError("Invalid cursor size")
        parts = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        if (
            not isinstance(parts, list)
            or len(parts) != _CURSOR_FIELDS
            or parts[:2] != [1, scope]
        ):
            raise ValueError("Invalid cursor scope")
        created, review_id = datetime.fromisoformat(parts[2]), UUID(parts[3])
        if created.utcoffset() != timedelta(0):
            raise ValueError("Invalid cursor timezone")
        return created, review_id
    except ValueError, TypeError, AttributeError, binascii.Error:
        raise _invalid() from None


async def _repository(
    id: str, principal: AuthenticatedPrincipal, session: AsyncSession
) -> UUID:
    try:
        repository_id = UUID(id)
    except ValueError:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "Resource was not found.") from None
    access = await session.scalar(
        select(RepositoryAccess.id).where(
            RepositoryAccess.repository_id == repository_id,
            RepositoryAccess.user_id == principal.user_id,
        )
    )
    if access is None:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "Resource was not found.")
    return repository_id


async def _usage(session: AsyncSession, ids: list[UUID]) -> dict[UUID, dict[str, Any]]:
    ledger = ReviewLlmAttempt
    rows = (
        await session.execute(
            select(
                ledger.review_job_id,
                func.count(ledger.id).label("attempts"),
                func.count(ledger.actual_input_tokens).label("known_inputs"),
                func.count(ledger.actual_output_tokens).label("known_outputs"),
                func.sum(ledger.actual_input_tokens).label("inputs"),
                func.sum(ledger.actual_output_tokens).label("outputs"),
                func.sum(ledger.reserved_input_tokens)
                .filter(ledger.actual_input_tokens.is_(None))
                .label("reserved_inputs"),
                func.sum(ledger.reserved_output_tokens)
                .filter(ledger.actual_output_tokens.is_(None))
                .label("reserved_outputs"),
            )
            .where(ledger.review_job_id.in_(ids))
            .group_by(ledger.review_job_id)
        )
    ).all()
    return {
        row.review_job_id: {
            "input_tokens": int(row.inputs) if row.inputs is not None else None,
            "output_tokens": int(row.outputs) if row.outputs is not None else None,
            "reserved_input_tokens": int(row.reserved_inputs or 0),
            "reserved_output_tokens": int(row.reserved_outputs or 0),
            "status": "complete"
            if row.known_inputs == row.known_outputs == row.attempts
            else "incomplete",
        }
        for row in rows
    }


def _limit_reasons(coverage: Any) -> list[str]:
    skipped = coverage.get("skipped", []) if isinstance(coverage, dict) else []
    if not isinstance(skipped, list):
        return []
    return [
        reason
        for reason in ("file_limit", "line_limit", "token_budget")
        if any(
            isinstance(entry, dict) and entry.get("reason") == reason
            for entry in skipped
        )
    ]


@router.get("/{id}/reviews", operation_id="listRepositoryReviews")
async def repository_reviews(  # noqa: PLR0913 -- explicit HTTP query contract
    id: str,
    principal: Principal,
    session: Session,
    *,
    days: str = "30",
    date_from: str | None = None,
    date_to: str | None = None,
    status: str | None = None,
    pull_request_number: str | None = None,
    limit: str = "20",
    cursor: str | None = None,
) -> dict[str, Any]:
    repository_id = await _repository(id, principal, session)
    start, end = _period(days, date_from, date_to)
    try:
        page_limit = int(limit)
        number_filter = (
            int(pull_request_number) if pull_request_number is not None else None
        )
        if not 1 <= page_limit <= _MAX_PAGE or (
            number_filter is not None and number_filter < 1
        ):
            raise ValueError("Invalid pagination")
        if status is not None and status not in set(ReviewStatus):
            raise ValueError("Invalid status")
    except ValueError:
        raise _invalid() from None
    scope = [
        str(repository_id),
        start.isoformat(),
        end.isoformat(),
        status,
        number_filter,
    ]
    query = (
        select(
            ReviewJob,
            ChangeRequest.external_number,
            Publication.status,
            GithubIdentity.login,
        )
        .join(ChangeRequest, ChangeRequest.id == ReviewJob.change_request_id)
        .join(Publication, Publication.review_job_id == ReviewJob.id)
        .outerjoin(
            GithubIdentity, GithubIdentity.user_id == ReviewJob.initiator_user_id
        )
        .where(
            ReviewJob.repository_id == repository_id,
            ReviewJob.created_at >= start,
            ReviewJob.created_at < end,
        )
        .order_by(ReviewJob.created_at.desc(), ReviewJob.id.desc())
    )
    if status is not None:
        query = query.where(ReviewJob.status == status)
    if number_filter is not None:
        query = query.where(ChangeRequest.external_number == str(number_filter))
    if cursor is not None:
        created, review_id = _cursor(cursor, scope)
        anchor = await session.execute(
            select(ReviewJob.id).where(
                ReviewJob.id == review_id,
                ReviewJob.repository_id == repository_id,
                ReviewJob.created_at == created,
            )
        )
        if anchor.first() is None:
            raise _invalid()
        query = query.where(
            tuple_(ReviewJob.created_at, ReviewJob.id)
            < tuple_(literal(created), literal(review_id))
        )
    rows = list((await session.execute(query.limit(page_limit + 1))).all())
    next_cursor = None
    if len(rows) > page_limit:
        rows = rows[:page_limit]
        last = rows[-1][0]
        next_cursor = base64.urlsafe_b64encode(
            json.dumps([1, scope, last.created_at.isoformat(), str(last.id)]).encode()
        ).decode()
    usages = await _usage(session, [row[0].id for row in rows])
    return {
        "items": [
            {
                "id": str(job.id),
                "pull_request_number": int(number),
                "requested_head_sha": job.requested_head_sha,
                "status": job.status,
                "publication_status": publication,
                "created_at": job.created_at,
                "started_at": job.started_at,
                "finished_at": job.finished_at,
                "trigger_type": job.trigger_type,
                "actor": {"id": str(job.initiator_user_id), "login": login}
                if job.initiator_user_id is not None
                else None,
                "usage": usages.get(
                    job.id,
                    {
                        "input_tokens": None,
                        "output_tokens": None,
                        "reserved_input_tokens": 0,
                        "reserved_output_tokens": 0,
                        "status": "unavailable",
                    },
                ),
                "cost": {"amount": None, "currency": None, "status": "unavailable"},
                "limit_reasons": _limit_reasons(job.coverage),
            }
            for job, number, publication, login in rows
        ],
        "next_cursor": next_cursor,
    }


@router.get("/{id}/analytics", operation_id="getRepositoryAnalytics")
async def repository_analytics(  # noqa: PLR0913 -- explicit HTTP query contract
    id: str,
    principal: Principal,
    session: Session,
    *,
    days: str = "30",
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    repository_id = await _repository(id, principal, session)
    start, end = _period(days, date_from, date_to)
    within_period = and_(
        ReviewJob.repository_id == repository_id,
        ReviewJob.created_at >= start,
        ReviewJob.created_at < end,
    )
    duration_eligible = and_(
        ReviewJob.status.in_([ReviewStatus.COMPLETED, ReviewStatus.PARTIAL]),
        ReviewJob.started_at.is_not(None),
        ReviewJob.finished_at >= ReviewJob.started_at,
    )
    stats = (
        await session.execute(
            select(
                func.count(ReviewJob.id).label("reviews"),
                func.count(ReviewJob.id).filter(duration_eligible).label("samples"),
                func.avg(
                    func.extract("epoch", ReviewJob.finished_at - ReviewJob.started_at)
                    * 1000
                )
                .filter(duration_eligible)
                .label("duration"),
            ).where(within_period)
        )
    ).one()
    day = func.date(func.timezone("UTC", ReviewJob.created_at))
    findings = (
        await session.execute(
            select(
                day.label("day"),
                Finding.category,
                func.count(Finding.id).label("count"),
            )
            .select_from(ReviewJob)
            .join(ChunkResult, ChunkResult.review_job_id == ReviewJob.id)
            .join(Finding, Finding.chunk_result_id == ChunkResult.id)
            .where(within_period)
            .group_by(day, Finding.category)
        )
    ).all()
    counts = {(row.day, row.category): row.count for row in findings}
    return {
        "period": {
            "date_from": start.date().isoformat(),
            "date_to": (end - timedelta(days=1)).date().isoformat(),
            "timezone": "UTC",
        },
        "totals": {
            "reviews": stats.reviews,
            "findings": sum(row.count for row in findings),
        },
        "findings_by_day": [
            {
                "date": (start + timedelta(days=offset)).date().isoformat(),
                **{
                    category.value: counts.get(
                        ((start + timedelta(days=offset)).date(), category.value), 0
                    )
                    for category in FindingCategory
                },
            }
            for offset in range((end - start).days)
        ],
        "average_duration_ms": float(stats.duration)
        if stats.duration is not None
        else None,
        "duration_sample_count": stats.samples,
    }


@router.get("/{id}/quotas", operation_id="getRepositoryQuotas")
async def repository_quotas(
    id: str, principal: Principal, session: Session
) -> dict[str, Any]:
    repository_id = await _repository(id, principal, session)
    now = await session.scalar(select(func.clock_timestamp()))
    assert now is not None
    window = now.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    starts = (
        select(QuotaUsage.value)
        .where(
            QuotaUsage.repository_id == repository_id,
            QuotaUsage.user_id == principal.user_id,
            QuotaUsage.type == "starts",
            QuotaUsage.window_start == window,
        )
        .scalar_subquery()
    )
    active = (
        select(func.count(ReviewJob.id))
        .where(
            ReviewJob.repository_id == repository_id,
            ReviewJob.finished_at.is_(None),
        )
        .scalar_subquery()
    )
    row = (
        await session.execute(
            select(
                RepositorySettings.max_starts_per_hour,
                RepositorySettings.max_active_jobs,
                func.coalesce(starts, 0).label("starts"),
                active.label("active"),
            )
            .join(Repository, Repository.current_settings_id == RepositorySettings.id)
            .where(Repository.id == repository_id)
        )
    ).one_or_none()
    if row is None:
        raise ApiError(
            503,
            "SERVICE_UNAVAILABLE",
            "Repository settings are unavailable.",
            retryable=True,
        )
    return {
        "starts_per_hour": {
            "limit": row.max_starts_per_hour,
            "used": row.starts,
            "remaining": max(0, row.max_starts_per_hour - row.starts),
            "resets_at": window + timedelta(hours=1),
            "scope": "repository_user",
        },
        "active_jobs": {
            "limit": row.max_active_jobs,
            "used": row.active,
            "remaining": max(0, row.max_active_jobs - row.active),
            "scope": "repository",
        },
        "daily_tokens": {"status": "unavailable"},
    }
