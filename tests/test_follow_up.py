"""System follow-up start after a terminal analysis (spec PR #42 §3.2.1)."""

# ruff: noqa: PLR2004 -- literal job and quota counts

import uuid
from dataclasses import replace
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_postgres_integration import PostgresHarness
from test_review_start_api import (
    HEAD,
    HEADERS,
    AllowedAuthorizer,
    FakeStartGithub,
    StartScope,
)

from app.adapters.follow_up import FollowUpOutcome, SystemFollowUp
from app.api.review_start_dependencies import get_one_active_per_pr
from app.application.github_ports import GithubFailure, GithubPullRequest
from app.application.review_start_ports import (
    RepositoryIdentity,
    ReviewStartAuthorizer,
)
from app.db import models
from app.domain.frozen_configuration import ModelRoute, admission_policy
from app.review.configuration import FrozenAdmissionResolver, RuntimeProfile

pytest_plugins = ("test_review_start_api",)
PROMPTS = Path(__file__).resolve().parents[1] / ".agents/reviewer"
NEWER, NEWEST = "c" * 40, "e" * 40


class LivePulls:
    def __init__(self, base: GithubPullRequest) -> None:
        self.pull = base
        self.failure: GithubFailure | None = None

    async def get_pull_request(
        self, owner: str, name: str, repository_external_id: str, number: int
    ) -> GithubPullRequest:
        if self.failure is not None:
            raise self.failure
        return replace(self.pull, source_repository_external_id=repository_external_id)


def _follow_up(
    postgres: PostgresHarness,
    pulls: LivePulls,
    authorizer: ReviewStartAuthorizer | None = None,
) -> SystemFollowUp:
    factory = async_sessionmaker(postgres.engine, expire_on_commit=False)
    resolver = FrozenAdmissionResolver(
        factory,
        PROMPTS,
        {
            "eurorouter/review-v1": RuntimeProfile(
                provider="eurorouter",
                base_url="https://llm.invalid/v1",
                api_keys=("synthetic-test-key",),
                allowed_models=("test-model",),
            )
        },
        primary=ModelRoute(
            profile_ref="eurorouter/review-v1",
            model_ref="test-model",
            model_digest=None,
        ),
        policy=admission_policy("0" * 64),
    )
    return SystemFollowUp(
        factory,
        pulls,
        resolver,
        authorizer=authorizer or AllowedAuthorizer(),
        prompt_root=PROMPTS,
    )


async def _start(client: httpx.AsyncClient, scope: StartScope) -> uuid.UUID:
    response = await client.post(
        scope.url, json={"requested_head_sha": HEAD}, headers=HEADERS
    )
    assert response.status_code == HTTPStatus.ACCEPTED, response.text
    return uuid.UUID(response.json()["review_id"])


async def _finish(postgres: PostgresHarness, review: uuid.UUID) -> None:
    async with postgres.engine.begin() as conn:
        await conn.execute(
            update(models.ReviewJob)
            .where(models.ReviewJob.id == review)
            .values(status="COMPLETED", finished_at=datetime.now(UTC))
        )


async def _jobs(postgres: PostgresHarness, scope: StartScope) -> list[models.ReviewJob]:
    async with async_sessionmaker(postgres.engine)() as session:
        return list(
            await session.scalars(
                select(models.ReviewJob)
                .where(models.ReviewJob.repository_id == scope.repository)
                .order_by(models.ReviewJob.created_at)
            )
        )


async def test_two_pushes_during_analysis_end_in_one_start_for_latest_head(
    app: FastAPI,
    client: httpx.AsyncClient,
    scope: StartScope,
    configured: FakeStartGithub,
    postgres: PostgresHarness,
) -> None:
    """DoD 6: pushes NEWER then NEWEST land while review J of HEAD runs."""
    app.dependency_overrides[get_one_active_per_pr] = lambda: True
    first = await _start(client, scope)
    for key, head in (("two", NEWER), ("three", NEWEST)):
        configured.pull_request = replace(configured.pull_request, head_sha=head)
        busy = await client.post(
            scope.url,
            json={"requested_head_sha": head},
            headers={**HEADERS, "Idempotency-Key": key},
        )
        assert busy.status_code == HTTPStatus.CONFLICT
    await _finish(postgres, first)
    follow_up = _follow_up(postgres, LivePulls(configured.pull_request))

    assert await follow_up.after_terminal(first) is FollowUpOutcome.STARTED
    # At-least-once terminal delivery never schedules a second follow-up.
    assert await follow_up.after_terminal(first) is FollowUpOutcome.REPLAYED

    jobs = await _jobs(postgres, scope)
    assert [job.requested_head_sha for job in jobs] == [HEAD, NEWEST]
    latest = jobs[1]
    assert latest.rerun_of == first
    assert latest.trigger_type == "system_follow_up"
    assert latest.initiator_user_id == scope.user
    assert latest.config_digest == jobs[0].config_digest
    async with async_sessionmaker(postgres.engine)() as session:
        change = await session.get(models.ChangeRequest, scope.change)
        starts = await session.scalar(
            select(models.QuotaUsage.value).where(
                models.QuotaUsage.repository_id == scope.repository,
                models.QuotaUsage.type == "starts",
            )
        )
    assert change is not None and change.head_sha == NEWEST
    assert starts == 2  # the follow-up is a real start (spec D9)


async def test_follow_up_is_a_noop_without_a_newer_head(
    client: httpx.AsyncClient,
    scope: StartScope,
    configured: FakeStartGithub,
    postgres: PostgresHarness,
) -> None:
    first = await _start(client, scope)
    pulls = LivePulls(configured.pull_request)
    follow_up = _follow_up(postgres, pulls)

    assert await follow_up.after_terminal(first) is FollowUpOutcome.NOT_TERMINAL
    await _finish(postgres, first)
    assert await follow_up.after_terminal(first) is FollowUpOutcome.SAME_HEAD
    pulls.failure = GithubFailure("PR_STALE_OR_CLOSED", retryable=False)
    assert await follow_up.after_terminal(first) is FollowUpOutcome.CLOSED
    pulls.failure = GithubFailure("VCS_UNAVAILABLE", retryable=True)
    assert await follow_up.after_terminal(first) is FollowUpOutcome.VCS_UNAVAILABLE
    assert len(await _jobs(postgres, scope)) == 1


async def test_follow_up_respects_an_active_review_on_the_pr(
    app: FastAPI,
    client: httpx.AsyncClient,
    scope: StartScope,
    configured: FakeStartGithub,
    postgres: PostgresHarness,
) -> None:
    """A human start that won the race leaves the system follow-up a no-op."""
    first = await _start(client, scope)
    await _finish(postgres, first)
    configured.pull_request = replace(configured.pull_request, head_sha=NEWER)
    manual = await client.post(
        scope.url,
        json={"requested_head_sha": NEWER},
        headers={**HEADERS, "Idempotency-Key": "manual"},
    )
    assert manual.status_code == HTTPStatus.ACCEPTED
    follow_up = _follow_up(postgres, LivePulls(configured.pull_request))

    assert await follow_up.after_terminal(first) is FollowUpOutcome.BUSY
    assert len(await _jobs(postgres, scope)) == 2


class _Authorizer:
    """Initiator's live GitHub permission, as the HTTP start revalidates it."""

    def __init__(self, failure: GithubFailure | None) -> None:
        self.failure = failure
        self.calls: list[tuple[uuid.UUID, RepositoryIdentity]] = []

    async def authorize_start(
        self, *, user_id: uuid.UUID, repository: RepositoryIdentity
    ) -> None:
        self.calls.append((user_id, repository))
        if self.failure is not None:
            raise self.failure


@pytest.mark.parametrize(
    ("failure", "outcome"),
    [
        (None, FollowUpOutcome.STARTED),
        (
            GithubFailure("VCS_ACCESS_DENIED", retryable=False),
            FollowUpOutcome.ACCESS_DENIED,
        ),
        (
            GithubFailure("VCS_UNAVAILABLE", retryable=True),
            FollowUpOutcome.VCS_UNAVAILABLE,
        ),
    ],
    ids=["allowed", "revoked", "unavailable"],
)
async def test_follow_up_revalidates_the_initiators_live_permission(  # noqa: PLR0913, PLR0917 -- explicit fixture and parameter boundaries
    client: httpx.AsyncClient,
    scope: StartScope,
    configured: FakeStartGithub,
    postgres: PostgresHarness,
    failure: GithubFailure | None,
    outcome: FollowUpOutcome,
) -> None:
    """Spec §3.2.1 step 1: an initiator who lost access gets no follow-up."""
    first = await _start(client, scope)
    await _finish(postgres, first)
    configured.pull_request = replace(configured.pull_request, head_sha=NEWER)
    authorizer = _Authorizer(failure)
    follow_up = _follow_up(postgres, LivePulls(configured.pull_request), authorizer)

    assert await follow_up.after_terminal(first) is outcome

    assert [(user, repo.id) for user, repo in authorizer.calls] == [
        (scope.user, scope.repository)
    ]
    expected = 2 if outcome is FollowUpOutcome.STARTED else 1
    assert len(await _jobs(postgres, scope)) == expected
