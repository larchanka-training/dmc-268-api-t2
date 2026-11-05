"""Over-budget diffs are reviewed in chunks that each fit the real prompt."""

import json
import uuid

import httpx
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_analysis_handler import github_http
from test_analysis_recovery import production_handler
from test_analysis_terminal import (  # noqa: F401 -- local fixture
    freeze,
    ledger_cleanup,
)
from test_postgres_integration import PostgresHarness

from app.adapters.outbox_dispatcher import PostgresOutboxDispatcher
from app.adapters.redis_queue import RedisQueue
from app.adapters.review_worker import PostgresReviewWorker
from app.db.models import ChunkResult, ContextPayload, ReviewJob

pytest_plugins = ("test_analysis_handler",)


def _long_line_file(name: str, lines: int) -> str:
    # Long lines make the rendered prompt nearly as large as the wire estimate,
    # so the fixed system prompt and few-shots decide whether a chunk fits.
    body = "".join(f"+payload_{i} = '{'x' * 600}'\n" for i in range(1, lines + 1))
    return (
        f"diff --git a/src/{name}.py b/src/{name}.py\n"
        "new file mode 100644\n"
        "index 0000000..456abcd\n"
        "--- /dev/null\n"
        f"+++ b/src/{name}.py\n"
        f"@@ -0,0 +1,{lines} @@\n{body}"
    )


def _provider(calls: list[httpx.Request]) -> httpx.MockTransport:
    def reply(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        output = {"summary": f"Chunk {len(calls)}.", "limitations": [], "findings": []}
        return httpx.Response(
            200,
            json={
                "model": "test-model",
                "choices": [{"message": {"content": json.dumps(output)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 40},
            },
        )

    return httpx.MockTransport(reply)


async def test_long_line_chunks_fit_the_rendered_prompt(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    review_id: uuid.UUID,
) -> None:
    factory = async_sessionmaker(postgres.engine, expire_on_commit=False)
    await freeze(factory, review_id)
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    assert await PostgresOutboxDispatcher(factory, queue).dispatch_batch() == 1
    calls: list[httpx.Request] = []
    diff = _long_line_file("a", 15) + _long_line_file("b", 15)
    async with (
        httpx.AsyncClient(transport=github_http(diff)) as github,
        httpx.AsyncClient(transport=_provider(calls)) as http,
    ):
        worker = PostgresReviewWorker(
            factory,
            queue,
            production_handler(factory, github, http),
            owner="chunking",
            task_kind="analyze",
        )
        assert await worker.run_once() is True
    async with factory() as session:
        review = await session.get(ReviewJob, review_id)
        contexts = (
            await session.scalars(
                select(ContextPayload).where(ContextPayload.review_job_id == review_id)
            )
        ).all()
        chunks = (
            await session.scalars(
                select(ChunkResult).where(ChunkResult.review_job_id == review_id)
            )
        ).all()
    assert review is not None and review.status == "COMPLETED", review and (
        review.status,
        review.coverage,
    )
    assert len(contexts) > 1 and len(chunks) == len(contexts) == len(calls)
    assert review.coverage is not None and review.coverage["skipped"] == []
