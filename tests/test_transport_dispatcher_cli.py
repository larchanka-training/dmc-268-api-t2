"""Existing --once performs one recovery sweep and one initial analyze batch."""

from datetime import timedelta

from redis.asyncio import Redis
from sqlalchemy import func, update
from test_outbox_dispatcher_cli import STREAM, invoke
from test_postgres_integration import PostgresHarness, _create_outbox_event

from app.db.models import OutboxEvent

pytest_plugins = ("test_outbox_dispatcher_cli",)


async def test_once_recovers_legacy_analyze_but_publish_remains_parked(
    postgres: PostgresHarness,
    runtime_redis: Redis,
) -> None:
    event_id, _ = await _create_outbox_event(postgres)
    await _create_outbox_event(postgres, task_kind="publish")
    assert invoke("--once").returncode == 0
    await runtime_redis.delete(f"{STREAM}:analyze")
    async with postgres.engine.begin() as connection:
        await connection.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == event_id)
            .values(
                dispatch_not_before=func.clock_timestamp() - timedelta(seconds=1),
            )
        )
    result = invoke("--once")
    assert result.returncode == 0, result.stderr
    assert await runtime_redis.xlen(f"{STREAM}:analyze") == 1
    assert await runtime_redis.xlen(f"{STREAM}:publish") == 0
