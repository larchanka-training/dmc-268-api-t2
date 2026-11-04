"""QA-02A: repository rules survive real inference through GitHub publication."""

import json
from typing import Any
from uuid import UUID

import httpx
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_analysis_gateway import OUTPUT, analysis_task
from test_analysis_handler import HEAD, github_http
from test_analysis_recovery import production_handler
from test_analysis_terminal import (
    PROFILES,
    PROMPTS,
    detail,
    freeze,
    ledger_cleanup,  # noqa: F401 -- register durable accounting cleanup
)
from test_github_publisher_postgres import FakeGithub
from test_postgres_integration import PostgresHarness

from app.adapters.outbox_dispatcher import PostgresOutboxDispatcher
from app.adapters.redis_queue import RedisQueue
from app.adapters.review_worker import PostgresReviewWorker
from app.application.ports import QueueTaskEnvelope
from app.core.config import Github, Settings
from app.db.models import (
    ChunkResult,
    Finding,
    OutboxEvent,
    RepositorySettings,
    ReviewJob,
    ReviewLlmAttempt,
)
from app.db.publication_models import PublicationOperation
from app.db.publications import PostgresPublicationStore
from app.db.worker_tasks import PostgresWorkerTasks
from app.domain.settings import compute_rules_digest
from app.publication_worker import run_once
from app.review.configuration import FrozenConfigurationReader

pytest_plugins = ("test_analysis_handler",)
RULE = "[SEC-1] Avoid division by zero"


async def test_repository_rule_reaches_github_once_through_real_gateway(  # noqa: PLR0915 -- one scenario across durable and external boundaries
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    review_id: UUID,
) -> None:
    sessions = async_sessionmaker(postgres.engine, expire_on_commit=False)
    async with sessions.begin() as session:
        job = await session.get(ReviewJob, review_id)
        assert job is not None
        repository_settings = await session.get(
            RepositorySettings, job.repository_settings_id
        )
        assert repository_settings is not None
        repository_settings.rules = {"instructions": [RULE]}
        repository_settings.rules_digest = compute_rules_digest(
            repository_settings.rules
        )
        job.rules_digest = repository_settings.rules_digest
        job.prompt_version = "v3"
        settings_id = repository_settings.id
    await freeze(sessions, review_id)
    config = await FrozenConfigurationReader(sessions, PROMPTS, PROFILES).resolve(
        review_id
    )
    assert config.prompt_version == "v3"
    assert config.repository_settings_id == settings_id
    assert config.instructions == (RULE,)
    async with sessions() as session:
        stored_settings = await session.get(RepositorySettings, settings_id)
        assert stored_settings is not None
        assert stored_settings.rules == {"instructions": [RULE]}

    requests: list[httpx.Request] = []

    def infer(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "llm.invalid"
        assert request.method == "POST"
        requests.append(request)
        body = json.loads(request.content)
        context = body["messages"][-1]["content"]
        assert context.count("<repo_rules>") == 1
        assert f"<repo_rules>\n{RULE}\n</repo_rules>" in context
        assert body["model"] == "test-model"
        output: dict[str, Any] = dict(OUTPUT)
        output["findings"] = [output["findings"][0] | {"rule_ref": "SEC-1"}]
        return httpx.Response(
            200,
            json={
                "model": "test-model",
                "choices": [{"message": {"content": json.dumps(output)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 40},
            },
        )

    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    analyze_task = await analysis_task(sessions, review_id)
    assert await PostgresOutboxDispatcher(sessions, queue).dispatch_batch() == 1
    async with (
        httpx.AsyncClient(transport=github_http()) as github,
        httpx.AsyncClient(transport=httpx.MockTransport(infer)) as http,
    ):
        worker = PostgresReviewWorker(
            sessions,
            queue,
            production_handler(sessions, github, http),
            owner="rules-analysis",
            task_kind="analyze",
        )
        assert await worker.run_once()
    assert len(requests) == 1
    assert await PostgresWorkerTasks(sessions).completed(analyze_task)
    async with sessions() as session:
        chunk = (
            await session.scalars(
                select(ChunkResult).where(ChunkResult.review_job_id == review_id)
            )
        ).one()
        finding = (
            await session.scalars(
                select(Finding).where(Finding.chunk_result_id == chunk.id)
            )
        ).one()
        assert chunk.schema_version == 2  # noqa: PLR2004 -- canonical v3 result
        assert finding.rule_ref == "SEC-1"
        assert finding.evidence == "return value / 0"
        fingerprint = finding.fingerprint
        publish = (
            await session.scalars(
                select(OutboxEvent).where(
                    OutboxEvent.review_job_id == review_id,
                    OutboxEvent.task_kind == "publish",
                )
            )
        ).one()
        assert publish.broker_published_at is None
        assert publish.trace_id == analyze_task.trace_id
    store = PostgresPublicationStore(sessions)
    state = await store.get(review_id)
    assert state is not None and state.status == "PENDING"
    assert await redis_client.xlen(f"{stream_prefix}:publish") == 0

    provider = FakeGithub()
    snapshot_http = github_http()

    async def publish_http(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.github.com"
        if request.method == "GET" and request.url.path.endswith("/pulls/13"):
            response = await snapshot_http.handle_async_request(request)
            return httpx.Response(200, json=response.json() | {"number": 13})
        return provider.respond(request)

    settings = Settings(
        github=Github(
            write_token=SecretStr("test-publication-write"), bot_user_id="9002"
        )
    )
    try:
        assert await run_once(
            sessions,
            queue,
            settings=settings,
            owner="rules-publisher",
            transport=httpx.MockTransport(publish_http),
        )
        entries = await redis_client.xrange(f"{stream_prefix}:publish")
        assert entries is not None and len(entries) == 1
        fields = entries[0][1]
        assert fields is not None
        publish_task = QueueTaskEnvelope(**json.loads(fields["envelope"]))
        assert publish_task.review_id == str(review_id)
        assert await PostgresWorkerTasks(sessions).completed(publish_task)

        # Duplicate the durable command in a new Redis message (not PEL recovery).
        await queue.enqueue(publish_task)
        assert await run_once(
            sessions,
            queue,
            settings=settings,
            owner="rules-redelivery",
            transport=httpx.MockTransport(publish_http),
        )
        assert len(provider.posts) == 1
        assert [path for method, path in provider.requests if method == "POST"] == [
            "/repos/team/api/pulls/13/reviews"
        ]
        posted = provider.posts[0]
        assert posted["commit_id"] == HEAD
        assert len(posted["comments"]) == 1
        comment = posted["comments"][0]
        assert "Правило: SEC-1\n" in comment["body"]
        assert fingerprint in comment["body"]
        assert (comment["path"], comment["side"], comment["line"]) == (
            "src/a.py",
            "RIGHT",
            1,
        )
        state = await store.get(review_id)
        assert state is not None and state.status == "PUBLISHED"
        assert state.finished_at is not None
        assert state.provider_metadata is not None
        assert state.provider_metadata["comment_ids"] == {fingerprint: "9003"}
        async with sessions() as session:
            job = await session.get(ReviewJob, review_id)
            assert job is not None and job.status == "COMPLETED"
            assert job.finished_at is not None and job.requested_head_sha == HEAD
            attempt = (
                await session.scalars(
                    select(ReviewLlmAttempt).where(
                        ReviewLlmAttempt.review_job_id == review_id
                    )
                )
            ).one()
            assert attempt.state == "SUCCEEDED"
            outbox = (
                await session.scalars(
                    select(OutboxEvent).where(OutboxEvent.review_job_id == review_id)
                )
            ).all()
            assert {event.task_kind for event in outbox} == {"analyze", "publish"}
            assert len(outbox) == 2  # noqa: PLR2004 -- one command per stage
            assert all(event.broker_published_at is not None for event in outbox)
        view = await detail(sessions, review_id)
        assert view["status"] == "COMPLETED"
        assert view["publication_status"] == "PUBLISHED"
        assert view["progress"] == {"completed_chunks": 1, "planned_chunks": 1}
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
