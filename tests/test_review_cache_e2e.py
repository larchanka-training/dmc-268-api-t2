"""Exact review cache through the analysis worker (DoD 3, 4, 8) and Redis."""

# ruff: noqa: PLR2004 -- literal call counts and line numbers

import asyncio
import uuid
from dataclasses import replace
from typing import Any

import pytest
from redis.asyncio import Redis
from sqlalchemy import select
from test_analysis_handler import DeterministicLlm
from test_incremental_review import FIRST_HEAD, SECOND_HEAD, _Cycles
from test_postgres_integration import PostgresHarness

from app.application.ports import ReviewChunkResult, TokenUsage
from app.db.models import ChunkResult, Finding
from app.review.cache import ReviewCachePolicy, ReviewResultCache, cache_identity
from app.vcs.diff import parse_diff

pytest_plugins = ("test_incremental_review",)


def _diff(start: int, *, neighbour: str = "x = 0") -> str:
    return (
        "diff --git a/src/a.py b/src/a.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/src/a.py\n"
        "+++ b/src/a.py\n"
        f"@@ -{start},2 +{start},2 @@\n"
        f" {neighbour}\n"
        "-return value\n"
        "+return value / 0\n"
    )


def _policy(**changes: Any) -> ReviewCachePolicy:
    """A fresh key namespace per test, short-lived in the shared test Redis."""
    return ReviewCachePolicy(
        key_version=uuid.uuid4().int % 10**9 + 1, ttl_seconds=60, **changes
    )


def _cycles(
    postgres: PostgresHarness, redis: Redis, prefix: str, cache_redis: Redis
) -> _Cycles:
    cycles = _Cycles(postgres, redis, prefix)
    cycles.incremental = None  # every cycle sends its whole diff
    cycles.cache = ReviewResultCache(cache_redis, _policy())
    return cycles


def _with_finding(cycles: _Cycles, line: int) -> None:
    finding = replace(
        DeterministicLlm().result.findings[0],
        path="src/a.py",
        start_line=line,
        end_line=line,
    )
    cycles.llm.result = replace(cycles.llm.result, findings=[finding])


async def _finding_lines(cycles: _Cycles, review: uuid.UUID) -> list[tuple[int, int]]:
    async with cycles.factory() as session:
        rows = await session.execute(
            select(Finding.start_line, Finding.end_line)
            .join(ChunkResult)
            .where(ChunkResult.review_job_id == review)
        )
        return [(row.start_line, row.end_line) for row in rows]


async def test_same_fragment_in_another_commit_is_served_from_cache(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    review_id: uuid.UUID,
) -> None:
    """DoD 3: 0 LLM calls; the cached finding moves with its fragment."""
    cycles = _cycles(postgres, redis_client, stream_prefix, redis_client)
    try:
        _with_finding(cycles, 11)
        await cycles.run(_diff(10), FIRST_HEAD)
        second = await cycles.push(review_id, SECOND_HEAD)
        await cycles.run(_diff(70), SECOND_HEAD)
        events = cycles.events

        assert len(cycles.llm.payloads) == 1
        job = await cycles.job(second)
        assert job.status == "COMPLETED"
        assert await _finding_lines(cycles, second) == [(71, 71)]
        assert [e["kind"] for e in events if e.get("metric") == "cache_hit"] == [
            "exact"
        ]
        saved = [e for e in events if e.get("metric") == "llm_tokens_saved_total"]
        assert saved[0]["value"] == 140  # DeterministicLlm usage 100 + 40
    finally:
        await cycles.purge()


@pytest.mark.parametrize(
    ("diff", "changes"),
    [
        (_diff(10, neighbour="x = 1"), {}),  # changed context
        (_diff(10), {"prompt_digest": "prompt-digest-v2"}),  # changed prompt
        (_diff(10), {"model_ref": "other-model"}),  # changed model
    ],
    ids=["context", "prompt", "model"],
)
async def test_changed_context_model_or_prompt_misses_the_cache(  # noqa: PLR0913, PLR0917 -- explicit fixture and parameter boundaries
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    review_id: uuid.UUID,
    diff: str,
    changes: dict[str, str],
) -> None:
    """DoD 4: the same fragment under another context or configuration."""
    cycles = _cycles(postgres, redis_client, stream_prefix, redis_client)
    try:
        await cycles.run(_diff(10), FIRST_HEAD)
        await cycles.push(review_id, SECOND_HEAD, **changes)
        await cycles.run(diff, SECOND_HEAD)

        assert len(cycles.llm.payloads) == 2
    finally:
        await cycles.purge()


async def test_review_completes_without_cache_when_redis_is_down(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    review_id: uuid.UUID,
) -> None:
    """DoD 8: Redis unavailable for the cache → fail-open, review completes."""
    dead = Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2)
    cycles = _cycles(postgres, redis_client, stream_prefix, dead)
    try:
        await cycles.run(_diff(10), FIRST_HEAD)
        events = cycles.events

        assert len(cycles.llm.payloads) == 1
        assert (await cycles.job(review_id)).status == "COMPLETED"
        assert any(e.get("metric") == "cache_error" for e in events)
    finally:
        await dead.aclose()
        await cycles.purge()


def _result() -> ReviewChunkResult:
    return ReviewChunkResult(2, "Summary", [], TokenUsage(10, 5), 3, [])


def _body() -> dict[str, Any]:
    return {
        "metadata": {"output_language": "ru"},
        "files": parse_diff(_diff(10)).files,
        "related_symbols": [],
    }


async def test_concurrent_identical_requests_call_the_llm_once(
    redis_client: Redis,
) -> None:
    """Single-flight: one caller computes, the other waits for its result."""
    cache = ReviewResultCache(redis_client, _policy(poll_interval_seconds=0.02))
    calls = 0

    async def call() -> ReviewChunkResult:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.2)
        return _result()

    results = await asyncio.gather(
        *(cache.review(_body(), "digest", call, lambda _: True) for _ in range(3))
    )

    assert calls == 1
    assert {result.summary for result in results} == {"Summary"}


async def test_invalid_results_are_never_cached(redis_client: Redis) -> None:
    policy = _policy()
    cache = ReviewResultCache(redis_client, policy)
    calls = 0

    async def call() -> ReviewChunkResult:
        nonlocal calls
        calls += 1
        return _result()

    for _ in range(2):
        await cache.review(_body(), "digest", call, lambda _: False)

    assert calls == 2
    key, _ = cache_identity(_body(), "digest", version=policy.key_version)
    assert await redis_client.exists(key) == 0  # nothing was ever stored
    assert await redis_client.exists(f"{key}:lock") == 0  # lock released
