"""Disposable transport verification of the readiness RED/GREEN contract.

Only the connection factory redirects the fixed logical dedicated endpoint to
an ephemeral loopback Redis. The real Lua inspector and PostgreSQL run unchanged.
"""

# ruff: noqa: F811 -- explicit local migration fixture import

import asyncio
import json
import os
import subprocess
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import httpx
import pytest
from redis.asyncio import Redis
from redis.exceptions import RedisError
from scripts import provision_simulation_admission as provisioning
from scripts import simulation_readiness as readiness
from test_migration_upgrade import MigrationDatabase, migration_database  # noqa: F401
from test_simulation_provisioning import CREATE
from test_simulation_readiness import activation_environment, invoke

from app.adapters.simulation_admission import (
    STATE_KEY,
    RedisSimulationAdmission,
    create_admission_client,
)
from app.core.simulation_hmac import decode_simulation_hmac, simulation_hmac_config_id

STARTUP_ATTEMPTS = 30


def docker(*arguments: str) -> str:
    return subprocess.run(
        ["docker", *arguments], capture_output=True, text=True, check=True, timeout=30
    ).stdout.strip()


@pytest.fixture
async def disposable_redis(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Redis]:
    if os.environ.get("DMC268_RUN_DOCKER_TESTS") != "1":
        pytest.skip("requires explicit disposable Docker test opt-in")
    name = f"dmc268-e3-readiness-{uuid4().hex}"
    factory = create_admission_client
    docker(
        "run",
        "--name",
        name,
        "--rm",
        "-d",
        "-p",
        "127.0.0.1::6379",
        "redis:8-alpine",
        "redis-server",
        "--appendonly",
        "yes",
        "--appendfsync",
        "always",
        "--no-appendfsync-on-rewrite",
        "no",
        "--maxmemory-policy",
        "noeviction",
        "--aof-load-truncated",
        "no",
    )
    client = None
    try:
        address = docker("port", name, "6379")
        url = f"redis://{address}/0"
        client = factory(url)
        for attempt in range(STARTUP_ATTEMPTS):
            try:
                await client.ping()
                break
            except RedisError:
                if attempt == STARTUP_ATTEMPTS - 1:
                    raise
                await asyncio.sleep(0.1)

        def connect(logical_url: str) -> Redis:
            assert logical_url == "redis://simulation-redis:6379/0"
            return factory(url)

        async def forbidden(*args: Any, **kwargs: Any) -> Any:
            pytest.fail("readiness attempted admission acquire or provider I/O")

        monkeypatch.setattr(provisioning, "create_admission_client", connect)
        monkeypatch.setattr(
            readiness, "create_admission_client", connect, raising=False
        )
        monkeypatch.setattr(RedisSimulationAdmission, "acquire", forbidden)
        monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
        yield client
    finally:
        if client is not None:
            await client.aclose()
        docker("rm", "-f", name)


async def assert_read_only(
    db: MigrationDatabase,
    client: Redis,
    environment: dict[str, str],
    expected: int,
    capsys: pytest.CaptureFixture[str],
) -> None:
    before = await client.dump(STATE_KEY)
    control = await db.connection.fetch("SELECT * FROM simulation_dispatch_control")
    claims = await db.connection.fetch("SELECT * FROM simulation_dispatch_claim")
    commands_before = await client.info("commandstats")
    assert await invoke(environment) == expected
    assert json.loads(capsys.readouterr().out) == {
        "ready": expected == 0,
        "history_continuity": "unproven",
    }
    assert await client.dump(STATE_KEY) == before
    assert (
        await db.connection.fetch("SELECT * FROM simulation_dispatch_control")
        == control
    )
    assert (
        await db.connection.fetch("SELECT * FROM simulation_dispatch_claim") == claims
    )
    commands_after = await client.info("commandstats")
    for command in ("set", "del", "eval", "evalsha", "flushdb", "flushall"):
        assert commands_after.get(f"cmdstat_{command}") == commands_before.get(
            f"cmdstat_{command}"
        )


@pytest.mark.parametrize("identity", ["initial", "matching"])
async def test_actual_ready_storage_is_read_only(
    migration_database: MigrationDatabase,
    disposable_redis: Redis,
    capsys: pytest.CaptureFixture[str],
    identity: str,
) -> None:
    db = migration_database
    db.migrate("upgrade", "head")
    environment = db.environment | activation_environment()
    assert (
        await provisioning.run(CREATE, environment | {"SIMULATION__ENABLED": "false"})
        == 0
    )
    capsys.readouterr()
    if identity == "matching":
        config_id = simulation_hmac_config_id(
            decode_simulation_hmac(environment["SIMULATION__HMAC_SECRET_B64"])
        )
        await db.connection.execute(
            "UPDATE simulation_dispatch_control SET hmac_config_id=$1", config_id
        )
        await db.connection.execute(
            "INSERT INTO simulation_dispatch_claim"
            "(operation_digest,user_id,repository_id) "
            "VALUES ($1,gen_random_uuid(),gen_random_uuid())",
            "c" * 64,
        )
    assert await disposable_redis.pttl(STATE_KEY) == -1
    await assert_read_only(db, disposable_redis, environment, 0, capsys)
    assert await disposable_redis.pttl(STATE_KEY) == -1


@pytest.mark.parametrize("defect", ["absent", "invalid", "hmac", "ttl"])
async def test_actual_invalid_state_is_rejected_without_repair(
    migration_database: MigrationDatabase,
    disposable_redis: Redis,
    capsys: pytest.CaptureFixture[str],
    defect: str,
) -> None:
    db = migration_database
    db.migrate("upgrade", "head")
    environment = db.environment | activation_environment()
    if defect != "absent":
        assert (
            await provisioning.run(
                CREATE, environment | {"SIMULATION__ENABLED": "false"}
            )
            == 0
        )
        capsys.readouterr()
        if defect == "invalid":
            await disposable_redis.set(STATE_KEY, '{"schema_version":2}')
        elif defect == "hmac":
            raw = await disposable_redis.get(STATE_KEY)
            assert isinstance(raw, str)
            value = json.loads(raw)
            value["hmac_config_id"] = "b" * 64
            await disposable_redis.set(STATE_KEY, json.dumps(value))
        else:
            await disposable_redis.pexpire(STATE_KEY, 100_000)
    await assert_read_only(db, disposable_redis, environment, 1, capsys)
