"""Provider-backed open pull requests for a user's connected repository."""

import base64
import binascii
import json
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import AuthenticatedPrincipal, get_authenticated_principal
from app.api.errors import ApiError
from app.api.repositories import get_repository_reader
from app.application.github_ports import GithubFailure, GithubRepositoryReader
from app.db.models import Repository, RepositoryAccess
from app.db.session import get_session

router = APIRouter(prefix="/api/v1/repositories")
_MAX_LIMIT = 100
_MAX_PAGE = 2_147_483_647
_MAX_CURSOR_LENGTH = 1024
_CURSOR_FIELDS = 4


@router.get("/{id}/pull-requests", operation_id="listPullRequests")
async def list_pull_requests(  # noqa: PLR0913 - explicit FastAPI dependencies
    id: str,
    principal: Annotated[AuthenticatedPrincipal, Depends(get_authenticated_principal)],
    reader: Annotated[GithubRepositoryReader, Depends(get_repository_reader)],
    session: Annotated[AsyncSession, Depends(get_session)],
    *,
    limit: str = "20",
    cursor: str | None = None,
) -> dict[str, object]:
    try:
        repository_id = UUID(id)
    except ValueError:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "Resource was not found.") from None
    try:
        if (
            not limit.isascii()
            or not limit.isdecimal()
            or len(limit) > len(str(_MAX_LIMIT))
        ):
            raise ValueError("Invalid limit")
        page_limit = int(limit)
        if not 1 <= page_limit <= _MAX_LIMIT:
            raise ValueError("Invalid limit")
        page = 1
        if cursor is not None:
            if not cursor or len(cursor) > _MAX_CURSOR_LENGTH:
                raise ValueError("Invalid cursor size")
            parts = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
            if (
                not isinstance(parts, list)
                or len(parts) != _CURSOR_FIELDS
                or type(parts[0]) is not int
                or parts[0] != 1
                or parts[1] != str(repository_id)
                or type(parts[2]) is not int
                or parts[2] != page_limit
                or type(parts[3]) is not int
                or not 1 <= parts[3] <= _MAX_PAGE
            ):
                raise ValueError("Invalid cursor")
            page = parts[3]
    except ValueError, TypeError, binascii.Error:
        raise ApiError(422, "VALIDATION_FAILED", "Request is invalid.") from None
    repository = (
        await session.execute(
            select(Repository.external_id, Repository.owner, Repository.repo_name)
            .join(RepositoryAccess)
            .where(
                Repository.id == repository_id,
                Repository.provider_name == "github",
                RepositoryAccess.user_id == principal.user_id,
            )
        )
    ).one_or_none()
    await session.close()
    if repository is None:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "Resource was not found.")
    try:
        items, next_page = await reader.list_pull_requests(
            *repository, limit=page_limit, page=page
        )
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
    next_cursor = (
        base64.urlsafe_b64encode(
            json.dumps([1, str(repository_id), page_limit, next_page]).encode()
        ).decode()
        if next_page is not None
        else None
    )
    return {"items": items, "next_cursor": next_cursor}
