"""Explicit publication composition with its real PostgreSQL adapter."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from test_github_publisher_postgres import FakeGithub, setup_publication

from app.core.config import Settings
from app.core.publication_runtime import make_publication_handler

pytest_plugins = ("test_publications_postgres",)


@dataclass(frozen=True)
class PublishTask:
    schema_version: int
    review_id: str
    task_kind: str = "publish"


async def test_publication_runtime_builds_real_handler_with_persisted_results(
    publication_engine: AsyncEngine,
    payload_data: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """AC-RUNTIME-04: explicit composition publishes through real store/client."""
    store, authority = await setup_publication(publication_engine, payload_data)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB__WRITE_TOKEN", "write-token-private")
    monkeypatch.setenv("GITHUB__BOT_USER_ID", "9002")
    provider = FakeGithub()
    completed = object()

    async with make_publication_handler(
        Settings(),
        async_sessionmaker(publication_engine, expire_on_commit=False),
        completed,
        transport=httpx.MockTransport(provider.respond),
    ) as handler:
        outcome = await handler.handle(
            PublishTask(1, str(authority.review_job_id)),
            authority,
        )

    assert outcome is completed
    assert len(provider.posts) == 1
    state = await store.get(authority.review_job_id)
    assert state is not None and state.status == "PUBLISHED"
