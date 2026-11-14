"""Explicit operator provisioning; never imported by API startup or deployment."""

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Mapping, Sequence
from typing import Never

from pydantic import SecretStr
from redis.asyncio import Redis

from app.adapters.simulation_admission import (
    _STATE,
    STATE_KEY,
    RedisSimulationAdmission,
    create_admission_client,
)
from app.core.config import Simulation
from app.core.simulation_hmac import decode_simulation_hmac

# Reuse the adapter's exact loader. EVAL_RO cannot mutate state even if this
# wrapper is changed accidentally. Identity decoding is diagnostic only.
_INSPECT = (
    _STATE
    + r"""
local identity = 'unknown'
if redis.call('TYPE', KEYS[1]).ok == 'string'
    and redis.call('STRLEN', KEYS[1]) <= 65536 then
    local ok, value = pcall(cjson.decode, redis.call('GET', KEYS[1]))
    if ok and type(value) == 'table'
        and type(value.hmac_config_id) == 'string' then
        identity = value.hmac_config_id == ARGV[1] and 'match' or 'mismatch'
    end
end
local ok, state = pcall(load_state, ARGV[1])
return {identity, ok and state and 'valid' or 'invalid'}
"""
)


# A new instance may have been inspected, authenticated and healthchecked.
# Unknown commands are deliberately refused rather than presumed read-only.
_INITIAL_READ_COMMANDS = frozenset(
    {
        "auth",
        "hello",
        "ping",
        "echo",
        "select",
        "quit",
        "info",
        "config|get",
        "client|setinfo",
        "client|setname",
        "client|getname",
        "client|id",
        "client|info",
        "client|list",
        "command",
        "command|info",
        "command|docs",
        "command|count",
        "command|list",
        "type",
        "pttl",
        "ttl",
        "exists",
        "dbsize",
        "get",
        "strlen",
        "dump",
        "time",
        "scan",
        "keys",
        "eval_ro",
        "evalsha_ro",
        "fcall_ro",
    }
)


class SafeParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise ValueError("invalid_arguments")


async def _operate(
    argv: Sequence[str], environ: Mapping[str, str]
) -> tuple[int, dict[str, object]]:
    parser = SafeParser(prog="provision-simulation-admission", allow_abbrev=False)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--create", action="store_true")
    parser.add_argument("--confirm-new-instance-no-history", action="store_true")
    parser.add_argument("--confirm-exclusive-dedicated-instance", action="store_true")
    try:
        args = parser.parse_args(argv)
    except ValueError:
        return 1, {"error": "invalid_arguments"}
    if args.create and not (
        args.confirm_new_instance_no_history
        and args.confirm_exclusive_dedicated_instance
    ):
        return 1, {"error": "operator_confirmation_required"}
    if args.create and environ.get("SIMULATION__ENABLED") != "false":
        return 1, {"error": "simulation_must_be_disabled"}
    try:
        secret = decode_simulation_hmac(environ.get("SIMULATION__HMAC_SECRET_B64"))
    except ValueError:
        return 1, {"error": "invalid_simulation_secret"}
    try:
        configured = environ.get("SIMULATION__REDIS_URL")
        simulation = Simulation(redis_url=SecretStr(configured) if configured else None)
        url = simulation.dedicated_redis_url("redis://redis:6379/0")
        simulation.dedicated_redis_url("redis://localhost:6379/0")
        if shared := environ.get("REDIS__URL"):
            simulation.dedicated_redis_url(shared)
    except ValueError:
        return 1, {"error": "simulation_redis_unavailable"}
    adapter = RedisSimulationAdmission(url, hmac_secret=secret)
    config_id = adapter.hmac_config_id
    await adapter.aclose()
    assert config_id is not None  # Validated secret; adapter owns the derivation.
    return await _operate_redis(url, config_id, args.create)


async def _operate_redis(
    url: str, config_id: str, create: bool
) -> tuple[int, dict[str, object]]:
    client = create_admission_client(url)
    try:
        report, error = await _inspect(client, config_id)
        if error is not None:
            return 1, report
        if create:
            return await _create(client, config_id, report)
        if report["state_exists"] and report["structure"] != "valid":
            report["error"] = "invalid_state"
            return 1, report
        return 0, report
    finally:
        await client.aclose()


async def _inspect(
    client: Redis, config_id: str
) -> tuple[dict[str, object], str | None]:
    await client.ping()
    policy = await client.config_get(
        "appendonly", "appendfsync", "no-appendfsync-on-rewrite", "maxmemory-policy"
    )
    info = await client.info("persistence")
    persistence: dict[str, object] = {
        name: policy.get(name)
        if policy.get(name) in {"yes", "no", "always", "everysec"}
        else "unexpected"
        for name in ("appendonly", "appendfsync", "no-appendfsync-on-rewrite")
    }
    report: dict[str, object] = {
        "activation_authorized": False,
        "history_continuity": "unproven",
        "mode": "check",
        "redis_available": True,
        "persistence": persistence,
    }
    persistence.update(
        {
            "aof_enabled": 1 if info.get("aof_enabled") == 1 else 0,
            "aof_last_write_status": "ok"
            if info.get("aof_last_write_status") == "ok"
            else "error",
        }
    )
    report["eviction_policy"] = (
        "noeviction" if policy.get("maxmemory-policy") == "noeviction" else "other"
    )
    kind = await client.type(STATE_KEY)
    report.update(
        state_exists=kind != "none",
        state_type=kind
        if kind in {"none", "string", "list", "hash", "set", "zset", "stream"}
        else "other",
        pttl_ms=await client.pttl(STATE_KEY),
    )
    report.update(structure="absent", hmac_identity="unknown")
    if kind != "none":
        identity, structure = await client.eval_ro(_INSPECT, 1, STATE_KEY, config_id)
        if identity not in {"match", "mismatch", "unknown"} or structure not in {
            "valid",
            "invalid",
        }:
            raise ValueError
        report.update(structure=structure, hmac_identity=identity)
    if policy.get("maxmemory-policy") != "noeviction":
        report["error"] = "unsafe_eviction_policy"
        return report, str(report["error"])
    if (
        policy.get("appendonly") != "yes"
        or policy.get("appendfsync") != "always"
        or policy.get("no-appendfsync-on-rewrite") != "no"
        or info.get("aof_enabled") != 1
        or info.get("aof_last_write_status") != "ok"
    ):
        report["error"] = "unsafe_persistence"
        return report, str(report["error"])
    return report, None


async def _new_instance_error(client: Redis) -> str | None:
    if await client.dbsize() != 0 or await client.info("keyspace"):
        return "instance_not_empty"
    stats = await client.info("stats")
    if stats.get("expired_keys") != 0 or stats.get("evicted_keys") != 0:
        return "prior_key_loss_detected"
    commands = await client.info("commandstats")
    if any(
        name.removeprefix("cmdstat_") not in _INITIAL_READ_COMMANDS for name in commands
    ):
        return "prior_use_or_unknown_command"
    return None


async def _create(
    client: Redis, config_id: str, report: dict[str, object]
) -> tuple[int, dict[str, object]]:
    if report["state_exists"]:
        report["error"] = "state_already_exists"
        return 1, report
    if error := await _new_instance_error(client):
        report["error"] = error
        return 1, report
    try:
        initial = json.dumps(
            {
                "schema_version": 3,
                "lease": None,
                "admissions": [],
                "consumed_keys": [],
                "hmac_config_id": config_id,
            },
            separators=(",", ":"),
        )
        if not await client.set(STATE_KEY, initial, nx=True):
            report["error"] = "concurrent_creation_refused"
            return 1, report
        if await client.get(STATE_KEY) != initial or await client.pttl(STATE_KEY) != -1:
            report["error"] = "creation_unconfirmed"
            return 1, report
        report.update(
            mode="create",
            status="created",
            state_exists=True,
            state_type="string",
            pttl_ms=-1,
            structure="valid",
            hmac_identity="match",
        )
        return 0, report
    except Exception:
        return 1, {"error": "creation_unconfirmed"}


async def run(argv: Sequence[str], environ: Mapping[str, str]) -> int:
    """Return one redacted result only after all resource cleanup succeeds."""
    try:
        code, report = await _operate(argv, environ)
    except Exception:
        code, report = 1, {"error": "redis_operation_failed"}
    report.update(activation_authorized=False, history_continuity="unproven")
    print(json.dumps(report))
    return code


if __name__ == "__main__":
    sys.exit(asyncio.run(run(sys.argv[1:], os.environ)))
