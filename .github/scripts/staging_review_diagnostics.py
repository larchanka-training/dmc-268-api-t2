"""Read an existing staging review; run over SSH stdin, never import the app."""

import json
import os
import re
import signal
import subprocess
import sys
from datetime import datetime
from typing import Any
from uuid import UUID

COMPOSE = [
    "docker",
    "compose",
    "-p",
    "dmc-268-t2-staging",
    "--env-file",
    ".env.staging",
    "-f",
    "docker-compose.staging.yml",
]
ROW_LIMIT = 200
STATUSES = {
    "QUEUED",
    "FETCHING_DIFF",
    "PARSING_CONTEXT",
    "LLM_PROCESSING",
    "COMPLETED",
    "PARTIAL",
    "FAILED",
    "SKIPPED",
}
REASONS = {
    "QUEUE_DEADLINE_EXCEEDED",
    "VCS_RATE_LIMITED",
    "VCS_UNAVAILABLE",
    "VCS_ACCESS_DENIED",
    "PR_STALE_OR_CLOSED",
    "DIFF_UNTRUSTWORTHY",
    "SOURCE_BLOB_UNAVAILABLE",
    "AST_PARSE_FAILED",
    "LLM_TIMEOUT",
    "LLM_UNAVAILABLE",
    "LLM_OUTPUT_INVALID",
    "ANALYSIS_DEADLINE_EXCEEDED",
}
ERROR_CODES = {
    "llm_error",
    "rate_limited",
    "provider_unavailable",
    "provider_timeout",
    "provider_auth_failed",
    "context_overflow",
    "provider_request_error",
    "invalid_llm_output",
    "budget_exceeded",
    "analysis_deadline_exceeded",
    "budget_input_exhausted",
    "budget_output_exhausted",
    "budget_requests_exhausted",
    "llm_unavailable",
    "unexpected_model_identity",
    "no_available_endpoint",
}
ERROR_CODES |= {
    "context_configuration_mismatch",
    "model_digest_unattestable",
    "unsupported_model_profile",
    "invalid_execution_credentials",
    "invalid_runtime_manifest",
    "review_not_found",
    "legacy_frozen_configuration",
    "unsupported_frozen_configuration_version",
    "repository_settings_mismatch",
    "unsupported_model_settings",
    "config_digest_mismatch",
    "rules_digest_mismatch",
    "repository_settings_digest_mismatch",
    "unsupported_prompt_bundle",
    "repository_instructions_mismatch",
    "execution_configuration_required",
    "execution_authority_required",
    "backend_result_invalid",
    "prompt_too_large",
}
PROFILES = {"eurorouter/review-v1", "ollama/review-v1"}
LOG_BYTES = 1024 * 1024
LOG_LINES = 2000
MAX_WINDOW_SECONDS = 1800
PROVIDER_CODES = {
    "model_not_found",
    "invalid_api_key",
    "insufficient_quota",
    "invalid_request_error",
    "model_not_available",
    "unsupported_model",
    # Server-side (5xx) codes and types of OpenAI-compatible routers.
    "server_error",
    "service_unavailable",
    "api_error",
    "provider_error",
    "upstream_error",
    "overloaded_error",
    "no_available_providers",
    "model_overloaded",
}
TRANSPORT_ERRORS = {
    "ConnectError",
    "ReadError",
    "WriteError",
    "CloseError",
    "RemoteProtocolError",
    "ProxyError",
}

# Runs only inside the existing staging API container. Credentials stay there.
# SQL selects no user/repository identities, settings, summaries or raw JSON.
DATABASE_PROGRAM = r'''
import asyncio
import json
import os
import sys
from uuid import UUID

import asyncpg

failure = "connection_error"

async def read():
    global failure
    connection = await asyncpg.connect(
        host="db", port=5432,
        user=os.environ["POSTGRES__USER"],
        password=os.environ["POSTGRES__PASSWORD"],
        database=os.environ["POSTGRES__DB"],
        timeout=5, command_timeout=10,
        server_settings={
            "default_transaction_read_only": "on",
            "statement_timeout": "10000", "lock_timeout": "1000",
            "idle_in_transaction_session_timeout": "15000",
        },
    )
    failure = "query_error"
    try:
        async with connection.transaction(isolation="repeatable_read", readonly=True):
            queries = {
                "job": """SELECT status, analysis_attempts, created_at, started_at,
                    finished_at FROM review_job WHERE id = $1""",
                "events": """SELECT sequence, occurred_at, event_type, status, phase,
                    reason_code, retryable, attempt,
                    safe_details->>'error_code' AS error_code
                    FROM review_event WHERE review_job_id = $1
                    ORDER BY sequence LIMIT 201""",
                "attempts": """SELECT analysis_attempt, state, error_code,
                    provider_profile_ref,
                    count(*) AS calls,
                    count(actual_input_tokens) AS known_input_calls,
                    count(actual_output_tokens) AS known_output_calls,
                    sum(actual_input_tokens)::bigint AS known_input_tokens,
                    sum(actual_output_tokens)::bigint AS known_output_tokens
                    FROM review_llm_attempt WHERE review_job_id = $1
                    GROUP BY analysis_attempt, state, error_code, provider_profile_ref
                    ORDER BY analysis_attempt, state, error_code, provider_profile_ref
                    LIMIT 201""",
            }
            result = {}
            for label, query in queries.items():
                rows = await connection.fetch(query, UUID(sys.argv[1]))
                result[label] = [dict(row) for row in rows]
        print(json.dumps(result, default=str))
    finally:
        await connection.close()

try:
    asyncio.run(read())
except Exception:
    print(json.dumps({"failure": failure}))
    raise SystemExit(1) from None
'''


def choice(value: Any, allowed: set[str]) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) and value in allowed else "REDACTED"


def number(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def timestamp(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.isoformat() if parsed.tzinfo is not None else None


def snapshot(data: dict[str, Any], review_id: str) -> dict[str, Any]:
    if not data["job"]:
        return {
            "database": "review_not_found",
            "review_id": review_id,
            "job": None,
            "events": [],
            "attempts": [],
        }
    row = data["job"][0]
    job = {
        "status": choice(row.get("status"), STATUSES),
        "analysis_attempts": number(row.get("analysis_attempts")),
    }
    for key in ("created_at", "started_at", "finished_at"):
        job[key] = timestamp(row.get(key))
    events = []
    for row in data["events"][:ROW_LIMIT]:
        event = {
            "sequence": number(row.get("sequence")),
            "occurred_at": timestamp(row.get("occurred_at")),
            "event_type": choice(
                row.get("event_type"),
                {
                    "TRANSITION",
                    "FAILURE",
                    "PUBLICATION_PENDING",
                    "PUBLICATION_STARTED",
                    "PUBLICATION_FINISHED",
                    "PUBLICATION_RECONCILED",
                },
            ),
            "status": choice(row.get("status"), STATUSES),
            "phase": choice(
                row.get("phase"), {"FETCHING_DIFF", "PARSING_CONTEXT", "LLM_PROCESSING"}
            ),
            "reason_code": choice(row.get("reason_code"), REASONS),
            "error_code": choice(row.get("error_code"), ERROR_CODES),
            "attempt": number(row.get("attempt")),
            "retryable": row.get("retryable")
            if type(row.get("retryable")) is bool
            else None,
        }
        events.append(event)
    attempts = []
    for row in data["attempts"][:ROW_LIMIT]:
        attempt: dict[str, Any] = {
            "state": choice(row.get("state"), {"RESERVED", "SUCCEEDED", "ERROR"}),
            "error_code": choice(row.get("error_code"), ERROR_CODES),
            "provider_profile_ref": choice(row.get("provider_profile_ref"), PROFILES),
        }
        for key in (
            "analysis_attempt",
            "calls",
            "known_input_calls",
            "known_output_calls",
            "known_input_tokens",
            "known_output_tokens",
        ):
            attempt[key] = number(row.get(key))
        attempts.append(attempt)
    return {
        "database": "ok",
        "review_id": review_id,
        "job": job,
        "events": events,
        "attempts": attempts,
        "events_truncated": len(data["events"]) > ROW_LIMIT,
        "attempts_truncated": len(data["attempts"]) > ROW_LIMIT,
    }


def worker_logs(job: dict[str, Any] | None, review_id: str) -> dict[str, Any]:
    skipped = {"state": "skipped_invalid_window", "events": []}
    if not job or not job.get("started_at") or not job.get("finished_at"):
        return skipped
    start = datetime.fromisoformat(job["started_at"])
    end = datetime.fromisoformat(job["finished_at"])
    if not 0 < (end - start).total_seconds() <= MAX_WINDOW_SECONDS:
        return skipped
    # Both bytes and execution time are bounded; no raw log data reaches stdout.
    with subprocess.Popen(
        [
            "timeout",
            "--kill-after=2s",
            "20s",
            *COMPOSE,
            "--profile",
            "analysis",
            "logs",
            "--no-color",
            "--no-log-prefix",
            "--tail",
            str(LOG_LINES),
            "--since",
            start.isoformat(),
            "--until",
            end.isoformat(),
            "worker",
        ],
        cwd="/opt/dmc-268-t2",
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    ) as process:
        assert process.stdout is not None
        raw = process.stdout.read(LOG_BYTES + 1)
        if len(raw) > LOG_BYTES:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=3)
    if len(raw) > LOG_BYTES or len(raw.splitlines()) >= LOG_LINES:
        return {
            "state": "truncated",
            "events": [],
            "tail_limit": LOG_LINES,
            "byte_limit": LOG_BYTES,
        }
    if process.returncode:
        return {"state": "unavailable", "events": []}
    events = []
    for line in raw[:LOG_BYTES].splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("event") != "llm_attempt_failed":
            continue
        when = timestamp(entry.get("timestamp"))
        if when is None or not start <= datetime.fromisoformat(when) <= end:
            continue
        if "review_id" in entry and entry["review_id"] != review_id:
            continue
        detail = entry.get("detail")
        http = (
            re.fullmatch(r"HTTP ([45][0-9]{2})(?: \(([A-Za-z0-9_.-]{1,64})\))?", detail)
            if isinstance(detail, str)
            else None
        )
        events.append(
            {
                "timestamp": when,
                "provider": choice(entry.get("provider"), {"eurorouter", "ollama"}),
                "attempt": number(entry.get("attempt")),
                "error_code": choice(entry.get("error_code"), ERROR_CODES),
                "http_status": int(http[1]) if http else None,
                "provider_error_code": choice(http[2], PROVIDER_CODES)
                if http
                else None,
                "transport_error": choice(detail, TRANSPORT_ERRORS)
                if isinstance(detail, str) and detail in TRANSPORT_ERRORS
                else None,
                "response_format": "non_json"
                if detail == "provider returned a non-JSON body"
                else None,
            }
        )
    return {
        "state": "ok" if events else "no_matching_events",
        "attribution": "time_window_only",
        "tail_limit": LOG_LINES,
        "byte_limit": LOG_BYTES,
        "events": events,
    }


def main() -> int:
    try:
        _, review_id, include_logs = sys.argv
        if str(UUID(review_id)) != review_id:
            raise ValueError
    except ValueError:
        print(json.dumps({"input": "invalid_review_id"}))
        return 1
    result = subprocess.run(
        [*COMPOSE, "exec", "-T", "api", "python", "-I", "-", review_id],
        input=DATABASE_PROGRAM,
        capture_output=True,
        text=True,
        cwd="/opt/dmc-268-t2",
        timeout=45,
        check=False,
    )
    if result.returncode:
        try:
            payload = json.loads(result.stdout)
            failure = payload.get("failure") if isinstance(payload, dict) else None
        except ValueError:
            failure = None
        print(
            json.dumps(
                {
                    "database": choice(failure, {"connection_error", "query_error"})
                    if failure in ("connection_error", "query_error")
                    else "container_error"
                }
            )
        )
        return 1
    report = snapshot(json.loads(result.stdout), review_id)
    if include_logs == "true":
        try:
            report["worker_logs"] = worker_logs(report["job"], review_id)
        except Exception:
            report["worker_logs"] = {"state": "unavailable", "events": []}
    print(json.dumps(report))
    return int(
        report.get("worker_logs", {}).get("state") in {"unavailable", "truncated"}
    )


if __name__ == "__main__":
    try:
        exit_code = main()
    except Exception:
        print(json.dumps({"database": "diagnostic_error"}))
        exit_code = 1
    sys.exit(exit_code)
