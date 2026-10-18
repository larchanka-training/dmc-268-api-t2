"""Connected repositories visible to the authenticated user."""

import asyncio
import base64
import binascii
import json
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import delete, insert, literal, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import (
    AuthenticatedPrincipal,
    VerifiedSession,
    get_authenticated_principal,
    get_verified_mutation_session,
)
from app.api.errors import ApiError
from app.application.github_ports import GithubFailure, GithubRepositoryReader
from app.core.config import Settings, get_settings
from app.db.models import (
    GithubIdentity,
    Repository,
    RepositoryAccess,
    RepositorySettings,
)
from app.db.session import get_session
from app.domain.settings import compute_rules_digest
from app.vcs.github import GithubClient

router = APIRouter(prefix="/api/v1/repositories")
_MAX_LIMIT = 100
_MAX_CURSOR_LENGTH = 1024
_CURSOR_FIELDS = 4
_READ_TIMEOUT_SECONDS = 15
_MAX_PROVIDER_PAGE = 2_147_483_647


async def get_repository_reader(
    principal: Annotated[AuthenticatedPrincipal, Depends(get_authenticated_principal)],
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AsyncIterator[GithubRepositoryReader]:
    """Use only the verified browser user's stored OAuth credential."""
    try:
        cipher = Fernet(settings.auth.credential_key.get_secret_value())
        encrypted = await session.scalar(
            select(GithubIdentity.encrypted_token).where(
                GithubIdentity.user_id == principal.user_id
            )
        )
        await session.close()
        if not encrypted:
            raise ValueError("Missing credential")
        token = cipher.decrypt(encrypted.encode()).decode()
        if (
            not token
            or not token.isascii()
            or any(ord(char) <= 32 or ord(char) == 127 for char in token)  # noqa: PLR2004
        ):
            raise ValueError("Invalid credential")
    except InvalidToken, ValueError, UnicodeError, SQLAlchemyError, OSError:
        raise ApiError(
            503, "SERVICE_UNAVAILABLE", "Service is unavailable.", retryable=True
        ) from None
    async with httpx.AsyncClient(
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "dmc-268-api-t2",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=15,
        follow_redirects=False,
    ) as client:
        yield GithubClient(client)


@router.get("", operation_id="listRepositories")
async def list_repositories(
    principal: Annotated[AuthenticatedPrincipal, Depends(get_authenticated_principal)],
    reader: Annotated[GithubRepositoryReader, Depends(get_repository_reader)],
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: str = "20",
    cursor: str | None = None,
) -> dict[str, object]:
    try:
        page_limit = int(limit)
        if not 1 <= page_limit <= _MAX_LIMIT:
            raise ValueError("Invalid limit")
    except ValueError:
        raise ApiError(422, "VALIDATION_FAILED", "Request is invalid.") from None
    statement = (
        select(
            Repository.id,
            Repository.provider_name,
            Repository.external_id,
            Repository.owner,
            Repository.repo_name,
            RepositoryAccess.role,
        )
        .join(RepositoryAccess)
        .where(
            RepositoryAccess.user_id == principal.user_id,
            Repository.provider_name == "github",
        )
        .order_by(Repository.owner, Repository.repo_name, Repository.id)
    )
    after = _decode_cursor(cursor) if cursor is not None else None
    items: list[dict[str, Any]] = []
    try:
        async with asyncio.timeout(_READ_TIMEOUT_SECONDS):
            while len(items) <= page_limit:
                batch = statement
                if after is not None:
                    owner, name, repository_id = after
                    batch = batch.where(
                        tuple_(Repository.owner, Repository.repo_name, Repository.id)
                        > tuple_(literal(owner), literal(name), literal(repository_id))
                    )
                rows = (await session.execute(batch.limit(_MAX_LIMIT))).all()
                # Materialize only scalar columns; no stream/transaction spans HTTP.
                await session.close()
                if not rows:
                    break
                for repository in rows:
                    if await reader.has_read_access(repository.external_id):
                        items.append(
                            {
                                "id": str(repository.id),
                                "provider": repository.provider_name,
                                "external_id": repository.external_id,
                                "owner": repository.owner,
                                "name": repository.repo_name,
                                "role": repository.role,
                                "capabilities": {
                                    "can_start_review": True,
                                    "can_manage_settings": repository.role == "admin",
                                },
                            }
                        )
                        if len(items) > page_limit:
                            break
                after = rows[-1].owner, rows[-1].repo_name, rows[-1].id
                if len(rows) < _MAX_LIMIT:
                    break
    except TimeoutError:
        raise ApiError(
            503,
            "UPSTREAM_UNAVAILABLE",
            "GitHub is temporarily unavailable.",
            retryable=True,
        ) from None
    except GithubFailure as error:
        raise ApiError(
            503,
            "UPSTREAM_RATE_LIMITED"
            if error.reason_code == "VCS_RATE_LIMITED"
            else "UPSTREAM_UNAVAILABLE",
            "GitHub is temporarily unavailable.",
            retryable=True,
            retry_after=error.retry_after if error.retryable else None,
        ) from None
    next_cursor = None
    if len(items) > page_limit:
        items.pop()
        last = items[-1]
        next_cursor = base64.urlsafe_b64encode(
            json.dumps([1, last["owner"], last["name"], last["id"]]).encode()
        ).decode()
    return {"items": items, "next_cursor": next_cursor}


@router.get("/available", operation_id="listAvailableRepositories")
async def list_available_repositories(
    principal: Annotated[AuthenticatedPrincipal, Depends(get_authenticated_principal)],
    reader: Annotated[GithubRepositoryReader, Depends(get_repository_reader)],
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: str = "20",
    cursor: str | None = None,
) -> dict[str, object]:
    try:
        if not limit.isascii() or not limit.isdecimal() or len(limit) > 3:  # noqa: PLR2004
            raise ValueError("Invalid limit")
        page_limit = int(limit)
        if not 1 <= page_limit <= _MAX_LIMIT:
            raise ValueError("Invalid limit")
    except ValueError:
        raise ApiError(422, "VALIDATION_FAILED", "Request is invalid.") from None
    page = (
        _decode_available_cursor(cursor, principal.user_id, page_limit)
        if cursor is not None
        else 1
    )
    await session.close()
    try:
        async with asyncio.timeout(_READ_TIMEOUT_SECONDS):
            items, next_page = await reader.list_available_repositories(
                limit=page_limit, page=page
            )
    except TimeoutError:
        raise ApiError(
            503,
            "UPSTREAM_UNAVAILABLE",
            "GitHub is temporarily unavailable.",
            retryable=True,
        ) from None
    except GithubFailure as error:
        raise ApiError(
            503,
            "UPSTREAM_RATE_LIMITED"
            if error.reason_code == "VCS_RATE_LIMITED"
            else "UPSTREAM_UNAVAILABLE",
            "GitHub is temporarily unavailable.",
            retryable=True,
            retry_after=error.retry_after if error.retryable else None,
        ) from None
    try:
        connected = set(
            await session.scalars(
                select(Repository.external_id)
                .join(RepositoryAccess)
                .where(
                    Repository.provider_name == "github",
                    RepositoryAccess.user_id == principal.user_id,
                    Repository.external_id.in_([item["external_id"] for item in items]),
                )
            )
        )
        await session.close()
    except SQLAlchemyError, OSError:
        raise ApiError(
            503, "SERVICE_UNAVAILABLE", "Service is unavailable.", retryable=True
        ) from None
    return {
        "items": [item for item in items if item["external_id"] not in connected],
        "next_cursor": (
            base64.urlsafe_b64encode(
                json.dumps(
                    [1, "github", str(principal.user_id), page_limit, next_page]
                ).encode()
            ).decode()
            if next_page is not None
            else None
        ),
    }


class ConnectRepositoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    provider: Literal["github"]
    external_id: str = Field(pattern=r"^[1-9][0-9]*$")


@router.post("/connect", status_code=201, operation_id="connectRepository")
async def connect_repository(
    request: Request,
    response: Response,
    verified: Annotated[VerifiedSession, Depends(get_verified_mutation_session)],
    reader: Annotated[GithubRepositoryReader, Depends(get_repository_reader)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    try:
        body = ConnectRepositoryRequest.model_validate(await request.json())
    except ValueError, ValidationError:
        raise ApiError(422, "VALIDATION_FAILED", "Request is invalid.") from None
    await session.close()
    try:
        async with asyncio.timeout(_READ_TIMEOUT_SECONDS):
            repository = await reader.get_repository(body.external_id)
    except TimeoutError:
        raise ApiError(
            503,
            "UPSTREAM_UNAVAILABLE",
            "GitHub is temporarily unavailable.",
            retryable=True,
        ) from None
    except GithubFailure as error:
        if error.reason_code == "VCS_ACCESS_DENIED":
            raise ApiError(403, "ACCESS_DENIED", "Access is denied.") from None
        raise ApiError(
            503,
            "UPSTREAM_RATE_LIMITED"
            if error.reason_code == "VCS_RATE_LIMITED"
            else "UPSTREAM_UNAVAILABLE",
            "GitHub is temporarily unavailable.",
            retryable=True,
            retry_after=error.retry_after if error.retryable else None,
        ) from None
    role = repository.pop("role")
    try:
        async with session.begin():
            repository_id = await session.scalar(
                pg_insert(Repository)
                .values(
                    provider_name="github",
                    external_id=repository["external_id"],
                    owner=repository["owner"],
                    repo_name=repository["name"],
                )
                .on_conflict_do_nothing(constraint="uq_repository_provider_external_id")
                .returning(Repository.id)
            )
            if repository_id is None:
                existing = (
                    await session.execute(
                        select(
                            Repository.id, Repository.owner, Repository.repo_name
                        ).where(
                            Repository.provider_name == "github",
                            Repository.external_id == repository["external_id"],
                        )
                    )
                ).one()
                repository_id = existing.id
                repository["owner"], repository["name"] = (
                    existing.owner,
                    existing.repo_name,
                )
            else:
                settings_id = uuid4()
                rules: dict[str, Any] = {"instructions": []}
                await session.execute(
                    insert(RepositorySettings).values(
                        id=settings_id,
                        repository_id=repository_id,
                        rules=rules,
                        ignores={"globs": []},
                        max_starts_per_hour=10,
                        max_active_jobs=4,
                        output_language="ru",
                        surrounding_lines=5,
                        rules_digest=compute_rules_digest(rules),
                    )
                )
                await session.execute(
                    update(Repository)
                    .where(Repository.id == repository_id)
                    .values(current_settings_id=settings_id)
                )
            access_id = await session.scalar(
                pg_insert(RepositoryAccess)
                .values(
                    repository_id=repository_id, user_id=verified.user_id, role=role
                )
                .on_conflict_do_nothing(
                    constraint="uq_repository_access_repository_user"
                )
                .returning(RepositoryAccess.id)
            )
            if access_id is None:
                await session.execute(
                    update(RepositoryAccess)
                    .where(
                        RepositoryAccess.repository_id == repository_id,
                        RepositoryAccess.user_id == verified.user_id,
                    )
                    .values(role=role)
                )
    except SQLAlchemyError, OSError:
        raise ApiError(
            503, "SERVICE_UNAVAILABLE", "Service is unavailable.", retryable=True
        ) from None
    response.status_code = 201 if access_id is not None else 200
    return {
        **repository,
        "id": str(repository_id),
        "role": role,
        "capabilities": {
            "can_start_review": True,
            "can_manage_settings": role == "admin",
        },
    }


@router.delete(
    "/{id}/connection",
    status_code=204,
    operation_id="disconnectRepository",
    responses={
        401: {"description": "AUTHENTICATION_REQUIRED"},
        403: {"description": "ACCESS_DENIED"},
        404: {"description": "RESOURCE_NOT_FOUND"},
        503: {"description": "SERVICE_UNAVAILABLE"},
    },
)
async def disconnect_repository(
    id: str,
    verified: Annotated[VerifiedSession, Depends(get_verified_mutation_session)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    try:
        repository_id = UUID(id)
    except ValueError:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "Resource was not found.") from None
    try:
        await session.execute(
            delete(RepositoryAccess).where(
                RepositoryAccess.repository_id == repository_id,
                RepositoryAccess.user_id == verified.user_id,
            )
        )
        await session.commit()
    except SQLAlchemyError, OSError:
        with suppress(SQLAlchemyError, OSError):
            await session.rollback()
        raise ApiError(
            503, "SERVICE_UNAVAILABLE", "Service is unavailable.", retryable=True
        ) from None
    return Response(status_code=204)


def _decode_available_cursor(cursor: str, user_id: UUID, limit: int) -> int:
    try:
        if not cursor or len(cursor) > _MAX_CURSOR_LENGTH:
            raise ValueError("Invalid cursor size")
        parts = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        if (
            not isinstance(parts, list)
            or len(parts) != 5  # noqa: PLR2004
            or type(parts[0]) is not int
            or parts[:3] != [1, "github", str(user_id)]
            or type(parts[3]) is not int
            or parts[3] != limit
            or type(parts[4]) is not int
            or not 1 <= parts[4] <= _MAX_PROVIDER_PAGE
        ):
            raise ValueError("Invalid cursor")
        return int(parts[4])
    except ValueError, TypeError, binascii.Error, RecursionError:
        raise ApiError(422, "VALIDATION_FAILED", "Request is invalid.") from None


def _decode_cursor(cursor: str) -> tuple[str, str, UUID]:
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
        for value in parts[1:3]:
            if "\x00" in value:
                raise ValueError("Invalid cursor text")
            value.encode("utf-8")
        return parts[1], parts[2], UUID(parts[3])
    except ValueError, TypeError, binascii.Error:
        raise ApiError(422, "VALIDATION_FAILED", "Request is invalid.") from None
