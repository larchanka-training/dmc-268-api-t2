"""A lost PostgreSQL claim must not discard the Redis scan cursor."""

import uuid
from datetime import timedelta

import httpx
from redis.asyncio import Redis
from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_analysis_gateway import analysis_task
from test_analysis_handler import github_http
from test_analysis_recovery import production_handler
from test_analysis_terminal import freeze, ledger_cleanup  # noqa: F401 -- fixture
from test_postgres_integration import PostgresHarness

from app.adapters.redis_queue import RedisQueue
from app.adapters.review_worker import PostgresReviewWorker
from app.db.models import TaskLease

pytest_plugins = ("test_analysis_handler",)


async def test_recovery_preserves_next_cursor_when_postgres_authority_is_lost(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    review_id: uuid.UUID,
) -> None:
    factory = async_sessionmaker(postgres.engine)
    await freeze(factory, review_id)
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    task = await analysis_task(factory, review_id)
    first = await queue.enqueue(task)
    second = await queue.enqueue(task)
    for _ in range(2):
        assert await queue.receive(task_kind="analyze", consumer_name="crashed")
    github = github_http()

    async def revoke(request: httpx.Request) -> httpx.Response:
        async with factory.begin() as session:
            await session.execute(
                update(TaskLease)
                .where(TaskLease.review_job_id == review_id)
                .values(expires_at=func.clock_timestamp() - timedelta(seconds=1))
            )
        return await github.handle_async_request(request)

    def forbidden(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Lost authority must prevent LLM debit and HTTP")

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(revoke)) as gh,
        httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as llm,
    ):
        worker = PostgresReviewWorker(
            factory,
            queue,
            production_handler(factory, gh, llm),
            owner="recover",
            task_kind="analyze",
        )
        assert await worker.recover_once(cursor="0-0", min_idle_time=0) == (
            second,
            False,
        )
    pending = await redis_client.xpending_range(
        f"{stream_prefix}:analyze", "review-workers", "-", "+", 10
    )
    assert [(row["message_id"], row["consumer"]) for row in pending] == [
        (first, "recover"),
        (second, "crashed"),
    ]
