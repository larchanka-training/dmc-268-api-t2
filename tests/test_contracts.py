from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from openapi_spec_validator import validate

from app.application.ports import (
    OpenPullRequest,
    OpenPullRequestPage,
    PullRequestAuthor,
    PullRequestRef,
    ReviewChunkResult,
    ReviewFinding,
)
from app.domain.enums import FindingCategory, FindingSeverity, FindingSide
from app.domain.settings import compute_rules_digest

ROOT = Path(__file__).parents[1]
SCHEMAS = ROOT / "schemas"


def _load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as source:
        return cast(dict[str, Any], json.load(source))


def _validator(path: Path) -> Draft202012Validator:
    schema = _load_json(path)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


@pytest.mark.parametrize(
    "path",
    [
        SCHEMAS / "context" / "v1.json",
        SCHEMAS / "review-result" / "v1.json",
        SCHEMAS / "queue" / "task-envelope.v1.json",
        SCHEMAS / "queue" / "dead" / "v1.json",
        SCHEMAS / "settings" / "v1.json",
    ],
)
def test_checked_in_json_schemas_are_valid(path: Path) -> None:
    _validator(path)


def _context_payload() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "review_id": "rev_123",
        "chunk_id": "chunk_123",
        "snapshot": {
            "provider": "github",
            "base_repository_id": "repo_base",
            "head_repository_id": "repo_head",
            "base_sha": "base123",
            "merge_base_sha": "merge123",
            "head_sha": "head123",
            "captured_at": "2026-10-04T10:00:00Z",
            "provider_metadata": {"pull_number": 42, "diff_version": "diff-v1"},
        },
        "metadata": {
            "title": "Correct the contract",
            "body": None,
            "base_ref": "main",
            "head_ref": "feature/contracts",
            "commit_messages": ["Align context contract"],
            "languages": ["Python"],
            "discussions": ["Keep provider payload out of model context."],
            "rules_version": "settings-v1",
            "output_language": "ru",
        },
        "files": [
            {
                "diff": {
                    "file_id": "file_1",
                    "old_path": "src/example.py",
                    "new_path": "src/example.py",
                    "status": "modified",
                    "old_blob_sha": "oldblob",
                    "new_blob_sha": "newblob",
                    "language": "Python",
                    "hunks": [
                        {
                            "hunk_id": "hunk_1",
                            "old_start": 1,
                            "old_count": 1,
                            "new_start": 1,
                            "new_count": 2,
                            "lines": [
                                {
                                    "kind": "context",
                                    "text": "value = 1",
                                    "old_line": 1,
                                    "new_line": 1,
                                },
                                {
                                    "kind": "added",
                                    "text": "return value",
                                    "old_line": None,
                                    "new_line": 2,
                                },
                            ],
                        }
                    ],
                },
                "source_windows": [
                    {
                        "path": "src/example.py",
                        "side": "NEW",
                        "commit_sha": "head123",
                        "start_line": 1,
                        "end_line": 2,
                        "text": "value = 1\nreturn value",
                        "covered_hunk_ids": ["hunk_1"],
                    }
                ],
                "whole_file": None,
            }
        ],
        "related_symbols": [],
        "coverage": {
            "included": [
                {
                    "path": "src/example.py",
                    "ranges": [{"start_line": 1, "end_line": 2}],
                }
            ],
            "skipped": [{"path": "vendor/x.js", "ranges": [], "reason": "policy"}],
            "context_quality": "degraded",
        },
        "budget": {
            "estimated_input_tokens": 1000,
            "reserved_output_tokens": 2000,
            "limit_input_tokens": 8000,
        },
    }


def test_context_payload_accepts_canonical_example() -> None:
    _validator(SCHEMAS / "context" / "v1.json").validate(_context_payload())


@pytest.mark.parametrize("mutation", ["empty_files", "bad_line_map", "extra_field"])
def test_context_payload_rejects_invalid_examples(mutation: str) -> None:
    payload = copy.deepcopy(_context_payload())
    if mutation == "empty_files":
        payload["files"] = []
    elif mutation == "bad_line_map":
        payload["files"][0]["diff"]["hunks"][0]["lines"][1]["old_line"] = 2
    else:
        payload["raw_provider_payload"] = {}

    with pytest.raises(ValidationError):
        _validator(SCHEMAS / "context" / "v1.json").validate(payload)


def _review_result() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "summary": "One correctness issue.",
        "findings": [
            {
                "path": "src/example.py",
                "side": "NEW",
                "start_line": 2,
                "end_line": 2,
                "category": "correctness",
                "severity": "high",
                "title": "Value is returned before validation",
                "explanation": "The new path bypasses validation.",
                "evidence": "return value",
                "recommendation": "Validate value before returning.",
                "proposed_diff_fix": None,
            }
        ],
        "limitations": ["Related generated sources were excluded."],
        "usage": {"input_tokens": 120, "output_tokens": 40},
        "latency_ms": 250,
    }


def test_review_result_accepts_canonical_example_and_nullable_usage() -> None:
    validator = _validator(SCHEMAS / "review-result" / "v1.json")
    result = _review_result()
    validator.validate(result)
    result["usage"] = None
    validator.validate(result)


@pytest.mark.parametrize("mutation", ["model_chunk_id", "bad_line", "bad_usage"])
def test_review_result_rejects_invalid_examples(mutation: str) -> None:
    result = copy.deepcopy(_review_result())
    if mutation == "model_chunk_id":
        result["chunk_id"] = "model-owned"
    elif mutation == "bad_line":
        result["findings"][0]["start_line"] = 0
    else:
        result["usage"] = {"tokens": 160}

    with pytest.raises(ValidationError):
        _validator(SCHEMAS / "review-result" / "v1.json").validate(result)


def test_queue_and_dead_letter_envelopes_enforce_safe_shapes() -> None:
    task_path = SCHEMAS / "queue" / "task-envelope.v1.json"
    task_schema = _load_json(task_path)
    assert set(task_schema["properties"]) == {
        "schema_version",
        "event_id",
        "review_id",
        "task_kind",
        "attempt",
        "trace_id",
    }
    task_validator = _validator(task_path)
    task_validator.validate(
        {
            "schema_version": 1,
            "event_id": "event_123",
            "review_id": "rev_123",
            "task_kind": "analyze",
            "attempt": 1,
            "trace_id": "trace_123",
        }
    )
    compatible_task = {
        "schema_version": 1,
        "event_id": "event_123",
        "review_id": "rev_123",
        "task_kind": "analyze",
        "attempt": 1,
        "trace_id": "trace_123",
        "future_optional_field": "ignored by v1 consumer",
    }
    task_validator.validate(compatible_task)
    compatible_task["task_kind"] = "unknown"
    with pytest.raises(ValidationError):
        task_validator.validate(compatible_task)

    dead_path = SCHEMAS / "queue" / "dead" / "v1.json"
    dead_schema = _load_json(dead_path)
    assert set(dead_schema["properties"]) == {
        "schema_version",
        "quarantine_id",
        "reason_code",
        "quarantined_at",
        "source_routing_key",
        "source_event_id",
        "review_id",
        "trace_id",
        "attempt",
        "source_schema_version",
    }
    dead_validator = _validator(dead_path)
    dead = {
        "schema_version": 1,
        "quarantine_id": "dead_123",
        "reason_code": "INVALID_ENVELOPE",
        "quarantined_at": "2026-10-04T10:00:00Z",
        "source_routing_key": "review.analyze",
        "source_event_id": "event_123",
        "review_id": "rev_123",
        "source_schema_version": 1,
        "attempt": 1,
        "trace_id": "trace_123",
    }
    dead_validator.validate(dead)
    diagnostic_only = copy.deepcopy(dead)
    diagnostic_only.pop("source_event_id")
    dead_validator.validate(diagnostic_only)

    compatible_dead = copy.deepcopy(dead)
    compatible_dead["future_optional_field"] = "ignored by v1 consumer"
    dead_validator.validate(compatible_dead)

    missing_required = copy.deepcopy(dead)
    missing_required.pop("quarantine_id")
    with pytest.raises(ValidationError):
        dead_validator.validate(missing_required)

    invalid_reason = copy.deepcopy(dead)
    invalid_reason["reason_code"] = "FUTURE_REASON"
    with pytest.raises(ValidationError):
        dead_validator.validate(invalid_reason)


def test_migration_contains_required_legacy_normalizations() -> None:
    migration = ROOT / "migrations" / "versions" / "0003_contract_foundation.py"
    source = migration.read_text(encoding="utf-8")

    assert "UPDATE chunk_result SET limitations = '[]'::jsonb" in source
    assert "76eb80bcc51fe28db9c1489e104e7237950feb72b9495062186c4488de69ebbf" in source
    assert "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a" in source
    for legacy_phase in ("snapshot", "context", "inference", "validation", "done"):
        assert f"WHEN '{legacy_phase}'" in source


def test_settings_contract_accepts_canonical_values() -> None:
    validator = _validator(SCHEMAS / "settings" / "v1.json")
    validator.validate(
        {
            "rules": {"instructions": ["Prefer explicit error handling."]},
            "ignores": {"globs": ["vendor/**", "**/*.generated.py"]},
        }
    )


def test_rules_digest_is_deterministic_and_instruction_order_is_significant() -> None:
    rules = {"instructions": ["First", "Second"]}
    assert compute_rules_digest(rules) == compute_rules_digest(dict(rules))
    assert compute_rules_digest(rules) != compute_rules_digest(
        {"instructions": ["Second", "First"]}
    )
    assert compute_rules_digest({"instructions": []}) == (
        "76eb80bcc51fe28db9c1489e104e7237950feb72b9495062186c4488de69ebbf"
    )


def test_application_ports_use_provider_neutral_typed_contracts() -> None:
    ref = PullRequestRef(repository_external_id="100", ref="main", sha="abc")
    pull_request = OpenPullRequest(
        number=42,
        title="Typed contracts",
        author=PullRequestAuthor(login="octocat"),
        state="OPEN",
        source=ref,
        base=ref,
    )
    assert OpenPullRequestPage(items=[pull_request], next_cursor="opaque").items == [
        pull_request
    ]

    finding = ReviewFinding(
        path="src/example.py",
        side=FindingSide.NEW,
        start_line=1,
        end_line=1,
        category=FindingCategory.CORRECTNESS,
        severity=FindingSeverity.HIGH,
        title="Issue",
        explanation="Explanation",
        evidence="Evidence",
        recommendation="Recommendation",
        proposed_diff_fix=None,
    )
    result = ReviewChunkResult(
        schema_version=1,
        summary="Summary",
        limitations=[],
        usage=None,
        latency_ms=0,
        findings=[finding],
    )
    assert result.findings == [finding]


@pytest.mark.parametrize(
    ("rules", "ignores"),
    [
        ({"instructions": [" "]}, {"globs": []}),
        ({"instructions": ["same", "same"]}, {"globs": []}),
        ({"instructions": []}, {"globs": ["/absolute/**"]}),
        ({"instructions": []}, {"globs": ["src/../secret"]}),
        ({"instructions": []}, {"globs": ["src\\windows"]}),
        ({"instructions": []}, {"globs": ["!src/keep.py"]}),
    ],
)
def test_settings_contract_rejects_invalid_values(
    rules: dict[str, Any], ignores: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        _validator(SCHEMAS / "settings" / "v1.json").validate(
            {"rules": rules, "ignores": ignores}
        )


def test_openapi_is_valid_and_contains_exact_required_route_surface() -> None:
    with (ROOT / "openapi.yaml").open(encoding="utf-8") as source:
        document = yaml.safe_load(source)

    validate(document)
    expected_operations = {
        ("/api/v1/auth/github/start", "get"),
        ("/api/v1/auth/github/callback", "get"),
        ("/api/v1/auth/logout", "post"),
        ("/api/v1/me", "get"),
        ("/api/v1/repositories", "get"),
        ("/api/v1/repositories/{id}/pull-requests", "get"),
        ("/api/v1/repositories/{id}/settings", "get"),
        ("/api/v1/repositories/{id}/settings", "put"),
        ("/api/v1/repositories/{id}/pull-requests/{number}/reviews", "post"),
        ("/api/v1/repositories/{id}/reviews", "get"),
        ("/api/v1/reviews/{id}", "get"),
        ("/api/v1/reviews/{id}/findings", "get"),
        ("/api/v1/reviews/{id}/diff", "get"),
        ("/api/v1/reviews/{id}/events", "get"),
        ("/api/v1/reviews/{id}/publication-retries", "post"),
        ("/api/v1/webhooks/github", "post"),
        ("/healthcheck", "get"),
        ("/readiness", "get"),
    }
    actual_operations = {
        (path, method)
        for path, path_item in document["paths"].items()
        for method in path_item
        if method in {"get", "post", "put", "patch", "delete"}
    }
    assert actual_operations == expected_operations
    assert all("stage" not in path for path in document["paths"])

    detail_properties = document["components"]["schemas"]["ReviewDetail"]["properties"]
    event_properties = document["components"]["schemas"]["ReviewEvent"]["properties"]
    assert "stage" not in detail_properties
    assert {"phase", "attempt", "safe_details"}.isdisjoint(event_properties)

    callback_parameters = {
        parameter["name"]: parameter
        for parameter in document["paths"]["/api/v1/auth/github/callback"]["get"][
            "parameters"
        ]
    }
    assert callback_parameters["state"]["required"] is True
    assert callback_parameters["code"]["required"] is False
    assert callback_parameters["error"]["required"] is False

    logout = document["paths"]["/api/v1/auth/logout"]["post"]
    assert "401" not in logout["responses"]
    assert {} in logout["security"]

    settings_path = document["paths"]["/api/v1/repositories/{id}/settings"]
    assert "Admin role required" in settings_path["get"]["description"]
    assert "Admin role required" in settings_path["put"]["description"]
    settings_replacement = document["components"]["schemas"][
        "RepositorySettingsReplacement"
    ]
    assert settings_replacement["properties"]["output_language"] == {"const": "ru"}
    settings_response = document["components"]["schemas"]["RepositorySettings"]
    assert settings_response["properties"]["output_language"] == {"const": "ru"}

    start_request = document["components"]["schemas"]["StartReviewRequest"]
    assert start_request["required"] == ["requested_head_sha"]
    start_responses = document["paths"][
        "/api/v1/repositories/{id}/pull-requests/{number}/reviews"
    ]["post"]["responses"]
    replay_ref = start_responses["200"]["content"]["application/json"]["schema"]
    accepted_ref = start_responses["202"]["content"]["application/json"]["schema"]
    assert replay_ref["$ref"].endswith("/StartReviewReplayResponse")
    assert accepted_ref["$ref"].endswith("/StartReviewResponse")
