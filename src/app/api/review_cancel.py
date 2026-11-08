"""Stop a review that has not finished."""

import hashlib
from collections.abc import Callable, Coroutine
from datetime import timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.routing import APIRoute
from pydantic import AnyUrl, TypeAdapter, ValidationError
from sqlalchemy import delete, exists, func, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request as StarletteRequest
from starlette.responses import JSONResponse, Response

from app.api.auth import VerifiedMutationIdentity, get_verified_mutation_identity
from app.api.errors import ApiError
from app.api.review_start_dependencies import get_review_start_authorizer
from app.application.github_ports import GithubFailure
from app.application.review_start_ports import (
    IdempotencyConflictError,
    RepositoryIdentity,
    ReviewStartAuthorizer,
)
from app.db.models import (
    IdempotencyRecord,
    Repository,
    RepositoryAccess,
    ReviewEvent,
    ReviewJob,
)
from app.db.review_events import append_review_event
from app.db.review_starts import find_replay
from app.db.session import get_session
from app.domain.enums import ReviewReasonCode


class CancelReviewRoute(APIRoute):
    def get_route_handler(
        self,
    ) -> Callable[[StarletteRequest], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def protected(request: StarletteRequest) -> Response:
            try:
                return await handler(request)
            except IntegrityError as error:
                cause = getattr(error.orig, "__cause__", None)
                if (
                    getattr(cause, "constraint_name", None)
                    == "uq_review_job_active_equivalent"
                ):
                    raise ApiError(
                        409,
                        "REVIEW_ALREADY_ACTIVE",
                        "An equivalent review is already active.",
                    ) from None
                raise ApiError(
                    503,
                    "SERVICE_UNAVAILABLE",
                    "Service is unavailable.",
                    retryable=True,
                ) from None
            except SQLAlchemyError:
                raise ApiError(
                    503,
                    "SERVICE_UNAVAILABLE",
                    "Service is unavailable.",
                    retryable=True,
                ) from None

        return protected


router = APIRouter(prefix="/api/v1/reviews", route_class=CancelReviewRoute)
_NOT_CANCELLABLE = "The review cannot be cancelled."


def _require_mutation(identity: VerifiedMutationIdentity) -> VerifiedMutationIdentity:
    if (
        not isinstance(identity, VerifiedMutationIdentity)
        or not isinstance(identity.user_id, UUID)
        or not all(
            checked is True
            for checked in (
                identity.session_verified,
                identity.origin_verified,
                identity.csrf_verified,
            )
        )
    ):
        raise ApiError(403, "ACCESS_DENIED", "Access is denied.")
    return identity


async def _require_headers(request: Request) -> str:
    key = request.headers.get("Idempotency-Key")
    origin = request.headers.get("Origin")
    if not key or not origin or not request.headers.get("X-CSRF-Token"):
        raise ApiError(422, "VALIDATION_FAILED", "Request is invalid.")
    try:
        TypeAdapter(AnyUrl).validate_python(origin)
    except ValidationError:
        raise ApiError(422, "VALIDATION_FAILED", "Request is invalid.") from None
    if await request.body():
        raise ApiError(422, "VALIDATION_FAILED", "Request is invalid.")
    return key


def _cancelled_body(review_id: UUID) -> dict[str, str]:
    return {
        "review_id": str(review_id),
        "status": "FAILED",
        "reason_code": ReviewReasonCode.USER_CANCELLED.value,
    }


def _ok(review_id: UUID) -> JSONResponse:
    return JSONResponse(status_code=200, content=_cancelled_body(review_id))


def _not_cancellable() -> ApiError:
    return ApiError(409, "REVIEW_NOT_CANCELLABLE", _NOT_CANCELLABLE)


async def _user_cancel_recorded(session: AsyncSession, review_id: UUID) -> bool:
    """True if this review was stopped successfully (USER_CANCELLED), any sequence."""
    recorded = await session.scalar(
        select(
            exists().where(
                ReviewEvent.review_job_id == review_id,
                ReviewEvent.reason_code == ReviewReasonCode.USER_CANCELLED.value,
            )
        )
    )
    return bool(recorded)


async def _remember(
    session: AsyncSession,
    *,
    review_id: UUID,
    scope: str,
    key: str,
    request_hash: str,
) -> None:
    await session.execute(
        delete(IdempotencyRecord).where(
            IdempotencyRecord.scope == scope,
            IdempotencyRecord.idempotency_key == key,
            IdempotencyRecord.expires_at <= func.clock_timestamp(),
        )
    )
    now = await session.scalar(select(func.clock_timestamp()))
    assert now is not None
    session.add(
        IdempotencyRecord(
            scope=scope,
            idempotency_key=key,
            request_hash=request_hash,
            review_job_id=review_id,
            created_at=now,
            expires_at=now + timedelta(hours=24),
        )
    )


async def _outcome(session: AsyncSession, review_id: UUID) -> JSONResponse:
    if await _user_cancel_recorded(session, review_id):
        return _ok(review_id)
    raise _not_cancellable()


@router.post("/{id}/cancellation", operation_id="cancelReview")
async def cancel_review_http(
    *,
    id: str,
    request: Request,
    identity: Annotated[
        VerifiedMutationIdentity, Depends(get_verified_mutation_identity)
    ],
    session: Annotated[AsyncSession, Depends(get_session)],
    authorizer: Annotated[ReviewStartAuthorizer, Depends(get_review_start_authorizer)],
) -> JSONResponse:
    caller = _require_mutation(identity)
    try:
        review_id = UUID(id)
    except ValueError:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "Resource was not found.") from None
    raw_key = await _require_headers(request)
    found = (
        await session.execute(
            select(ReviewJob, Repository)
            .join(Repository, Repository.id == ReviewJob.repository_id)
            .join(
                RepositoryAccess,
                RepositoryAccess.repository_id == ReviewJob.repository_id,
            )
            .where(
                ReviewJob.id == review_id,
                RepositoryAccess.user_id == caller.user_id,
            )
        )
    ).one_or_none()
    if found is None:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "Resource was not found.")
    _, repository = found
    key = hashlib.sha256(raw_key.encode()).hexdigest()
    request_hash = hashlib.sha256(b"").hexdigest()
    scope = f"cancelReview:{caller.user_id}:{review_id}"
    try:
        replay = await find_replay(
            session, scope=scope, key=key, request_hash=request_hash
        )
    except IdempotencyConflictError:
        raise ApiError(
            409, "IDEMPOTENCY_CONFLICT", "Idempotency key conflicts with the request."
        ) from None
    if replay is not None:
        return await _outcome(session, review_id)
    await session.commit()
    if repository.integration_status != "healthy":
        raise ApiError(
            409, "INTEGRATION_UNHEALTHY", "The repository integration is unhealthy."
        )
    try:
        await authorizer.authorize_start(
            user_id=caller.user_id,
            repository=RepositoryIdentity(
                repository.id,
                repository.provider_name,
                repository.external_id,
                repository.owner,
                repository.repo_name,
            ),
        )
    except GithubFailure as error:
        if error.reason_code == "VCS_ACCESS_DENIED":
            raise ApiError(403, "ACCESS_DENIED", "Access is denied.") from None
        raise ApiError(
            503,
            "SERVICE_UNAVAILABLE",
            "Service is unavailable.",
            retryable=error.retryable,
            retry_after=error.retry_after if error.retryable else None,
        ) from None
    return await _apply_cancel(
        session,
        review_id=review_id,
        user_id=caller.user_id,
        scope=scope,
        key=key,
        request_hash=request_hash,
    )


async def _apply_cancel(  # noqa: PLR0913 -- apply txn needs caller identity for access recheck
    session: AsyncSession,
    *,
    review_id: UUID,
    user_id: UUID,
    scope: str,
    key: str,
    request_hash: str,
) -> JSONResponse:
    found = (
        await session.execute(
            select(ReviewJob, Repository)
            .join(Repository, Repository.id == ReviewJob.repository_id)
            .join(
                RepositoryAccess,
                RepositoryAccess.repository_id == ReviewJob.repository_id,
            )
            .where(
                ReviewJob.id == review_id,
                RepositoryAccess.user_id == user_id,
            )
        )
    ).one_or_none()
    if found is None:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "Resource was not found.")
    _, repository = found
    if repository.integration_status != "healthy":
        raise ApiError(
            409, "INTEGRATION_UNHEALTHY", "The repository integration is unhealthy."
        )
    now = await session.scalar(select(func.clock_timestamp()))
    assert now is not None
    updated = await session.scalar(
        update(ReviewJob)
        .where(ReviewJob.id == review_id, ReviewJob.finished_at.is_(None))
        .values(status="FAILED", finished_at=now)
        .returning(ReviewJob.id)
    )
    if updated is None:
        await session.rollback()
        if await _user_cancel_recorded(session, review_id):
            return _ok(review_id)
        await _remember(
            session,
            review_id=review_id,
            scope=scope,
            key=key,
            request_hash=request_hash,
        )
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
        raise _not_cancellable()
    await append_review_event(
        session,
        review_id,
        event_type="USER_CANCELLED",
        publication_status="NOT_READY",
        reason_code=ReviewReasonCode.USER_CANCELLED.value,
        retryable=False,
    )
    await _remember(
        session,
        review_id=review_id,
        scope=scope,
        key=key,
        request_hash=request_hash,
    )
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return await _outcome(session, review_id)
    return _ok(review_id)
