"""Read-only activation gate at its operator CLI interface; no paid calls."""

# ruff: noqa: F811 -- explicit local migration fixture import

import hashlib
import json
import os
from pathlib import Path
from typing import cast

import pytest
from redis.asyncio import Redis
from scripts import provision_simulation_admission as provisioning
from scripts import simulation_readiness as readiness
from scripts.simulation_readiness import run
from test_migration_upgrade import MigrationDatabase, migration_database  # noqa: F401
from test_simulation_provisioning import RedisDouble, environment

from app.adapters.simulation_admission import STATE_KEY


@pytest.fixture
def ready_redis(monkeypatch: pytest.MonkeyPatch) -> RedisDouble:
    client = RedisDouble()
    client.values[STATE_KEY] = "synthetic-state"
    client.structural_result = ["match", "valid"]
    monkeypatch.setattr(
        provisioning, "create_admission_client", lambda url: cast(Redis, client)
    )
    monkeypatch.setattr(
        readiness, "create_admission_client", lambda url: cast(Redis, client)
    )
    return client


async def invoke(env: dict[str, str]) -> int:
    with pytest.MonkeyPatch.context() as patch:
        for key in tuple(os.environ):
            if key.startswith(("SIMULATION", "LLM__", "ANALYSIS__", "POSTGRES__")):
                patch.delenv(key)
        for key, value in env.items():
            patch.setenv(key, value)
        return await run(os.environ)


def activation_environment() -> dict[str, str]:
    manifest = Path(__file__).parents[1] / "deploy/analysis-runtime.staging.json"
    return environment() | {
        "SIMULATION__ENABLED": "true",
        "DEPLOY_ENV": "staging",
        "SIMULATION_ACTIVATION_APPROVED": "true",
        "SIMULATION__MANIFEST_PATH": str(manifest),
        "SIMULATION_MANIFEST_SHA256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        "SIMULATION_EXPECTED_IMAGE_SHA": "a" * 40,
        "GIT_SHA": "a" * 40,
        "LLM__PRIMARY_PROVIDER": "eurorouter",
        "LLM__FALLBACK_ENABLED": "false",
        "LLM__EUROROUTER__BASE_URL": "https://api.eurouter.ai/api/v1",
        "LLM__EUROROUTER__MODEL": "mistral-small-4",
        "LLM__EUROROUTER__API_KEYS": "synthetic-readiness-key",
    }


@pytest.mark.parametrize(
    "override",
    [
        {"SIMULATION__ENABLED": "false"},
        {"SIMULATION__ENABLED": "TRUE"},
        {"DEPLOY_ENV": "develop"},
        {"SIMULATION_ACTIVATION_APPROVED": "false"},
    ],
)
async def test_activation_requires_explicit_staging_authorization(
    override: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert await invoke(activation_environment() | override) == 1
    assert json.loads(capsys.readouterr().out) == {
        "ready": False,
        "history_continuity": "unproven",
    }


@pytest.mark.parametrize("secret", ["", "bad!", "c3ludGhldGlj", " c3Nzc3Nz"])
async def test_missing_or_invalid_canonical_hmac_fails_without_leakage(
    secret: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        await invoke(activation_environment() | {"SIMULATION__HMAC_SECRET_B64": secret})
        == 1
    )
    assert (
        capsys.readouterr().out
        == '{"ready": false, "history_continuity": "unproven"}\n'
    )


@pytest.mark.parametrize(
    "defect",
    [
        "absent",
        "invalid",
        "hmac",
        "ttl",
        "offline",
        "aof",
        "fsync",
        "eviction",
        "write",
    ],
)
async def test_readiness_requires_actual_valid_durable_matching_redis_state(
    defect: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = RedisDouble()
    client.values[STATE_KEY] = "synthetic-state"
    client.structural_result = ["match", "valid"]
    if defect == "absent":
        client.values.clear()
    elif defect == "invalid":
        client.structural_result = ["unknown", "invalid"]
    elif defect == "hmac":
        client.structural_result = ["mismatch", "invalid"]
    elif defect == "ttl":
        client.ttl = 10000
    elif defect == "offline":
        client.available = False
    elif defect == "aof":
        client.policy["appendonly"] = "no"
    elif defect == "fsync":
        client.policy["appendfsync"] = "everysec"
    elif defect == "eviction":
        client.policy["maxmemory-policy"] = "allkeys-lru"
    elif defect == "write":
        client.persistence["aof_last_write_status"] = "err"
    monkeypatch.setattr(
        provisioning, "create_admission_client", lambda url: cast(Redis, client)
    )
    assert await invoke(activation_environment()) == 1
    assert json.loads(capsys.readouterr().out)["ready"] is False


@pytest.mark.parametrize("identity", ["mismatch", "null-with-claims"])
async def test_postgres_hmac_requires_match_or_pristine_unclaimed_initial_state(
    migration_database: MigrationDatabase,
    ready_redis: RedisDouble,
    capsys: pytest.CaptureFixture[str],
    identity: str,
) -> None:
    db = migration_database
    db.migrate("upgrade", "head")
    if identity == "mismatch":
        await db.connection.execute(
            "UPDATE simulation_dispatch_control SET hmac_config_id=$1", "b" * 64
        )
    else:
        await db.connection.execute(
            "INSERT INTO simulation_dispatch_claim"
            "(operation_digest,user_id,repository_id) "
            "VALUES ($1,gen_random_uuid(),gen_random_uuid())",
            "c" * 64,
        )
    before = await db.connection.fetch("SELECT * FROM simulation_dispatch_control")
    assert await invoke(activation_environment() | db.environment) == 1
    assert json.loads(capsys.readouterr().out)["ready"] is False
    assert (
        await db.connection.fetch("SELECT * FROM simulation_dispatch_control") == before
    )
    assert ready_redis.set_calls == []


@pytest.mark.parametrize(
    "url",
    [
        "redis://redis:6379/0",
        "redis://unexpected:6379/0",
        "redis://simulation-redis:6379/1",
        "",
    ],
)
async def test_exact_dedicated_instance_endpoint_is_required(
    url: str, ready_redis: RedisDouble, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await invoke(activation_environment() | {"SIMULATION__REDIS_URL": url}) == 1
    assert json.loads(capsys.readouterr().out)["ready"] is False
    assert not ready_redis.set_calls


@pytest.mark.parametrize(
    "override",
    [
        {"SIMULATION__MANIFEST_PATH": "/missing/manifest"},
        {"SIMULATION_MANIFEST_SHA256": "b" * 64},
        {"GIT_SHA": "b" * 40},
        {"LLM__PRIMARY_PROVIDER": "ollama"},
        {"LLM__EUROROUTER__MODEL": "other"},
        {"LLM__FALLBACK_ENABLED": "true"},
        {"LLM__EUROROUTER__API_KEYS": ""},
    ],
)
async def test_selected_image_manifest_and_approved_direct_provider_are_required(
    override: dict[str, str],
    ready_redis: RedisDouble,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert await invoke(activation_environment() | override) == 1
    assert json.loads(capsys.readouterr().out)["ready"] is False


async def test_readiness_requires_postgres_migration_0022(
    migration_database: MigrationDatabase,
    ready_redis: RedisDouble,
    capsys: pytest.CaptureFixture[str],
) -> None:
    migration_database.migrate("upgrade", "0021")
    assert await invoke(activation_environment() | migration_database.environment) == 1
    assert json.loads(capsys.readouterr().out)["ready"] is False


@pytest.mark.parametrize("control", ["blocked", "absent"])
async def test_postgres_deny_latch_must_exist_and_be_clear(
    migration_database: MigrationDatabase,
    ready_redis: RedisDouble,
    capsys: pytest.CaptureFixture[str],
    control: str,
) -> None:
    db = migration_database
    db.migrate("upgrade", "head")
    await db.connection.execute(
        "UPDATE simulation_dispatch_control SET blocked=true"
        if control == "blocked"
        else "DELETE FROM simulation_dispatch_control"
    )
    assert await invoke(activation_environment() | db.environment) == 1
    assert json.loads(capsys.readouterr().out)["ready"] is False


@pytest.mark.parametrize("counter", ["expired_keys", "evicted_keys"])
async def test_detected_redis_history_loss_blocks_an_otherwise_ready_deployment(
    migration_database: MigrationDatabase,
    ready_redis: RedisDouble,
    capsys: pytest.CaptureFixture[str],
    counter: str,
) -> None:
    db = migration_database
    db.migrate("upgrade", "head")
    env = db.environment | activation_environment()
    assert await invoke(env) == 0
    assert json.loads(capsys.readouterr().out)["ready"] is True
    ready_redis.loss_stats[counter] = 1
    assert await invoke(env) == 1
    assert json.loads(capsys.readouterr().out)["ready"] is False
    assert (
        await db.connection.fetchval(
            "SELECT hmac_config_id FROM simulation_dispatch_control"
        )
        is None
    )
    assert (
        await db.connection.fetchval("SELECT count(*) FROM simulation_dispatch_claim")
        == 0
    )
    assert ready_redis.set_calls == []
