"""Verify the merged analysis-to-publication seam with PostgreSQL and Redis."""

from dataclasses import replace
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_analysis_handler import BASE, HEAD, DeterministicLlm, github_http
from test_analysis_handler import (
    review_id as review_id,  # noqa: PLC0414 -- shared analysis fixture
)
from test_contracts import SCHEMAS
from test_github_publisher_postgres import FakeGithub
from test_postgres_integration import PostgresHarness

from app.adapters.analysis_handler import PostgresAnalysisHandler
from app.adapters.outbox_dispatcher import PostgresOutboxDispatcher
from app.adapters.redis_queue import RedisQueue
from app.adapters.review_worker import PostgresReviewWorker
from app.core.config import Github, Settings
from app.db.models import ReviewEvent
from app.db.publication_models import PublicationOperation
from app.db.publications import PostgresPublicationStore
from app.publication_worker import run_once
from app.vcs.github import GithubClient

pytest_plugins = ("test_postgres_integration", "test_redis_queue")


@pytest.mark.parametrize(("schema_version", "rule_ref"), [(1, None), (2, "SEC-1")])
async def test_completed_analysis_is_published_and_acknowledged(  # noqa: PLR0913, PLR0917 -- real integration boundaries
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    review_id: UUID,
    schema_version: int,
    rule_ref: str | None,
) -> None:
    sessions = async_sessionmaker(postgres.engine)
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    llm = DeterministicLlm()
    llm.result = replace(
        llm.result,
        schema_version=schema_version,
        findings=[replace(llm.result.findings[0], rule_ref=rule_ref)],
    )
    assert await PostgresOutboxDispatcher(sessions, queue).dispatch_batch() == 1
    async with httpx.AsyncClient(transport=github_http()) as client:
        handler = PostgresAnalysisHandler(
            sessions, GithubClient(client), llm, schemas_dir=Path(SCHEMAS)
        )
        assert await PostgresReviewWorker(
            sessions, queue, handler, owner="analysis", task_kind="analyze"
        ).run_once()

    provider = FakeGithub()

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pulls/13"):
            return httpx.Response(
                200,
                json={
                    "number": 13,
                    "state": "open",
                    "title": "Fix division",
                    "body": "Description",
                    "head": {"sha": HEAD, "repo": {"id": 43}, "ref": "feature"},
                    "base": {"sha": BASE, "repo": {"id": 42}, "ref": "main"},
                },
            )
        return provider.respond(request)

    try:
        assert await run_once(
            sessions,
            queue,
            settings=Settings(
                github=Github(
                    write_token=SecretStr("test-publication-write"), bot_user_id="9002"
                )
            ),
            owner="publisher",
            transport=httpx.MockTransport(respond),
        )
        state = await PostgresPublicationStore(sessions).get(review_id)
        assert state is not None and state.status == "PUBLISHED"
        assert len(llm.payloads) == 1
        assert len(provider.posts) == 1
        comments = provider.posts[0]["comments"]
        assert len(comments) == 1
        assert ("SEC-1" in comments[0]["body"]) is (rule_ref is not None)
        async with sessions() as session:
            events = (
                await session.scalars(
                    select(ReviewEvent)
                    .where(ReviewEvent.review_job_id == review_id)
                    .order_by(ReviewEvent.sequence)
                )
            ).all()
        assert [event.sequence for event in events] == list(range(1, len(events) + 1))
        assert [
            event.publication_status
            for event in events
            if event.event_type == "PUBLICATION_PENDING"
        ] == ["PENDING"]
        assert events[-1].publication_status == "PUBLISHED"
        for kind in ("analyze", "publish"):
            pending = await redis_client.xpending(
                f"{stream_prefix}:{kind}", "review-workers"
            )
            assert pending["pending"] == 0
    finally:
        async with sessions.begin() as session:
            await session.execute(
                delete(PublicationOperation).where(
                    PublicationOperation.review_job_id == review_id
                )
            )
