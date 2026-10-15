from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass
from dataclasses import fields as dataclass_fields
from datetime import UTC, datetime
from typing import cast

from redis.asyncio import Redis
from redis.client import NEVER_DECODE
from redis.exceptions import ResponseError

from app.application.ports import (
    MAX_QUEUE_ENVELOPE_BYTES,
    QueueClaim,
    QueueDelivery,
    QueueEnvelopeReason,
    QueueEnvelopeValidationError,
    QueueTaskEnvelope,
    TaskKind,
)

_DEAD_RETENTION_SECONDS = 7 * 24 * 60 * 60
type _StreamMessage = tuple[str | bytes, dict[str | bytes, str | bytes]]
type _ReadResponse = list[tuple[str | bytes, list[_StreamMessage]]]
type DeadReason = QueueEnvelopeReason


@dataclass(frozen=True, slots=True)
class _SourceIdentity:
    source_event_id: str | None = None
    review_id: str | None = None
    trace_id: str | None = None
    attempt: int | None = None
    source_schema_version: int | None = None


def _nonempty_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _positive_integer(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value < 1 or (isinstance(value, float) and not value.is_integer()):
        return None
    return int(value)


def _source_identity(serialized: str | bytes | None) -> _SourceIdentity:
    if serialized is None:
        return _SourceIdentity()
    encoded = serialized.encode("utf-8") if isinstance(serialized, str) else serialized
    if len(encoded) > MAX_QUEUE_ENVELOPE_BYTES:
        return _SourceIdentity()
    try:
        payload = json.loads(serialized)
    except ValueError:
        return _SourceIdentity()
    if not isinstance(payload, dict):
        return _SourceIdentity()
    return _SourceIdentity(
        source_event_id=_nonempty_string(payload.get("event_id")),
        review_id=_nonempty_string(payload.get("review_id")),
        trace_id=_nonempty_string(payload.get("trace_id")),
        attempt=_positive_integer(payload.get("attempt")),
        source_schema_version=_positive_integer(payload.get("schema_version")),
    )


class InvalidQueueMessageError(ValueError):
    """Safe rejection metadata; the original message remains pending."""

    def __init__(  # noqa: PLR0913 -- safe rejection identity and optional scan cursor
        self,
        message: str,
        *,
        message_id: str,
        source_routing_key: TaskKind,
        reason_code: DeadReason = "INVALID_ENVELOPE",
        source_identity: _SourceIdentity | None = None,
        next_cursor: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message_id = message_id
        self.source_routing_key = source_routing_key
        self.reason_code = reason_code
        self.source_identity = source_identity or _SourceIdentity()
        self.next_cursor = next_cursor


def _decode_envelope(serialized: str | bytes) -> QueueTaskEnvelope:
    encoded = serialized.encode("utf-8") if isinstance(serialized, str) else serialized
    if len(encoded) > MAX_QUEUE_ENVELOPE_BYTES:
        raise QueueEnvelopeValidationError(
            "INVALID_ENVELOPE", "Queue task envelope exceeds 16 KiB"
        )
    try:
        payload = json.loads(serialized)
    except ValueError:
        raise QueueEnvelopeValidationError("INVALID_ENVELOPE") from None
    if isinstance(payload, dict):
        version = _positive_integer(payload.get("schema_version"))
        if version is not None and version != 1:
            raise QueueEnvelopeValidationError("UNSUPPORTED_SCHEMA_VERSION")
        task_kind = payload.get("task_kind")
        if (
            isinstance(task_kind, str)
            and task_kind
            and task_kind not in ("analyze", "publish")
        ):
            raise QueueEnvelopeValidationError("UNKNOWN_TASK_KIND")
    required = {field.name for field in dataclass_fields(QueueTaskEnvelope)}
    if not isinstance(payload, dict) or not required <= payload.keys():
        raise QueueEnvelopeValidationError("INVALID_ENVELOPE")
    if (
        type(payload["schema_version"]) not in (int, float)
        or payload["schema_version"] != 1
        or payload["task_kind"] not in ("analyze", "publish")
    ):
        raise QueueEnvelopeValidationError("INVALID_ENVELOPE")
    attempt = payload["attempt"]
    if (
        type(attempt) not in (int, float)
        or attempt < 1
        or (isinstance(attempt, float) and not attempt.is_integer())
    ):
        raise QueueEnvelopeValidationError("INVALID_ENVELOPE")
    if any(
        not isinstance(payload[name], str) or not payload[name]
        for name in ("event_id", "review_id", "trace_id")
    ):
        raise QueueEnvelopeValidationError("INVALID_ENVELOPE")
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
        payload = envelope.to_json()
        canonical = _decode_envelope(payload)
        payload = canonical.to_json()
        message_id = await self._client.xadd(
            f"{self._stream_prefix}:{canonical.task_kind}", {"envelope": payload}
        )
        return message_id.decode() if isinstance(message_id, bytes) else message_id

    async def receive(
        self, *, task_kind: TaskKind, consumer_name: str
    ) -> QueueDelivery | None:
        """Raise InvalidQueueMessageError with pending work for explicit quarantine."""
        if task_kind not in ("analyze", "publish"):
            raise ValueError("Unsupported source task channel")
        stream = f"{self._stream_prefix}:{task_kind}"
        await self._ensure_group(stream)
        # Decode only Redis IDs, never untrusted fields in the transport parser.
        messages = await self._client.execute_command(  # type: ignore[no-untyped-call]
            "XREADGROUP",
            "GROUP",
            self._group_name,
            consumer_name,
            "COUNT",
            1,
            "STREAMS",
            stream,
            ">",
            **{NEVER_DECODE: True},
        )
        if not messages:
            return None
        # Default Redis responses for new messages, without CLAIM or tombstones.
        message_id, fields = cast(_ReadResponse, messages)[0][1][0]
        return self._delivery(message_id, fields, task_kind)

    async def reclaim(
        self, *, consumer_name: str, cursor: str, min_idle_time: int = 90_000
    ) -> QueueClaim:
        """One XAUTOCLAIM, one analyze delivery; no polling or completion inference."""
        await self._ensure_group(f"{self._stream_prefix}:analyze")
        # execute_command may retry an ambiguous claim after a lost response.
        # Borrow one exclusive connection and send once, retaining Redis parsing.
        pool = self._client.connection_pool
        connection = await pool.get_connection()  # type: ignore[no-untyped-call]
        try:
            await connection.send_command(
                "XAUTOCLAIM",
                f"{self._stream_prefix}:analyze",
                self._group_name,
                consumer_name,
                min_idle_time,
                cursor,
                "COUNT",
                1,
            )
            response = await self._client.parse_response(
                connection, "XAUTOCLAIM", **{NEVER_DECODE: True}
            )
        except BaseException:
            # Never return an unread/ambiguous response to another pool borrower.
            await connection.disconnect()
            raise
        finally:
            await pool.release(connection)
        next_cursor = response[0]
        next_cursor = (
            next_cursor.decode() if isinstance(next_cursor, bytes) else next_cursor
        )
        messages = cast(list[_StreamMessage], response[1])
        return QueueClaim(
            cursor=next_cursor,
            delivery=self._delivery(*messages[0], "analyze", next_cursor=next_cursor)
            if messages
            else None,
            deleted_ids=tuple(
                item.decode() if isinstance(item, bytes) else item
                for item in response[2]
            ),
        )

    async def _ensure_group(self, stream: str) -> None:
        try:
            await self._client.xgroup_create(
                stream, self._group_name, id="0", mkstream=True
            )
        except ResponseError as error:
            if not str(error).startswith("BUSYGROUP "):
                raise

    def _delivery(
        self,
        message_id: str | bytes,
        fields: dict[str | bytes, str | bytes],
        task_kind: TaskKind,
        *,
        next_cursor: str | None = None,
    ) -> QueueDelivery:
        serialized = fields.get("envelope", fields.get(b"envelope"))
        try:
            if serialized is None:
                raise ValueError("Invalid queue task envelope")
            envelope = _decode_envelope(serialized)
            if envelope.task_kind != task_kind:
                raise ValueError("Queue task kind does not match channel")
        except ValueError as error:
            raise InvalidQueueMessageError(
                str(error),
                message_id=message_id.decode()
                if isinstance(message_id, bytes)
                else message_id,
                source_routing_key=task_kind,
                reason_code=error.reason_code
                if isinstance(error, QueueEnvelopeValidationError)
                else "INVALID_ENVELOPE",
                source_identity=_source_identity(serialized),
                next_cursor=next_cursor,
            ) from None
        return QueueDelivery(
            message_id=message_id.decode()
            if isinstance(message_id, bytes)
            else message_id,
            envelope=envelope,
        )

    async def quarantine(self, rejected: InvalidQueueMessageError) -> str:
        """Write safe dead v1 then ACK its source; propagate any Redis failure.

        A missing source_event_id marks diagnostic-only metadata, never replay work.
        Failure after XADD may produce another dead entry on retry (at-least-once).
        """
        if rejected.source_routing_key not in ("analyze", "publish"):
            raise ValueError("Unsupported source task channel")
        await self.trim_dead_letters()
        payload = {
            "schema_version": 1,
            "quarantine_id": str(uuid.uuid4()),
            "reason_code": rejected.reason_code,
            "quarantined_at": datetime.now(UTC).isoformat(),
            "source_routing_key": rejected.source_routing_key,
            **{
                name: value
                for name, value in asdict(rejected.source_identity).items()
                if value is not None
            },
        }
        message_id = await self._client.xadd(
            f"{self._stream_prefix}:dead",
            {"envelope": json.dumps(payload, separators=(",", ":"))},
        )
        await self._client.xack(
            f"{self._stream_prefix}:{rejected.source_routing_key}",
            self._group_name,
            rejected.message_id,
        )
        return message_id.decode() if isinstance(message_id, bytes) else message_id

    async def trim_dead_letters(self) -> int:
        """Remove entries older than seven days; also call periodically when idle."""
        seconds, microseconds = await self._client.time()
        cutoff_ms = (seconds - _DEAD_RETENTION_SECONDS) * 1000 + microseconds // 1000
        return await self._client.xtrim(
            f"{self._stream_prefix}:dead", minid=f"{cutoff_ms}-0", approximate=False
        )

    async def ack(self, delivery: QueueDelivery) -> bool:
        acknowledged = await self._client.xack(
            f"{self._stream_prefix}:{delivery.envelope.task_kind}",
            self._group_name,
            delivery.message_id,
        )
        return acknowledged == 1
