"""Continuous runtime schedules recovery at real Redis/time boundaries."""

# ruff: noqa: PLR2004 -- explicit schedule and protocol expectations

import asyncio
import json
import signal
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

import httpx
import pytest
from redis.asyncio import Redis
from redis.asyncio.connection import Connection
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import NoPermissionError
from redis.typing import EncodableT
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_analysis_gateway import analysis_task
from test_analysis_handler import github_http
from test_analysis_pending_recovery import snapshot
from test_analysis_recovery import paid_response, production_handler
from test_analysis_terminal import freeze, ledger_cleanup  # noqa: F401 -- fixture
from test_analysis_worker_execution import runtime_environment  # noqa: F401 -- fixture
from test_postgres_integration import PostgresHarness
from test_review_worker import redis_without_command

from app.adapters.outbox_dispatcher import PostgresOutboxDispatcher
from app.adapters.redis_queue import InvalidQueueMessageError, RedisQueue
from app.adapters.review_worker import PostgresReviewWorker
from app.analysis_worker import run
from app.db.models import ReviewLlmAttempt, TaskLease
from app.db.task_leases import PostgresTaskLeaseStore

pytest_plugins = ("test_analysis_handler",)
STREAM = "dmc268:review-tasks"


@pytest.mark.usefixtures("runtime_environment")
async def test_continuous_backlog_gets_one_bounded_recovery_per_eligible_tick(  # noqa: PLR0915 -- Redis setup and schedule observations
    postgres: PostgresHarness,
    redis_client: Redis,
    review_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = async_sessionmaker(postgres.engine, expire_on_commit=False)
    await freeze(factory, review_id)
    queue = RedisQueue(redis_client)
    await PostgresOutboxDispatcher(factory, queue).dispatch_batch()
    entries = await redis_client.xrange(f"{STREAM}:analyze")
    assert entries and entries[0][1]
    fields: dict[EncodableT, EncodableT] = {"envelope": str(entries[0][1]["envelope"])}
    assert isinstance(entries[0][0], str)
    ids = [entries[0][0]]
    for _ in range(11):
        message_id = await redis_client.xadd(f"{STREAM}:analyze", fields)
        assert isinstance(message_id, str)
        ids.append(message_id)
    for message_id in ids:
        assert await queue.receive(task_kind="analyze", consumer_name="crashed")
        if message_id not in ids[-2:]:
            continue
        await redis_client.xclaim(
            f"{STREAM}:analyze",
            "review-workers",
            "crashed",
            0,
            [message_id],
            idle=90_000,
        )
    leases = PostgresTaskLeaseStore(factory)
    lease = await leases.acquire(review_id, owner="healthy", task_kind="analyze")
    assert lease is not None
    # Continuous new backlog is independently quarantinable and always ACKed.
    for _ in range(6):
        await redis_client.xadd(f"{STREAM}:analyze", {"envelope": "invalid"})
    dead_before = await redis_client.xlen(f"{STREAM}:dead")
    clock = 0.0
    commands: list[tuple[Any, ...]] = []
    receives = 0
    original = Connection.send_command

    async def observe(connection: Connection, *args: Any, **kwargs: Any) -> None:
        nonlocal clock, receives
        if args[0] == "XAUTOCLAIM":
            commands.append(args)
        if args[0] == "XREADGROUP":
            commands.append(("new",))
            receives += 1
            # A long execution skips missed ticks; it must not queue catch-up claims.
            clock = (30.0, 30.0, 1000.0, 1030.0, 1060.0, 1060.0)[receives - 1]
            if receives == 6:
                signal.raise_signal(signal.SIGTERM)
                await asyncio.sleep(0.01)
        await original(connection, *args, **kwargs)

    # Clock is an external boundary; loop timers and lease clocks stay real.
    monkeypatch.setattr("app.analysis_worker.monotonic", lambda: clock, raising=False)
    monkeypatch.setattr(Connection, "send_command", observe)
    try:
        await asyncio.wait_for(run(once=False, poll_interval=0.01), 10)
    finally:
        await leases.release(lease)
    claims = [row for row in commands if row[0] == "XAUTOCLAIM"]
    assert len(claims) == 4
    assert [row[5] for row in claims] == ["0-0", ids[10], ids[11], "0-0"]
    for claim in claims:
        assert claim[1:3] == (f"{STREAM}:analyze", "review-workers")
        assert claim[4] == 90_000
        assert claim[6:] == ("COUNT", 1)
    assert [row[0] for row in commands] == [
        "new",
        "XAUTOCLAIM",
        "new",
        "new",
        "XAUTOCLAIM",
        "new",
        "XAUTOCLAIM",
        "new",
        "XAUTOCLAIM",
        "new",
    ]
    assert (await redis_client.xpending(f"{STREAM}:analyze", "review-workers"))[
        "pending"
    ] == 12
    assert await redis_client.xlen(f"{STREAM}:dead") == dead_before + 6


@pytest.mark.usefixtures("runtime_environment")
async def test_idle_poll_cannot_delay_the_next_recovery_tick(
    postgres: PostgresHarness,
    redis_client: Redis,
    review_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    await freeze(async_sessionmaker(postgres.engine), review_id)
    clock = 0.0
    delays: list[float] = []
    original_send = Connection.send_command
    original_timeout = asyncio.timeout

    async def observe(connection: Connection, *args: Any, **kwargs: Any) -> None:
        nonlocal clock
        if args[0] == "XREADGROUP":
            clock = 29.99
        await original_send(connection, *args, **kwargs)

    @asynccontextmanager
    async def timeout(delay: float) -> Any:
        delays.append(delay)
        signal.raise_signal(signal.SIGTERM)
        async with original_timeout(1):
            yield

    monkeypatch.setattr("app.analysis_worker.monotonic", lambda: clock)
    monkeypatch.setattr(Connection, "send_command", observe)
    monkeypatch.setattr(asyncio, "timeout", timeout)
    await run(once=False, poll_interval=60)
    assert delays == [pytest.approx(0.01)]


@pytest.mark.usefixtures("runtime_environment")
async def test_continuous_isolates_unknown_pg_identity_and_processes_next_delivery(
    postgres: PostgresHarness,
    redis_client: Redis,
    review_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    factory = async_sessionmaker(postgres.engine)
    await freeze(factory, review_id)
    await PostgresOutboxDispatcher(factory, RedisQueue(redis_client)).dispatch_batch()
    entries = await redis_client.xrange(f"{STREAM}:analyze")
    assert entries and entries[0][1]
    envelope = json.loads(entries[0][1]["envelope"])
    envelope["trace_id"] = "private-forged-identity"
    await redis_client.delete(f"{STREAM}:analyze")
    bad = await redis_client.xadd(
        f"{STREAM}:analyze", {"envelope": json.dumps(envelope)}
    )
    following = await redis_client.xadd(
        f"{STREAM}:analyze", {"envelope": "private-invalid-json"}
    )
    original = Connection.send_command
    acks = []

    async def observe(connection: Connection, *args: Any, **kwargs: Any) -> None:
        if args[0] == "XACK":
            acks.append(args[-1])
            signal.raise_signal(signal.SIGTERM)
            await asyncio.sleep(0.01)
        await original(connection, *args, **kwargs)

    monkeypatch.setattr(Connection, "send_command", observe)
    await asyncio.wait_for(run(once=False, poll_interval=0.01), 5)
    assert acks == [following]
    pending = await redis_client.xpending_range(
        f"{STREAM}:analyze", "review-workers", "-", "+", 10
    )
    assert [row["message_id"] for row in pending] == [bad]


@pytest.mark.usefixtures("runtime_environment")
async def test_continuous_retains_lost_lease_delivery_and_processes_next(
    postgres: PostgresHarness,
    redis_client: Redis,
    review_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    factory = async_sessionmaker(postgres.engine)
    await freeze(factory, review_id)
    await PostgresOutboxDispatcher(factory, RedisQueue(redis_client)).dispatch_batch()
    entries = await redis_client.xrange(f"{STREAM}:analyze")
    assert entries
    lost = entries[0][0]
    following = await redis_client.xadd(
        f"{STREAM}:analyze", {"envelope": "private-invalid-json"}
    )
    github = github_http()
    calls = []

    async def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        assert request.url.host == "api.github.com", "lease loss must prevent LLM"
        async with factory.begin() as session:
            await session.execute(
                update(TaskLease)
                .where(TaskLease.review_job_id == review_id)
                .values(expires_at=func.clock_timestamp() - timedelta(seconds=1))
            )
        return await github.handle_async_request(request)

    class Transport(httpx.MockTransport):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(respond)

    original = Connection.send_command
    acks = []

    async def observe(connection: Connection, *args: Any, **kwargs: Any) -> None:
        if args[0] == "XACK":
            acks.append(args[-1])
            signal.raise_signal(signal.SIGTERM)
            await asyncio.sleep(0.01)
        await original(connection, *args, **kwargs)

    monkeypatch.setattr("httpx._client.AsyncHTTPTransport", Transport)
    monkeypatch.setattr(Connection, "send_command", observe)
    await asyncio.wait_for(run(once=False, poll_interval=0.01), 5)
    assert calls and set(calls) == {"api.github.com"}
    assert acks == [following]
    pending = await redis_client.xpending_range(
        f"{STREAM}:analyze", "review-workers", "-", "+", 10
    )
    assert [row["message_id"] for row in pending] == [lost]
    async with factory() as session:
        assert (
            await session.scalar(select(func.count()).select_from(ReviewLlmAttempt))
            == 0
        )


@pytest.mark.usefixtures("runtime_environment")
async def test_stop_during_recovery_prevents_another_new_receive(
    postgres: PostgresHarness,
    redis_client: Redis,
    review_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    await freeze(async_sessionmaker(postgres.engine), review_id)
    queue = RedisQueue(redis_client)
    pending = await redis_client.xadd(f"{STREAM}:analyze", {"envelope": "invalid"})
    with pytest.raises(InvalidQueueMessageError):
        await queue.receive(task_kind="analyze", consumer_name="crashed")
    await redis_client.xclaim(
        f"{STREAM}:analyze", "review-workers", "crashed", 0, [pending], idle=90_000
    )
    await redis_client.xadd(f"{STREAM}:analyze", {"envelope": "invalid"})
    clock = 0.0
    receives = 0
    original = Connection.send_command

    async def observe(connection: Connection, *args: Any, **kwargs: Any) -> None:
        nonlocal clock, receives
        if args[0] == "XREADGROUP":
            receives += 1
            clock = 30.0
        if args[0] == "XACK" and args[-1] == pending:
            signal.raise_signal(signal.SIGTERM)
            await asyncio.sleep(0.01)
        await original(connection, *args, **kwargs)

    monkeypatch.setattr("app.analysis_worker.monotonic", lambda: clock)
    monkeypatch.setattr(Connection, "send_command", observe)
    await asyncio.wait_for(run(once=False, poll_interval=0.01), 5)
    assert receives == 1


@pytest.mark.usefixtures("runtime_environment")
@pytest.mark.parametrize("failed_command", ["XAUTOCLAIM", "XADD"])
async def test_continuous_recovery_infrastructure_failures_remain_visible(  # noqa: PLR0913 -- fixtures and failure boundary
    postgres: PostgresHarness,
    redis_client: Redis,
    review_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
    *,
    capsys: pytest.CaptureFixture[str],
    failed_command: str,
) -> None:
    await freeze(async_sessionmaker(postgres.engine), review_id)
    queue = RedisQueue(redis_client)
    pending = await redis_client.xadd(f"{STREAM}:analyze", {"envelope": "invalid"})
    with pytest.raises(InvalidQueueMessageError):
        await queue.receive(task_kind="analyze", consumer_name="crashed")
    await redis_client.xclaim(
        f"{STREAM}:analyze", "review-workers", "crashed", 0, [pending], idle=90_000
    )
    clock = 0.0
    original = Connection.send_command

    async def fail(connection: Connection, *args: Any, **kwargs: Any) -> None:
        nonlocal clock
        if args[0] == "XREADGROUP":
            clock = 30.0
        if args[0] == failed_command:
            raise RedisConnectionError("private-transport-payload")
        await original(connection, *args, **kwargs)

    monkeypatch.setattr("app.analysis_worker.monotonic", lambda: clock)
    monkeypatch.setattr(Connection, "send_command", fail)
    with pytest.raises(RedisConnectionError):
        await asyncio.wait_for(run(once=False, poll_interval=0.01), 5)
    output = capsys.readouterr()
    assert "private-" not in output.out + output.err
    assert "analysis_worker_recovery " not in output.out
    assert (await redis_client.xpending(f"{STREAM}:analyze", "review-workers"))[
        "pending"
    ] == 1


@pytest.mark.usefixtures("runtime_environment")
async def test_continuous_recovers_lost_ack_without_second_http_or_debit(
    postgres: PostgresHarness,
    redis_client: Redis,
    review_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    factory = async_sessionmaker(postgres.engine, expire_on_commit=False)
    await freeze(factory, review_id)
    queue = RedisQueue(redis_client)
    message_id = await queue.enqueue(await analysis_task(factory, review_id))
    calls: list[httpx.Request] = []
    async with (
        httpx.AsyncClient(transport=github_http()) as github,
        httpx.AsyncClient(transport=paid_response(calls)) as llm,
        redis_without_command(redis_client, STREAM, "xack") as denied,
    ):
        worker = PostgresReviewWorker(
            factory,
            RedisQueue(denied),
            production_handler(factory, github, llm),
            owner="crashed",
            task_kind="analyze",
        )
        with pytest.raises(NoPermissionError):
            await worker.run_once()
    committed = await snapshot(factory, review_id)
    assert len(calls) == len(committed["review_llm_attempt"]) == 1
    assert committed["review_job"][0]["status"] == "COMPLETED"
    await redis_client.xclaim(
        f"{STREAM}:analyze",
        "review-workers",
        "crashed",
        0,
        [message_id],
        idle=90_000,
    )
    clock = 0.0
    original = Connection.send_command
    acks = []

    async def observe(connection: Connection, *args: Any, **kwargs: Any) -> None:
        nonlocal clock
        if args[0] == "XREADGROUP":
            clock = 30.0
        if args[0] == "XACK":
            acks.append(args[-1])
            signal.raise_signal(signal.SIGTERM)
            await asyncio.sleep(0.01)
        await original(connection, *args, **kwargs)

    class ForbiddenHTTP(httpx.MockTransport):
        def __init__(self, **kwargs: Any) -> None:
            def forbidden(request: httpx.Request) -> httpx.Response:
                raise AssertionError("Completed recovery must never repeat HTTP")

            super().__init__(forbidden)

    monkeypatch.setattr("app.analysis_worker.monotonic", lambda: clock)
    monkeypatch.setattr(Connection, "send_command", observe)
    monkeypatch.setattr("httpx._client.AsyncHTTPTransport", ForbiddenHTTP)
    await asyncio.wait_for(run(once=False, poll_interval=0.01), 5)
    assert acks == [message_id]
    assert await snapshot(factory, review_id) == committed
    assert (await redis_client.xpending(f"{STREAM}:analyze", "review-workers"))[
        "pending"
    ] == 0
