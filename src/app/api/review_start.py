"""Authenticated review start boundary."""

import hashlib
import json
from collections.abc import Callable, Coroutine
from copy import deepcopy
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.routing import APIRoute
from jsonschema import Draft202012Validator
from pydantic import AnyUrl, BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.responses import JSONResponse, Response
from structlog.contextvars import get_contextvars

from app.api.auth import VerifiedMutationIdentity, get_verified_mutation_identity
from app.api.errors import ApiError
from app.api.review_start_dependencies import (
    get_review_configuration_resolver,
    get_review_start_authorizer,
)
from app.application.github_ports import GithubFailure, GithubRequest
from app.application.ports import StartReviewCommand
from app.application.review_start_ports import (
    AdmissionResourceNotFoundError,
    ChangeRequestIdentity,
    FrozenAnalysisConfiguration,
    IdempotencyConflictError,
    RepositoryIdentity,
    ReviewAlreadyActiveError,
    ReviewConfigurationResolver,
    ReviewHeadMismatchError,
    ReviewQuotaExceededError,
    ReviewStartAuthorizer,
    SettingsVersionChangedError,
)
from app.core import github_runtime
from app.core.config import get_settings
from app.db.models import (
    ChangeRequest,
    Repository,
    RepositoryAccess,
    RepositorySettings,
    ReviewJob,
)
from app.db.review_starts import (
    change_identity,
    find_replay,
    persist_start,
    settings_identity,
)
from app.db.session import get_session
from app.domain.settings import compute_rules_digest


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
_SETTINGS_VALIDATOR = Draft202012Validator(
    json.loads(Path("schemas/settings/v1.json").read_text(encoding="utf-8"))
)


_ADMISSION_ERRORS: dict[type[Exception], tuple[int, str, str]] = {
    ReviewQuotaExceededError: (429, "QUOTA_EXCEEDED", "Quota is exceeded."),
    IdempotencyConflictError: (
        409,
        "IDEMPOTENCY_CONFLICT",
        "Idempotency key conflicts with the request.",
    ),
    ReviewAlreadyActiveError: (
        409,
        "REVIEW_ALREADY_ACTIVE",
        "An equivalent review is already active.",
    ),
    SettingsVersionChangedError: (
        409,
        "SETTINGS_VERSION_CONFLICT",
        "Repository settings changed.",
    ),
    AdmissionResourceNotFoundError: (
        404,
        "RESOURCE_NOT_FOUND",
        "Resource was not found.",
    ),
    ReviewHeadMismatchError: (
        409,
        "PR_HEAD_MISMATCH",
        "Pull request head does not match.",
    ),
}


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


async def _check_rerun(
    session: AsyncSession, reference: str | None, change: ChangeRequest
) -> None:
    if reference is None:
        return
    try:
        rerun_id = UUID(reference)
    except ValueError:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "Resource was not found.") from None
    parent = await session.scalar(
        select(ReviewJob.id).where(
            ReviewJob.id == rerun_id,
            ReviewJob.repository_id == change.repository_id,
            ReviewJob.change_request_id == change.id,
        )
    )
    if parent is None:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "Resource was not found.")


def _pull_request_number(value: str) -> int:
    try:
        number = int(value)
        if number < 1:
            raise ValueError("Invalid pull request number")
    except ValueError:
        raise ApiError(422, "VALIDATION_FAILED", "Request is invalid.") from None
    return number


async def _revalidate_github(
    authorizer: ReviewStartAuthorizer,
    user_id: UUID,
    repository_identity: RepositoryIdentity,
    github_request: GithubRequest,
    expected_change: ChangeRequestIdentity,
) -> None:
    try:
        await authorizer.authorize_start(
            user_id=user_id, repository=repository_identity
        )
        async with github_runtime.github_connection(get_settings()) as github:
            snapshot = await github.fetch(github_request)
    except GithubFailure as error:
        if error.reason_code == "VCS_ACCESS_DENIED":
            raise ApiError(403, "ACCESS_DENIED", "Access is denied.") from None
        if error.reason_code == "PR_STALE_OR_CLOSED":
            raise ApiError(
                404, "RESOURCE_NOT_FOUND", "Resource was not found."
            ) from None
        raise ApiError(
            503,
            "SERVICE_UNAVAILABLE",
            "Service is unavailable.",
            retryable=error.retryable,
            retry_after=error.retry_after if error.retryable else None,
        ) from None
    proof = snapshot.metadata.get("snapshot", {})
    if (
        not isinstance(proof, dict)
        or proof.get("provider") != repository_identity.provider
        or any(
            not isinstance(proof.get(field), str) or not proof[field]
            for field in (
                "head_sha",
                "base_sha",
                "base_repository_id",
                "head_repository_id",
            )
        )
    ):
        raise ApiError(
            503, "SERVICE_UNAVAILABLE", "Service is unavailable.", retryable=True
        )
    if proof.get("head_sha") != github_request.head_sha:
        raise ApiError(409, "PR_HEAD_MISMATCH", "Pull request head does not match.")
    if (
        proof.get("base_sha") != github_request.base_sha
        or proof.get("base_repository_id") != repository_identity.external_id
        or (
            expected_change.source_repository_external_id is not None
            and proof.get("head_repository_id")
            != expected_change.source_repository_external_id
        )
    ):
        raise ApiError(404, "RESOURCE_NOT_FOUND", "Resource was not found.")
    metadata = snapshot.metadata.get("metadata")
    if not isinstance(metadata, dict) or any(
        not isinstance(metadata.get(field), str) or not metadata[field]
        for field in ("head_ref", "base_ref")
    ):
        raise ApiError(
            503, "SERVICE_UNAVAILABLE", "Service is unavailable.", retryable=True
        )
    if (
        metadata["head_ref"] != expected_change.source_ref
        or metadata["base_ref"] != expected_change.base_ref
    ):
        raise ApiError(404, "RESOURCE_NOT_FOUND", "Resource was not found.")


@router.post("/{id}/pull-requests/{number}/reviews", operation_id="startReview")
async def start_review_http(  # noqa: PLR0913 -- explicit HTTP and injected boundaries
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
        select(Repository)
        .join(RepositoryAccess)
        .where(
            Repository.id == repository_id,
            RepositoryAccess.user_id == identity.user_id,
        )
    )
    if repository is None:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "Resource was not found.")
    body = await _mutation_body(request)
    request_hash = hashlib.sha256(
        json.dumps(
            {"number": pr_number, **body.model_dump()},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    key = hashlib.sha256(request.headers["Idempotency-Key"].encode()).hexdigest()
    scope = f"startReview:{identity.user_id}:{repository_id}"
    try:
        replay = await find_replay(
            session, scope=scope, key=key, request_hash=request_hash
        )
    except IdempotencyConflictError:
        raise ApiError(
            409, "IDEMPOTENCY_CONFLICT", "Idempotency key conflicts with the request."
        ) from None
    if replay is not None:
        resource_url = f"/api/v1/reviews/{replay[0]}"
        return JSONResponse(
            status_code=200,
            headers={"Location": resource_url},
            content={
                "review_id": str(replay[0]),
                "status": replay[1],
                "resource_url": resource_url,
            },
        )
    change = await session.scalar(
        select(ChangeRequest).where(
            ChangeRequest.repository_id == repository.id,
            ChangeRequest.external_number == str(pr_number),
        )
    )
    if change is None:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "Resource was not found.")
    await _check_rerun(session, body.rerun_of, change)
    if change.current_status != "open":
        raise ApiError(404, "RESOURCE_NOT_FOUND", "Resource was not found.")
    if body.requested_head_sha != change.head_sha:
        raise ApiError(409, "PR_HEAD_MISMATCH", "Pull request head does not match.")
    settings = await session.get(RepositorySettings, repository.current_settings_id)
    if settings is None:
        raise ApiError(503, "SERVICE_UNAVAILABLE", "Service is unavailable.")
    if (
        not settings.output_language
        or not _SETTINGS_VALIDATOR.is_valid(
            {"rules": settings.rules, "ignores": settings.ignores}
        )
        or settings.rules_digest != compute_rules_digest(settings.rules)
    ):
        raise ApiError(
            409,
            "LEGACY_SETTINGS_UNSUPPORTED",
            "Create a new supported settings version before starting a review.",
        )
    repository_identity = RepositoryIdentity(
        repository.id,
        repository.provider_name,
        repository.external_id,
        repository.owner,
        repository.repo_name,
    )
    github_request = GithubRequest(
        repository.owner,
        repository.repo_name,
        repository.external_id,
        pr_number,
        body.requested_head_sha,
        change.base_sha,
    )
    settings_id, rules_digest, change_id = settings.id, settings.rules_digest, change.id
    expected_change = change_identity(change)
    expected_settings = settings_identity(settings)
    await session.rollback()
    await _revalidate_github(
        authorizer,
        identity.user_id,
        repository_identity,
        github_request,
        expected_change,
    )
    config = await resolver.resolve(
        repository=repository_identity,
        settings_id=settings_id,
        rules_digest=rules_digest,
    )
    if not isinstance(config, FrozenAnalysisConfiguration) or any(
        not isinstance(value, str) or not value.strip()
        for value in (config.model_ref, config.prompt_version, config.prompt_digest)
    ):
        raise ApiError(
            503,
            "SERVICE_UNAVAILABLE",
            "Review configuration is unavailable.",
            retryable=True,
        )
    command = StartReviewCommand(
        change_request_id=change_id,
        repository_settings_id=settings_id,
        requested_head_sha=body.requested_head_sha,
        rules_digest=rules_digest,
        trigger_type="manual",
        initiator_user_id=identity.user_id,
        model_ref=config.model_ref,
        model_digest=config.model_digest,
        model_settings=deepcopy(config.model_settings),
        prompt_version=config.prompt_version,
        prompt_digest=config.prompt_digest,
        trace_id=get_contextvars()["request_id"],
        rerun_of=UUID(body.rerun_of) if body.rerun_of is not None else None,
    )
    try:
        review_id, status, created = await persist_start(
            command,
            factory=async_sessionmaker(session.bind, expire_on_commit=False),
            expected_repository=repository_identity,
            scope=scope,
            key=key,
            request_hash=request_hash,
            expected_change=expected_change,
            expected_settings=expected_settings,
        )
    except tuple(_ADMISSION_ERRORS) as error:
        raise ApiError(*_ADMISSION_ERRORS[type(error)]) from None
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
