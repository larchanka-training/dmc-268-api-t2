"""Provision only UUID-owned disposable Redis; synthetic data and HMAC only.

Characterization of the CLI guarantees already developed through RED/GREEN, now
using the existing isolated Docker fixture and the real adapter's Lua validator.
"""

# ruff: noqa: PLR2004 -- schema, TTL and status literals are the public contract

import asyncio
import base64
import json
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from redis.client import NEVER_DECODE
from scripts.provision_simulation_admission import run
from test_simulation_redis_persistence import (
    RedisProject,
)
from test_simulation_redis_persistence import (
    redis_project as _redis_project,
)

from app.adapters.simulation_admission import STATE_KEY, RedisSimulationAdmission

# Explicit isolated fixture re-export, without global plugin registration.
redis_project = _redis_project

SECRET = b"synthetic-provision-hmac-material!"
CREATE = [
    "--create",
    "--confirm-new-instance-no-history",
    "--confirm-exclusive-dedicated-instance",
]


def environment(project: RedisProject, secret: bytes = SECRET) -> dict[str, str]:
    return {
        "SIMULATION__ENABLED": "false",
        "SIMULATION__REDIS_URL": project.url(),
        "REDIS__URL": project.url("redis"),
        "SIMULATION__HMAC_SECRET_B64": base64.b64encode(secret).decode(),
    }


async def test_provision_admit_duplicate_and_retain_across_restart_recreate(
    redis_project: RedisProject, capsys: pytest.CaptureFixture[str]
) -> None:
    env = environment(redis_project)
    async with Redis.from_url(env["REDIS__URL"], decode_responses=True) as shared:
        await shared.xadd("synthetic:review", {"task": "retained"})
        queue_before = await shared.xrange("synthetic:review")
        policy_before = await shared.config_get("appendonly", "maxmemory-policy")
        async with Redis.from_url(redis_project.url(), decode_responses=True) as client:
            assert await run([], env) == 0
            assert await client.dbsize() == 0
            assert await run(CREATE, env) == 0
            assert await client.pttl(STATE_KEY) == -1
            adapter = RedisSimulationAdmission(redis_project.url(), hmac_secret=SECRET)
            user, repository = uuid4(), uuid4()
            try:
                raw_initial = await client.get(STATE_KEY)
                assert raw_initial is not None
                initial = json.loads(raw_initial)
                assert initial["hmac_config_id"] == adapter.hmac_config_id
                first = await adapter.acquire(user, repository, b"synthetic-exact-key")
                assert first.status == "ACQUIRED" and first.lease is not None
                assert await adapter.release(first.lease.owner_token) == "RELEASED"
                before = await client.get(STATE_KEY)
                assert (
                    await adapter.acquire(user, repository, b"synthetic-exact-key")
                ).status == "DUPLICATE"
                assert await run(CREATE, env) == 1
                assert await run(["--check"], env) == 0
                assert (
                    await run(
                        CREATE,
                        environment(
                            redis_project, b"different-synthetic-hmac-material!"
                        ),
                    )
                    == 1
                )
                assert await client.get(STATE_KEY) == before
            finally:
                await adapter.aclose()
        for action in ["restart", "recreate"]:
            if action == "restart":
                redis_project.compose(
                    "--profile", "simulation", "restart", "simulation-redis"
                )
            else:
                redis_project.compose(
                    "--profile",
                    "simulation",
                    "up",
                    "-d",
                    "--no-deps",
                    "--force-recreate",
                    "--wait",
                    "--wait-timeout",
                    "40",
                    "simulation-redis",
                )
            env = environment(redis_project)
            async with Redis.from_url(
                redis_project.url(), decode_responses=True
            ) as client:
                assert await client.get(STATE_KEY) == before
                assert await client.pttl(STATE_KEY) == -1
                assert await run(CREATE, env) == 1
            adapter = RedisSimulationAdmission(redis_project.url(), hmac_secret=SECRET)
            try:
                assert (
                    await adapter.acquire(user, repository, b"synthetic-exact-key")
                ).status == "DUPLICATE"
            finally:
                await adapter.aclose()
        assert await shared.xrange("synthetic:review") == queue_before
        assert (
            await shared.config_get("appendonly", "maxmemory-policy") == policy_before
        )
    output = capsys.readouterr().out
    assert base64.b64encode(SECRET).decode() not in output
    assert "synthetic-exact-key" not in output
    assert '"digest"' not in output


@pytest.mark.parametrize(
    "variant", ["missing", "corrupt", "v1", "v2", "type", "ttl", "hmac"]
)
async def test_invalid_or_lost_state_is_never_repaired(
    variant: str, redis_project: RedisProject, capsys: pytest.CaptureFixture[str]
) -> None:
    env = environment(redis_project)
    adapter = RedisSimulationAdmission(redis_project.url(), hmac_secret=SECRET)
    try:
        async with Redis.from_url(redis_project.url(), decode_responses=True) as client:
            state: dict[str, object] = {
                "schema_version": 3,
                "lease": None,
                "admissions": [],
                "consumed_keys": [],
                "hmac_config_id": adapter.hmac_config_id,
            }
            if variant == "type":
                await client.rpush(STATE_KEY, "synthetic")
            elif variant != "missing":
                if variant in {"v1", "v2"}:
                    state["schema_version"] = 1 if variant == "v1" else 2
                if variant == "hmac":
                    state["hmac_config_id"] = "a" * 64
                await client.set(
                    STATE_KEY, "{broken" if variant == "corrupt" else json.dumps(state)
                )
                if variant == "ttl":
                    await client.pexpire(STATE_KEY, 60000)
            before = await client.execute_command(
                "DUMP", STATE_KEY, **{NEVER_DECODE: True}
            )  # type: ignore[no-untyped-call]
            assert await run(["--check"], env) == (0 if variant == "missing" else 1)
            report = json.loads(capsys.readouterr().out)
            assert report["structure"] == (
                "absent" if variant == "missing" else "invalid"
            )
            if variant == "hmac":
                assert report["hmac_identity"] == "mismatch"
            # Loss of state is not first provisioning: no provenance attestation.
            assert await run(["--create"] if variant == "missing" else CREATE, env) == 1
            assert (
                await adapter.acquire(uuid4(), uuid4(), b"synthetic-negative")
            ).status == "INVALID_STATE"
            after = await client.execute_command(
                "DUMP", STATE_KEY, **{NEVER_DECODE: True}
            )  # type: ignore[no-untyped-call]
            assert after == before
            ttl = await client.pttl(STATE_KEY)
            assert (
                (0 < ttl <= 60000)
                if variant == "ttl"
                else ttl == (-2 if variant == "missing" else -1)
            )
    finally:
        await adapter.aclose()


async def test_valid_old_snapshot_only_passes_structure(
    redis_project: RedisProject, capsys: pytest.CaptureFixture[str]
) -> None:
    env = environment(redis_project)
    async with Redis.from_url(redis_project.url(), decode_responses=True) as client:
        assert await run(CREATE, env) == 0
        snapshot = await client.get(STATE_KEY)
        assert isinstance(snapshot, str)
        adapter = RedisSimulationAdmission(redis_project.url(), hmac_secret=SECRET)
        try:
            result = await adapter.acquire(uuid4(), uuid4(), b"synthetic-lost-tail")
            assert result.status == "ACQUIRED"
        finally:
            await adapter.aclose()
        assert await client.get(STATE_KEY) != snapshot
        # Simulate a restore ONLY inside the disposable instance.
        await client.set(STATE_KEY, snapshot)
        capsys.readouterr()
        assert await run(["--check"], env) == 0
        report = json.loads(capsys.readouterr().out)
        assert report["structure"] == "valid"
        assert report["hmac_identity"] == "match"
        assert report["activation_authorized"] is False
        assert report["history_continuity"] == "unproven"
        assert await run(CREATE, env) == 1
        assert await client.get(STATE_KEY) == snapshot


async def test_independent_real_clients_create_once(
    redis_project: RedisProject,
) -> None:
    env = environment(redis_project)
    assert sorted(await asyncio.gather(run(CREATE, env), run(CREATE, env))) == [0, 1]
    async with Redis.from_url(redis_project.url(), decode_responses=True) as client:
        assert await client.dbsize() == 1
        assert await client.pttl(STATE_KEY) == -1
