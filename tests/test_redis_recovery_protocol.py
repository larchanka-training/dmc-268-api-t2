"""Redis protocol boundary: exact bounded command and transport failures."""

import asyncio
from typing import Any

import pytest
from redis.asyncio import Redis
from redis.asyncio.connection import AbstractConnection, Connection, ConnectionPool
from redis.exceptions import ConnectionError, ResponseError, TimeoutError

from app.adapters.redis_queue import RedisQueue
from app.application.ports import QueueTaskEnvelope


class ProtocolConnection(Connection):
    def __init__(self, response: Any, commands: list[tuple[Any, ...]]) -> None:
        super().__init__()
        self.response = response
        self.commands = commands
        self.disconnected = False

    async def send_command(self, *args: Any, **kwargs: Any) -> None:
        self.commands.append(args)

    async def read_response(self, *args: Any, **kwargs: Any) -> Any:
        assert kwargs == {"disable_decoding": True}
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response

    async def disconnect(self, *args: Any, **kwargs: Any) -> None:
        self.disconnected = True


class ProtocolPool(ConnectionPool):
    def __init__(self, response: Any, commands: list[tuple[Any, ...]]) -> None:
        super().__init__()
        self.wire = ProtocolConnection(response, commands)
        self.released = False

    async def get_connection(self, *args: Any, **kwargs: Any) -> ProtocolConnection:
        return self.wire

    async def release(self, connection: AbstractConnection) -> None:
        assert connection is self.wire
        self.released = True


class ProtocolRedis(Redis):
    def __init__(self, response: Any) -> None:
        self.commands: list[tuple[Any, ...]] = []
        self.pool = ProtocolPool(response, self.commands)
        super().__init__(connection_pool=self.pool)

    async def execute_command(self, *args: Any, **options: Any) -> Any:
        self.commands.append(args)
        assert args[0] == "XGROUP CREATE"
        raise ResponseError("BUSYGROUP Consumer Group name already exists")


@pytest.mark.parametrize("raw", [False, True])
async def test_reclaim_uses_one_full_delivery_command_with_production_policy(
    raw: bool,
) -> None:
    task = QueueTaskEnvelope(1, "event", "review", "analyze", 1, "trace")

    def wire(value: str) -> str | bytes:
        return value.encode() if raw else value

    client = ProtocolRedis(
        [
            wire("123-9"),
            [(wire("123-1"), [wire("envelope"), wire(task.to_json())])],
            [wire("123-0")],
        ]
    )
    claimed = await RedisQueue(client).reclaim(
        consumer_name="caller-owned", cursor="12-0"
    )
    assert client.commands == [
        (
            "XGROUP CREATE",
            "dmc268:review-tasks:analyze",
            "review-workers",
            "0",
            b"MKSTREAM",
        ),
        (
            "XAUTOCLAIM",
            "dmc268:review-tasks:analyze",
            "review-workers",
            "caller-owned",
            90_000,
            "12-0",
            "COUNT",
            1,
        ),
    ]
    assert claimed.cursor == "123-9" and claimed.deleted_ids == ("123-0",)
    assert claimed.delivery is not None and claimed.delivery.message_id == "123-1"
    assert claimed.delivery.envelope == task
    assert client.pool.released and not client.pool.wire.disconnected


@pytest.mark.parametrize(
    "error",
    [
        asyncio.CancelledError(),
        ConnectionError("offline"),
        TimeoutError("timeout"),
        ResponseError("NOGROUP reset during claim"),
    ],
)
async def test_reclaim_transport_failure_propagates_without_delivery_or_retry(
    error: BaseException,
) -> None:
    client = ProtocolRedis(error)
    with pytest.raises(type(error)):
        await RedisQueue(client).reclaim(consumer_name="b", cursor="0-0")
    assert sum(command[0] == "XAUTOCLAIM" for command in client.commands) == 1
    assert client.pool.released and client.pool.wire.disconnected
