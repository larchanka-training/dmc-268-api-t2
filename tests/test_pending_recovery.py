"""Stage 8C-02: bounded Redis recovery through the existing PostgreSQL worker."""

import asyncio
import json
import os
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from redis.asyncio import Redis
from redis.exceptions import ConnectionError, NoPermissionError, TimeoutError
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_postgres_integration import PostgresHarness
from test_review_worker import (
    SkipAnalysis,
    redis_without_command,
)

from app.adapters.redis_queue import InvalidQueueMessageError, RedisQueue
from app.adapters.review_worker import PostgresReviewWorker
from app.application.ports import QueueTaskEnvelope
from app.db.task_leases import PostgresTaskLeaseStore

pytest_plugins = ("test_review_worker",)


async def test_reclaimed_delivery_respects_competing_postgres_lease(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    task: QueueTaskEnvelope,
) -> None:
    factory = async_sessionmaker(postgres.engine)
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    message_id = await queue.enqueue(task)
    assert await queue.receive(task_kind="analyze", consumer_name="crashed")
    leases = PostgresTaskLeaseStore(factory)
    lease = await leases.acquire(
        uuid.UUID(task.review_id), owner="live", task_kind="analyze"
    )
    assert lease is not None
    handler = SkipAnalysis(postgres)
    worker = PostgresReviewWorker(
        factory, queue, handler, owner="recovery", task_kind="analyze"
    )
    try:
        cursor, acked = await worker.recover_once(cursor="0-0", min_idle_time=0)
        assert cursor == "0-0" and acked is False
        assert handler.claims == []
        pending = await redis_client.xpending_range(
            f"{stream_prefix}:analyze", "review-workers", "-", "+", 10
        )
        assert [(row["message_id"], row["consumer"]) for row in pending] == [
            (message_id, "recovery")
        ]
    finally:
        assert await leases.release(lease)


async def test_worker_does_not_claim_pending_while_execution_slot_is_busy(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    task: QueueTaskEnvelope,
) -> None:
    entered, release, attempted = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class HeldAnalysis(SkipAnalysis):
        async def handle(self, *args: Any) -> Any:
            entered.set()
            await release.wait()
            return await super().handle(*args)

    class ObservedQueue(RedisQueue):
        async def reclaim(self, **kwargs: Any) -> Any:
            attempted.set()
            return await super().reclaim(**kwargs)

    queue = ObservedQueue(redis_client, stream_prefix=stream_prefix)
    await queue.enqueue(task)
    handler = HeldAnalysis(postgres)
    worker = PostgresReviewWorker(
        handler.factory, queue, handler, owner="one-slot", task_kind="analyze"
    )
    first = asyncio.create_task(worker.run_once())
    recovery = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        recovery = asyncio.create_task(
            worker.recover_once(cursor="0-0", min_idle_time=0)
        )
        # FIFO loop barrier: recovery has run to its first suspension point.
        turn = asyncio.get_running_loop().create_future()
        asyncio.get_running_loop().call_soon(turn.set_result, None)
        await turn
        assert not attempted.is_set(), "busy worker must not claim a batch into memory"
        release.set()
        assert await first
        assert await recovery == ("0-0", False)
        assert attempted.is_set()
    finally:
        release.set()
        first.cancel()
        if recovery is not None:
            recovery.cancel()
            await asyncio.gather(first, recovery, return_exceptions=True)
        else:
            await asyncio.gather(first, return_exceptions=True)


async def test_recovery_cursor_passes_identity_mismatch_to_next_pending_delivery(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    task: QueueTaskEnvelope,
) -> None:
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    bad_id = await queue.enqueue(replace(task, trace_id="not-authoritative"))
    good_id = await queue.enqueue(task)
    for _ in range(2):
        assert await queue.receive(task_kind="analyze", consumer_name="crashed")
    handler = SkipAnalysis(postgres)
    worker = PostgresReviewWorker(
        handler.factory, queue, handler, owner="recover", task_kind="analyze"
    )
    cursor, acked = await worker.recover_once(cursor="0-0", min_idle_time=0)
    assert cursor == good_id and not acked
    assert handler.claims == []
    cursor, acked = await worker.recover_once(cursor=cursor, min_idle_time=0)
    assert cursor == "0-0" and acked
    assert len(handler.claims) == 1
    pending = await redis_client.xpending_range(
        f"{stream_prefix}:analyze", "review-workers", "-", "+", 10
    )
    assert [row["message_id"] for row in pending] == [bad_id]
    assert await redis_client.xlen(f"{stream_prefix}:dead") == 0


@pytest.mark.parametrize(
    "fields,reason",
    [
        (
            {"envelope": "private-source:not-json", "prompt": "private-prompt"},
            "INVALID_ENVELOPE",
        ),
        (
            {"envelope": '{"schema_version":2,"source":"private-source"}'},
            "UNSUPPORTED_SCHEMA_VERSION",
        ),
        (
            {
                "envelope": (
                    '{"schema_version":1,"task_kind":"unknown",'
                    '"source":"private-source"}'
                )
            },
            "UNKNOWN_TASK_KIND",
        ),
        (
            {
                "envelope": QueueTaskEnvelope(
                    1, "event", "review", "publish", 1, "trace"
                ).to_json()
            },
            "INVALID_ENVELOPE",
        ),
        ({"missing": "private-source"}, "INVALID_ENVELOPE"),
        ({"envelope": "x" * 16385}, "INVALID_ENVELOPE"),
        ({"envelope": b"\xff", b"\xfe": b"\xfd"}, "INVALID_ENVELOPE"),
    ],
)
async def test_invalid_reclaimed_envelope_uses_safe_dlq_before_source_ack(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    fields: dict[Any, Any],
    reason: str,
) -> None:

    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    message_id = await redis_client.xadd(f"{stream_prefix}:analyze", fields)
    with pytest.raises(InvalidQueueMessageError) as rejected:
        await queue.receive(task_kind="analyze", consumer_name="crashed")
    assert rejected.value.message_id == message_id
    handler = SkipAnalysis(postgres)
    worker = PostgresReviewWorker(
        handler.factory, queue, handler, owner="recovery", task_kind="analyze"
    )
    assert await worker.recover_once(cursor="0-0", min_idle_time=0) == ("0-0", True)
    assert handler.claims == []
    dead = await redis_client.xrange(f"{stream_prefix}:dead")
    assert dead is not None and len(dead) == 1
    assert "private-" not in str(dead)
    assert dead[0][1] is not None
    payload = json.loads(dead[0][1]["envelope"])
    assert payload["reason_code"] == reason
    Draft202012Validator(
        json.loads(Path("schemas/queue/dead/v1.json").read_text()),
        format_checker=FormatChecker(),
    ).validate(payload)
    assert (await redis_client.xpending(f"{stream_prefix}:analyze", "review-workers"))[
        "pending"
    ] == 0
    assert await redis_client.xlen(f"{stream_prefix}:analyze") == 1


@pytest.mark.parametrize("failure", ["xadd", "xack", "timeout", "connection"])
async def test_failed_recovery_quarantine_keeps_original_delivery_pending(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    failure: str,
) -> None:
    message_id = await redis_client.xadd(
        f"{stream_prefix}:analyze", {"envelope": "private-provider-response:not-json"}
    )
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    with pytest.raises(InvalidQueueMessageError) as rejected:
        await queue.receive(task_kind="analyze", consumer_name="crashed")
    assert rejected.value.message_id == message_id
    handler = SkipAnalysis(postgres)

    async def recover(client: Redis) -> None:
        worker = PostgresReviewWorker(
            handler.factory,
            RedisQueue(client, stream_prefix=stream_prefix),
            handler,
            owner="recovery",
            task_kind="analyze",
        )
        with pytest.raises((NoPermissionError, TimeoutError, ConnectionError)):
            await worker.recover_once(cursor="0-0", min_idle_time=0)

    if failure in {"xadd", "xack"}:
        async with redis_without_command(
            redis_client, stream_prefix, failure
        ) as client:
            await recover(client)
    else:
        # External SDK transport fault; all non-DLQ commands hit real Redis.
        class FailingRedis(Redis):
            async def execute_command(self, *args: Any, **kwargs: Any) -> Any:
                if args[:2] == ("XADD", f"{stream_prefix}:dead"):
                    raise (
                        TimeoutError("transport timeout")
                        if failure == "timeout"
                        else ConnectionError("transport disconnect")
                    )
                return await super().execute_command(*args, **kwargs)  # type: ignore[no-untyped-call]

        async with FailingRedis.from_url(
            os.environ["DMC268_TEST_REDIS_URL"], decode_responses=True
        ) as client:
            await recover(client)
    assert handler.claims == []
    pending = await redis_client.xpending_range(
        f"{stream_prefix}:analyze", "review-workers", "-", "+", 10
    )
    assert [(row["message_id"], row["consumer"]) for row in pending] == [
        (message_id, "recovery")
    ]
    assert await redis_client.xlen(f"{stream_prefix}:dead") == (
        1 if failure == "xack" else 0
    )
    healthy = PostgresReviewWorker(
        handler.factory, queue, handler, owner="healthy", task_kind="analyze"
    )
    assert await healthy.recover_once(cursor="0-0", min_idle_time=0) == ("0-0", True)
    assert (await redis_client.xpending(f"{stream_prefix}:analyze", "review-workers"))[
        "pending"
    ] == 0
    assert "private-" not in str(await redis_client.xrange(f"{stream_prefix}:dead"))


async def test_recovery_returns_cursor_after_invalid_envelope_quarantine(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    task: QueueTaskEnvelope,
) -> None:
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    bad = await redis_client.xadd(
        f"{stream_prefix}:analyze", {"missing": "private-source"}
    )
    good = await queue.enqueue(task)
    with pytest.raises(InvalidQueueMessageError):
        await queue.receive(task_kind="analyze", consumer_name="crashed")
    assert await queue.receive(task_kind="analyze", consumer_name="crashed")
    handler = SkipAnalysis(postgres)
    worker = PostgresReviewWorker(
        handler.factory, queue, handler, owner="recovery", task_kind="analyze"
    )
    cursor, acked = await worker.recover_once(cursor="0-0", min_idle_time=0)
    assert acked and cursor == good
    assert handler.claims == []
    assert await worker.recover_once(cursor=cursor, min_idle_time=0) == ("0-0", True)
    assert len(handler.claims) == 1
    assert (await redis_client.xpending(f"{stream_prefix}:analyze", "review-workers"))[
        "pending"
    ] == 0
    remaining = await redis_client.xrange(f"{stream_prefix}:analyze")
    assert remaining is not None and remaining[0][0] == bad
