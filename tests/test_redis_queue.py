from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, cast

import pytest
import pytest_asyncio
from jsonschema import Draft202012Validator
from redis.asyncio import Redis
from redis.typing import EncodableT, FieldT

from app.adapters.redis_queue import RedisQueue
from app.application.ports import QueueTaskEnvelope, TaskKind

_REDIS_URL = os.environ.get("DMC268_TEST_REDIS_URL")
if not _REDIS_URL:
    pytest.skip(
        "set DMC268_TEST_REDIS_URL to run Redis queue integration tests",
        allow_module_level=True,
    )


@pytest_asyncio.fixture
async def redis_client() -> AsyncIterator[Redis]:
    assert _REDIS_URL is not None
    async with Redis.from_url(_REDIS_URL, decode_responses=True) as client:
        yield client


@pytest_asyncio.fixture
async def stream_prefix(redis_client: Redis) -> AsyncIterator[str]:
    prefix = f"dmc268:test:{uuid.uuid4().hex}"
    try:
        yield prefix
    finally:
        await redis_client.delete(
            f"{prefix}:analyze", f"{prefix}:publish", f"{prefix}:unknown"
        )


def envelope(task_kind: TaskKind = "analyze") -> QueueTaskEnvelope:
    return QueueTaskEnvelope(
        schema_version=1,
        event_id="event_123",
        review_id="rev_123",
        task_kind=task_kind,
        attempt=1,
        trace_id="trace_123",
    )


@pytest.mark.parametrize("task_kind", ["analyze", "publish"])
async def test_enqueue_confirms_canonical_envelope_in_separate_task_channels(
    redis_client: Redis, stream_prefix: str, task_kind: TaskKind
) -> None:
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)

    message_id = await queue.enqueue(envelope(task_kind))

    messages = await redis_client.xrange(f"{stream_prefix}:{task_kind}")
    assert messages is not None
    assert len(messages) == 1
    assert messages[0][0] == message_id
    fields = messages[0][1]
    assert fields is not None
    payload = json.loads(fields["envelope"])
    assert payload == {
        "schema_version": 1,
        "event_id": "event_123",
        "review_id": "rev_123",
        "task_kind": task_kind,
        "attempt": 1,
        "trace_id": "trace_123",
    }
    schema_path = Path(__file__).parents[1] / "schemas/queue/task-envelope.v1.json"
    Draft202012Validator(json.loads(schema_path.read_text())).validate(payload)


@pytest.mark.parametrize("task_kind", ["analyze", "publish"])
async def test_received_task_stays_pending_without_ack_and_is_not_given_as_new_work(
    redis_client: Redis, stream_prefix: str, task_kind: TaskKind
) -> None:
    queue = RedisQueue(
        redis_client, stream_prefix=stream_prefix, group_name="review-workers"
    )
    task = envelope(task_kind)
    message_id = await queue.enqueue(task)

    delivery = await queue.receive(task_kind=task_kind, consumer_name="worker-1")

    assert delivery is not None
    assert delivery.message_id == message_id
    assert delivery.envelope == task
    # A new adapter/consumer must neither ACK nor silently reclaim this task.
    next_consumer = RedisQueue(
        redis_client, stream_prefix=stream_prefix, group_name="review-workers"
    )
    assert (
        await next_consumer.receive(task_kind=task_kind, consumer_name="worker-2")
        is None
    )
    pending = await redis_client.xpending_range(
        f"{stream_prefix}:{task_kind}", "review-workers", "-", "+", 10
    )
    assert len(pending) == 1
    assert pending[0]["message_id"] == message_id
    assert pending[0]["consumer"] == "worker-1"


@pytest.mark.parametrize(
    "stream_fields",
    [
        {"envelope": "private-source: not JSON"},
        {"envelope": "[]"},
        {"envelope": "{}"},
        {"other": "missing envelope"},
    ],
)
async def test_malformed_transport_payload_is_rejected_with_safe_error(
    redis_client: Redis, stream_prefix: str, stream_fields: dict[str, str]
) -> None:
    message_id = await redis_client.xadd(
        f"{stream_prefix}:analyze",
        cast(dict[FieldT, EncodableT], stream_fields),
    )
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)

    with pytest.raises(ValueError, match=r"^Invalid queue task envelope$"):
        await queue.receive(task_kind="analyze", consumer_name="worker-1")

    pending = await redis_client.xpending_range(
        f"{stream_prefix}:analyze", "review-workers", "-", "+", 10
    )
    assert [entry["message_id"] for entry in pending] == [message_id]


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("schema_version", 2),
        ("schema_version", True),
        ("task_kind", "unknown"),
        ("attempt", 0),
        ("attempt", True),
        ("attempt", "1"),
        ("event_id", ""),
        ("review_id", None),
        ("trace_id", 123),
    ],
)
async def test_consumer_rejects_invalid_envelopes_and_keeps_them_unacknowledged(
    redis_client: Redis, stream_prefix: str, field: str, invalid_value: object
) -> None:
    payload = {**asdict(envelope()), field: invalid_value, "source": "private-source"}
    message_id = await redis_client.xadd(
        f"{stream_prefix}:analyze", {"envelope": json.dumps(payload)}
    )
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)

    with pytest.raises(ValueError, match=r"^Invalid queue task envelope$"):
        await queue.receive(task_kind="analyze", consumer_name="worker-1")

    pending = await redis_client.xpending_range(
        f"{stream_prefix}:analyze", "review-workers", "-", "+", 10
    )
    assert [entry["message_id"] for entry in pending] == [message_id]


async def test_explicit_ack_removes_pending_work_and_repeated_ack_returns_false(
    redis_client: Redis, stream_prefix: str
) -> None:
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    await queue.enqueue(envelope())
    delivery = await queue.receive(task_kind="analyze", consumer_name="worker-1")
    assert delivery is not None

    assert await queue.ack(delivery) is True

    pending = await redis_client.xpending(f"{stream_prefix}:analyze", "review-workers")
    assert pending["pending"] == 0
    assert await queue.ack(delivery) is False


async def test_consumer_ignores_unknown_optional_v1_fields(
    redis_client: Redis, stream_prefix: str
) -> None:
    task = envelope()
    payload = {**asdict(task), "future_optional_field": {"value": "ignored"}}
    await redis_client.xadd(
        f"{stream_prefix}:analyze", {"envelope": json.dumps(payload)}
    )
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)

    delivery = await queue.receive(task_kind="analyze", consumer_name="worker-1")

    assert delivery is not None
    assert delivery.envelope == task


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [("schema_version", 2), ("task_kind", "unknown"), ("attempt", 0)],
)
async def test_producer_rejects_invalid_envelope_before_enqueue(
    redis_client: Redis, stream_prefix: str, field: str, invalid_value: object
) -> None:
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    task = replace(envelope(), **{field: cast(Any, invalid_value)})

    with pytest.raises(ValueError, match=r"^Invalid queue task envelope$"):
        await queue.enqueue(task)

    assert (
        await redis_client.exists(
            f"{stream_prefix}:analyze", f"{stream_prefix}:unknown"
        )
        == 0
    )


@pytest.mark.parametrize("operation", ["enqueue", "receive"])
async def test_envelope_size_limit_is_16_kib_of_utf8_on_both_boundaries(
    redis_client: Redis, stream_prefix: str, operation: str
) -> None:
    empty = replace(envelope(), trace_id="")
    overhead = len(json.dumps(asdict(empty), separators=(",", ":")).encode())
    available = 16_384 - overhead
    task = replace(empty, trace_id="я" * (available // 2) + "x" * (available % 2))
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)

    await queue.enqueue(task)
    delivery = await queue.receive(task_kind="analyze", consumer_name="worker-1")
    assert delivery is not None
    assert delivery.envelope == task

    oversized = replace(task, trace_id=task.trace_id + "x")
    with pytest.raises(ValueError, match=r"^Queue task envelope exceeds 16 KiB$"):
        if operation == "enqueue":
            await queue.enqueue(oversized)
        else:
            await redis_client.xadd(
                f"{stream_prefix}:analyze",
                {
                    "envelope": json.dumps(
                        asdict(oversized), ensure_ascii=False, separators=(",", ":")
                    )
                },
            )
            await queue.receive(task_kind="analyze", consumer_name="worker-1")

    pending = await redis_client.xpending(f"{stream_prefix}:analyze", "review-workers")
    assert pending["pending"] == (1 if operation == "enqueue" else 2)


async def test_queue_supports_default_redis_client_byte_responses(
    redis_client: Redis, stream_prefix: str
) -> None:
    assert _REDIS_URL is not None
    async with Redis.from_url(_REDIS_URL) as raw_client:
        queue = RedisQueue(raw_client, stream_prefix=stream_prefix)
        message_id = await queue.enqueue(envelope())

        delivery = await queue.receive(task_kind="analyze", consumer_name="worker-1")

        assert delivery is not None
        assert delivery.message_id == message_id
        assert delivery.envelope == envelope()
        assert await queue.ack(delivery) is True
    pending = await redis_client.xpending(f"{stream_prefix}:analyze", "review-workers")
    assert pending["pending"] == 0


async def test_consumer_rejects_task_kind_that_does_not_match_its_channel(
    redis_client: Redis, stream_prefix: str
) -> None:
    message_id = await redis_client.xadd(
        f"{stream_prefix}:analyze",
        {"envelope": json.dumps(asdict(envelope("publish")))},
    )
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)

    with pytest.raises(ValueError, match=r"^Queue task kind does not match channel$"):
        await queue.receive(task_kind="analyze", consumer_name="worker-1")

    pending = await redis_client.xpending_range(
        f"{stream_prefix}:analyze", "review-workers", "-", "+", 10
    )
    assert [entry["message_id"] for entry in pending] == [message_id]


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(b"\xff", id="invalid-utf8"),
        pytest.param(
            b'{"schema_version":1,"event_id":"event_123","review_id":"rev_123",'
            b'"task_kind":"analyze","attempt":'
            + b"9" * 5000
            + b',"trace_id":"trace_123"}',
            id="overlong-json-integer",
        ),
    ],
)
async def test_decode_errors_are_safe_and_leave_message_pending_with_byte_client(
    stream_prefix: str, payload: bytes
) -> None:
    assert _REDIS_URL is not None
    async with Redis.from_url(_REDIS_URL, decode_responses=False) as raw_client:
        message_id = await raw_client.xadd(
            f"{stream_prefix}:analyze", {"envelope": payload}
        )
        queue = RedisQueue(raw_client, stream_prefix=stream_prefix)

        with pytest.raises(ValueError, match=r"^Invalid queue task envelope$"):
            await queue.receive(task_kind="analyze", consumer_name="worker-1")

        pending = await raw_client.xpending_range(
            f"{stream_prefix}:analyze", "review-workers", "-", "+", 10
        )
        assert [entry["message_id"] for entry in pending] == [message_id]
        assert pending[0]["consumer"] == b"worker-1"
