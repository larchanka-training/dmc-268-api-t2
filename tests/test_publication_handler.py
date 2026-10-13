"""Publication task bridge with the real publisher, PostgreSQL and HTTP edge."""

from dataclasses import dataclass, replace
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from test_github_publisher_postgres import FakeGithub, setup_publication

from app.adapters.publication_handler import (
    PublicationRecoveryRequiredError,
    PublicationTaskHandler,
)
from app.vcs.github import GithubClient

pytest_plugins = ("test_publications_postgres",)


@dataclass(frozen=True)
class PublishTask:
    schema_version: int
    review_id: str
    task_kind: str = "publish"


async def test_handler_returns_injected_completion_after_durable_publication(
    publication_engine: AsyncEngine,
    payload_data: dict[str, Any],
) -> None:
    """AC-HANDLER-01: the bridge returns the worker's exact completion sentinel."""
    store, authority = await setup_publication(publication_engine, payload_data)
    provider = FakeGithub()
    completed = object()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(provider.respond)
    ) as http:
        handler = PublicationTaskHandler(
            store,
            GithubClient(http),
            bot_user_id="9002",
            completed_outcome=completed,
        )
        result = await handler.handle(
            PublishTask(1, str(authority.review_job_id)), authority
        )

    assert result is completed
    assert len(provider.posts) == 1
    state = await store.get(authority.review_job_id)
    assert state is not None and state.status == "PUBLISHED"


@pytest.mark.parametrize(
    "invalid_task",
    [
        {"schema_version": 2},
        {"schema_version": True},
        {"task_kind": "analyze"},
        {"lease_task_kind": "analyze"},
        {"review_id": "00000000-0000-0000-0000-000000000001"},
        {"review_id": "private-token"},
    ],
)
async def test_handler_rejects_invalid_task_identity_before_publication(
    publication_engine: AsyncEngine,
    payload_data: dict[str, Any],
    invalid_task: dict[str, Any],
) -> None:
    """AC-HANDLER-02: invalid task identity never consumes publication authority."""
    store, authority = await setup_publication(publication_engine, payload_data)
    provider = FakeGithub()
    changes = dict(invalid_task)
    authority = replace(authority, task_kind=changes.pop("lease_task_kind", "publish"))
    task = replace(PublishTask(1, str(authority.review_job_id)), **changes)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(provider.respond)
    ) as http:
        handler = PublicationTaskHandler(
            store,
            GithubClient(http),
            bot_user_id="9002",
            completed_outcome=object(),
        )
        with pytest.raises(ValueError, match="Invalid publication task"):
            await handler.handle(task, authority)

    assert provider.posts == []
    state = await store.get(authority.review_job_id)
    assert state is not None and state.status == "PENDING"


@pytest.mark.parametrize("status", ["FAILED", "UNKNOWN"])
async def test_handler_requires_recovery_for_failed_or_ambiguous_publication(
    publication_engine: AsyncEngine,
    payload_data: dict[str, Any],
    status: str,
) -> None:
    """AC-HANDLER-03: uncertain/failed publish work never returns completion."""
    store, authority = await setup_publication(publication_engine, payload_data)
    provider = FakeGithub()
    if status == "FAILED":
        provider.reject_post = 403
    else:
        provider.lose_post_response = True
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(provider.respond)
    ) as http:
        handler = PublicationTaskHandler(
            store,
            GithubClient(http),
            bot_user_id="9002",
            completed_outcome=object(),
        )
        with pytest.raises(
            PublicationRecoveryRequiredError, match="Publication requires recovery"
        ) as error:
            await handler.handle(
                PublishTask(1, str(authority.review_job_id)), authority
            )

    assert error.value.status == status
    assert error.value.review_id == authority.review_job_id
    assert str(error.value) == "Publication requires recovery"
    assert len(provider.posts) == 1
    state = await store.get(authority.review_job_id)
    assert state is not None and state.status == status
