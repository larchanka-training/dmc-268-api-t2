"""Explicit runtime connections for GitHub publication credentials."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.publication_handler import PublicationTaskHandler
from app.core.config import Settings
from app.db.publications import PostgresPublicationStore
from app.vcs.github import GithubClient


class PublicationConfigError(ValueError):
    """Publication is explicitly disabled until its own credentials are present."""


@asynccontextmanager
async def github_publisher_connection(
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> AsyncIterator[GithubClient]:
    token = settings.github.write_token.get_secret_value()
    if not token.strip() or not settings.github.bot_user_id:
        raise PublicationConfigError(
            "GitHub publication credentials require GITHUB__WRITE_TOKEN "
            "and GITHUB__BOT_USER_ID"
        )
    headers = {
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "dmc-268-api-t2",
        "Authorization": f"Bearer {token}",
    }
    async with httpx.AsyncClient(
        headers=headers,
        timeout=15,
        follow_redirects=False,
        transport=transport,
    ) as client:
        yield GithubClient(client)


@asynccontextmanager
async def make_publication_handler[T](
    settings: Settings,
    sessions: async_sessionmaker[AsyncSession],
    completed_outcome: T,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> AsyncIterator[PublicationTaskHandler[T]]:
    """Compose publication explicitly; the caller owns queue wiring and execution."""
    async with github_publisher_connection(settings, transport=transport) as github:
        yield PublicationTaskHandler(
            PostgresPublicationStore(sessions),
            github,
            bot_user_id=settings.github.bot_user_id,
            completed_outcome=completed_outcome,
        )
