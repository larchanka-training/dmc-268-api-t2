"""Authenticated review start boundary."""

from collections.abc import Callable, Coroutine
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.routing import APIRoute
from pydantic import AnyUrl, BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse, Response
from structlog.contextvars import get_contextvars

from app.adapters.review_admission import admit_review
from app.api.auth import VerifiedMutationIdentity, get_verified_mutation_identity
from app.api.errors import ApiError
from app.api.review_start_dependencies import (
    get_one_active_per_pr,
    get_review_configuration_resolver,
    get_review_start_authorizer,
)
from app.application.review_start_ports import (
    ReviewAdmissionError,
    ReviewAdmissionRequest,
    ReviewConfigurationResolver,
    ReviewStartAuthorizer,
)
from app.db.models import (
    Repository,
    RepositoryAccess,
)
from app.db.session import get_session


class ReviewStartRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def protected(request: Request) -> Response:
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


router = APIRouter(prefix="/api/v1/repositories", route_class=ReviewStartRoute)


class StartReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    requested_head_sha: str = Field(min_length=1)
    rerun_of: str | None = None


async def _mutation_body(request: Request) -> StartReviewRequest:
    try:
        body = StartReviewRequest.model_validate(await request.json())
        key = request.headers.get("Idempotency-Key")
        origin = request.headers.get("Origin")
        if not key or not origin or not request.headers.get("X-CSRF-Token"):
            raise ValueError("Missing mutation headers")
        TypeAdapter(AnyUrl).validate_python(origin)
    except ValueError, ValidationError:
        raise ApiError(422, "VALIDATION_FAILED", "Request is invalid.") from None
    return body


def _pull_request_number(value: str) -> int:
    try:
        number = int(value)
        if number < 1:
            raise ValueError("Invalid pull request number")
    except ValueError:
        raise ApiError(422, "VALIDATION_FAILED", "Request is invalid.") from None
    return number


@router.post("/{id}/pull-requests/{number}/reviews", operation_id="startReview")
async def start_review_http(  # noqa: PLR0913 -- explicit admission boundaries and snapshots
    *,
    id: str,
    number: str,
    request: Request,
    identity: Annotated[
        VerifiedMutationIdentity, Depends(get_verified_mutation_identity)
    ],
    session: Annotated[AsyncSession, Depends(get_session)],
    authorizer: Annotated[ReviewStartAuthorizer, Depends(get_review_start_authorizer)],
    resolver: Annotated[
        ReviewConfigurationResolver, Depends(get_review_configuration_resolver)
    ],
    one_active_per_pr: Annotated[bool, Depends(get_one_active_per_pr)],
) -> JSONResponse:
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
    pr_number = _pull_request_number(number)
    try:
        repository_id = UUID(id)
    except ValueError:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "Resource was not found.") from None
    repository = await session.scalar(
        select(Repository.id)
        .join(RepositoryAccess)
        .where(
            Repository.id == repository_id,
            RepositoryAccess.user_id == identity.user_id,
        )
    )
    if repository is None:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "Resource was not found.")
    body = await _mutation_body(request)
    try:
        review_id, status, created = await admit_review(
            ReviewAdmissionRequest(
                actor_id=identity.user_id,
                repository_id=repository_id,
                number=pr_number,
                requested_head_sha=body.requested_head_sha,
                rerun_of=body.rerun_of,
                idempotency_key=request.headers["Idempotency-Key"],
                trace_id=get_contextvars()["request_id"],
            ),
            session=session,
            authorizer=authorizer,
            resolver=resolver,
            one_active_per_pr=one_active_per_pr,
        )
    except ReviewAdmissionError as error:
        raise ApiError(
            error.status,
            error.code,
            error.message,
            retryable=error.retryable,
            retry_after=error.retry_after,
        ) from None
    resource_url = f"/api/v1/reviews/{review_id}"
    return JSONResponse(
        status_code=202 if created else 200,
        headers={"Location": resource_url},
        content={
            "review_id": str(review_id),
            "status": status,
            "resource_url": resource_url,
        },
    )
