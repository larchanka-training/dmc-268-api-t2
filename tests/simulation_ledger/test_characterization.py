"""Already-green characterization of composed guarantees; no claimed new TDD RED.

These consequences reuse the independently red/green claim, quota, gate, outcome
and incident slices. They check the real external send counter, not a fake ledger.
"""

# ruff: noqa: F811 -- explicit fixtures and AC constants

import asyncio
import json
from uuid import uuid4

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from test_simulation_admission import (
    _ReplyGate,
    admissions,  # noqa: F401
    gated_admission,  # noqa: F401
    redis_client,  # noqa: F401
    state_key,  # noqa: F401
)

from app.adapters.simulation_admission import RedisSimulationAdmission
from app.adapters.simulation_dispatch_ledger import PostgresSimulationDispatchLedger
from app.application.simulation_execution import SimulationExecutionCoordinator

from . import SECRET
from .pg_faults import lost_commit_reply
from .provider_boundary import PaidBoundary


@pytest.mark.parametrize(
    "denial", ["duplicate", "user", "repository", "global", "missing-ledger"]
)
async def test_postgres_denial_has_zero_provider_requests(
    denial: str,
    ledger_engine: AsyncEngine,
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
) -> None:
    scope = (uuid4(), uuid4(), b"deny")
    ledger = PostgresSimulationDispatchLedger(
        async_sessionmaker(ledger_engine), hmac_secret=SECRET
    )
    expected = "UNAVAILABLE"
    if denial == "duplicate":
        assert await ledger.claim(*scope) == "AUTHORIZED"
        expected = "DUPLICATE"
    elif denial == "user":
        for i in range(3):
            assert (
                await ledger.claim(scope[0], uuid4(), str(i).encode()) == "AUTHORIZED"
            )
        expected = "RATE_USER"
    elif denial == "repository":
        for _ in range(10):
            assert await ledger.claim(uuid4(), scope[1], b"seed") == "AUTHORIZED"
        expected = "RATE_REPOSITORY"
    elif denial == "global":
        for _ in range(20):
            assert await ledger.claim(uuid4(), uuid4(), b"seed") == "AUTHORIZED"
        expected = "RATE_GLOBAL"
    paid = PaidBoundary()
    coordinator = SimulationExecutionCoordinator[object](
        admissions[0], ledger=None if denial == "missing-ledger" else ledger
    )
    assert (await coordinator.execute(*scope, paid.execute)).status == expected
    assert paid.calls == []
    assert await coordinator.aclose()


async def test_redis_busy_never_consumes_a_postgres_claim(
    ledger_engine: AsyncEngine,
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
) -> None:
    held = await admissions[0].acquire(uuid4(), uuid4(), b"hold")
    assert held.status == "ACQUIRED"
    paid = PaidBoundary()
    ledger = PostgresSimulationDispatchLedger(
        async_sessionmaker(ledger_engine), hmac_secret=SECRET
    )
    coordinator = SimulationExecutionCoordinator[object](admissions[1], ledger=ledger)
    assert (
        await coordinator.execute(uuid4(), uuid4(), b"busy", paid.execute)
    ).status == "BUSY"
    async with ledger_engine.connect() as observer:
        assert (
            await observer.scalar(
                text("SELECT count(*) FROM simulation_dispatch_claim")
            )
            == 0
        )
    assert paid.calls == []


@pytest.mark.parametrize("before", [False, True])
async def test_lost_pg_commit_reply_never_sends(
    before: bool,
    ledger_engine: AsyncEngine,
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
) -> None:
    paid = PaidBoundary()
    async with lost_commit_reply(ledger_engine.url, before=before) as (url, fault):
        engine = create_async_engine(url, connect_args={"ssl": False})
        ledger = PostgresSimulationDispatchLedger(
            async_sessionmaker(engine), hmac_secret=SECRET
        )
        coordinator = SimulationExecutionCoordinator[object](
            admissions[0], ledger=ledger
        )
        request = asyncio.create_task(
            coordinator.execute(uuid4(), uuid4(), b"lost", paid.execute)
        )
        try:
            await asyncio.wait_for(fault.committed.wait(), 2)
            async with ledger_engine.connect() as observer:
                assert await observer.scalar(
                    text("SELECT count(*) FROM simulation_dispatch_claim")
                ) == (0 if before else 1)
            fault.release.set()
            assert (await request).status == "UNAVAILABLE"
            assert paid.calls == []
            assert await coordinator.aclose()
        finally:
            fault.release.set()
            await asyncio.gather(request, return_exceptions=True)
            await engine.dispose()


@pytest.mark.parametrize("cancel", [False, True])
async def test_lost_redis_reply_or_cancellation_before_pg_never_sends(
    cancel: bool,
    ledger_engine: AsyncEngine,
    gated_admission: tuple[RedisSimulationAdmission, _ReplyGate],
) -> None:
    admission, gate = gated_admission
    gate.mode = "cancel" if cancel else "disconnect"
    ledger = PostgresSimulationDispatchLedger(
        async_sessionmaker(ledger_engine), hmac_secret=SECRET
    )
    paid = PaidBoundary()
    coordinator = SimulationExecutionCoordinator[object](admission, ledger=ledger)
    request = asyncio.create_task(
        coordinator.execute(uuid4(), uuid4(), b"lost", paid.execute)
    )
    await asyncio.wait_for(gate.committed.wait(), 2)
    if cancel:
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
    else:
        assert (await request).status == "UNAVAILABLE"
    assert await coordinator.aclose()
    async with ledger_engine.connect() as observer:
        assert (
            await observer.scalar(
                text("SELECT count(*) FROM simulation_dispatch_claim")
            )
            == 0
        )
    assert paid.calls == []


@pytest.mark.parametrize("ending", ["timeout", "cancel", "reset", "provider-timeout"])
async def test_possible_http_send_never_repeats_after_redis_rollback(
    ending: str,
    ledger_engine: AsyncEngine,
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
    redis_client: Redis,
    state_key: str,
) -> None:
    snapshot = await redis_client.get(state_key)
    assert snapshot is not None
    scope = (uuid4(), uuid4(), b"once")
    paid = PaidBoundary()
    if ending in ("timeout", "cancel"):
        paid.finish = asyncio.Event()
    else:
        paid.failure = (
            httpx.ReadTimeout("synthetic")
            if ending == "provider-timeout"
            else httpx.ReadError("synthetic reset")
        )
    ledger = PostgresSimulationDispatchLedger(
        async_sessionmaker(ledger_engine), hmac_secret=SECRET
    )
    coordinator = SimulationExecutionCoordinator[object](
        admissions[0], ledger=ledger, timeout_s=0.2
    )
    task = asyncio.create_task(coordinator.execute(*scope, paid.execute))
    await asyncio.wait_for(paid.entered.wait(), 2)
    if ending == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        assert (await task).status in ("TIMEOUT", "EXECUTION_FAILED")
    assert await coordinator.aclose()
    await redis_client.set(state_key, snapshot)
    other = PostgresSimulationDispatchLedger(
        async_sessionmaker(ledger_engine), hmac_secret=SECRET
    )
    successor = SimulationExecutionCoordinator[object](admissions[1], ledger=other)
    assert (await successor.execute(*scope, paid.execute)).status == "DUPLICATE"
    assert len(paid.calls) == 1


async def test_redis_rollback_preserves_authoritative_quota_for_distinct_keys(
    ledger_engine: AsyncEngine,
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
    redis_client: Redis,
    state_key: str,
) -> None:
    snapshot = await redis_client.get(state_key)
    assert snapshot is not None
    user, repo = uuid4(), uuid4()
    ledger = PostgresSimulationDispatchLedger(
        async_sessionmaker(ledger_engine), hmac_secret=SECRET
    )
    for i in range(3):
        assert await ledger.claim(user, repo, str(i).encode()) == "AUTHORIZED"
    await redis_client.set(state_key, snapshot)
    paid = PaidBoundary()
    coordinator = SimulationExecutionCoordinator[object](admissions[1], ledger=ledger)
    for key in (b"distinct-one", b"distinct-two"):
        assert (
            await coordinator.execute(user, repo, key, paid.execute)
        ).status == "RATE_USER"
    assert paid.calls == []


async def test_exact_byte_identity_matches_redis_and_retains_no_payload(
    ledger_engine: AsyncEngine,
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
    redis_client: Redis,
    state_key: str,
) -> None:
    user, repo = uuid4(), uuid4()
    key = b" Exact-Key-\xff "
    ledger = PostgresSimulationDispatchLedger(
        async_sessionmaker(ledger_engine), hmac_secret=SECRET
    )
    # Both stores use the same stable secret and existing operation framing.
    assert (await admissions[0].acquire(user, repo, key)).status == "ACQUIRED"
    assert await ledger.claim(user, repo, key) == "AUTHORIZED"
    saved = await redis_client.get(state_key)
    assert saved is not None
    raw = json.loads(saved)
    async with ledger_engine.connect() as observer:
        digest = await observer.scalar(
            text("SELECT operation_digest FROM simulation_dispatch_claim")
        )
    assert digest == raw["consumed_keys"][0]["digest"]
    assert await ledger.claim(user, repo, b"exact-key-\xff") == "AUTHORIZED"
    assert await ledger.claim(uuid4(), repo, key) == "AUTHORIZED"
    assert await ledger.claim(user, uuid4(), key) == "AUTHORIZED"


async def test_rolling_quota_expiry_never_expires_consumed_idempotency(
    ledger_engine: AsyncEngine,
) -> None:
    user, repo = uuid4(), uuid4()
    ledger = PostgresSimulationDispatchLedger(
        async_sessionmaker(ledger_engine), hmac_secret=SECRET
    )
    for key in (b"one", b"two", b"three"):
        assert await ledger.claim(user, repo, key) == "AUTHORIZED"
    async with ledger_engine.begin() as db:
        await db.execute(
            text(
                "UPDATE simulation_dispatch_claim SET "
                "authorized_at=clock_timestamp()-interval '25 hours'"
            )
        )
    assert await ledger.claim(user, repo, b"four") == "AUTHORIZED"
    assert await ledger.claim(user, repo, b"one") == "DUPLICATE"


async def test_future_clock_rows_and_all_attempt_states_consume_quota(
    ledger_engine: AsyncEngine,
) -> None:
    user, repo = uuid4(), uuid4()
    ledger = PostgresSimulationDispatchLedger(
        async_sessionmaker(ledger_engine), hmac_secret=SECRET
    )
    for key in (b"authorized", b"completed", b"unknown"):
        assert await ledger.claim(user, repo, key) == "AUTHORIZED"
    await ledger.finish(user, repo, b"completed", "COMPLETED")
    await ledger.finish(user, repo, b"unknown", "UNKNOWN")
    async with ledger_engine.begin() as db:
        await db.execute(
            text(
                "UPDATE simulation_dispatch_claim SET "
                "authorized_at=clock_timestamp()+interval '1 day'"
            )
        )
    assert await ledger.claim(user, uuid4(), b"distinct") == "RATE_USER"
