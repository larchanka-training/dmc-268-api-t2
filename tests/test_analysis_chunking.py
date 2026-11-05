"""Over-budget diffs are reviewed in chunks that each fit the real prompt."""

import base64
import json
import uuid
from pathlib import Path

import httpx
from redis.asyncio import Redis
from sqlalchemy import select, update
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
from app.review.prompt import PromptBuilder

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


async def test_v4_chunking_explicitly_degrades_optional_source_context(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    review_id: uuid.UUID,
) -> None:
    factory = async_sessionmaker(postgres.engine, expire_on_commit=False)
    async with factory.begin() as session:
        await session.execute(
            update(ReviewJob)
            .where(ReviewJob.id == review_id)
            .values(
                prompt_version="v4",
                prompt_digest=PromptBuilder.load(
                    Path(".agents/reviewer"), "v4"
                ).prompt_digest,
            )
        )
    await freeze(factory, review_id)
    sources: dict[str, bytes] = {}
    diff = ""
    for name in ("a", "b"):
        piece = _long_line_file(name, 24).replace(
            "+payload_1 = '" + "x" * 600 + "'\n", "+import os\n", 1
        )
        sources[f"src/{name}.py"] = (
            "\n".join(
                line[1:]
                for line in piece.splitlines()
                if line.startswith("+") and not line.startswith("+++")
            )
            + "\n"
        ).encode()
        diff += piece
    snapshot_http = github_http(diff)
    source_reads: list[httpx.Request] = []

    async def github_reply(request: httpx.Request) -> httpx.Response:
        if request.url.path in {"/repositories/43", "/repos/source/fork"}:
            return httpx.Response(
                200, json={"id": 43, "owner": {"login": "source"}, "name": "fork"}
            )
        if request.url.path.startswith("/repos/source/fork/commits/"):
            return httpx.Response(200, json={"sha": request.url.path.rsplit("/", 1)[1]})
        if "/contents/" in request.url.path:
            source_reads.append(request)
            raw = sources[request.url.path.split("/contents/", 1)[1]]
            return httpx.Response(
                200,
                json={
                    "type": "file",
                    "size": len(raw),
                    "sha": "456abcd" + "0" * 33,
                    "encoding": "base64",
                    "content": base64.b64encode(raw).decode(),
                },
            )
        return await snapshot_http.handle_async_request(request)

    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    assert await PostgresOutboxDispatcher(factory, queue).dispatch_batch() == 1
    calls: list[httpx.Request] = []
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(github_reply)) as github,
        httpx.AsyncClient(transport=_provider(calls)) as http,
    ):
        worker = PostgresReviewWorker(
            factory,
            queue,
            production_handler(factory, github, http),
            owner="v4-chunking",
            task_kind="analyze",
        )
        assert await worker.run_once() is True
    async with factory() as session:
        review = await session.get(ReviewJob, review_id)
        contexts = list(
            await session.scalars(
                select(ContextPayload).where(ContextPayload.review_job_id == review_id)
            )
        )
        chunks = list(
            await session.scalars(
                select(ChunkResult).where(ChunkResult.review_job_id == review_id)
            )
        )
    assert review is not None and review.status in {"COMPLETED", "PARTIAL"}
    assert len(contexts) > 1 and len(contexts) == len(chunks) == len(calls)
    assert len(source_reads) == len(sources)
    assert all(request.url.params["ref"] == "a" * 40 for request in source_reads)
    assert (
        review.coverage is not None and review.coverage["context_quality"] == "degraded"
    )
    for context in contexts:
        body = context.payload_body
        assert body["coverage"]["context_quality"] == "degraded"
        assert not body["related_symbols"]
        assert all(
            not file["source_windows"] and file["whole_file"] is None
            for file in body["files"]
        )
