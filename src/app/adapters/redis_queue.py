from __future__ import annotations

import json
from dataclasses import asdict
from dataclasses import fields as dataclass_fields
from typing import cast

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from app.application.ports import QueueDelivery, QueueTaskEnvelope, TaskKind

_MAX_ENVELOPE_BYTES = 16 * 1024
type _StreamMessage = tuple[str | bytes, dict[str | bytes, str | bytes]]
type _ReadResponse = list[tuple[str | bytes, list[_StreamMessage]]]


def _decode_envelope(serialized: str | bytes) -> QueueTaskEnvelope:
    encoded = serialized.encode("utf-8") if isinstance(serialized, str) else serialized
    if len(encoded) > _MAX_ENVELOPE_BYTES:
        raise ValueError("Queue task envelope exceeds 16 KiB")
    try:
        payload = json.loads(serialized)
    except ValueError:
        raise ValueError("Invalid queue task envelope") from None
    required = {field.name for field in dataclass_fields(QueueTaskEnvelope)}
    if not isinstance(payload, dict) or not required <= payload.keys():
        raise ValueError("Invalid queue task envelope")
    if (
        type(payload["schema_version"]) not in (int, float)
        or payload["schema_version"] != 1
        or payload["task_kind"] not in ("analyze", "publish")
    ):
        raise ValueError("Invalid queue task envelope")
    attempt = payload["attempt"]
    if (
        type(attempt) not in (int, float)
        or attempt < 1
        or (isinstance(attempt, float) and not attempt.is_integer())
    ):
        raise ValueError("Invalid queue task envelope")
    if any(
        not isinstance(payload[name], str) or not payload[name]
        for name in ("event_id", "review_id", "trace_id")
    ):
        raise ValueError("Invalid queue task envelope")
    payload["schema_version"] = 1
    payload["attempt"] = int(attempt)
    return QueueTaskEnvelope(
        **{
            field.name: payload[field.name]
            for field in dataclass_fields(QueueTaskEnvelope)
        }
    )


class RedisQueue:
    def __init__(
        self,
        client: Redis,
        *,
        stream_prefix: str = "dmc268:review-tasks",
        group_name: str = "review-workers",
    ) -> None:
        self._client = client
        self._stream_prefix = stream_prefix
        self._group_name = group_name

    async def enqueue(self, envelope: QueueTaskEnvelope) -> str:
        payload = json.dumps(
            asdict(envelope), ensure_ascii=False, separators=(",", ":")
        )
        canonical = _decode_envelope(payload)
        payload = json.dumps(
            asdict(canonical), ensure_ascii=False, separators=(",", ":")
        )
        message_id = await self._client.xadd(
            f"{self._stream_prefix}:{canonical.task_kind}", {"envelope": payload}
        )
        return message_id.decode() if isinstance(message_id, bytes) else message_id

    async def receive(
        self, *, task_kind: TaskKind, consumer_name: str
    ) -> QueueDelivery | None:
        stream = f"{self._stream_prefix}:{task_kind}"
        try:
            await self._client.xgroup_create(
                stream, self._group_name, id="0", mkstream=True
            )
        except ResponseError as error:
            if not str(error).startswith("BUSYGROUP "):
                raise
        messages = await self._client.xreadgroup(
            self._group_name, consumer_name, {stream: ">"}, count=1
        )
        if not messages:
            return None
        # Default Redis responses for new messages, without CLAIM or tombstones.
        message_id, fields = cast(_ReadResponse, messages)[0][1][0]
        serialized = fields.get("envelope", fields.get(b"envelope"))
        if serialized is None:
            raise ValueError("Invalid queue task envelope")
        envelope = _decode_envelope(serialized)
        if envelope.task_kind != task_kind:
            raise ValueError("Queue task kind does not match channel")
        return QueueDelivery(
            message_id=message_id.decode()
            if isinstance(message_id, bytes)
            else message_id,
            envelope=envelope,
        )

    async def ack(self, delivery: QueueDelivery) -> bool:
        acknowledged = await self._client.xack(
            f"{self._stream_prefix}:{delivery.envelope.task_kind}",
            self._group_name,
            delivery.message_id,
        )
        return acknowledged == 1
