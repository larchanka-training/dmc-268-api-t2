"""Runtime wiring for the external GitHub gateway."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx

from app.application.github_ports import (
    GithubPort,
    GithubPullRequestReader,
    RepositoryFileReader,
)
from app.core.circuit_breaker import CircuitBreaker
from app.core.config import Settings
from app.vcs.github import GithubClient


@asynccontextmanager
async def github_connection(
    settings: Settings, *, breaker: CircuitBreaker | None = None
) -> AsyncIterator[GithubPort]:
    """Long-lived workers pass a breaker; request-scoped callers need none."""
    async with _github_client(settings, breaker=breaker) as client:
        yield client


@asynccontextmanager
async def github_pull_request_connection(
    settings: Settings,
) -> AsyncIterator[GithubPullRequestReader]:
    async with _github_client(settings) as client:
        yield client


@asynccontextmanager
async def github_file_connection(
    settings: Settings,
) -> AsyncIterator[RepositoryFileReader]:
    async with _github_client(settings) as client:
        yield client


@asynccontextmanager
async def _github_client(
    settings: Settings, *, breaker: CircuitBreaker | None = None
) -> AsyncIterator[GithubClient]:
    headers = {"X-GitHub-Api-Version": "2022-11-28", "User-Agent": "dmc-268-api-t2"}
    token = settings.github.token.get_secret_value()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(
        headers=headers, timeout=15, follow_redirects=False
    ) as client:
        yield GithubClient(client, breaker=breaker)
