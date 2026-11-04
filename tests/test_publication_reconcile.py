"""CLI: the documented operator path resolves a parked UNKNOWN (issue #52).

Real PostgreSQL, scripted GitHub. The utility works at the publication seam —
the queue delivery stays parked in the consumer PEL and is never reclaimed
here; a resolved outcome makes every later pass a no-op.
"""

import uuid

import httpx
import pytest
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from test_github_publisher_postgres import FakeGithub
from test_publication_dispatcher import publication_command

from app.adapters.publication_handler import PublicationRecoveryRequiredError
from app.adapters.redis_queue import RedisQueue
from app.application.publication_ports import PublicationAuthorityError
from app.core.config import Github, Settings
from app.publication_reconcile import run_once as reconcile_once
from app.publication_worker import run_once as publish_once

pytest_plugins = ("test_publications_postgres", "test_redis_queue")

pytestmark = pytest.mark.chaos


def _settings() -> Settings:
    return Settings(
        github=Github(
            write_token=SecretStr("test-publication-write"), bot_user_id="9002"
        )
    )


async def test_cli_resolves_lost_post_unknown_without_manual_sql(
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

    # The POST lands remotely but its response is lost: durable UNKNOWN.
    lost = FakeGithub()
    lost.lose_post_response = True
    with pytest.raises(PublicationRecoveryRequiredError):
        await publish_once(
            sessions,
            queue,
            settings=_settings(),
            owner="reconcile-park",
            transport=httpx.MockTransport(lost.respond),
        )

    # The operator path grants authority and reconciles: PUBLISHED with the
    # lost POST being the only POST ever sent (reads, never another write).
    outcome = await reconcile_once(
        sessions,
        _settings(),
        review_id,
        owner="reconcile-cli",
        transport=httpx.MockTransport(lost.respond),
    )
    assert outcome["status"] == "PUBLISHED"
    assert outcome["remote_review_id"] == "9001"
    assert len(lost.posts) == 1

    # The resolved outcome is terminal: a second invocation is refused before
    # any provider HTTP (no rebroadcast, no double reconciliation).
    refused = FakeGithub()
    with pytest.raises(PublicationAuthorityError):
        await reconcile_once(
            sessions,
            _settings(),
            review_id,
            owner="reconcile-cli",
            transport=httpx.MockTransport(refused.respond),
        )
    assert refused.requests == []
