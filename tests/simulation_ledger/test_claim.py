"""Claim interface exercised against actual PostgreSQL commits and independent pools."""

import asyncio
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    AsyncSessionTransaction,
    async_sessionmaker,
    create_async_engine,
)

from app.adapters.simulation_dispatch_ledger import PostgresSimulationDispatchLedger

from . import SECRET
from .pg_faults import lost_commit_reply


async def test_committed_claim_is_consumed_in_an_independent_instance(
    ledger_engine: AsyncEngine,
) -> None:
    user, repository = uuid4(), uuid4()
    first = PostgresSimulationDispatchLedger(
        async_sessionmaker(ledger_engine), hmac_secret=SECRET
    )
    second = PostgresSimulationDispatchLedger(
        async_sessionmaker(ledger_engine), hmac_secret=SECRET
    )
    assert await first.claim(user, repository, b" Exact-Case-\xff ") == "AUTHORIZED"
    assert await second.claim(user, repository, b" Exact-Case-\xff ") == "DUPLICATE"


async def race_claims(
    ledger_engine: AsyncEngine,
    first_scope: tuple[UUID, UUID, bytes],
    second_scope: tuple[UUID, UUID, bytes],
    isolation_level: str = "READ COMMITTED",
) -> tuple[str, str]:

    entered, finish = asyncio.Event(), asyncio.Event()

    class HeldCommit(AsyncSessionTransaction):
        async def __aexit__(self, *args: object) -> None:
            if args[0] is None:
                await self.session.flush()
                entered.set()
                await finish.wait()
            await super().__aexit__(*args)

    class HeldSession(AsyncSession):
        def begin(self) -> AsyncSessionTransaction:
            return HeldCommit(self)

    other = create_async_engine(
        ledger_engine.url,
        isolation_level=isolation_level,
        connect_args={"server_settings": {"application_name": "b2c2-duplicate-waiter"}},
    )
    first = PostgresSimulationDispatchLedger(
        async_sessionmaker(ledger_engine, class_=HeldSession), hmac_secret=SECRET
    )
    second = PostgresSimulationDispatchLedger(
        async_sessionmaker(other), hmac_secret=SECRET
    )
    one = asyncio.create_task(first.claim(*first_scope))
    two = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        two = asyncio.create_task(second.claim(*second_scope))
        async with other.connect() as observer:
            await observer.execution_options(isolation_level="AUTOCOMMIT")
            async with asyncio.timeout(2):
                while not await observer.scalar(
                    text(
                        "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE "
                        "application_name='b2c2-duplicate-waiter' "
                        "AND cardinality(pg_blocking_pids(pid))>0)"
                    )
                ):
                    await asyncio.sleep(0)
        finish.set()
        return await one, await two
    finally:
        finish.set()
        await asyncio.gather(one, *([two] if two else []), return_exceptions=True)
        await other.dispose()


async def test_concurrent_duplicate_claim_returns_only_one_authorization(
    ledger_engine: AsyncEngine,
) -> None:
    scope = (uuid4(), uuid4(), b"concurrent")
    assert await race_claims(ledger_engine, scope, scope) == ("AUTHORIZED", "DUPLICATE")


async def test_last_user_allowance_cannot_be_spent_by_two_instances(
    ledger_engine: AsyncEngine,
) -> None:
    user, repo = uuid4(), uuid4()
    ledger = PostgresSimulationDispatchLedger(
        async_sessionmaker(ledger_engine), hmac_secret=SECRET
    )
    for key in (b"one", b"two"):
        assert await ledger.claim(user, repo, key) == "AUTHORIZED"
    assert await race_claims(
        ledger_engine, (user, repo, b"last"), (user, uuid4(), b"over")
    ) == ("AUTHORIZED", "RATE_USER")


async def test_last_repository_allowance_cannot_be_spent_by_two_instances(
    ledger_engine: AsyncEngine,
) -> None:
    repo = uuid4()
    ledger = PostgresSimulationDispatchLedger(
        async_sessionmaker(ledger_engine), hmac_secret=SECRET
    )
    for _ in range(9):
        assert await ledger.claim(uuid4(), repo, b"seed") == "AUTHORIZED"
    assert await race_claims(
        ledger_engine, (uuid4(), repo, b"last"), (uuid4(), repo, b"over")
    ) == ("AUTHORIZED", "RATE_REPOSITORY")


async def test_last_global_allowance_cannot_be_spent_by_two_instances(
    ledger_engine: AsyncEngine,
) -> None:
    ledger = PostgresSimulationDispatchLedger(
        async_sessionmaker(ledger_engine), hmac_secret=SECRET
    )
    for _ in range(19):
        assert await ledger.claim(uuid4(), uuid4(), b"seed") == "AUTHORIZED"
    assert await race_claims(
        ledger_engine, (uuid4(), uuid4(), b"last"), (uuid4(), uuid4(), b"over")
    ) == ("AUTHORIZED", "RATE_GLOBAL")


async def test_unknown_commit_outcome_never_authorizes_or_retries(
    ledger_engine: AsyncEngine,
) -> None:

    async with lost_commit_reply(ledger_engine.url) as (url, fault):
        engine = create_async_engine(url, connect_args={"ssl": False})
        ledger = PostgresSimulationDispatchLedger(
            async_sessionmaker(engine), hmac_secret=SECRET
        )
        user, repo = uuid4(), uuid4()
        request = asyncio.create_task(ledger.claim(user, repo, b"lost-commit"))
        try:
            await asyncio.wait_for(fault.committed.wait(), 2)
            async with ledger_engine.connect() as observer:
                assert (
                    await observer.scalar(
                        text("SELECT count(*) FROM simulation_dispatch_claim")
                    )
                    == 1
                )
            fault.release.set()
            assert await request == "UNAVAILABLE"
            assert fault.commits == 1
            independent = PostgresSimulationDispatchLedger(
                async_sessionmaker(ledger_engine), hmac_secret=SECRET
            )
            assert await independent.claim(user, repo, b"lost-commit") == "UNAVAILABLE"
        finally:
            fault.release.set()
            await asyncio.gather(request, return_exceptions=True)
            await engine.dispose()


async def test_component_incident_blocks_new_claims_in_independent_instances(
    ledger_engine: AsyncEngine,
) -> None:
    first = PostgresSimulationDispatchLedger(
        async_sessionmaker(ledger_engine), hmac_secret=SECRET
    )
    second = PostgresSimulationDispatchLedger(
        async_sessionmaker(ledger_engine), hmac_secret=SECRET
    )
    await first.block()
    assert await second.claim(uuid4(), uuid4(), b"after-incident") == "UNAVAILABLE"


async def test_postgres_failure_latches_denial_after_reconnect(
    ledger_engine: AsyncEngine,
) -> None:

    async with lost_commit_reply(ledger_engine.url) as (url, fault):
        engine = create_async_engine(url, connect_args={"ssl": False})
        first = PostgresSimulationDispatchLedger(
            async_sessionmaker(engine), hmac_secret=SECRET
        )
        request = asyncio.create_task(first.claim(uuid4(), uuid4(), b"uncertain"))
        try:
            await asyncio.wait_for(fault.committed.wait(), 2)
            fault.release.set()
            assert await request == "UNAVAILABLE"
            independent = PostgresSimulationDispatchLedger(
                async_sessionmaker(ledger_engine), hmac_secret=SECRET
            )
            assert (
                await independent.claim(uuid4(), uuid4(), b"after-reconnect")
                == "UNAVAILABLE"
            )
        finally:
            fault.release.set()
            await asyncio.gather(request, return_exceptions=True)
            await engine.dispose()


async def test_changed_hmac_cannot_restore_a_consumed_key(
    ledger_engine: AsyncEngine,
) -> None:
    scope = (uuid4(), uuid4(), b"stable-identity")
    first = PostgresSimulationDispatchLedger(
        async_sessionmaker(ledger_engine), hmac_secret=SECRET
    )
    changed = PostgresSimulationDispatchLedger(
        async_sessionmaker(ledger_engine),
        hmac_secret=b"different-test-secret-material-32bytes",
    )
    assert await first.claim(*scope) == "AUTHORIZED"
    assert await changed.claim(*scope) == "UNAVAILABLE"
    assert await first.claim(uuid4(), uuid4(), b"after-mismatch") == "UNAVAILABLE"


async def test_authorization_requires_synchronous_commit_on_the_primary(
    ledger_engine: AsyncEngine,
) -> None:

    async with ledger_engine.begin() as observer:
        await observer.execute(
            text("""
            CREATE FUNCTION require_durable_dispatch() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                IF current_setting('synchronous_commit') <> 'on' THEN
                    RAISE EXCEPTION 'non-durable dispatch authorization';
                END IF;
                RETURN NEW;
            END $$
        """)
        )
        await observer.execute(
            text(
                "CREATE TRIGGER durable_dispatch BEFORE INSERT ON "
                "simulation_dispatch_claim FOR EACH ROW "
                "EXECUTE FUNCTION require_durable_dispatch()"
            )
        )
    off = create_async_engine(
        ledger_engine.url,
        connect_args={"server_settings": {"synchronous_commit": "off"}},
    )
    try:
        ledger = PostgresSimulationDispatchLedger(
            async_sessionmaker(off), hmac_secret=SECRET
        )
        assert await ledger.claim(uuid4(), uuid4(), b"durable") == "AUTHORIZED"
    finally:
        await off.dispose()


async def test_pool_snapshot_isolation_cannot_overspend_last_quota(
    ledger_engine: AsyncEngine,
) -> None:
    engine = ledger_engine.execution_options(isolation_level="REPEATABLE READ")
    ledger = PostgresSimulationDispatchLedger(
        async_sessionmaker(engine), hmac_secret=SECRET
    )
    user, repo = uuid4(), uuid4()
    for key in (b"one", b"two"):
        assert await ledger.claim(user, repo, key) == "AUTHORIZED"
    assert await race_claims(
        engine, (user, repo, b"last"), (user, repo, b"over"), "REPEATABLE READ"
    ) == ("AUTHORIZED", "RATE_USER")


async def test_deny_latch_requires_synchronous_commit(
    ledger_engine: AsyncEngine,
) -> None:
    async with ledger_engine.begin() as observer:
        await observer.execute(
            text("""
            CREATE FUNCTION require_durable_deny() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN
                IF current_setting('synchronous_commit') <> 'on' THEN
                    RAISE EXCEPTION 'non-durable deny';
                END IF;
                RETURN NEW;
            END $$
        """)
        )
        await observer.execute(
            text(
                "CREATE TRIGGER durable_deny BEFORE UPDATE ON "
                "simulation_dispatch_control FOR EACH ROW "
                "EXECUTE FUNCTION require_durable_deny()"
            )
        )
    off = create_async_engine(
        ledger_engine.url,
        connect_args={"server_settings": {"synchronous_commit": "off"}},
    )
    try:
        ledger = PostgresSimulationDispatchLedger(
            async_sessionmaker(off), hmac_secret=SECRET
        )
        await ledger.block()
        independent = PostgresSimulationDispatchLedger(
            async_sessionmaker(ledger_engine), hmac_secret=SECRET
        )
        assert (
            await independent.claim(uuid4(), uuid4(), b"after-durable-deny")
            == "UNAVAILABLE"
        )
    finally:
        await off.dispose()
