"""Public CLI runtime preserves the existing durable analysis handoff."""

# ruff: noqa: PLR2004 -- fixed provider usage and durable-state expectations

import json
import os
import socket
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from redis.asyncio import Redis
from redis.asyncio.connection import Connection
from sqlalchemy import select, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_analysis_gateway import OUTPUT
from test_analysis_handler import github_http
from test_analysis_terminal import (  # noqa: F401 -- register ledger cleanup fixture
    detail,
    freeze,
    ledger_cleanup,
)
from test_postgres_integration import PostgresHarness
from test_runtime_manifest import manifest_values, write_manifest

from app.adapters.outbox_dispatcher import PostgresOutboxDispatcher
from app.adapters.redis_queue import RedisQueue
from app.analysis_worker import run
from app.application.ports import QueueTaskEnvelope
from app.core.config import get_settings
from app.db.models import OutboxEvent, ReviewJob, ReviewLlmAttempt
from app.db.worker_tasks import PostgresWorkerTasks
from app.domain.frozen_configuration import FrozenConfigurationError

pytest_plugins = ("test_analysis_handler",)
STREAM = "dmc268:review-tasks"


@pytest_asyncio.fixture
async def runtime_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, redis_client: Redis
) -> AsyncIterator[None]:
    url = make_url(os.environ["DMC268_TEST_DATABASE_URL"])
    environment = {
        "POSTGRES__HOST": url.host or "localhost",
        "POSTGRES__PORT": str(url.port or 5432),
        "POSTGRES__USER": url.username or "",
        "POSTGRES__PASSWORD": url.password or "",
        "POSTGRES__DB": url.database or "",
        "REDIS__URL": os.environ["DMC268_TEST_REDIS_URL"],
        "ANALYSIS__CONFIG_PATH": str(write_manifest(tmp_path, manifest_values())),
        "LLM__EUROROUTER__API_KEYS": "synthetic-test-key",
        "LLM__EUROROUTER__MODEL": "wrong-current-model",
        "LLM__PROMPT_VERSION": "wrong-current-prompt",
        "LOGGING__FILE_ENABLED": "false",
        "NO_PROXY": "*",
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()
        await redis_client.delete(f"{STREAM}:analyze", f"{STREAM}:publish")


@pytest.mark.usefixtures("runtime_environment")
async def test_once_preserves_frozen_execution_and_durable_proof_before_ack(  # noqa: PLR0915 -- inspect debit and handoff at external edges
    postgres: PostgresHarness,
    redis_client: Redis,
    review_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = async_sessionmaker(postgres.engine, expire_on_commit=False)
    await freeze(factory, review_id)
    queue = RedisQueue(redis_client)
    assert await PostgresOutboxDispatcher(factory, queue).dispatch_batch() == 1
    entries = await redis_client.xrange(f"{STREAM}:analyze")
    assert entries is not None and len(entries) == 1
    message_id, fields = entries[0]
    assert isinstance(message_id, str) and fields is not None
    task = QueueTaskEnvelope(**json.loads(fields["envelope"]))
    calls: list[dict[str, Any]] = []
    transports: list[httpx.MockTransport] = []
    closed: list[httpx.MockTransport] = []
    github = github_http()

    async def respond(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.github.com":
            assert request.method == "GET"
            return await github.handle_async_request(request)
        assert request.url.host == "llm.invalid"
        assert request.headers["authorization"] == "Bearer synthetic-test-key"
        calls.append(json.loads(request.content))
        # A separate PostgreSQL connection must see the debit before inference.
        async with factory() as session:
            attempt = (
                await session.scalars(
                    select(ReviewLlmAttempt).where(
                        ReviewLlmAttempt.review_job_id == review_id
                    )
                )
            ).one()
            assert attempt.state == "RESERVED"
            assert attempt.model_ref == "test-model"
            assert attempt.reserved_input_tokens > 0
            assert attempt.reserved_output_tokens == 2000
        return httpx.Response(
            200,
            json={
                "model": "test-model",
                "choices": [{"message": {"content": json.dumps(OUTPUT)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 40},
            },
        )

    class TrackingTransport(httpx.MockTransport):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(respond)
            transports.append(self)

        async def aclose(self) -> None:
            closed.append(self)
            await super().aclose()

    monkeypatch.setattr("httpx._client.AsyncHTTPTransport", TrackingTransport)
    original_send = Connection.send_command
    acknowledged: list[str] = []

    async def check_ack(connection: Connection, *args: Any, **kwargs: Any) -> None:
        if args[0] == "XACK":
            assert args[1:] == (f"{STREAM}:analyze", "review-workers", message_id)
            assert await PostgresWorkerTasks(factory).completed(task)
            view = await detail(factory, review_id)
            assert view["status"] == "COMPLETED"
            assert view["publication_status"] == "PENDING"
            assert view["progress"] == {"completed_chunks": 1, "planned_chunks": 1}
            assert view["summary"] == OUTPUT["summary"]
            acknowledged.append(message_id)
        await original_send(connection, *args, **kwargs)

    monkeypatch.setattr(Connection, "send_command", check_ack)
    await run(once=True, poll_interval=1)
    assert len(calls) == 1
    assert calls[0]["model"] == "test-model"
    assert calls[0]["temperature"] == 0
    assert calls[0]["response_format"]["type"] == "json_schema"
    assert acknowledged == [message_id]
    assert (await redis_client.xpending(f"{STREAM}:analyze", "review-workers"))[
        "pending"
    ] == 0
    assert await redis_client.xlen(f"{STREAM}:publish") == 0
    async with factory() as session:
        attempt = (
            await session.scalars(
                select(ReviewLlmAttempt).where(
                    ReviewLlmAttempt.review_job_id == review_id
                )
            )
        ).one()
        assert attempt.state == "SUCCEEDED"
        assert (attempt.actual_input_tokens, attempt.actual_output_tokens) == (100, 40)
        publish = (
            await session.scalars(
                select(OutboxEvent).where(
                    OutboxEvent.review_job_id == review_id,
                    OutboxEvent.task_kind == "publish",
                )
            )
        ).one()
        assert publish.broker_published_at is None
        assert publish.broker_message_id is None
        assert publish.dispatch_not_before is None
    assert len(transports) == 2
    assert set(closed) == set(transports)


@pytest.mark.usefixtures("runtime_environment")
async def test_debug_http_failure_never_logs_transport_exception_repr(
    postgres: PostgresHarness,
    redis_client: Redis,
    review_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    factory = async_sessionmaker(postgres.engine, expire_on_commit=False)
    await freeze(factory, review_id)
    dispatcher = PostgresOutboxDispatcher(factory, RedisQueue(redis_client))
    assert await dispatcher.dispatch_batch() == 1
    monkeypatch.setenv("LOGGING__LEVEL", "DEBUG")
    monkeypatch.setenv("LOGGING__FORMAT", "json")
    get_settings.cache_clear()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    transports: list[httpx.AsyncHTTPTransport] = []
    closed: list[httpx.AsyncHTTPTransport] = []
    failures: list[str] = []

    class LocalFailureTransport(httpx.AsyncHTTPTransport):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            transports.append(self)

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            assert request.url.host == "api.github.com"
            # Exercise actual httpcore logging, but permit only a loopback wire call.
            request.url = httpx.URL(f"http://127.0.0.1:{port}/controlled-http-failure")
            try:
                return await super().handle_async_request(request)
            except httpx.ConnectError:
                failures.append("ConnectError")
                raise

        async def __aexit__(self, *args: Any) -> None:
            closed.append(self)
            await super().__aexit__(*args)

    monkeypatch.setattr("httpx._client.AsyncHTTPTransport", LocalFailureTransport)
    await run(once=True, poll_interval=1)
    assert failures == ["ConnectError"]
    assert len(transports) == 2
    assert set(closed) == set(transports)
    output = capfd.readouterr().out
    assert "exception=" not in output
    assert "ConnectError(" not in output


@pytest.mark.parametrize("defect", ["credentials", "frozen-prompt"])
@pytest.mark.usefixtures("runtime_environment")
async def test_preparation_failure_closes_http_without_receive_or_provider_call(
    postgres: PostgresHarness,
    redis_client: Redis,
    review_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
    defect: str,
) -> None:
    factory = async_sessionmaker(postgres.engine, expire_on_commit=False)
    await freeze(factory, review_id)
    if defect == "credentials":
        monkeypatch.delenv("LLM__EUROROUTER__API_KEYS")
    else:
        async with factory.begin() as session:
            await session.execute(
                update(ReviewJob)
                .where(ReviewJob.id == review_id)
                .values(prompt_digest="0" * 64)
            )
    transports: list[httpx.MockTransport] = []
    closed: list[httpx.MockTransport] = []

    def forbidden(request: httpx.Request) -> httpx.Response:
        raise AssertionError("startup failure must precede external HTTP")

    class TrackingTransport(httpx.MockTransport):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(forbidden)
            transports.append(self)

        async def aclose(self) -> None:
            closed.append(self)
            await super().aclose()

    monkeypatch.setattr("httpx._client.AsyncHTTPTransport", TrackingTransport)
    before = {row["id"] for row in await redis_client.client_list()}
    with pytest.raises(FrozenConfigurationError):
        await run(once=True, poll_interval=1)
    assert await redis_client.exists(f"{STREAM}:analyze") == 0
    assert {row["id"] for row in await redis_client.client_list()} == before
    assert len(transports) == 2
    assert set(closed) == set(transports)
