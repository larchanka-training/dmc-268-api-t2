"""Versioned repository settings HTTP boundary."""

import asyncio
import json
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from jsonschema import Draft202012Validator
from pydantic import ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse

from app.api.auth import (
    AuthenticatedPrincipal,
    VerifiedMutationIdentity,
    get_authenticated_principal,
    get_verified_mutation_identity,
)
from app.api.errors import ApiError
from app.api.repositories import get_repository_reader
from app.application.github_ports import (
    GithubFailure,
    GithubRepositoryReader,
    RepositoryFile,
)
from app.context.loader import RepoConfigLoader
from app.db.models import Repository, RepositoryAccess, RepositorySettings
from app.db.session import get_session
from app.domain.repository_policy import (
    PUBLIC_POLICY_FIELDS,
    ReviewPreferences,
    StoredReviewPolicy,
    public_preferences,
)
from app.domain.settings import compute_rules_digest, project_historical_empty_settings

MAX_SETTINGS_INTEGER = 2_147_483_647
router = APIRouter(prefix="/api/v1/repositories")
_PREVIEW_CACHE: OrderedDict[tuple[str, ...], RepositoryFile] = OrderedDict()
_SETTINGS_VALIDATOR = Draft202012Validator(
    json.loads(Path("schemas/settings/v1.json").read_text(encoding="utf-8"))
)


@asynccontextmanager
async def _database_errors() -> AsyncIterator[None]:
    try:
        yield
    except SQLAlchemyError, OSError:
        raise ApiError(
            503, "SERVICE_UNAVAILABLE", "Service is unavailable.", retryable=True
        ) from None


@router.get("/{id}/settings", operation_id="getRepositorySettings")
async def get_repository_settings(
    id: str,
    principal: Annotated[AuthenticatedPrincipal, Depends(get_authenticated_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    reader: Annotated[GithubRepositoryReader, Depends(get_repository_reader)],
) -> JSONResponse:
    async with _database_errors():
        repository_id = _repository_id(id)
        row = (
            await session.execute(
                select(RepositorySettings, Repository.external_id)
                .join(
                    Repository, Repository.current_settings_id == RepositorySettings.id
                )
                .join(RepositoryAccess)
                .where(
                    Repository.id == repository_id,
                    RepositoryAccess.user_id == principal.user_id,
                )
            )
        ).one_or_none()
        if row is None:
            raise ApiError(404, "RESOURCE_NOT_FOUND", "Resource was not found.")
        settings, external_id = row
        response = JSONResponse(
            content={
                **public_preferences(settings.review_policy),
                "version": str(settings.id),
                "rules": project_historical_empty_settings(
                    settings.rules, "instructions"
                ),
                "ignores": project_historical_empty_settings(settings.ignores, "globs"),
                "max_starts_per_hour": settings.max_starts_per_hour,
                "max_active_jobs": settings.max_active_jobs,
                "output_language": settings.output_language,
                "surrounding_lines": settings.surrounding_lines,
                "created_at": settings.created_at.isoformat(),
            },
            headers={"ETag": f'"{settings.id}"'},
        )
        canonical = (
            bool(settings.output_language)
            and settings.rules_digest == compute_rules_digest(settings.rules)
            and _SETTINGS_VALIDATOR.is_valid(
                {
                    "rules": project_historical_empty_settings(
                        settings.rules, "instructions"
                    ),
                    "ignores": project_historical_empty_settings(
                        settings.ignores, "globs"
                    ),
                }
            )
        )
        await session.rollback()
        await _require_admin(reader, external_id)
        if not canonical:
            raise ApiError(
                409,
                "LEGACY_SETTINGS_UNSUPPORTED",
                "Create a supported settings version before using these settings.",
                headers={"ETag": response.headers["ETag"]},
            )
        return response


async def _require_admin(reader: GithubRepositoryReader, external_id: str) -> None:
    try:
        repository = await reader.get_repository(external_id)
    except GithubFailure as error:
        if error.reason_code == "VCS_ACCESS_DENIED":
            raise ApiError(403, "ACCESS_DENIED", "Access is denied.") from None
        raise ApiError(
            503,
            "SERVICE_UNAVAILABLE",
            "GitHub is temporarily unavailable.",
            retryable=error.retryable,
            retry_after=error.retry_after,
        ) from None
    if repository["role"] != "admin":
        raise ApiError(403, "ACCESS_DENIED", "Access is denied.")


class SettingsReplacement(ReviewPreferences):
    model_config = ConfigDict(strict=True, extra="forbid")
    rules: dict[str, Any]
    ignores: dict[str, Any]
    max_starts_per_hour: int = Field(ge=0, le=MAX_SETTINGS_INTEGER)
    max_active_jobs: int = Field(ge=0, le=MAX_SETTINGS_INTEGER)
    output_language: Literal["ru"]
    surrounding_lines: int = Field(ge=0, le=MAX_SETTINGS_INTEGER)


@router.put("/{id}/settings", operation_id="replaceRepositorySettings")
async def replace_repository_settings(
    id: str,
    request: Request,
    identity: Annotated[
        VerifiedMutationIdentity, Depends(get_verified_mutation_identity)
    ],
    session: Annotated[AsyncSession, Depends(get_session)],
    reader: Annotated[GithubRepositoryReader, Depends(get_repository_reader)],
) -> JSONResponse:
    async with _database_errors():
        repository_id = _repository_id(id)
        if not all(
            (
                identity.session_verified,
                identity.origin_verified,
                identity.csrf_verified,
            )
        ):
            raise ApiError(403, "ACCESS_DENIED", "Access is denied.")
        try:
            if not request.headers.get("If-Match"):
                raise ValueError("Missing If-Match")
            replacement = SettingsReplacement.model_validate(await request.json())
            if not _SETTINGS_VALIDATOR.is_valid(
                {"rules": replacement.rules, "ignores": replacement.ignores}
            ):
                raise ValueError("Noncanonical settings")
        except ValueError, ValidationError:
            raise ApiError(422, "VALIDATION_FAILED", "Request is invalid.") from None
        external_id = await session.scalar(
            select(Repository.external_id)
            .join(RepositoryAccess)
            .where(
                Repository.id == repository_id,
                RepositoryAccess.user_id == identity.user_id,
            )
        )
        if external_id is None:
            raise ApiError(404, "RESOURCE_NOT_FOUND", "Resource was not found.")
        await session.rollback()
        await _require_admin(reader, external_id)
        async with session.begin():
            repository = await session.scalar(
                select(Repository)
                .where(Repository.id == repository_id)
                .with_for_update()
            )
            access = await session.scalar(
                select(RepositoryAccess.id)
                .where(
                    RepositoryAccess.repository_id == repository_id,
                    RepositoryAccess.user_id == identity.user_id,
                )
                .with_for_update(read=True)
            )
            if repository is None or access is None:
                raise ApiError(404, "RESOURCE_NOT_FOUND", "Resource was not found.")
            if request.headers.get("If-Match") != f'"{repository.current_settings_id}"':
                raise ApiError(
                    409, "SETTINGS_VERSION_CONFLICT", "Repository settings changed."
                )
            settings = RepositorySettings(
                repository_id=repository_id,
                **replacement.model_dump(exclude=set(PUBLIC_POLICY_FIELDS)),
                review_policy=StoredReviewPolicy(
                    auto_review_enabled=replacement.auto_review_enabled,
                    base_branches=replacement.base_branches,
                    minimum_severity=replacement.minimum_severity,
                    authorized_by=identity.user_id
                    if replacement.auto_review_enabled
                    else None,
                ).model_dump(mode="json"),
                rules_digest=compute_rules_digest(replacement.rules),
            )
            session.add(settings)
            await session.flush()
            repository.current_settings_id = settings.id
            return JSONResponse(
                content={
                    **public_preferences(settings.review_policy),
                    "version": str(settings.id),
                    **replacement.model_dump(),
                    "created_at": settings.created_at.isoformat(),
                },
                headers={"ETag": f'"{settings.id}"'},
            )


def _repository_id(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "Resource was not found.") from None


@router.get("/{id}/rules-preview", operation_id="getRepositoryRulesPreview")
async def get_repository_rules_preview(
    id: str,
    principal: Annotated[AuthenticatedPrincipal, Depends(get_authenticated_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    reader: Annotated[GithubRepositoryReader, Depends(get_repository_reader)],
) -> dict[str, Any]:
    identifier = _repository_id(id)
    async with _database_errors():
        external_id = await session.scalar(
            select(Repository.external_id)
            .join(RepositoryAccess)
            .where(
                Repository.id == identifier,
                RepositoryAccess.user_id == principal.user_id,
            )
        )
        await session.rollback()
    if external_id is None:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "Resource was not found.")
    try:
        async with asyncio.timeout(15):
            revision = await reader.resolve_default_branch(external_id)
            loaded = await RepoConfigLoader(reader, _PREVIEW_CACHE).load(
                tenant=str(principal.user_id),
                owner=revision.owner,
                name=revision.name,
                external_id=external_id,
                revision=revision.revision,
                instructions=(),
            )
    except GithubFailure as error:
        if error.reason_code == "VCS_ACCESS_DENIED":
            raise ApiError(403, "ACCESS_DENIED", "Access is denied.") from None
        raise ApiError(
            503,
            "SERVICE_UNAVAILABLE",
            "GitHub is temporarily unavailable.",
            retryable=error.retryable,
            retry_after=error.retry_after,
        ) from None
    except TimeoutError:
        raise ApiError(
            503,
            "SERVICE_UNAVAILABLE",
            "GitHub is temporarily unavailable.",
            retryable=True,
        ) from None
    return {
        "default_branch": revision.branch,
        "instructions": list(loaded.rules.instructions),
        "provenance": loaded.provenance,
    }
