"""Read-only Playground audit at the workflow/SSH and Docker runtime seams."""

import hashlib
import json
import os
import runpy
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from app.adapters.simulation_admission import _STATE, STATE_KEY

ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github/workflows/staging-simulation-audit.yml"
SCRIPT = ROOT / ".github/scripts/staging_simulation_audit.py"
SHA = "a30aa96d3cac79a6a9f6bbb505405894a42764c8"
VOLUME = "dmc-268-t2-staging_simulation-redis-data"
SECRET = "synthetic-secret"
RETAINED_CLAIMS = 7
TIMEOUT = 5


def test_manual_main_only_workflow_reuses_safe_ssh() -> None:
    config = yaml.safe_load(WORKFLOW.read_text())
    assert config.get("on", config.get(True)) == {"workflow_dispatch": None}
    assert config["permissions"] == {"contents": "read"}
    assert config["concurrency"] == {
        "group": "deploy-staging",
        "cancel-in-progress": False,
    }
    job = config["jobs"]["audit"]
    assert job["if"] == (
        "github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main'"
    )
    deploy = yaml.safe_load(
        (ROOT / ".github/workflows/deploy-staging.yml").read_text()
    )["jobs"]["deploy"]
    for name in ("SSH_HOST", "SSH_HOST_KEY"):
        assert job["env"][name] == deploy["env"][name]
    assert job["env"]["SSH_USER"] == "${{ secrets.VPS_DMC268_U }}"
    assert job["env"]["SSHPASS"] == "${{ secrets.VPS_DMC268_P }}"
    assert job["timeout-minutes"] <= TIMEOUT
    source = WORKFLOW.read_text()
    for required in (
        "StrictHostKeyChecking=yes",
        "ConnectTimeout=10",
        "ServerAliveCountMax=2",
        "timeout 120s",
        "persist-credentials: false",
        "secrets.STAGING_SIMULATION_HMAC_SECRET_B64 != ''",
        "< .github/scripts/staging_simulation_audit.py",
        "2>/dev/null",
    ):
        assert required in source
    assert "ssh-keyscan" not in source
    for step in job["steps"]:
        if "run" in step:
            assert (
                subprocess.run(
                    ["bash", "-n"],
                    input=step["run"],
                    text=True,
                    capture_output=True,
                    check=False,
                ).returncode
                == 0
            )


@pytest.fixture
def audit_cli(tmp_path: Path) -> Any:
    docker = tmp_path / "docker"
    docker.write_text("""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path

data = json.loads(Path(os.environ["AUDIT_DATA"]).read_text())
args = sys.argv[1:]
with open(os.environ["DOCKER_CALLS"], "a") as out:
    out.write(json.dumps(args) + "\\n")
if data.get("inspection_error"):
    print("synthetic-secret", file=sys.stderr)
    sys.exit(1)
if args[:2] == ["volume", "ls"]:
    print("dmc-268-t2-staging_simulation-redis-data" if data.get("volume") else "")
elif args[:1] == ["ps"]:
    service = next(
        a.rsplit("=", 1)[1]
        for a in args
        if a.startswith("label=com.docker.compose.service=")
    )
    sys.stdout.write(
        {"api": "a" * 12, "redis": "b" * 12, "simulation-redis": "c" * 12}.get(
            service, ""
        )
        if data.get(service, True)
        else ""
    )
elif args[:1] == ["inspect"]:
    service = {"a": "api", "b": "redis", "c": "simulation-redis"}[args[-1][0]]
    print(json.dumps([data["containers"][service]]))
elif args[:1] == ["exec"]:
    print(json.dumps(data.get("runtime", {})))
else:
    raise AssertionError(args)
""")
    docker.chmod(0o755)

    def run(data: dict[str, Any], secret: str = "true") -> dict[str, Any]:
        script = tmp_path / "audit.py"
        script.write_text(SCRIPT.read_text().replace("/opt/dmc-268-t2", str(tmp_path)))
        (tmp_path / "data.json").write_text(json.dumps(data))
        (tmp_path / "docker-calls").write_text("")
        (tmp_path / ".env.staging").write_text(data.get("active_env", ""))
        (tmp_path / "docker-compose.staging.yml").write_text("compose-fixture")
        if data.get("rollback"):
            (tmp_path / ".env.staging.simulation-rollback").mkdir(exist_ok=True)
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                str(script),
                SHA,
                secret,
                hashlib.sha256(b"compose-fixture").hexdigest(),
            ],
            env=os.environ
            | {
                "PATH": f"{tmp_path}:{os.environ['PATH']}",
                "AUDIT_DATA": str(tmp_path / "data.json"),
                "DOCKER_CALLS": str(tmp_path / "docker-calls"),
            },
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert SECRET not in result.stdout + result.stderr
        return json.loads(result.stdout)  # type: ignore[no-any-return]

    return run


def test_missing_external_volume_is_reported_without_creation(audit_cli: Any) -> None:
    report = audit_cli({"volume": False})
    assert report["checks"]["external_volume"] == "MISSING"


def test_absent_redis_container_is_missing(audit_cli: Any) -> None:
    report = audit_cli({"volume": True, "simulation-redis": False})
    assert report["checks"]["redis_container"] == "MISSING"


def test_stopped_redis_is_blocked_without_starting(audit_cli: Any) -> None:
    report = audit_cli(
        {
            "volume": True,
            "containers": {
                "simulation-redis": {"State": {"Running": False}},
            },
        }
    )
    assert report["checks"]["redis_container"] == "BLOCKED"


def test_wrong_data_mount_blocks_readiness(audit_cli: Any) -> None:
    report = audit_cli(
        {
            "volume": True,
            "containers": {
                "simulation-redis": {
                    "State": {"Running": True},
                    "Mounts": [
                        {
                            "Type": "volume",
                            "Name": "wrong-volume",
                            "Destination": "/data",
                            "RW": True,
                        }
                    ],
                },
            },
        }
    )
    assert report["checks"]["redis_mount"] == "BLOCKED"


def test_missing_hmac_secret_is_missing_not_approval(audit_cli: Any) -> None:
    report = audit_cli({"volume": False, "simulation-redis": False}, secret="false")
    assert report["checks"]["github_hmac_secret"] == "MISSING"
    assert report["checks"]["hmac_history"] == "MANUAL EVIDENCE REQUIRED"
    assert report["activation_authorized"] is False


def test_incomplete_rollback_blocks_audit(audit_cli: Any) -> None:
    report = audit_cli({"volume": False, "simulation-redis": False, "rollback": True})
    assert report["checks"]["rollback_clear"] == "BLOCKED"


@pytest.fixture
def runtime_cli(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> Any:

    def run(mode: str) -> dict[str, Any]:
        class Redis:
            def __init__(self, **kwargs: Any) -> None:
                assert kwargs["host"] == "172.18.0.5"
                assert kwargs["socket_timeout"] <= TIMEOUT

            async def config_get(self, *names: str) -> dict[str, str]:
                if mode == "unreadable":
                    raise OSError(SECRET)
                policy = {
                    "appendonly": "yes",
                    "appendfsync": "always",
                    "no-appendfsync-on-rewrite": "no",
                    "aof-load-truncated": "no",
                    "maxmemory-policy": "noeviction",
                }
                if mode == "unsafe":
                    policy["appendfsync"] = "everysec"
                return {n: policy[n] for n in names}

            async def info(self, section: str) -> dict[str, Any]:
                if section == "stats":
                    return {"expired_keys": 2, "evicted_keys": 0}
                assert section == "persistence"
                return {"aof_enabled": 1, "aof_last_write_status": "ok"}

            async def type(self, key: str) -> str:
                return "none" if mode == "state_missing" else "string"

            async def pttl(self, key: str) -> int:
                return 5000 if mode == "ttl" else -1

            async def eval_ro(
                self, script: str, count: int, key: str, identity: str
            ) -> int:

                assert script.startswith(_STATE)
                assert count == 1 and key == STATE_KEY
                return 0 if mode == "corrupt" else 1

            async def aclose(self) -> None:
                pass

        class Transaction:
            async def __aenter__(self) -> None:
                pass

            async def __aexit__(self, *args: object) -> None:
                pass

        class Connection:
            def transaction(self, **kwargs: Any) -> Transaction:
                assert kwargs == {"isolation": "repeatable_read", "readonly": True}
                return Transaction()

            async def fetch(self, query: str) -> list[dict[str, Any]]:
                assert query.lstrip().startswith("SELECT")
                if "alembic_version" in query:
                    return [{"version_num": "0022"}]
                if "to_regclass" in query:
                    return [
                        {
                            "control": mode != "tables_missing",
                            "claims": mode != "tables_missing",
                        }
                    ]
                if "FROM simulation_dispatch_control" in query:
                    return (
                        []
                        if mode == "singleton_missing"
                        else [
                            {
                                "id": 1,
                                "blocked": mode == "ledger_blocked",
                                "hmac_config_id": None,
                            }
                        ]
                    )
                if "FROM simulation_dispatch_claim" in query:
                    return [{"retained_claims": 0, "invalid_claims": 0}]
                if "pg_is_in_recovery" in query:
                    return [{"primary": True, "durable": True}]
                raise AssertionError(query)

            async def close(self) -> None:
                pass

        async def connect(**kwargs: Any) -> Connection:
            assert kwargs["timeout"] <= TIMEOUT and kwargs["command_timeout"] <= TIMEOUT
            assert kwargs["server_settings"]["default_transaction_read_only"] == "on"
            return Connection()

        for name in ("POSTGRES__USER", "POSTGRES__PASSWORD", "POSTGRES__DB"):
            monkeypatch.setenv(name, SECRET)
        monkeypatch.setitem(sys.modules, "asyncpg", SimpleNamespace(connect=connect))
        monkeypatch.setitem(sys.modules, "redis.asyncio", SimpleNamespace(Redis=Redis))
        monkeypatch.setattr(sys, "argv", ["<runtime>", "172.18.0.5"])
        monkeypatch.setenv(
            "SIMULATION__HMAC_SECRET_B64",
            "eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHg=",
        )
        program = runpy.run_path(str(SCRIPT))["RUNTIME_PROGRAM"]
        exec(compile(program, "<runtime>", "exec"), {})
        output = capsys.readouterr()
        assert SECRET not in output.out + output.err
        return json.loads(output.out)  # type: ignore[no-any-return]

    return run


@pytest.mark.parametrize(
    ("mode", "status"), [("unsafe", "BLOCKED"), ("unreadable", "UNKNOWN")]
)
def test_redis_policy_cannot_pass_when_unsafe_or_unreadable(
    runtime_cli: Any, mode: str, status: str
) -> None:
    assert runtime_cli(mode)["redis_policy"] == status


def test_redis_reports_observed_history_loss(runtime_cli: Any) -> None:
    assert runtime_cli("loss")["redis_loss_indicators"] == "BLOCKED"


@pytest.mark.parametrize(
    ("mode", "status"), [("state_missing", "MISSING"), ("ttl", "BLOCKED")]
)
def test_admission_missing_or_expiring_fails_closed(
    runtime_cli: Any, mode: str, status: str
) -> None:
    assert runtime_cli(mode)["admission_state"] == status


@pytest.mark.parametrize(
    ("mode", "status"), [("valid", "READY"), ("corrupt", "BLOCKED")]
)
def test_admission_structure_uses_canonical_readonly_loader(
    runtime_cli: Any, mode: str, status: str
) -> None:
    assert runtime_cli(mode)["admission_state"] == status


@pytest.mark.parametrize(
    ("mode", "key", "status"),
    [
        ("ledger_blocked", "ledger_unblocked", "BLOCKED"),
        ("singleton_missing", "ledger_singleton", "MISSING"),
        ("tables_missing", "ledger_tables", "MISSING"),
    ],
)
def test_ledger_blocked_or_missing_cannot_pass(
    runtime_cli: Any, mode: str, key: str, status: str
) -> None:
    assert runtime_cli(mode)[key] == status


def test_ledger_readiness_contract_is_checked_without_exposing_identity(
    runtime_cli: Any,
) -> None:
    report = runtime_cli("valid")
    assert {
        key: report[key]
        for key in (
            "alembic_revision",
            "ledger_tables",
            "ledger_singleton",
            "ledger_unblocked",
            "ledger_identity",
            "postgres_primary",
            "postgres_synchronous_commit",
        )
    } == dict.fromkeys(
        (
            "alembic_revision",
            "ledger_tables",
            "ledger_singleton",
            "ledger_unblocked",
            "ledger_identity",
            "postgres_primary",
            "postgres_synchronous_commit",
        ),
        "READY",
    )
    assert report["retained_claims"] == 0
    assert report["revision"] == "0022"


def deployed_data() -> dict[str, Any]:
    return {
        "volume": True,
        "containers": {
            "api": {
                "Id": "a" * 64,
                "State": {"Running": True},
                "Config": {
                    "Image": "ghcr.io/larchanka-training/dmc-268-api-t2:sha-" + SHA,
                    "Env": [
                        "GIT_SHA=" + SHA,
                        "SIMULATION__ENABLED=false",
                        "REDIS__URL=redis://redis:6379/0",
                        "SIMULATION__REDIS_URL=redis://simulation-redis:6379/0",
                    ],
                },
                "Mounts": [],
            },
            "redis": {
                "Id": "b" * 64,
                "Mounts": [],
                "NetworkSettings": {
                    "Networks": {
                        "dmc-268-t2-staging_backend": {"IPAddress": "172.18.0.4"}
                    }
                },
            },
            "simulation-redis": {
                "Id": "c" * 64,
                "State": {"Running": True},
                "Mounts": [
                    {
                        "Type": "volume",
                        "Name": VOLUME,
                        "Destination": "/data",
                        "RW": True,
                    }
                ],
                "NetworkSettings": {
                    "Networks": {
                        "dmc-268-t2-staging_backend": {"IPAddress": "172.18.0.5"}
                    }
                },
            },
        },
        "active_env": "API_IMAGE=ghcr.io/larchanka-training/dmc-268-api-t2:sha-"
        + SHA
        + "\nSIMULATION__ENABLED=false\nSIMULATION__REDIS_URL=redis://simulation-redis:6379/0\n",
    }


def test_deployed_image_and_disabled_configuration_match_expected_sha(
    audit_cli: Any,
) -> None:
    report = audit_cli(deployed_data())
    for key in ("playground_disabled", "api_image", "active_env_compose_match"):
        assert report["checks"][key] == "READY"


def test_successful_inspection_uses_only_existing_containers_and_safe_output(
    audit_cli: Any, tmp_path: Path
) -> None:
    data = deployed_data()
    data["runtime"] = {
        "redis_policy": "READY",
        "admission_state": "READY",
        "retained_claims": 7,
        "revision": "0022",
        "hmac_config_id": SECRET,
        "unexpected": SECRET,
    }
    report = audit_cli(data)
    assert report["checks"]["dedicated_instance"] == "READY"
    assert report["checks"]["redis_policy"] == "READY"
    assert report["checks"]["admission_state"] == "READY"
    assert report["retained_claims"] == RETAINED_CLAIMS
    assert report["metrics"]["disk_available_bytes"] > 0
    assert report["metrics"]["inodes_available"] > 0
    assert report["metrics"]["memory_available_bytes"] > 0
    assert report["checks"]["capacity_acceptance"] == "MANUAL EVIDENCE REQUIRED"
    calls = [
        json.loads(line)
        for line in (tmp_path / "docker-calls").read_text().splitlines()
    ]
    assert any(c[:1] == ["exec"] for c in calls)
    assert all("-B" in c for c in calls if c[0] == "exec")
    assert all(c[0] in ("volume", "ps", "inspect", "exec") for c in calls)
    assert all(c[0] != "volume" or c[1] == "ls" for c in calls)


def test_inspection_and_ssh_errors_are_unknown_and_redacted(
    audit_cli: Any, tmp_path: Path
) -> None:
    report = audit_cli({"inspection_error": True})
    assert report["checks"]["external_volume"] == "UNKNOWN"
    assert report["checks"]["redis_container"] == "UNKNOWN"
    config = yaml.safe_load(WORKFLOW.read_text())
    step = config["jobs"]["audit"]["steps"][-1]
    for executable, body in (
        ("sshpass", "echo synthetic-secret >&2; exit 255"),
        ("sha256sum", "echo " + "0" * 64),
    ):
        path = tmp_path / executable
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o755)
    result = subprocess.run(
        ["bash", "-c", step["run"]],
        cwd=ROOT,
        env=os.environ
        | {
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "EXPECTED_SHA": SHA,
            "HMAC_SECRET_PRESENT": "false",
            "SSH_USER": "test",
            "SSH_HOST": "test",
        },
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode != 0
    assert SECRET not in result.stdout + result.stderr
    assert json.loads(result.stdout)["transport"] == "UNKNOWN"


def test_runtime_cannot_override_host_or_github_evidence(audit_cli: Any) -> None:
    data = deployed_data()
    data["runtime"] = {"github_hmac_secret": "READY", "rollback_clear": "READY"}
    data["rollback"] = True
    report = audit_cli(data, secret="false")
    assert report["checks"]["github_hmac_secret"] == "MISSING"
    assert report["checks"]["rollback_clear"] == "BLOCKED"


def test_unreadable_rollback_marker_is_unknown(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = runpy.run_path(str(SCRIPT))
    original_lstat = os.lstat

    def lstat(path: Any, **kwargs: Any) -> Any:
        if str(path).endswith(".env.staging.simulation-rollback"):
            raise PermissionError(SECRET)
        return original_lstat(path, **kwargs)

    def unavailable(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args[0], 1, "", SECRET)

    monkeypatch.setattr(os, "lstat", lstat)
    monkeypatch.setattr(subprocess, "run", unavailable)
    monkeypatch.setattr(sys, "argv", ["audit", SHA, "false", "0" * 64])
    assert module["main"]() == 0
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err
    assert json.loads(output.out)["checks"]["rollback_clear"] == "UNKNOWN"
