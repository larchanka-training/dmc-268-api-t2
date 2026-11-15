"""Real Redis + PostgreSQL + MockTransport at the paid provider boundary."""

# ruff: noqa: F811 -- explicit local fixture imports

import asyncio
from contextlib import suppress
from uuid import uuid4

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    AsyncSessionTransaction,
    async_sessionmaker,
    create_async_engine,
)
from test_simulation_admission import (
    _ReplyGate,
    admissions,  # noqa: F401 -- local fixture
    gated_admission,  # noqa: F401 -- local fixture
    redis_client,  # noqa: F401 -- local fixture
    state_key,  # noqa: F401 -- local fixture
)

from app.adapters.simulation_admission import RedisSimulationAdmission
from app.adapters.simulation_dispatch_ledger import PostgresSimulationDispatchLedger
from app.api.rule_simulations import SimulationCodeInput, SimulationRules
from app.application.simulation_execution import SimulationExecutionCoordinator
from app.llm.openai_compatible import OpenAICompatibleClient
from app.review.simulation import CodeSimulationService

from . import SECRET


async def test_confirmed_claim_precedes_the_only_http_request_without_held_connection(
    ledger_engine: AsyncEngine,
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
) -> None:

    count = 0

    async def send(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        # A one-connection pool cannot serve this query if authorization retains it.
        async with asyncio.timeout(1), ledger_engine.connect() as observer:
            assert (
                await observer.scalar(
                    text("SELECT count(*) FROM simulation_dispatch_claim")
                )
                == 1
            )
        return httpx.Response(
            200, json={"choices": [{"message": {"content": '{"findings":[]}'}}]}
        )

    ledger = PostgresSimulationDispatchLedger(
        async_sessionmaker(ledger_engine), hmac_secret=SECRET
    )
    coordinator = SimulationExecutionCoordinator[object](admissions[0], ledger=ledger)
    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as http:
        client = OpenAICompatibleClient(
            name="eurorouter",
            http=http,
            base_url="https://api.eurouter.ai/api/v1",
            model="mistral-small-4",
            api_key="synthetic-key",
        )
        service = CodeSimulationService(
            client, max_input_tokens=8192, max_output_tokens=2000
        )

        async def execute(deadline: float) -> object:
            return await service.simulate(
                SimulationCodeInput(kind="code", path="example.py", source="print(1)"),
                SimulationRules(
                    format="rules-md/1",
                    markdown="# Security\n## SEC-1: No eval\nAvoid eval.\n",
                ),
                deadline=deadline,
            )

        assert (
            await coordinator.execute(uuid4(), uuid4(), b"paid", execute)
        ).status == "SUCCESS"
        assert count == 1
        assert await coordinator.aclose()


async def test_success_records_completed_without_restoring_dispatch_right(
    ledger_engine: AsyncEngine,
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
) -> None:

    ledger = PostgresSimulationDispatchLedger(
        async_sessionmaker(ledger_engine), hmac_secret=SECRET
    )
    scope = (uuid4(), uuid4(), b"completed")
    coordinator = SimulationExecutionCoordinator[str](admissions[0], ledger=ledger)
    assert (
        await coordinator.execute(*scope, lambda _: asyncio.sleep(0, result="ok"))
    ).status == "SUCCESS"
    async with ledger_engine.connect() as observer:
        assert (
            await observer.scalar(text("SELECT state FROM simulation_dispatch_claim"))
            == "COMPLETED"
        )
    assert await ledger.claim(*scope) == "DUPLICATE"


async def test_provider_error_records_unknown_and_never_refunds(
    ledger_engine: AsyncEngine,
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
) -> None:

    ledger = PostgresSimulationDispatchLedger(
        async_sessionmaker(ledger_engine), hmac_secret=SECRET
    )
    scope = (uuid4(), uuid4(), b"provider-error")
    coordinator = SimulationExecutionCoordinator[str](admissions[0], ledger=ledger)

    async def fail(_: float) -> str:
        raise httpx.ReadError("synthetic connection reset after send")

    assert (await coordinator.execute(*scope, fail)).status == "EXECUTION_FAILED"
    async with ledger_engine.connect() as observer:
        assert (
            await observer.scalar(text("SELECT state FROM simulation_dispatch_claim"))
            == "UNKNOWN"
        )
    await ledger.finish(*scope, "COMPLETED")
    assert await ledger.claim(*scope) == "DUPLICATE"
    async with ledger_engine.connect() as observer:
        assert (
            await observer.scalar(text("SELECT state FROM simulation_dispatch_claim"))
            == "UNKNOWN"
        )


async def test_cancellation_after_commit_records_unknown_without_repeat(
    ledger_engine: AsyncEngine,
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
) -> None:

    entered = asyncio.Event()
    ledger = PostgresSimulationDispatchLedger(
        async_sessionmaker(ledger_engine), hmac_secret=SECRET
    )
    scope = (uuid4(), uuid4(), b"cancelled")
    coordinator = SimulationExecutionCoordinator[str](admissions[0], ledger=ledger)

    async def wait(_: float) -> str:
        entered.set()
        await asyncio.Event().wait()
        return "unreachable"

    request = asyncio.create_task(coordinator.execute(*scope, wait))
    await asyncio.wait_for(entered.wait(), 2)
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert await coordinator.aclose()
    async with ledger_engine.connect() as observer:
        assert (
            await observer.scalar(text("SELECT state FROM simulation_dispatch_claim"))
            == "UNKNOWN"
        )
    assert await ledger.claim(*scope) == "DUPLICATE"


async def test_late_success_after_deadline_remains_unknown(
    ledger_engine: AsyncEngine,
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
) -> None:

    entered, finish = asyncio.Event(), asyncio.Event()
    ledger = PostgresSimulationDispatchLedger(
        async_sessionmaker(ledger_engine), hmac_secret=SECRET
    )
    scope = (uuid4(), uuid4(), b"late")
    coordinator = SimulationExecutionCoordinator[str](
        admissions[0], ledger=ledger, timeout_s=0.1, cleanup_timeout_s=0.01
    )

    async def late(_: float) -> str:
        entered.set()
        while not finish.is_set():
            with suppress(asyncio.CancelledError):
                await finish.wait()
        return "late response"

    request = asyncio.create_task(coordinator.execute(*scope, late))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert (await request).status == "TIMEOUT"
        assert not await coordinator.aclose()
        finish.set()
        async with asyncio.timeout(2):
            while not await coordinator.aclose():
                await asyncio.sleep(0)
        async with ledger_engine.connect() as observer:
            assert (
                await observer.scalar(
                    text("SELECT state FROM simulation_dispatch_claim")
                )
                == "UNKNOWN"
            )
        assert await ledger.claim(*scope) == "DUPLICATE"
    finally:
        finish.set()
        await asyncio.gather(request, return_exceptions=True)
        await coordinator.aclose()


async def test_detected_redis_failure_denies_other_api_instances(
    ledger_engine: AsyncEngine,
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
    redis_client: Redis,
    state_key: str,
) -> None:

    await redis_client.delete(state_key)
    first = PostgresSimulationDispatchLedger(
        async_sessionmaker(ledger_engine), hmac_secret=SECRET
    )
    second = PostgresSimulationDispatchLedger(
        async_sessionmaker(ledger_engine), hmac_secret=SECRET
    )
    coordinator = SimulationExecutionCoordinator[str](admissions[0], ledger=first)
    result = await coordinator.execute(
        uuid4(),
        uuid4(),
        b"redis-missing",
        lambda _: asyncio.sleep(0, result="must-not-send"),
    )
    assert result.status == "INVALID_STATE"
    assert (
        await second.claim(uuid4(), uuid4(), b"after-observed-failure") == "UNAVAILABLE"
    )
    async with ledger_engine.connect() as observer:
        assert (
            await observer.scalar(
                text("SELECT count(*) FROM simulation_dispatch_claim")
            )
            == 0
        )


async def test_terminal_write_failure_blocks_new_permissions(
    ledger_engine: AsyncEngine,
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
) -> None:
    async with ledger_engine.begin() as db:
        await db.execute(
            text("""
            CREATE FUNCTION fail_terminal_write() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN
                RAISE EXCEPTION 'synthetic terminal storage failure';
            END $$
        """)
        )
        await db.execute(
            text(
                "CREATE TRIGGER terminal_failure BEFORE UPDATE ON "
                "simulation_dispatch_claim FOR EACH ROW "
                "EXECUTE FUNCTION fail_terminal_write()"
            )
        )
    ledger = PostgresSimulationDispatchLedger(
        async_sessionmaker(ledger_engine), hmac_secret=SECRET
    )
    coordinator = SimulationExecutionCoordinator[str](admissions[0], ledger=ledger)
    assert (
        await coordinator.execute(
            uuid4(), uuid4(), b"finished", lambda _: asyncio.sleep(0, result="ok")
        )
    ).status == "SUCCESS"
    independent = PostgresSimulationDispatchLedger(
        async_sessionmaker(ledger_engine), hmac_secret=SECRET
    )
    assert (
        await independent.claim(uuid4(), uuid4(), b"after-storage-fault")
        == "UNAVAILABLE"
    )


async def test_lost_redis_cleanup_reply_blocks_new_permissions(
    ledger_engine: AsyncEngine,
    gated_admission: tuple[RedisSimulationAdmission, _ReplyGate],
) -> None:
    admission, gate = gated_admission
    ledger = PostgresSimulationDispatchLedger(
        async_sessionmaker(ledger_engine), hmac_secret=SECRET
    )
    coordinator = SimulationExecutionCoordinator[str](admission, ledger=ledger)

    async def execute(_: float) -> str:
        gate.mode = "disconnect"
        return "validated"

    result = await coordinator.execute(uuid4(), uuid4(), b"cleanup-reply", execute)
    assert result.status == "SUCCESS"
    assert result.cleanup == "UNAVAILABLE"
    independent = PostgresSimulationDispatchLedger(
        async_sessionmaker(ledger_engine), hmac_secret=SECRET
    )
    assert (
        await independent.claim(uuid4(), uuid4(), b"after-cleanup-fault")
        == "UNAVAILABLE"
    )


async def test_slow_commit_cleanup_retains_task_ownership_after_deadline(
    ledger_engine: AsyncEngine,
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
) -> None:

    committed, finish, exited = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class DelayedCommit(AsyncSessionTransaction):
        async def __aexit__(self, *args: object) -> None:
            await super().__aexit__(*args)
            committed.set()
            try:
                while not finish.is_set():
                    with suppress(asyncio.CancelledError):
                        await finish.wait()
            finally:
                exited.set()

    class DelayedSession(AsyncSession):
        def begin(self) -> AsyncSessionTransaction:
            return DelayedCommit(self)

    ledger = PostgresSimulationDispatchLedger(
        async_sessionmaker(ledger_engine, class_=DelayedSession), hmac_secret=SECRET
    )
    coordinator = SimulationExecutionCoordinator[str](
        admissions[0], ledger=ledger, timeout_s=0.2, cleanup_timeout_s=0.01
    )
    calls = 0

    async def execute(_: float) -> str:
        nonlocal calls
        calls += 1
        return "must not run"

    request = asyncio.create_task(
        coordinator.execute(uuid4(), uuid4(), b"slow-ack", execute)
    )
    try:
        await asyncio.wait_for(committed.wait(), 2)
        assert (await request).status == "TIMEOUT"
        assert not await coordinator.aclose()
        assert calls == 0
    finally:
        finish.set()
        await asyncio.wait_for(exited.wait(), 2)
        await asyncio.gather(request, return_exceptions=True)
        async with asyncio.timeout(2):
            while not await coordinator.aclose():
                await asyncio.sleep(0)


async def test_postgres_deadline_denies_permissions_after_store_recovers(
    ledger_engine: AsyncEngine,
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
) -> None:

    committed, finish = asyncio.Event(), asyncio.Event()

    class DelayedCommit(AsyncSessionTransaction):
        async def __aexit__(self, *args: object) -> None:
            await super().__aexit__(*args)
            committed.set()
            while not finish.is_set():
                with suppress(asyncio.CancelledError):
                    await finish.wait()

    class DelayedSession(AsyncSession):
        def begin(self) -> AsyncSessionTransaction:
            return DelayedCommit(self)

    ledger = PostgresSimulationDispatchLedger(
        async_sessionmaker(ledger_engine, class_=DelayedSession), hmac_secret=SECRET
    )
    coordinator = SimulationExecutionCoordinator[str](
        admissions[0], ledger=ledger, timeout_s=0.2, cleanup_timeout_s=0.01
    )
    request = asyncio.create_task(
        coordinator.execute(
            uuid4(),
            uuid4(),
            b"deadline",
            lambda _: asyncio.sleep(0, result="must-not-send"),
        )
    )
    try:
        await asyncio.wait_for(committed.wait(), 2)
        assert (await request).status == "TIMEOUT"
        independent = PostgresSimulationDispatchLedger(
            async_sessionmaker(ledger_engine), hmac_secret=SECRET
        )
        assert (
            await independent.claim(uuid4(), uuid4(), b"after-timeout") == "UNAVAILABLE"
        )
    finally:
        finish.set()
        await asyncio.gather(request, return_exceptions=True)
        async with asyncio.timeout(2):
            while not await coordinator.aclose():
                await asyncio.sleep(0)


async def test_store_failure_cleanup_shares_the_existing_single_budget(
    ledger_engine: AsyncEngine,
    gated_admission: tuple[RedisSimulationAdmission, _ReplyGate],
) -> None:

    admission, gate = gated_admission
    ledger = PostgresSimulationDispatchLedger(
        async_sessionmaker(ledger_engine), hmac_secret=SECRET
    )
    coordinator = SimulationExecutionCoordinator[str](
        admission, ledger=ledger, cleanup_timeout_s=0.2
    )
    other = create_async_engine(ledger_engine.url)
    held = await other.connect()
    transaction = await held.begin()
    cleanup_started = 0.0

    async def execute(_: float) -> str:
        nonlocal cleanup_started
        await held.execute(text("SELECT pg_advisory_xact_lock(268, 32)"))
        gate.mode = "cancel"
        cleanup_started = asyncio.get_running_loop().time()
        return "validated"

    try:
        assert (
            await coordinator.execute(uuid4(), uuid4(), b"one-cleanup-budget", execute)
        ).status == "SUCCESS"
        elapsed = asyncio.get_running_loop().time() - cleanup_started
        response_bound_s = 0.35
        assert elapsed < response_bound_s, (
            f"cleanup consumed two budgets: {elapsed:.3f}s"
        )
    finally:
        await transaction.rollback()
        await held.close()
        async with asyncio.timeout(2):
            while not await coordinator.aclose():
                await asyncio.sleep(0)
        await other.dispose()


async def test_immediate_shutdown_keeps_deferred_deny_write_owned(
    ledger_engine: AsyncEngine,
    gated_admission: tuple[RedisSimulationAdmission, _ReplyGate],
) -> None:
    admission, gate = gated_admission
    ledger = PostgresSimulationDispatchLedger(
        async_sessionmaker(ledger_engine), hmac_secret=SECRET
    )
    coordinator = SimulationExecutionCoordinator[str](
        admission, ledger=ledger, cleanup_timeout_s=0.05
    )

    async def execute(_: float) -> str:
        gate.mode = "cancel"
        return "validated"

    result = await coordinator.execute(uuid4(), uuid4(), b"immediate-shutdown", execute)
    assert result.status == "SUCCESS"
    assert result.cleanup == "TIMEOUT"
    async with asyncio.timeout(2):
        while not await coordinator.aclose():
            await asyncio.sleep(0)
    independent = PostgresSimulationDispatchLedger(
        async_sessionmaker(ledger_engine), hmac_secret=SECRET
    )
    assert await independent.claim(uuid4(), uuid4(), b"after-shutdown") == "UNAVAILABLE"
