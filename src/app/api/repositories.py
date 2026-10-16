"""Connected repositories visible to the authenticated user."""

import asyncio
import base64
import binascii
import json
from collections.abc import AsyncIterator
from typing import Annotated, Any
from uuid import UUID

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends
from sqlalchemy import literal, select, tuple_
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import AuthenticatedPrincipal, get_authenticated_principal
from app.api.errors import ApiError
from app.application.github_ports import GithubFailure, GithubRepositoryReader
from app.core.config import Settings, get_settings
from app.db.models import GithubIdentity, Repository, RepositoryAccess
from app.db.session import get_session
from app.vcs.github import GithubClient

router = APIRouter(prefix="/api/v1/repositories")
_MAX_LIMIT = 100
_MAX_CURSOR_LENGTH = 1024
_CURSOR_FIELDS = 4
_READ_TIMEOUT_SECONDS = 15


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
