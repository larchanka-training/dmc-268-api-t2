"""Chaos: LLM API failures — timeout and 429 exhaustion — end to end.

Every scenario drives the production seams (Redis queue, PostgreSQL attempt
accounting, frozen execution factory) and only scripts the provider transport.
"""

# ruff: noqa: PLR2004 -- literal ledger and attempt counts

import json
import random
import uuid
from collections import defaultdict, deque
from datetime import timedelta

import httpx
import pytest
from pydantic import BaseModel, ConfigDict, SecretStr
from redis.asyncio import Redis
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_analysis_gateway import OUTPUT, analysis_task
from test_analysis_handler import github_http
from test_analysis_pending_recovery import snapshot
from test_analysis_recovery import production_handler
from test_analysis_terminal import freeze, ledger_cleanup  # noqa: F401 -- fixture
from test_postgres_integration import PostgresHarness

from app.adapters.outbox_dispatcher import PostgresOutboxDispatcher
from app.adapters.redis_queue import RedisQueue
from app.adapters.review_worker import PostgresReviewWorker
from app.core.config import LLM, Eurorouter, Ollama
from app.db.models import OutboxEvent, ReviewEvent, ReviewJob
from app.llm.budget import BudgetLimits, RunBudget
from app.llm.client import ChatMessage, LLMRequest
from app.llm.providers import build_gateway

pytest_plugins = ("test_analysis_handler",)

pytestmark = pytest.mark.chaos


async def _redrive(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    review_id: uuid.UUID,
    task_id: str,
) -> None:
    """Process-death recovery: stream gone, retry and dispatch windows elapsed."""
    factory = async_sessionmaker(postgres.engine, expire_on_commit=False)
    await redis_client.delete(f"{stream_prefix}:analyze")
    async with factory.begin() as session:
        await session.execute(
            update(ReviewJob)
            .where(ReviewJob.id == review_id)
            .values(retry_at=func.clock_timestamp() - timedelta(seconds=1))
        )
        await session.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == uuid.UUID(task_id))
            .values(dispatch_not_before=func.clock_timestamp() - timedelta(seconds=1))
        )
    assert (
        await PostgresOutboxDispatcher(
            factory, RedisQueue(redis_client, stream_prefix=stream_prefix)
        ).recover_batch()
        == 1
    )


async def test_llm_timeout_defers_retry_then_recovery_completes_exactly_once(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    review_id: uuid.UUID,
) -> None:
    factory = async_sessionmaker(postgres.engine, expire_on_commit=False)
    await freeze(factory, review_id)
    task = await analysis_task(factory, review_id)
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    assert await PostgresOutboxDispatcher(factory, queue).dispatch_batch() == 1

    calls: list[httpx.Request] = []

    def hanging_provider(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ReadTimeout("chaos: provider never answers")

    # Pass 1: every LLM attempt times out -> attempt is ledgered, retry deferred,
    # the original delivery stays unacknowledged.
    async with (
        httpx.AsyncClient(transport=github_http()) as github,
        httpx.AsyncClient(transport=httpx.MockTransport(hanging_provider)) as http,
    ):
        worker = PostgresReviewWorker(
            factory,
            queue,
            production_handler(factory, github, http),
            owner="chaos-llm-timeout",
            task_kind="analyze",
        )
        assert await worker.run_once() is False

    after_timeout = await snapshot(factory, review_id)
    job = after_timeout["review_job"][0]
    assert job["finished_at"] is None
    assert job["analysis_attempts"] == 1
    assert job["retry_at"] is not None
    assert after_timeout["chunk_result"] == []
    assert after_timeout["publication"][0]["status"] == "NOT_READY"
    ledger = after_timeout["review_llm_attempt"]
    # One ledger row per HTTP attempt; both timed out inside the first run.
    assert [row["state"] for row in ledger] == ["ERROR", "ERROR"]
    assert {row["error_code"] for row in ledger} == {"provider_timeout"}
    assert len(calls) == 2
    pending = await redis_client.xpending(f"{stream_prefix}:analyze", "review-workers")
    assert pending["pending"] == 1

    # Pass 2: provider recovered -> recovery re-drives the same review once.
    async with (
        httpx.AsyncClient(transport=github_http()) as github,
        httpx.AsyncClient(transport=paid_provider(calls)) as http,
    ):
        await _redrive(postgres, redis_client, stream_prefix, review_id, task.event_id)
        worker = PostgresReviewWorker(
            factory,
            queue,
            production_handler(factory, github, http),
            owner="chaos-llm-timeout-recovery",
            task_kind="analyze",
        )
        assert await worker.run_once() is True

    llm_posts_after_recovery = len(calls)
    after = await snapshot(factory, review_id)
    job = after["review_job"][0]
    assert job["status"] == "COMPLETED" and job["finished_at"] is not None
    assert job["analysis_attempts"] == 2
    assert len(after["chunk_result"]) == 1
    assert after["publication"][0]["status"] == "PENDING"
    assert (
        len([row for row in after["outbox_event"] if row["task_kind"] == "publish"])
        == 1
    )
    ledger = after["review_llm_attempt"]
    assert sorted(row["state"] for row in ledger) == ["ERROR", "ERROR", "SUCCEEDED"]
    failed_tokens = [row["fencing_token"] for row in ledger if row["state"] == "ERROR"]
    succeeded = next(row for row in ledger if row["state"] == "SUCCEEDED")
    assert succeeded["fencing_token"] > max(failed_tokens)

    # Terminal duplicate: redelivery ACKs with zero provider HTTP and no changes.
    before_duplicate = await snapshot(factory, review_id)
    await queue.enqueue(task)
    async with (
        httpx.AsyncClient(transport=github_http()) as github,
        httpx.AsyncClient(transport=forbidden_provider(calls)) as http,
    ):
        duplicate = PostgresReviewWorker(
            factory,
            queue,
            production_handler(factory, github, http),
            owner="chaos-llm-timeout-duplicate",
            task_kind="analyze",
        )
        assert await duplicate.run_once() is True
    assert await snapshot(factory, review_id) == before_duplicate
    assert await redis_client.xlen(f"{stream_prefix}:publish") == 0
    # Exactly the timed-out pairs plus one paid call; the duplicate added none.
    assert llm_posts_after_recovery == 3 and len(calls) == 3


async def test_llm_rate_limited_everywhere_exhausts_to_terminal_failure(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    review_id: uuid.UUID,
) -> None:
    factory = async_sessionmaker(postgres.engine, expire_on_commit=False)
    await freeze(factory, review_id)
    task = await analysis_task(factory, review_id)
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    assert await PostgresOutboxDispatcher(factory, queue).dispatch_batch() == 1

    calls: list[httpx.Request] = []

    def always_rate_limited(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "1"})

    # Three worker attempts (policy.retry.worker_max_attempts), all rate limited.
    for owner in ("chaos-429-one", "chaos-429-two", "chaos-429-three"):
        async with (
            httpx.AsyncClient(transport=github_http()) as github,
            httpx.AsyncClient(
                transport=httpx.MockTransport(always_rate_limited)
            ) as http,
        ):
            worker = PostgresReviewWorker(
                factory,
                queue,
                production_handler(factory, github, http),
                owner=owner,
                task_kind="analyze",
            )
            await worker.run_once()
        if owner != "chaos-429-three":
            attempt = await snapshot(factory, review_id)
            assert attempt["review_job"][0]["finished_at"] is None
            assert attempt["review_job"][0]["retry_at"] is not None
            await _redrive(
                postgres, redis_client, stream_prefix, review_id, task.event_id
            )

    after = await snapshot(factory, review_id)
    job = after["review_job"][0]
    assert job["status"] == "FAILED" and job["finished_at"] is not None
    assert job["analysis_attempts"] == 3
    assert after["chunk_result"] == []
    assert after["publication"][0]["status"] == "NOT_READY"
    assert not [row for row in after["outbox_event"] if row["task_kind"] == "publish"]
    ledger = after["review_llm_attempt"]
    assert {row["error_code"] for row in ledger} == {"rate_limited"}
    assert all(row["state"] == "ERROR" for row in ledger)
    reasons = await _review_reasons(postgres, review_id)
    assert "LLM_UNAVAILABLE" in reasons

    # Terminal duplicate: ACK without provider HTTP and without state changes.
    before_duplicate = await snapshot(factory, review_id)
    await queue.enqueue(task)
    async with (
        httpx.AsyncClient(transport=github_http()) as github,
        httpx.AsyncClient(transport=forbidden_provider(calls)) as http,
    ):
        duplicate = PostgresReviewWorker(
            factory,
            queue,
            production_handler(factory, github, http),
            owner="chaos-429-duplicate",
            task_kind="analyze",
        )
        assert await duplicate.run_once() is True
    assert await snapshot(factory, review_id) == before_duplicate
    assert await redis_client.xlen(f"{stream_prefix}:publish") == 0


async def _review_reasons(postgres: PostgresHarness, review_id: uuid.UUID) -> set[str]:
    async with async_sessionmaker(postgres.engine)() as session:
        rows = await session.scalars(
            select(ReviewEvent.reason_code).where(
                ReviewEvent.review_job_id == review_id
            )
        )
        return {reason for reason in rows if reason is not None}


def paid_provider(calls: list[httpx.Request]) -> httpx.MockTransport:
    def reply(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "model": "test-model",
                "choices": [{"message": {"content": json.dumps(OUTPUT)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 40},
            },
        )

    return httpx.MockTransport(reply)


def forbidden_provider(calls: list[httpx.Request]) -> httpx.MockTransport:
    def forbidden(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise AssertionError("terminal duplicate must not use provider HTTP")

    return httpx.MockTransport(forbidden)


# ---------------------------------------------------------------------------
# Gateway-level: unparseable Retry-After must degrade to the standard backoff
# cooldown instead of crashing or poisoning the cooldown clock.
# ---------------------------------------------------------------------------


class _Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: str


class _ScriptedProvider:
    """Responses queued per bearer key behind one MockTransport."""

    def __init__(self) -> None:
        self.script: dict[str, deque[httpx.Response | Exception]] = defaultdict(deque)
        self.calls: list[str] = []

    def queue(self, key: str, *responses: httpx.Response | Exception) -> None:
        self.script[key].extend(responses)

    def handler(self, request: httpx.Request) -> httpx.Response:
        key = request.headers["authorization"].removeprefix("Bearer ")
        self.calls.append(key)
        if self.script[key]:
            item = self.script[key].popleft()
            if isinstance(item, Exception):
                raise item
            return item
        return httpx.Response(
            200,
            json={
                "model": "euro",
                "choices": [{"message": {"content": '{"verdict": "ok"}'}}],
            },
        )


class _Sleeper:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def _gateway_settings() -> LLM:
    return LLM(
        primary_provider="eurorouter",
        fallback_enabled=True,
        eurorouter=Eurorouter(
            base_url="https://euro.example/v1",
            model="euro",
            api_keys=[SecretStr("key-one"), SecretStr("key-two")],
        ),
        ollama=Ollama(base_url="http://ollama:11434", model="qwen"),
    )


@pytest.mark.parametrize("retry_after", ["Tue, 24 Jun 2026 10:00:00 GMT", "soon", ""])
async def test_llm_429_with_unparseable_retry_after_degrades_to_backoff_cooldown(
    retry_after: str,
) -> None:
    provider = _ScriptedProvider()
    sleeper = _Sleeper()
    clock = _Clock()
    provider.queue("key-one", httpx.Response(429, headers={"Retry-After": retry_after}))
    gateway = build_gateway(
        _gateway_settings(),
        httpx.AsyncClient(transport=httpx.MockTransport(provider.handler)),
        sleep=sleeper,
        rng=random.Random(0),
        clock=clock,
    )
    request = LLMRequest(
        messages=[ChatMessage(role="user", content="diff")],
        max_output_tokens=2000,
        timeout_s=120,
    )
    budget = RunBudget(
        BudgetLimits(
            max_requests=8,
            max_input_tokens=80_000,
            max_output_tokens=16_000,
            deadline_s=600,
        ),
        clock=clock,
    )

    result = await gateway.generate_structured(
        request, _Answer, budget=budget, input_tokens=1000
    )

    assert result.provider == "eurorouter"
    # key-one is dropped for its (backoff-sized) cooldown, key-two answers.
    assert provider.calls == ["key-one", "key-two"]
    # A short clock advance makes key-one usable again: no 30s-style cooldown
    # was invented from the unparseable header.
    clock.now += 5
    await gateway.generate_structured(
        request, _Answer, budget=budget, input_tokens=1000
    )
    assert provider.calls == ["key-one", "key-two", "key-one"]
