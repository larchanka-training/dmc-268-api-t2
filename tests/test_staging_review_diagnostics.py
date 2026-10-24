"""Manual diagnostic entry points expose only bounded, safe review metadata."""

import ast
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github/workflows/staging-review-diagnostics.yml"
SCRIPT = ROOT / ".github/scripts/staging_review_diagnostics.py"
REVIEW_ID = "bf518af2-0360-4971-852f-dddc27fedede"
START = "2026-10-23T12:00:00+00:00"
END = "2026-10-23T12:01:00+00:00"
SECRET = "synthetic-secret@example.invalid"
LOG_LINES = 2000


@pytest.fixture
def diagnostic_cli(tmp_path: Path) -> Any:
    """Replace only the fixed deployment directory and external Docker/DB edges."""
    (tmp_path / "asyncpg.py").write_text("""
import json, os
from datetime import datetime
from decimal import Decimal
class Transaction:
    async def __aenter__(self): pass
    async def __aexit__(self, *args): pass
class Connection:
    def transaction(self, **kwargs):
        assert kwargs == {"isolation": "repeatable_read", "readonly": True}
        return Transaction()
    async def fetch(self, query, review_id):
        assert str(review_id) == os.environ["EXPECTED_REVIEW_ID"]
        assert query.lstrip().startswith("SELECT")
        assert "$1" in query
        with open(os.environ["SQL_CAPTURE"], "a") as stream:
            stream.write(query + "\\n")
        with open(os.environ["DB_DATA_FILE"]) as stream:
            data = json.load(stream)
        if data.get("failure") == "query":
            raise RuntimeError("synthetic-secret@example.invalid SQL schema or timeout")
        for table, key in [("review_job", "job"), ("review_event", "events"),
                           ("review_llm_attempt", "attempts")]:
            if "FROM " + table + " " in query:
                rows = data[key]
                if key == "attempts" and "::bigint" not in query:
                    rows = [row | {k: Decimal(v) if v is not None else None
                                   for k, v in row.items() if k in
                                   ("known_input_tokens", "known_output_tokens")}
                            for row in rows]
                return [{k: datetime.fromisoformat(v) if k.endswith("_at") and v else v
                         for k, v in row.items()} for row in rows]
        raise AssertionError("unexpected query")
    async def close(self): pass
async def connect(**kwargs):
    assert kwargs["host"] == "db" and kwargs["port"] == 5432
    assert kwargs["timeout"] <= 5 and kwargs["command_timeout"] <= 10
    settings = kwargs["server_settings"]
    assert settings["default_transaction_read_only"] == "on"
    assert int(settings["statement_timeout"]) <= 10000
    assert int(settings["lock_timeout"]) <= 1000
    assert int(settings["idle_in_transaction_session_timeout"]) <= 15000
    with open(os.environ["DB_DATA_FILE"]) as stream:
        data = json.load(stream)
    if data.get("failure") == "connection":
        raise OSError("synthetic-secret@example.invalid connection credentials")
    return Connection()
""")
    docker = tmp_path / "docker"
    docker.write_text("""#!/usr/bin/env python3
import os, sys
prefix = ["compose", "-p", "dmc-268-t2-staging", "--env-file", ".env.staging",
          "-f", "docker-compose.staging.yml"]
assert sys.argv[1:len(prefix)+1] == prefix
args = sys.argv[len(prefix)+1:]
if args[:3] == ["--profile", "analysis", "logs"]:
    import json
    with open(os.environ["DB_DATA_FILE"]) as stream:
        data = json.load(stream)
    if data.get("log_failure"):
        print("synthetic-secret@example.invalid docker failure", file=sys.stderr)
        sys.exit(1)
    assert args[3:] == ["--no-color", "--no-log-prefix", "--tail", "2000",
                        "--since", data["job"][0]["started_at"],
                        "--until", data["job"][0]["finished_at"], "worker"]
    for line in data["logs"]:
        print(line if isinstance(line, str) else json.dumps(line))
    sys.exit(0)
assert args[:6] == ["exec", "-T", "api", "python", "-I", "-"]
sys.argv = ["<database>", *args[6:]]
sys.path.insert(0, os.environ["FAKE_MODULES"])
exec(compile(sys.stdin.read(), "<database>", "exec"), {"__name__": "__main__"})
""")
    docker.chmod(0o755)

    def run(
        data: dict[str, Any], review_id: str = REVIEW_ID, logs: str = "false"
    ) -> subprocess.CompletedProcess[str]:
        executable = tmp_path / "diagnostic.py"
        executable.write_text(
            SCRIPT.read_text().replace("/opt/dmc-268-t2", str(tmp_path))
        )
        (tmp_path / "data.json").write_text(json.dumps(data))
        return subprocess.run(
            [sys.executable, "-I", str(executable), review_id, logs],
            env=os.environ
            | {
                "PATH": f"{tmp_path}:{os.environ['PATH']}",
                "FAKE_MODULES": str(tmp_path),
                "DB_DATA_FILE": str(tmp_path / "data.json"),
                "EXPECTED_REVIEW_ID": REVIEW_ID,
                "SQL_CAPTURE": str(tmp_path / "queries"),
                "POSTGRES__USER": SECRET,
                "POSTGRES__PASSWORD": SECRET,
                "POSTGRES__DB": SECRET,
            },
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    return run


def saved_review() -> dict[str, Any]:
    return {
        "job": [
            {
                "status": "FAILED",
                "analysis_attempts": 1,
                "created_at": START,
                "started_at": START,
                "finished_at": END,
                "final_summary": SECRET,
            }
        ],
        "events": [
            {
                "sequence": 4,
                "occurred_at": END,
                "event_type": "TRANSITION",
                "status": "FAILED",
                "phase": None,
                "reason_code": "LLM_UNAVAILABLE",
                "attempt": 1,
                "retryable": False,
                "error_code": "provider_auth_failed",
                "safe_details": {"prompt": SECRET},
            }
        ],
        "attempts": [
            {
                "analysis_attempt": 1,
                "state": "ERROR",
                "error_code": "provider_auth_failed",
                "provider_profile_ref": "eurorouter/review-v1",
                "calls": 2,
                "known_input_calls": 1,
                "known_output_calls": 0,
                "known_input_tokens": 120,
                "known_output_tokens": None,
                "model_ref": SECRET,
            }
        ],
    }


def test_cli_reports_saved_review_events_and_aggregate_known_usage(
    diagnostic_cli: Any,
) -> None:
    result = diagnostic_cli(saved_review())
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["database"] == "ok"
    assert report["review_id"] == REVIEW_ID
    assert report["job"]["status"] == "FAILED"
    assert report["events"][0]["reason_code"] == "LLM_UNAVAILABLE"
    assert report["events"][0]["error_code"] == "provider_auth_failed"
    assert report["attempts"] == [
        {
            "analysis_attempt": 1,
            "state": "ERROR",
            "error_code": "provider_auth_failed",
            "provider_profile_ref": "eurorouter/review-v1",
            "calls": 2,
            "known_input_calls": 1,
            "known_output_calls": 0,
            "known_input_tokens": 120,
            "known_output_tokens": None,
        }
    ]
    assert SECRET not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "value",
    [
        "",
        "not-a-uuid",
        REVIEW_ID.upper(),
        REVIEW_ID + "\n",
        "bf518af203604971852fdddc27fedede",
    ],
)
def test_remote_cli_rejects_noncanonical_uuid_before_docker(
    diagnostic_cli: Any,
    tmp_path: Path,
    value: str,
) -> None:
    result = diagnostic_cli(saved_review(), review_id=value)
    assert result.returncode != 0
    assert json.loads(result.stdout) == {"input": "invalid_review_id"}
    assert not (tmp_path / "queries").exists()


@pytest.mark.parametrize(
    "mode, expected, exit_code",
    [
        ("absent", "review_not_found", 0),
        ("connection", "connection_error", 1),
        ("query", "query_error", 1),
    ],
)
def test_cli_distinguishes_missing_review_from_dependency_errors(
    diagnostic_cli: Any,
    mode: str,
    expected: str,
    exit_code: int,
) -> None:
    data = saved_review()
    if mode == "absent":
        data["job"] = []
        data["events"] = []
        data["attempts"] = []
    else:
        data["failure"] = mode
    result = diagnostic_cli(data)
    assert result.returncode == exit_code
    assert json.loads(result.stdout)["database"] == expected
    assert SECRET not in result.stdout + result.stderr


def test_opt_in_logs_project_only_safe_failure_fields_in_review_window(
    diagnostic_cli: Any,
) -> None:
    data = saved_review()
    entry = {
        "event": "llm_attempt_failed",
        "timestamp": "2026-10-23T12:00:30+00:00",
        "provider": "eurorouter",
        "attempt": 1,
        "error_code": "provider_request_error",
        "detail": "HTTP 404 (model_not_found)",
        "prompt": SECRET,
        "headers": {"Authorization": SECRET},
        "response": SECRET,
    }
    data["logs"] = [
        SECRET,
        entry,
        entry | {"event": "llm_attempt_succeeded"},
        entry | {"timestamp": "2026-10-23T11:59:59+00:00"},
        entry | {"timestamp": "2026-10-23T12:01:01+00:00"},
        entry | {"review_id": "00000000-0000-0000-0000-000000000001"},
        entry | {"detail": SECRET, "provider": SECRET, "error_code": SECRET},
    ]
    result = diagnostic_cli(data, logs="true")
    assert result.returncode == 0, result.stdout + result.stderr
    logs = json.loads(result.stdout)["worker_logs"]
    assert logs["state"] == "ok"
    assert logs["attribution"] == "time_window_only"
    assert logs["tail_limit"] == LOG_LINES
    assert logs["events"] == [
        {
            "timestamp": "2026-10-23T12:00:30+00:00",
            "provider": "eurorouter",
            "attempt": 1,
            "error_code": "provider_request_error",
            "http_status": 404,
            "provider_error_code": "model_not_found",
            "transport_error": None,
            "response_format": None,
        },
        {
            "timestamp": "2026-10-23T12:00:30+00:00",
            "provider": "REDACTED",
            "attempt": 1,
            "error_code": "REDACTED",
            "http_status": None,
            "provider_error_code": None,
            "transport_error": None,
            "response_format": None,
        },
    ]
    assert SECRET not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "mode, expected, exit_code",
    [
        ("unavailable", "unavailable", 1),
        ("oversized", "truncated", 1),
        ("empty", "no_matching_events", 0),
        ("unfinished", "skipped_invalid_window", 0),
        ("too-long", "skipped_invalid_window", 0),
        ("missing-job", "skipped_invalid_window", 0),
    ],
)
def test_optional_logs_preserve_database_evidence_when_incomplete(
    diagnostic_cli: Any,
    mode: str,
    expected: str,
    exit_code: int,
) -> None:
    data = saved_review()
    data["logs"] = []
    if mode == "unavailable":
        data["log_failure"] = True
    elif mode == "oversized":
        data["logs"] = ["x" * (1024 * 1024 + 2)]
    elif mode == "unfinished":
        data["job"][0]["finished_at"] = None
    elif mode == "too-long":
        data["job"][0]["finished_at"] = "2026-10-23T13:00:00+00:00"
    elif mode == "missing-job":
        data["job"] = []
        data["events"] = []
        data["attempts"] = []
    result = diagnostic_cli(data, logs="true")
    assert result.returncode == exit_code
    report = json.loads(result.stdout)
    assert report["database"] == ("review_not_found" if mode == "missing-job" else "ok")
    assert report["worker_logs"]["state"] == expected
    assert SECRET not in result.stdout + result.stderr


def test_pre_inference_failure_retains_known_configuration_cause(
    diagnostic_cli: Any,
) -> None:
    data = saved_review()
    data["job"][0]["analysis_attempts"] = 0
    data["events"][0].update(
        event_type="FAILURE", error_code="legacy_frozen_configuration"
    )
    data["attempts"] = []
    result = diagnostic_cli(data)
    assert result.returncode == 0
    report = json.loads(result.stdout)
    assert report["events"][0]["event_type"] == "FAILURE"
    assert report["events"][0]["error_code"] == "legacy_frozen_configuration"
    assert report["attempts"] == []


def test_ssh_payload_parses_on_host_python_312() -> None:
    # The SSH host uses system Python, unlike the Python 3.14 API container.
    ast.parse(SCRIPT.read_text(), feature_version=(3, 12))


def workflow() -> dict[Any, Any]:
    return yaml.safe_load(WORKFLOW.read_text())  # type: ignore[no-any-return]


def test_manual_main_only_workflow_validates_uuid_before_ssh() -> None:
    config = workflow()
    trigger = config.get("on", config.get(True))
    assert isinstance(trigger, dict)
    assert set(trigger) == {"workflow_dispatch"}
    inputs = trigger["workflow_dispatch"]["inputs"]
    assert inputs["review_id"]["required"] is True
    assert inputs["review_id"]["type"] == "string"
    assert inputs["include_worker_logs"]["default"] is False
    assert config["permissions"] == {"contents": "read"}
    assert config["concurrency"] == {
        "group": "deploy-staging",
        "cancel-in-progress": False,
    }
    job = config["jobs"]["diagnose"]
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
    step = job["steps"][0]
    assert step["env"]["REVIEW_ID"] == "${{ inputs.review_id }}"
    for value in ("", "not-a-uuid", REVIEW_ID + "\n", "$(touch /tmp/unsafe)"):
        result = subprocess.run(
            ["bash", "-c", step["run"]],
            env=os.environ | {"REVIEW_ID": value},
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        assert result.returncode != 0
        assert value not in result.stdout + result.stderr if value else True
    source = WORKFLOW.read_text()
    assert "StrictHostKeyChecking=yes" in source
    assert 'UserKnownHostsFile="$HOME/.ssh/known_hosts"' in source
    assert "ssh-keyscan" not in source
    assert "timeout 120s" in source
    assert "persist-credentials: false" in source
    assert "< .github/scripts/staging_review_diagnostics.py" in source
