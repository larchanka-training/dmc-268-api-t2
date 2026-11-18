"""Read-only Playground inventory over SSH stdin; never starts infrastructure."""

import hashlib
import ipaddress
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path("/opt/dmc-268-t2")
PROJECT = "dmc-268-t2-staging"
VOLUME = PROJECT + "_simulation-redis-data"
CHECKS = (
    "external_volume",
    "redis_container",
    "redis_mount",
    "dedicated_instance",
    "redis_policy",
    "admission_state",
    "redis_loss_indicators",
    "github_hmac_secret",
    "playground_disabled",
    "rollback_clear",
    "api_image",
    "deployment_configuration",
    "active_env_compose_match",
    "alembic_revision",
    "ledger_tables",
    "ledger_singleton",
    "ledger_unblocked",
    "ledger_identity",
    "postgres_primary",
    "postgres_synchronous_commit",
)
RUNTIME_CHECKS = {
    "redis_policy",
    "admission_state",
    "redis_loss_indicators",
    "alembic_revision",
    "ledger_tables",
    "ledger_singleton",
    "ledger_unblocked",
    "ledger_identity",
    "postgres_primary",
    "postgres_synchronous_commit",
}
MANUAL = (
    "hmac_history",
    "volume_provenance",
    "capacity_acceptance",
    "postgres_backup_restore",
    "dedicated_redis_offhost_backup_restore",
    "recovery_incidents",
)


RUNTIME_PROGRAM = r'''
import asyncio
import json
import os
import sys


async def read_ledger():
    import asyncpg

    connection = await asyncpg.connect(
        host="db",
        port=5432,
        user=os.environ["POSTGRES__USER"],
        password=os.environ["POSTGRES__PASSWORD"],
        database=os.environ["POSTGRES__DB"],
        timeout=5,
        command_timeout=5,
        server_settings={
            "default_transaction_read_only": "on",
            "statement_timeout": "5000",
            "lock_timeout": "1000",
            "idle_in_transaction_session_timeout": "15000",
        },
    )
    found = {}
    try:
        async with connection.transaction(isolation="repeatable_read", readonly=True):
            revisions = await connection.fetch(
                "SELECT version_num FROM alembic_version LIMIT 2"
            )
            versions = [row["version_num"] for row in revisions]
            found["alembic_revision"] = "READY" if versions == ["0022"] else "BLOCKED"
            found["revision"] = (
                versions[0]
                if len(versions) == 1
                and versions[0] in {f"{i:04d}" for i in range(1, 23)}
                else None
            )
            primary = (
                await connection.fetch(
                    "SELECT NOT pg_is_in_recovery() AS primary, "
                    "current_setting('synchronous_commit') = 'on' AS durable"
                )
            )[0]
            found["postgres_primary"] = (
                "READY" if primary["primary"] is True else "BLOCKED"
            )
            found["postgres_synchronous_commit"] = (
                "READY" if primary["durable"] is True else "BLOCKED"
            )
            tables = (
                await connection.fetch("""SELECT
                to_regclass('public.simulation_dispatch_control')
                    IS NOT NULL AS control,
                to_regclass('public.simulation_dispatch_claim')
                    IS NOT NULL AS claims""")
            )[0]
            found["ledger_tables"] = (
                "READY" if tables["control"] and tables["claims"] else "MISSING"
            )
            claims = None
            if tables["claims"]:
                counts = (
                    await connection.fetch("""SELECT count(*) AS retained_claims,
                    count(*) FILTER (WHERE operation_digest !~ '^[0-9a-f]{64}$'
                        OR state NOT IN ('DISPATCH_AUTHORIZED','COMPLETED','UNKNOWN'))
                        AS invalid_claims
                    FROM simulation_dispatch_claim""")
                )[0]
                claims = counts["retained_claims"]
                found["retained_claims"] = claims
                if counts["invalid_claims"] != 0:
                    found["ledger_identity"] = "BLOCKED"
            if tables["control"]:
                rows = await connection.fetch(
                    "SELECT id, blocked, hmac_config_id "
                    "FROM simulation_dispatch_control LIMIT 2"
                )
                if not rows:
                    found["ledger_singleton"] = "MISSING"
                elif len(rows) != 1 or rows[0]["id"] != 1:
                    found["ledger_singleton"] = "BLOCKED"
                else:
                    found["ledger_singleton"] = "READY"
                    found["ledger_unblocked"] = (
                        "READY" if rows[0]["blocked"] is False else "BLOCKED"
                    )
                    if claims is not None and found.get("ledger_identity") != "BLOCKED":
                        identity = rows[0]["hmac_config_id"]
                        if identity is None:
                            found["ledger_identity"] = (
                                "READY" if claims == 0 else "BLOCKED"
                            )
                        else:
                            try:
                                from app.core.simulation_hmac import (
                                    decode_simulation_hmac,
                                    simulation_hmac_config_id,
                                )

                                expected = simulation_hmac_config_id(
                                    decode_simulation_hmac(
                                        os.environ.get("SIMULATION__HMAC_SECRET_B64")
                                    )
                                )
                                found["ledger_identity"] = (
                                    "READY" if identity == expected else "BLOCKED"
                                )
                            except Exception:
                                pass
    finally:
        await connection.close()
    return found


async def audit():
    result = {
        name: "UNKNOWN"
        for name in (
            "redis_policy",
            "admission_state",
            "redis_loss_indicators",
            "alembic_revision",
            "ledger_tables",
            "ledger_singleton",
            "ledger_unblocked",
            "ledger_identity",
            "postgres_primary",
            "postgres_synchronous_commit",
        )
    }
    if sys.argv[1] != "-":
        try:
            from redis.asyncio import Redis

            client = Redis(
                host=sys.argv[1],
                port=6379,
                db=0,
                decode_responses=True,
                protocol=2,
                socket_timeout=5,
                socket_connect_timeout=5,
                lib_name=None,
                lib_version=None,
            )
            try:
                expected = {
                    "appendonly": "yes",
                    "appendfsync": "always",
                    "no-appendfsync-on-rewrite": "no",
                    "aof-load-truncated": "no",
                    "maxmemory-policy": "noeviction",
                }
                policy = await client.config_get(*expected)
                persistence = await client.info("persistence")
                result["redis_policy"] = (
                    "READY"
                    if (
                        policy == expected
                        and persistence.get("aof_enabled") == 1
                        and persistence.get("aof_last_write_status") == "ok"
                    )
                    else "BLOCKED"
                )
                stats = await client.info("stats")
                counts = [stats.get(name) for name in ("expired_keys", "evicted_keys")]
                if all(type(value) is int and value >= 0 for value in counts):
                    result["redis_loss_indicators"] = (
                        "READY" if counts == [0, 0] else "BLOCKED"
                    )
                from app.adapters.simulation_admission import STATE_KEY

                kind = await client.type(STATE_KEY)
                ttl = await client.pttl(STATE_KEY)
                if kind == "none":
                    result["admission_state"] = "MISSING"
                elif kind != "string" or ttl != -1:
                    result["admission_state"] = "BLOCKED"
                else:
                    try:
                        from app.adapters.simulation_admission import _STATE
                        from app.core.simulation_hmac import (
                            decode_simulation_hmac,
                            simulation_hmac_config_id,
                        )

                        identity = simulation_hmac_config_id(
                            decode_simulation_hmac(
                                os.environ.get("SIMULATION__HMAC_SECRET_B64")
                            )
                        )
                        valid = await client.eval_ro(
                            _STATE
                            + "\nlocal ok,s=pcall(load_state,ARGV[1]); "
                            "return ok and s and 1 or 0",
                            1,
                            STATE_KEY,
                            identity,
                        )
                        result["admission_state"] = "READY" if valid == 1 else "BLOCKED"
                    except Exception:
                        pass
            finally:
                await client.aclose()
        except Exception:
            result["redis_policy"] = "UNKNOWN"
    try:
        result.update(await read_ledger())
    except Exception:
        pass
    print(json.dumps(result))


asyncio.run(audit())
'''


def docker(*args: str, program: str | None = None) -> str:
    result = subprocess.run(
        ["docker", *args],
        input=program,
        capture_output=True,
        text=True,
        timeout=70 if program is not None else 8,
        check=False,
    )
    if result.returncode:
        raise ValueError("inspection_failed")
    return result.stdout


def container(service: str) -> dict[str, Any] | None:
    ids = docker(
        "ps",
        "--all",
        "--filter",
        "label=com.docker.compose.project=" + PROJECT,
        "--filter",
        "label=com.docker.compose.service=" + service,
        "--format",
        "{{.ID}}",
    ).splitlines()
    if not ids:
        return None
    if len(ids) != 1 or not re.fullmatch(r"[0-9a-f]{12,64}", ids[0]):
        raise ValueError("ambiguous_container")
    return json.loads(docker("inspect", ids[0]))[0]  # type: ignore[no-any-return]


def active_environment() -> dict[str, str]:
    values = {}
    for line in (ROOT / ".env.staging").read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        name, value = line.split("=", 1)
        if name in values:
            raise ValueError("duplicate_environment")
        values[name] = (json.loads(value) if value.startswith('"') else value).replace(
            "$$", "$"
        )
    return values


def main() -> int:  # noqa: PLR0912, PLR0915 -- independent reads retain partial evidence
    expected, secret_present, compose_hash = sys.argv[1:]
    if (
        not re.fullmatch(r"[0-9a-f]{40}", expected)
        or secret_present not in {"true", "false"}
        or not re.fullmatch(r"[0-9a-f]{64}", compose_hash)
    ):
        raise ValueError("invalid_input")
    checks = dict.fromkeys(CHECKS, "UNKNOWN")
    checks.update(dict.fromkeys(MANUAL, "MANUAL EVIDENCE REQUIRED"))
    checks["github_hmac_secret"] = "READY" if secret_present == "true" else "MISSING"
    try:
        (ROOT / ".env.staging.simulation-rollback").lstat()
        checks["rollback_clear"] = "BLOCKED"
    except FileNotFoundError:
        checks["rollback_clear"] = "READY"
    except OSError:
        pass
    try:
        volumes = docker(
            "volume", "ls", "--filter", "name=" + VOLUME, "--format", "{{.Name}}"
        ).splitlines()
        checks["external_volume"] = "READY" if VOLUME in volumes else "MISSING"
    except Exception:
        pass
    simulation: dict[str, Any] | None = None
    try:
        simulation = container("simulation-redis")
        checks["redis_container"] = (
            "MISSING"
            if simulation is None
            else ("READY" if simulation["State"]["Running"] else "BLOCKED")
        )
    except Exception:
        pass
    if simulation is not None:
        mounts = [
            m for m in simulation.get("Mounts", []) if m.get("Destination") == "/data"
        ]
        checks["redis_mount"] = (
            "READY"
            if len(mounts) == 1
            and (
                mounts[0].get("Type") == "volume"
                and mounts[0].get("Name") == VOLUME
                and mounts[0].get("RW") is True
            )
            else "BLOCKED"
        )
    api = None
    try:
        api = container("api")
        if api is not None and api["State"]["Running"]:
            env = dict(item.split("=", 1) for item in api["Config"]["Env"])
            checks["playground_disabled"] = (
                "READY" if env.get("SIMULATION__ENABLED") == "false" else "BLOCKED"
            )
            image = "ghcr.io/larchanka-training/dmc-268-api-t2:sha-" + expected
            checks["api_image"] = (
                "READY"
                if api["Config"]["Image"] == image and env.get("GIT_SHA") == expected
                else "BLOCKED"
            )
            active = active_environment()
            same = active.get("API_IMAGE") == api["Config"]["Image"]
            for key in (
                "SIMULATION__ENABLED",
                "SIMULATION__REDIS_URL",
                "SIMULATION__MANIFEST_PATH",
                "SIMULATION__HMAC_SECRET_B64",
                "ANALYSIS__CONFIG_PATH",
            ):
                same = same and active.get(key, "") == env.get(key, "")
            same = (
                same
                and hashlib.sha256(
                    (ROOT / "docker-compose.staging.yml").read_bytes()
                ).hexdigest()
                == compose_hash
            )
            checks["active_env_compose_match"] = "READY" if same else "BLOCKED"
    except Exception:
        pass
    details: dict[str, Any] = {}
    address = "-"
    try:
        shared = container("redis")
        if simulation is not None and shared is not None:
            network = PROJECT + "_backend"
            sim_ip = simulation["NetworkSettings"]["Networks"][network]["IPAddress"]
            shared_ip = shared["NetworkSettings"]["Networks"][network]["IPAddress"]
            valid_ip = str(ipaddress.IPv4Address(sim_ip)) == sim_ip
            distinct = (
                valid_ip
                and bool(shared_ip)
                and sim_ip != shared_ip
                and simulation["Id"] != shared["Id"]
            )
            shared_sources = {
                m.get("Name")
                for m in shared.get("Mounts", [])
                if m.get("Type") == "volume"
            }
            distinct = (
                distinct
                and VOLUME not in shared_sources
                and checks["redis_mount"] == "READY"
            )
            checks["dedicated_instance"] = "READY" if distinct else "BLOCKED"
            if distinct and checks["redis_container"] == "READY":
                address = sim_ip
    except Exception:
        pass
    if api is not None and api.get("State", {}).get("Running"):
        try:
            api_id = api["Id"]
            if not re.fullmatch(r"[0-9a-f]{64}", api_id):
                raise ValueError("invalid_container")
            runtime = json.loads(
                docker(
                    "exec",
                    "-i",
                    api_id,
                    "python",
                    "-B",
                    "-I",
                    "-",
                    address,
                    program=RUNTIME_PROGRAM,
                )
            )
            for key in RUNTIME_CHECKS:
                if key in runtime and runtime[key] in {
                    "READY",
                    "MISSING",
                    "BLOCKED",
                    "UNKNOWN",
                }:
                    checks[key] = runtime[key]
            count = runtime.get("retained_claims")
            if type(count) is int and count >= 0:
                details["retained_claims"] = count
            revision = runtime.get("revision")
            details["revision"] = (
                revision if revision in {f"{i:04d}" for i in range(1, 23)} else None
            )
        except Exception:
            pass
    metrics: dict[str, int] = {}
    try:
        disk = os.statvfs(ROOT)
        metrics["disk_available_bytes"] = disk.f_bavail * disk.f_frsize
        metrics["inodes_available"] = disk.f_favail
        memory = dict(
            line.split(":", 1)
            for line in Path("/proc/meminfo").read_text().splitlines()
        )
        metrics["memory_available_bytes"] = (
            int(memory["MemAvailable"].split()[0]) * 1024
        )
    except Exception:
        pass
    print(
        json.dumps(
            {
                "checks": checks,
                "metrics": metrics,
                **details,
                "activation_authorized": False,
                "history_continuity": "unproven",
            }
        )
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print('{"inspection":"UNKNOWN","activation_authorized":false}')
        sys.exit(1)
