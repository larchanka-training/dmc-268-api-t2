"""Runtime wiring for the external GitHub gateway."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx

from app.application.github_ports import GithubPort
from app.core.config import Settings
from app.vcs.github import GithubClient


@asynccontextmanager
async def github_connection(settings: Settings) -> AsyncIterator[GithubPort]:
    headers = {"X-GitHub-Api-Version": "2022-11-28", "User-Agent": "dmc-268-api-t2"}
    token = settings.github.token.get_secret_value()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(
        headers=headers, timeout=15, follow_redirects=False
    ) as client:
        yield GithubClient(client)
