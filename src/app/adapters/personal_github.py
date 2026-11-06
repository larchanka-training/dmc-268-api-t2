"""Personal OAuth credential context shared by HTTP and durable admission."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

import httpx
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import GithubIdentity
from app.vcs.github import GithubClient


class PersonalCredentialUnavailableError(Exception):
    """No safe current personal credential could be opened."""


@asynccontextmanager
async def personal_github_connection(
    user_id: UUID,
    settings: Settings,
    session: AsyncSession,
) -> AsyncIterator[GithubClient]:
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
            or any(ord(char) <= ord(" ") or ord(char) == ord("\x7f") for char in token)
        ):
            raise ValueError("Invalid credential")
    except InvalidToken, ValueError, UnicodeError, SQLAlchemyError, OSError:
        raise PersonalCredentialUnavailableError from None
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
