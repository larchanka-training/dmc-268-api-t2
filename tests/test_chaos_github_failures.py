"""Chaos: network failures when posting the review to the GitHub API.

The publication runtime (dispatcher + worker + publisher) runs against a
scripted GitHub; PostgreSQL and Redis stay real. Pins the no-client-retry
contract: an ambiguous POST lands durable UNKNOWN and is never rewritten by
redelivery; only the reconciliation seam resolves it — and absence alone
never releases the operation.
"""

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy import func, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from test_github_publisher_postgres import FakeGithub
from test_publication_dispatcher import publication_command

from app.adapters.publication_handler import PublicationRecoveryRequiredError
from app.adapters.redis_queue import RedisQueue
from app.application.github_publication import GithubPublisher
from app.application.publication_ports import PublicationAuthority
from app.core.config import Github, Settings
from app.db.models import OutboxEvent, TaskLease
from app.db.publications import PostgresPublicationStore
from app.db.task_leases import PostgresTaskLeaseStore
from app.publication_worker import run_once
from app.vcs.github import GithubClient

pytest_plugins = ("test_publications_postgres", "test_redis_queue")

pytestmark = pytest.mark.chaos


def _settings() -> Settings:
    return Settings(
        github=Github(
            write_token=SecretStr("test-publication-write"), bot_user_id="9002"
        )
    )


def _past() -> object:
    return func.clock_timestamp() - text("interval '1 second'")


def _publisher(
    engine: AsyncEngine,
    authority: PublicationAuthority,
    transport: httpx.MockTransport,
) -> GithubPublisher:
    return GithubPublisher(
        PostgresPublicationStore(async_sessionmaker(engine)),
        GithubClient(httpx.AsyncClient(transport=transport)),
        authority=authority,
        bot_user_id="9002",
    )


async def _takeover_authority(
    engine: AsyncEngine, review_id: uuid.UUID
) -> PublicationAuthority:
    """Bump the parked lease's fencing token, as an operator takeover would.

    Only the FAILED-redelivery test still models the takeover by hand: the
    production grant (acquire_reconciliation) covers the UNKNOWN outcome
    (issue #52); re-entry into publish() for a terminal FAILED is separate.
    """
    async with async_sessionmaker(engine).begin() as session:
        row = (
            await session.execute(
                update(TaskLease)
                .where(TaskLease.review_job_id == review_id)
                .values(
                    fencing_token=TaskLease.fencing_token + 1,
                    expires_at=datetime.now(UTC) + timedelta(seconds=90),
                )
                .returning(TaskLease.owner, TaskLease.fencing_token)
            )
        ).one()
    return PublicationAuthority(review_id, row.owner, row.fencing_token)


async def test_lost_post_response_never_rewrites_and_reconciles_to_exactly_once(
    publication_engine: AsyncEngine,
    payload_data: dict[str, object],
    redis_client: Redis,
    stream_prefix: str,
) -> None:
    engine = publication_engine
    command = await publication_command(engine, payload_data)
    review_id = uuid.UUID(command.review_id)
    sessions = async_sessionmaker(engine)
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)

    # The POST leaves the gate but its response is lost on the network.
    lost = FakeGithub()
    lost.lose_post_response = True
    with pytest.raises(PublicationRecoveryRequiredError):
        await run_once(
            sessions,
            queue,
            settings=_settings(),
            owner="chaos-pub-lost",
            transport=httpx.MockTransport(lost.respond),
        )

    state = await PostgresPublicationStore(sessions).get(review_id)
    assert state is not None and state.status == "UNKNOWN"
    assert state.provider_metadata is not None
    assert state.provider_metadata.get("confirmed_absent") is False
    assert len(lost.posts) == 1
    pending = await redis_client.xpending(f"{stream_prefix}:publish", "review-workers")
    assert pending["pending"] == 1

    # Publish-level redelivery: the retained UNKNOWN returns with zero HTTP.
    authority = await PostgresTaskLeaseStore(sessions).acquire_reconciliation(
        review_id, owner="reconcile-operator"
    )
    assert authority is not None and authority.fencing_token > 0
    publisher = _publisher(engine, authority, httpx.MockTransport(lost.respond))
    outcome = await publisher.publish(review_job_id=review_id)
    assert outcome["status"] == "UNKNOWN"
    assert len(lost.posts) == 1

    # Reconciliation against the remote where the POST did land: resolves
    # PUBLISHED without a second POST ever being sent.
    reconciled = await publisher.reconcile(review_job_id=review_id)
    assert reconciled["status"] == "PUBLISHED"
    assert len(lost.posts) == 1
    state = await PostgresPublicationStore(sessions).get(review_id)
    assert state is not None and state.status == "PUBLISHED"
    assert state.provider_metadata is not None
    assert state.provider_metadata.get("remote_review_id") == "9001"

    # Quiescence without touching the queue: the durable outcome is terminal,
    # so the dispatcher enqueues nothing and no provider HTTP happens. The
    # parked delivery itself stays in the consumer PEL: production has no
    # publish-stream reclaim (the open F-2 limitation, see issue #52) — the
    # test documents the parking instead of faking an automatic recovery.
    # Stream deletion happens only in the stream_prefix fixture cleanup.
    async with sessions.begin() as session:
        await session.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == uuid.UUID(command.event_id))
            .values(dispatch_not_before=_past())
        )
    untouched = FakeGithub()
    assert (
        await run_once(
            sessions,
            queue,
            settings=_settings(),
            owner="chaos-pub-close",
            transport=httpx.MockTransport(untouched.respond),
        )
        is False
    )
    assert untouched.posts == [] and untouched.requests == []
    pending = await redis_client.xpending(f"{stream_prefix}:publish", "review-workers")
    assert pending["pending"] == 1  # parked delivery retained, never reclaimed
    assert await redis_client.xlen(f"{stream_prefix}:publish") == 1


async def test_github_429_lands_terminal_failed_without_retry_hammering(
    publication_engine: AsyncEngine,
    payload_data: dict[str, object],
    redis_client: Redis,
    stream_prefix: str,
) -> None:
    engine = publication_engine
    command = await publication_command(engine, payload_data)
    review_id = uuid.UUID(command.review_id)
    sessions = async_sessionmaker(engine)
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)

    limited = FakeGithub()
    limited.reject_post = 429
    with pytest.raises(PublicationRecoveryRequiredError):
        await run_once(
            sessions,
            queue,
            settings=_settings(),
            owner="chaos-pub-429",
            transport=httpx.MockTransport(limited.respond),
        )

    state = await PostgresPublicationStore(sessions).get(review_id)
    assert state is not None and state.status == "FAILED"
    assert state.provider_metadata is not None
    assert state.provider_metadata.get("confirmed_absent") is True
    # Exactly one POST attempt: the durable FAILED parks the delivery without
    # client-side retry hammering (a rejected POST lands in posts, not in
    # reviews, so the count of posts is what pins the no-retry contract).
    assert len(limited.posts) == 1
    assert limited.reviews == []  # the POST never landed on the remote
    http_calls = len(limited.requests)
    pending = await redis_client.xpending(f"{stream_prefix}:publish", "review-workers")
    assert pending["pending"] == 1

    # Publish-level redelivery: retained FAILED with zero provider HTTP.
    publisher = _publisher(
        engine,
        await _takeover_authority(engine, review_id),
        httpx.MockTransport(limited.respond),
    )
    outcome = await publisher.publish(review_job_id=review_id)
    assert outcome["status"] == "FAILED"
    assert outcome["confirmed_absent"] is True
    assert len(limited.posts) == 1
    assert len(limited.requests) == http_calls
