"""Runtime wiring for the external GitHub gateway."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

import httpx
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.github_ports import (
    GithubFailure,
    GithubPort,
    GithubPullRequestReader,
)
from app.application.review_start_ports import RepositoryIdentity
from app.core.config import Settings
from app.db.models import GithubIdentity
from app.vcs.github import GithubClient

_DEL = 127
_SPACE = 32


class CredentialUnavailableError(ValueError):
    """The user's stored GitHub credential is missing or unusable."""


@asynccontextmanager
async def github_connection(settings: Settings) -> AsyncIterator[GithubPort]:
    async with _github_client(settings) as client:
        yield client


@asynccontextmanager
async def github_pull_request_connection(
    settings: Settings,
) -> AsyncIterator[GithubPullRequestReader]:
    async with _github_client(settings) as client:
        yield client


@asynccontextmanager
async def _github_client(settings: Settings) -> AsyncIterator[GithubClient]:
    headers = {"X-GitHub-Api-Version": "2022-11-28", "User-Agent": "dmc-268-api-t2"}
    token = settings.github.token.get_secret_value()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(
        headers=headers, timeout=15, follow_redirects=False
    ) as client:
        yield GithubClient(client)


async def stored_github_token(
    settings: Settings, session: AsyncSession, user_id: UUID
) -> str:
    """Decrypt the user's own stored OAuth token; never the bot token."""
    try:
        cipher = Fernet(settings.auth.credential_key.get_secret_value())
        encrypted = await session.scalar(
            select(GithubIdentity.encrypted_token).where(
                GithubIdentity.user_id == user_id
            )
        )
        await session.close()
        if not encrypted:
            raise ValueError("Missing credential")
        token = cipher.decrypt(encrypted.encode()).decode()
        if (
            not token
            or not token.isascii()
            or any(ord(char) <= _SPACE or ord(char) == _DEL for char in token)
        ):
            raise ValueError("Invalid credential")
    except InvalidToken, ValueError, UnicodeError, SQLAlchemyError, OSError:
        raise CredentialUnavailableError from None
    return token


@asynccontextmanager
async def user_github_client(
    token: str, *, transport: httpx.AsyncBaseTransport | None = None
) -> AsyncIterator[GithubClient]:
    async with httpx.AsyncClient(
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "dmc-268-api-t2",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=15,
        follow_redirects=False,
        transport=transport,
    ) as client:
        yield GithubClient(client)


class StoredCredentialAuthorizer:
    """startReview's permission revalidation for worker-side starts (the system
    follow-up): the initiator's own credential, no browser session. A missing or
    unusable credential cannot prove access, so the start is refused."""

    def __init__(
        self,
        settings: Settings,
        factory: async_sessionmaker[AsyncSession],
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._factory = factory
        self._transport = transport

    async def authorize_start(
        self, *, user_id: UUID, repository: RepositoryIdentity
    ) -> None:
        try:
            async with self._factory() as session:
                token = await stored_github_token(self._settings, session, user_id)
        except CredentialUnavailableError:
            raise GithubFailure("VCS_ACCESS_DENIED", retryable=False) from None
        async with user_github_client(token, transport=self._transport) as github:
            allowed = await github.has_read_access(repository.external_id)
        if not allowed:
            raise GithubFailure("VCS_ACCESS_DENIED", retryable=False)
