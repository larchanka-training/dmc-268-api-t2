"""Execution preparation rejects credentials before reading Redis or spending."""

import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_analysis_terminal import freeze
from test_postgres_integration import PostgresHarness
from test_runtime_manifest import PROMPTS, manifest_values, write_manifest

from app.adapters import analysis_composition
from app.adapters.outbox_dispatcher import PostgresOutboxDispatcher
from app.adapters.redis_queue import RedisQueue
from app.db.models import ReviewEvent, ReviewJob, ReviewLlmAttempt, TaskLease
from app.domain.frozen_configuration import FrozenConfigurationError
from app.review.configuration import load_runtime_configuration
from app.vcs.github import GithubClient

pytest_plugins = ("test_analysis_handler",)


@pytest.mark.parametrize("location", ["primary", "fallback", "history"])
@pytest.mark.parametrize(
    "credential",
    [None, "", "  ", "one,,two", "bad\nkey", "good key", "synthetic-test-key\n"],
)
async def test_preparation_refuses_credentials_before_receive(  # noqa: PLR0913, PLR0917
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    review_id: uuid.UUID,
    tmp_path: Path,
    location: str,
    credential: str | None,
) -> None:
    factory = async_sessionmaker(postgres.engine, expire_on_commit=False)
    await freeze(factory, review_id)
    values = manifest_values()
    if location != "primary":
        values["admission"]["primary"]["profile_ref"] = "ollama/review-v1"
        routing = values["admission"]["policy"]["routing"]
        routing["primary_profile_ref"] = "ollama/review-v1"
        if location == "fallback":
            routing["fallback"] = [
                {
                    "profile_ref": "eurorouter/review-v1",
                    "model_ref": "fallback-model",
                    "model_digest": None,
                }
            ]
    runtime = load_runtime_configuration(write_manifest(tmp_path, values))
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    await PostgresOutboxDispatcher(factory, queue).dispatch_batch()
    stream = f"{stream_prefix}:analyze"
    before = await redis_client.xrange(stream)
    calls = []

    def forbidden(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise AssertionError("startup must be offline")

    async def state() -> tuple[Any, ...]:
        async with factory() as session:
            job = await session.get(ReviewJob, review_id)
            assert job is not None
            counts = [
                await session.scalar(select(func.count()).select_from(table))
                for table in (TaskLease, ReviewLlmAttempt, ReviewEvent)
            ]
            return (job.status, job.analysis_attempts, job.started_at, *counts)

    original = await state()
    env = {} if credential is None else {"LLM__EUROROUTER__API_KEYS": credential}
    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as http:
        with pytest.raises(
            FrozenConfigurationError, match="invalid_execution_credentials"
        ):
            await analysis_composition.prepare_postgres_analysis_worker(
                factory,
                queue,
                runtime=runtime,
                environ=env,
                github=GithubClient(http),
                http=http,
                prompt_root=PROMPTS,
                schemas_dir=Path("schemas"),
                owner="preparation",
            )
    assert await state() == original
    assert await redis_client.xrange(stream) == before
    assert await redis_client.xinfo_groups(stream) == []
    assert calls == []


async def test_preparation_accepts_synthetic_execution_credentials_without_inference(
    postgres: PostgresHarness, redis_client: Redis, stream_prefix: str, tmp_path: Path
) -> None:
    runtime = load_runtime_configuration(write_manifest(tmp_path, manifest_values()))
    factory = async_sessionmaker(postgres.engine)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: pytest.fail("unexpected HTTP"))
    ) as http:
        worker = await analysis_composition.prepare_postgres_analysis_worker(
            factory,
            RedisQueue(redis_client, stream_prefix=stream_prefix),
            runtime=runtime,
            environ={"LLM__EUROROUTER__API_KEYS": "synthetic-test-key"},
            github=GithubClient(http),
            http=http,
            prompt_root=PROMPTS,
            schemas_dir=Path("schemas"),
            owner="prepared",
        )
        assert worker is not None
    assert not await redis_client.exists(f"{stream_prefix}:analyze")
