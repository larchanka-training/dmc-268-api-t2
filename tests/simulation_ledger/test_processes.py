"""Already-green process/crash characterization; no in-process mutex assumptions."""

# ruff: noqa: F811, PLR0913, PLR0917, PLR2004 -- fixtures and crash AC values

import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from test_simulation_admission import admissions, redis_client, state_key  # noqa: F401

from app.adapters.simulation_admission import RedisSimulationAdmission
from app.adapters.simulation_dispatch_ledger import PostgresSimulationDispatchLedger
from app.application.simulation_execution import SimulationExecutionCoordinator

from . import SECRET
from .provider_boundary import PaidBoundary


async def start_child(
    engine: AsyncEngine,
    user: UUID,
    repo: UUID,
    key: str,
    mode: str = "claim",
    state_key: str = "",
) -> asyncio.subprocess.Process:
    options = {
        "url": engine.url.render_as_string(hide_password=False),
        "name": "b2c2-process-test",
        "user": str(user),
        "repository": str(repo),
        "key": key,
        "mode": mode,
        "redis": os.environ["DMC268_TEST_REDIS_URL"],
        "state_key": state_key,
    }
    return await asyncio.create_subprocess_exec(
        sys.executable,
        str(Path(__file__).with_name("claim_process.py")),
        env=dict(os.environ) | {"B2C2_PROCESS_OPTIONS": json.dumps(options)},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def test_two_processes_cannot_spend_the_last_user_allowance(
    ledger_engine: AsyncEngine,
) -> None:
    user, repo = uuid4(), uuid4()
    ledger = PostgresSimulationDispatchLedger(
        async_sessionmaker(ledger_engine), hmac_secret=SECRET
    )
    for key in (b"one", b"two"):
        assert await ledger.claim(user, repo, key) == "AUTHORIZED"
    children = []
    try:
        async with ledger_engine.begin() as held:
            await held.execute(text("SELECT pg_advisory_xact_lock(268, 32)"))
            children = [
                await start_child(ledger_engine, user, repo, key)
                for key in ("three", "four")
            ]
            # The ordinary one-slot test pool is held. Use a separate observer pool.
            observer_engine = create_async_engine(
                ledger_engine.url, isolation_level="AUTOCOMMIT"
            )
            try:
                async with observer_engine.connect() as observer, asyncio.timeout(10):
                    while (
                        await observer.scalar(
                            text(
                                "SELECT count(*) FROM pg_stat_activity WHERE "
                                "application_name='b2c2-process-test' "
                                "AND wait_event_type='Lock'"
                            )
                        )
                        != 2
                    ):
                        await asyncio.sleep(0)
            finally:
                await observer_engine.dispose()
        async with asyncio.timeout(10):
            results = [await child.communicate() for child in children]
        assert sorted(output.decode().strip() for output, _ in results) == [
            "AUTHORIZED",
            "RATE_USER",
        ]
        assert all(child.returncode == 0 for child in children), results
    finally:
        for child in children:
            if child.returncode is None:
                child.kill()
                await child.wait()


@pytest.mark.parametrize(
    "mode,marker",
    [
        ("crash_after_claim", "CLAIM_ACK_BEFORE_HTTP"),
        ("crash_during_http", "MOCK_HTTP_ACCEPTED"),
    ],
)
async def test_process_crash_cannot_resume_claim_even_after_redis_rollback(
    mode: str,
    marker: str,
    ledger_engine: AsyncEngine,
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
    redis_client: Redis,
    state_key: str,
) -> None:
    user, repo = uuid4(), uuid4()
    snapshot = await redis_client.get(state_key)
    assert snapshot is not None
    child = await start_child(ledger_engine, user, repo, "crash", mode, state_key)
    try:
        async with asyncio.timeout(10):
            output, errors = await child.communicate()
        assert child.returncode == 17, errors
        assert output.decode().strip() == marker
        await redis_client.set(state_key, snapshot)
        ledger = PostgresSimulationDispatchLedger(
            async_sessionmaker(ledger_engine), hmac_secret=SECRET
        )
        coordinator = SimulationExecutionCoordinator[object](
            admissions[1], ledger=ledger
        )
        paid = PaidBoundary()
        assert (
            await coordinator.execute(user, repo, b"crash", paid.execute)
        ).status == "DUPLICATE"
        assert paid.calls == []
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()
