"""Read saved review results."""

import base64
import binascii
import json
from contextlib import suppress
from datetime import datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import ValidationError
from sqlalchemy import func, literal, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import AuthenticatedPrincipal, get_authenticated_principal
from app.api.errors import ApiError
from app.api.review_dto import Coverage
from app.db.models import (
    ChangeRequest,
    ChunkResult,
    ContextPayload,
    Finding,
    Publication,
    RepositoryAccess,
    ReviewEvent,
    ReviewJob,
)
from app.db.session import get_session
from app.domain.enums import ReviewReasonCode

router = APIRouter(prefix="/api/v1/reviews")
_MAX_LIMIT = 100
_MAX_CURSOR_LENGTH = 512
_CURSOR_FIELDS = 4

Principal = Annotated[AuthenticatedPrincipal, Depends(get_authenticated_principal)]


@router.get("/{id}")
async def review_detail(
    id: str,
    principal: Principal,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    # Worker completion and context retention must not split this projection.
    await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
    row = (
        await session.execute(
            select(ReviewJob, ChangeRequest, Publication)
            .join(ChangeRequest, ChangeRequest.id == ReviewJob.change_request_id)
            .join(Publication, Publication.review_job_id == ReviewJob.id)
            .join(
                RepositoryAccess,
                RepositoryAccess.repository_id == ReviewJob.repository_id,
            )
            .where(
                ReviewJob.id == _review_uuid(id),
                RepositoryAccess.user_id == principal.user_id,
            )
        )
    ).one_or_none()
    if row is None:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "Resource was not found.")
    review, change, publication = row
    completed = await session.scalar(
        select(func.count())
        .select_from(ChunkResult)
        .where(ChunkResult.review_job_id == review.id)
    )
    planned = await session.scalar(
        select(func.count())
        .select_from(ContextPayload)
        .where(ContextPayload.review_job_id == review.id)
    )
    retained_results = await session.scalar(
        select(func.count())
        .select_from(ChunkResult)
        .where(
            ChunkResult.review_job_id == review.id,
            ChunkResult.context_payload_id.is_(None),
        )
    )
    planned = (planned or 0) + (retained_results or 0)
    usages = (
        await session.scalars(
            select(ChunkResult.usage).where(ChunkResult.review_job_id == review.id)
        )
    ).all()
    usage = None
    if usages and all(
        isinstance(item, dict)
        and all(
            type(item.get(key)) is int and item[key] >= 0
            for key in ("input_tokens", "output_tokens")
        )
        for item in usages
    ):
        usage = {
            key: sum(item[key] for item in usages if item is not None)
            for key in ("input_tokens", "output_tokens")
        }
    coverage = None
    if review.coverage:
        with suppress(ValidationError):
            coverage = Coverage.model_validate(review.coverage).model_dump()
    error = None
    event = (
        await session.execute(
            select(ReviewEvent.reason_code, ReviewEvent.retryable)
            .where(
                ReviewEvent.review_job_id == review.id,
                ReviewEvent.status == review.status,
                ReviewEvent.event_type != "FORMAT_REPAIR",
            )
            .order_by(ReviewEvent.occurred_at.desc(), ReviewEvent.id.desc())
            .limit(1)
        )
    ).first()
    if event is not None and event.reason_code in set(ReviewReasonCode):
        error = {
            "reason_code": event.reason_code,
            "retryable": event.retryable,
            "retry_at": review.retry_at,
        }
    snapshot = None
    if (
        review.base_sha is not None
        and review.merge_base_sha is not None
        and review.captured_at is not None
    ):
        snapshot = {
            "base_sha": review.base_sha,
            "merge_base_sha": review.merge_base_sha,
            "head_sha": review.requested_head_sha,
            "captured_at": review.captured_at,
        }
    return {
        "id": str(review.id),
        "repository_id": str(review.repository_id),
        "pull_request_number": int(change.external_number),
        "status": review.status,
        "requested_head_sha": review.requested_head_sha,
        "snapshot": snapshot,
        "progress": {"completed_chunks": completed, "planned_chunks": planned},
        "summary": review.final_summary,
        "coverage": coverage,
        "usage": usage,
        "publication_status": publication.status,
        "created_at": review.created_at,
        "started_at": review.started_at,
        "finished_at": review.finished_at,
        "retry_at": review.retry_at,
        "error": error,
    }


@router.get("/{id}/findings")
async def review_findings(
    id: str,
    principal: Principal,
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: str = "20",
    cursor: str | None = None,
) -> dict[str, Any]:
    review_id = await session.scalar(
        select(ReviewJob.id)
        .join(
            RepositoryAccess, RepositoryAccess.repository_id == ReviewJob.repository_id
        )
        .where(
            ReviewJob.id == _review_uuid(id),
            RepositoryAccess.user_id == principal.user_id,
        )
    )
    if review_id is None:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "Resource was not found.")
    try:
        page_limit = int(limit)
        if not 1 <= page_limit <= _MAX_LIMIT:
            raise ValueError("Invalid limit")
    except ValueError:
        raise ApiError(422, "VALIDATION_FAILED", "Request is invalid.") from None
    query = (
        select(Finding)
        .join(ChunkResult, ChunkResult.id == Finding.chunk_result_id)
        .where(ChunkResult.review_job_id == review_id)
        .order_by(Finding.created_at, Finding.id)
    )
    if cursor is not None:
        cursor_review, created_at, finding_id = _decode_cursor(cursor)
        anchor = await session.scalar(
            select(Finding.id)
            .join(ChunkResult, ChunkResult.id == Finding.chunk_result_id)
            .where(
                ChunkResult.review_job_id == review_id,
                Finding.id == finding_id,
                Finding.created_at == created_at,
            )
        )
        if cursor_review != review_id or anchor is None:
            raise ApiError(422, "VALIDATION_FAILED", "Request is invalid.")
        query = query.where(
            tuple_(Finding.created_at, Finding.id)
            > tuple_(literal(created_at), literal(finding_id))
        )
    findings = list((await session.scalars(query.limit(page_limit + 1))).all())
    next_cursor = None
    if len(findings) > page_limit:
        findings = findings[:page_limit]
        last = findings[-1]
        next_cursor = base64.urlsafe_b64encode(
            json.dumps(
                [1, str(review_id), last.created_at.isoformat(), str(last.id)],
                separators=(",", ":"),
            ).encode()
        ).decode()
    fields = (
        "fingerprint",
        "path",
        "side",
        "start_line",
        "end_line",
        "category",
        "severity",
        "title",
        "explanation",
        "evidence",
        "recommendation",
        "proposed_diff_fix",
    )
    return {
        "items": [
            {
                "id": str(finding.id),
                **{key: getattr(finding, key) for key in fields},
                "publication_reference": None,
            }
            for finding in findings
        ],
        "next_cursor": next_cursor,
    }


def _decode_cursor(cursor: str) -> tuple[UUID, datetime, UUID]:
    try:
        if not cursor or len(cursor) > _MAX_CURSOR_LENGTH:
            raise ValueError("Invalid cursor size")
        parts = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        if (
            not isinstance(parts, list)
            or len(parts) != _CURSOR_FIELDS
            or type(parts[0]) is not int
            or parts[0] != 1
            or not all(isinstance(value, str) for value in parts[1:])
        ):
            raise ValueError("Invalid cursor shape")
        review_id = UUID(parts[1])
        created_at = datetime.fromisoformat(parts[2])
        finding_id = UUID(parts[3])
        if created_at.utcoffset() != timedelta(0):
            raise ValueError("Invalid cursor timezone")
        return review_id, created_at, finding_id
    except ValueError, TypeError, binascii.Error:
        raise ApiError(422, "VALIDATION_FAILED", "Request is invalid.") from None


def _review_uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "Resource was not found.") from None
