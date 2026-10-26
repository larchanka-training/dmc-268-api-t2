"""The manually invoked audit exposes counts, without activating analysis."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github/workflows/staging-backlog-audit.yml"
CONNECT_TIMEOUT = 5
QUERY_TIMEOUT = 10
STATEMENT_TIMEOUT_MS = 10000
LOCK_TIMEOUT_MS = 1000
RESP2 = 2


def workflow() -> dict[Any, Any]:
    return yaml.safe_load(WORKFLOW.read_text())  # type: ignore[no-any-return]


def test_manual_main_only_audit_reuses_pinned_deployment_ssh() -> None:
    audit = workflow()
    # PyYAML's YAML 1.1 loader treats the unquoted Actions key `on` as True.
    assert audit.get("on", audit.get(True)) == {"workflow_dispatch": None}
    assert audit["permissions"] == {"contents": "read"}
    assert list(audit["jobs"]) == ["audit"]
    job = audit["jobs"]["audit"]
    assert job["if"] == (
        "github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main'"
    )
    deploy = yaml.safe_load(
        (ROOT / ".github/workflows/deploy-staging.yml").read_text()
    )["jobs"]["deploy"]
    for key in ("SSH_HOST", "SSH_HOST_KEY"):
        assert job["env"][key] == deploy["env"][key]
    assert job["env"]["SSH_USER"] == "${{ secrets.VPS_DMC268_U }}"
    assert job["env"]["SSHPASS"] == "${{ secrets.VPS_DMC268_P }}"
    source = WORKFLOW.read_text()
    assert set(re.findall(r"secrets\.([A-Za-z0-9_]+)", source)) == {
        "VPS_DMC268_U",
        "VPS_DMC268_P",
    }
    assert "StrictHostKeyChecking=yes" in source
    assert 'UserKnownHostsFile="$HOME/.ssh/known_hosts"' in source
    assert "ssh-keyscan" not in source


def remote_script() -> str:
    steps = workflow()["jobs"]["audit"]["steps"]
    run: str = next(s["run"] for s in steps if s["name"] == "Audit staging backlog")
    return run.split("<<'REMOTE'\n", 1)[1].rsplit("\nREMOTE", 1)[0]


@pytest.mark.parametrize("state", ["", "exited", "running", "restarting", "error"])
def test_audit_only_reports_staging_analysis_service_states(
    tmp_path: Path, state: str
) -> None:
    docker = tmp_path / "docker"
    docker.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "if 'exec' in sys.argv:\n"
        "    assert sys.argv[-6:] == ['exec', '-T', 'api', 'python', '-I', '-']\n"
        "    sys.stdin.read()\n"
        "    sys.exit(0)\n"
        "expected = ['compose', '-p', 'dmc-268-t2-staging', '--env-file', "
        "'.env.staging', '-f', 'docker-compose.staging.yml', '--profile', "
        "'analysis', 'ps', '--all', '--format', '{{.State}}']\n"
        "assert sys.argv[1:-1] == expected\n"
        "assert sys.argv[-1] in ('worker', 'outbox-dispatcher')\n"
        "if os.environ['STATE'] == 'error':\n"
        "    print('synthetic-secret', file=sys.stderr)\n"
        "    sys.exit(1)\n"
        "print(os.environ['STATE'])\n"
    )
    docker.chmod(0o755)
    script = remote_script()
    assert "cd /opt/dmc-268-t2\n" in script
    # Substitute only the deployment directory; all external commands are doubles.
    result = subprocess.run(
        ["bash", "-c", script.replace("cd /opt/dmc-268-t2", f"cd {tmp_path}")],
        env=os.environ | {"PATH": f"{tmp_path}:{os.environ['PATH']}", "STATE": state},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert "synthetic-secret" not in result.stdout + result.stderr
    if state == "error":
        assert result.returncode != 0
        assert "Service status unavailable" in result.stdout
    else:
        assert result.returncode == 0, result.stderr
        for service in ("worker", "outbox-dispatcher"):
            assert f"{service}: {state or 'absent'}" in result.stdout
        if state in ("running", "restarting"):
            assert "::warning::" in result.stdout
    forbidden = (
        r"\b(?:XREADGROUP|XACK|XGROUP|XDEL|XTRIM|FLUSH\w*)\b",
        r"\b(?:INSERT|UPDATE|DELETE|TRUNCATE|ALTER|DROP|CREATE)\b",
        r"\b(?:up|stop|start|restart|pull|push|deploy|migrate)\b",
        r"set -x|compose config|gh api|curl |wget ",
    )
    for pattern in forbidden:
        assert not re.search(pattern, script, re.IGNORECASE)


def diagnostic_python() -> str:
    return remote_script().split("<<'PY'\n", 1)[1].split("\nPY", 1)[0]


@pytest.mark.parametrize("unavailable", [False, True])
def test_postgres_audit_is_bounded_read_only_and_outputs_only_counts(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    unavailable: bool,
) -> None:
    queries: list[str] = []

    class Transaction:
        async def __aenter__(self) -> None:
            pass

        async def __aexit__(self, *args: object) -> None:
            pass

    class Connection:
        def transaction(self, **kwargs: object) -> Transaction:
            assert kwargs == {"isolation": "repeatable_read", "readonly": True}
            return Transaction()

        async def fetch(self, query: str) -> list[dict[str, int]]:
            queries.append(query)
            assert query.lstrip().startswith("SELECT")
            return [{"count": 7}]

        async def close(self) -> None:
            pass

    async def connect(**kwargs: Any) -> Connection:
        assert kwargs["host"] == "db"
        assert kwargs["timeout"] <= CONNECT_TIMEOUT
        assert kwargs["command_timeout"] <= QUERY_TIMEOUT
        settings = kwargs["server_settings"]
        assert settings["default_transaction_read_only"] == "on"
        assert int(settings["statement_timeout"]) <= STATEMENT_TIMEOUT_MS
        assert int(settings["lock_timeout"]) <= LOCK_TIMEOUT_MS
        if unavailable:
            raise OSError("synthetic-secret connection string")
        return Connection()

    monkeypatch.setitem(sys.modules, "asyncpg", SimpleNamespace(connect=connect))
    for key in ("POSTGRES__USER", "POSTGRES__PASSWORD", "POSTGRES__DB"):
        monkeypatch.setenv(key, "synthetic-secret")
    code = diagnostic_python()
    if unavailable:
        with pytest.raises(SystemExit, match="audit failed"):
            exec(compile(code, "<audit>", "exec"), {})
    else:
        exec(compile(code, "<audit>", "exec"), {})
        sql = "\n".join(queries)
        for fragment in (
            "FROM review_job",
            "GROUP BY status",
            "finished_at IS NULL",
            "started_at IS NULL AND queue_deadline_at <= now()",
            "analysis_deadline_at <= now()",
            "FROM outbox_event",
            "broker_published_at IS NULL AND quarantined_at IS NULL",
            "broker_published_at IS NOT NULL",
            "dispatch_not_before > now()",
            "quarantined_at IS NOT NULL",
            "task_kind = 'analyze'",
            "FROM task_lease",
            "expires_at > now()",
            "FROM webhook_receipt",
            "processing_status IN ('PENDING', 'PROCESSING')",
        ):
            assert fragment in sql
        output = capsys.readouterr().out
        assert "synthetic-secret" not in output
        for label in (
            "review_jobs_by_status",
            "unfinished_reviews",
            "analyze_outbox",
            "active_analysis_leases",
            "outstanding_webhook_receipts",
        ):
            assert label in output
        assert "7" in output
    captured = capsys.readouterr()
    assert "synthetic-secret" not in captured.out + captured.err


@pytest.mark.parametrize("mode", ["groups", "no-groups", "missing", "unavailable"])
def test_redis_audit_reads_only_known_stream_metrics(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], mode: str
) -> None:
    keys = [f"dmc268:review-tasks:{kind}" for kind in ("analyze", "publish", "dead")]
    calls: list[tuple[str, str]] = []

    class ResponseError(Exception):
        pass

    class Redis:
        def __init__(self, **kwargs: Any) -> None:
            assert kwargs["host"] == "redis"
            assert kwargs["socket_timeout"] <= CONNECT_TIMEOUT
            assert kwargs["socket_connect_timeout"] <= CONNECT_TIMEOUT
            assert kwargs["protocol"] == RESP2
            assert kwargs["lib_name"] is None
            assert kwargs["lib_version"] is None

        async def xlen(self, key: str) -> int:
            calls.append(("XLEN", key))
            if mode == "unavailable":
                raise OSError("synthetic-secret redis://credentials")
            return 0 if mode == "missing" else 12

        async def xinfo_groups(self, key: str) -> list[dict[str, Any]]:
            calls.append(("XINFO GROUPS", key))
            if mode == "missing":
                raise ResponseError("no such key")
            if mode == "no-groups":
                return []
            return [
                {
                    "name": "synthetic-secret",
                    "pending": 2,
                    "lag": 3,
                    "consumers": 1,
                    "last-delivered-id": "synthetic-secret",
                },
                {"name": "synthetic-secret", "pending": 0, "lag": None, "consumers": 0},
            ]

        async def aclose(self) -> None:
            pass

    monkeypatch.setitem(sys.modules, "redis.asyncio", SimpleNamespace(Redis=Redis))
    monkeypatch.setitem(
        sys.modules, "redis.exceptions", SimpleNamespace(ResponseError=ResponseError)
    )
    code = remote_script().split("<<'REDIS_PY'\n", 1)[1].split("\nREDIS_PY", 1)[0]
    if mode == "unavailable":
        with pytest.raises(SystemExit, match="Redis audit failed"):
            exec(compile(code, "<audit>", "exec"), {})
    else:
        exec(compile(code, "<audit>", "exec"), {})
    output = capsys.readouterr()
    assert "synthetic-secret" not in output.out + output.err
    if mode == "unavailable":
        assert calls == [("XLEN", keys[0])]
    else:
        assert calls == [
            (command, key) for key in keys for command in ("XLEN", "XINFO GROUPS")
        ]
        metrics = [
            json.loads(line) for line in output.out.splitlines() if line.startswith("{")
        ]
        assert [m["stream"] for m in metrics] == keys
        assert all(m["length"] == (0 if mode == "missing" else 12) for m in metrics)
        expected = [
            {"pending": 2, "lag": 3, "consumers": 1},
            {"pending": 0, "lag": None, "consumers": 0},
        ]
        assert all(
            m["groups"] == (expected if mode == "groups" else []) for m in metrics
        )
        assert "not waiting jobs" in output.out
