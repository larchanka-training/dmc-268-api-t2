"""Exact Redis delivery evidence, independent of application timestamps."""

import json
import os
from dataclasses import asdict, replace
from typing import Any

import pytest
from redis.asyncio import Redis
from redis.asyncio.connection import Connection, ConnectionPool
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from redis.exceptions import TimeoutError

from app.adapters.redis_queue import RedisQueue
from app.application.ports import QueueTaskEnvelope

pytest_plugins = ("test_redis_queue",)


async def test_probe_distinguishes_exact_present_missing_and_foreign_identity(
    redis_client: Redis,
    stream_prefix: str,
) -> None:
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    task = QueueTaskEnvelope(1, "event", "review", "analyze", 1, "trace")
    message_id = await queue.enqueue(task)
    assert await queue.probe(task, message_id) == "PRESENT"
    assert (
        await queue.probe(replace(task, trace_id="foreign"), message_id) == "UNTRUSTED"
    )
    await redis_client.xdel(f"{stream_prefix}:analyze", message_id)
    assert await queue.probe(task, message_id) == "ABSENT"


async def test_probe_outage_is_unknown_without_fabricating_absence() -> None:

    task = QueueTaskEnvelope(1, "event", "review", "analyze", 1, "trace")
    async with Redis(
        host="127.0.0.1",
        port=1,
        socket_connect_timeout=0.1,
        socket_timeout=0.1,
        retry=Retry(NoBackoff(), 0),
    ) as unavailable:
        assert await RedisQueue(unavailable).probe(task, "123-0") == "UNKNOWN"


async def test_one_enqueue_never_retries_xadd_after_real_response_loss(
    redis_client: Redis,
    stream_prefix: str,
) -> None:

    sent = []
    dropped = False

    class DropSendResponse(Connection):
        send_response = False

        async def send_command(self, *args: Any, **kwargs: Any) -> None:
            await super().send_command(*args, **kwargs)
            self.send_response = args[0] == "XADD"
            if self.send_response:
                sent.append(args)

        async def read_response(self, *args: Any, **kwargs: Any) -> Any:
            nonlocal dropped
            response = await super().read_response(*args, **kwargs)
            if self.send_response:
                self.send_response = False
                if not dropped:
                    dropped = True
                    raise TimeoutError("lost XADD response")
            return response

    pool = ConnectionPool.from_url(
        os.environ["DMC268_TEST_REDIS_URL"],
        connection_class=DropSendResponse,
        retry=Retry(NoBackoff(), 1),
        max_connections=1,
    )
    async with Redis.from_pool(pool) as client:
        try:
            await RedisQueue(client, stream_prefix=stream_prefix).enqueue(
                QueueTaskEnvelope(1, "event", "review", "analyze", 1, "trace")
            )
        except TimeoutError:
            pass
        else:
            pytest.fail(f"response timeout hidden; XADD sent {len(sent)} times")
        assert len(sent) == 1
        assert await client.ping()
    assert await redis_client.xlen(f"{stream_prefix}:analyze") == 1


async def test_probe_recreates_missing_group_without_new_delivery(
    redis_client: Redis,
    stream_prefix: str,
) -> None:
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    task = QueueTaskEnvelope(1, "event", "review", "analyze", 1, "trace")
    message_id = await queue.enqueue(task)
    assert await queue.receive(task_kind="analyze", consumer_name="old")
    await redis_client.xgroup_destroy(f"{stream_prefix}:analyze", "review-workers")
    assert await queue.probe(task, message_id) == "PRESENT"
    groups = await redis_client.xinfo_groups(f"{stream_prefix}:analyze")
    assert len(groups) == 1 and groups[0]["last-delivered-id"] == "0-0"
    assert await redis_client.xlen(f"{stream_prefix}:analyze") == 1


async def test_transport_expiry_uses_redis_time_and_future_id_never_expires(
    redis_client: Redis,
    stream_prefix: str,
) -> None:
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    task = QueueTaskEnvelope(1, "event", "review", "analyze", 1, "trace")
    seconds, micros = await redis_client.time()
    now_ms = seconds * 1000 + micros // 1000
    old_id = await redis_client.xadd(
        f"{stream_prefix}:analyze",
        {"envelope": task.to_json()},
        id=f"{now_ms - 1_200_001}-0",
    )
    assert isinstance(old_id, str)
    assert await queue.probe(task, old_id) == "EXPIRED"
    future_id = await redis_client.xadd(
        f"{stream_prefix}:analyze",
        {"envelope": task.to_json()},
        id=f"{now_ms + 60_000}-0",
    )
    assert isinstance(future_id, str)
    assert await queue.probe(task, future_id) == "PRESENT"


async def test_indeterminate_probe_response_is_unknown_not_absent(
    stream_prefix: str,
) -> None:

    class IndeterminateRedis(Redis):
        async def execute_command(self, *args: Any, **options: Any) -> Any:
            if args[0] == "XRANGE":
                return None
            return await super().execute_command(*args, **options)  # type: ignore[no-untyped-call]

    async with IndeterminateRedis.from_url(
        os.environ["DMC268_TEST_REDIS_URL"]
    ) as client:
        task = QueueTaskEnvelope(1, "event", "review", "analyze", 1, "trace")
        assert (
            await RedisQueue(client, stream_prefix=stream_prefix).probe(task, "123-0")
            == "UNKNOWN"
        )


async def test_probe_rejects_noncanonical_extra_payload_without_ack_or_dlq(
    redis_client: Redis,
    stream_prefix: str,
) -> None:
    task = QueueTaskEnvelope(1, "event", "review", "analyze", 1, "trace")
    message_id = await redis_client.xadd(
        f"{stream_prefix}:analyze",
        {"envelope": json.dumps(asdict(task) | {"prompt": "private-source"})},
    )
    assert isinstance(message_id, str)
    assert (
        await RedisQueue(redis_client, stream_prefix=stream_prefix).probe(
            task, message_id
        )
        == "UNTRUSTED"
    )
    assert await redis_client.xlen(f"{stream_prefix}:analyze") == 1
    assert await redis_client.xlen(f"{stream_prefix}:dead") == 0
