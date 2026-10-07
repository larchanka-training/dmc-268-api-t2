from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from openapi_spec_validator import validate
from referencing import Registry, Resource

from app.application.ports import (
    BuiltContextPayload,
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
    registry = Registry().with_resources(
        (document["$id"], Resource.from_contents(document))
        for file in SCHEMAS.rglob("*.json")
        if "$id" in (document := _load_json(file))
    )
    return Draft202012Validator(
        schema, format_checker=FormatChecker(), registry=registry
    )


@pytest.mark.parametrize(
    "path",
    [
        SCHEMAS / "context" / "v1.json",
        SCHEMAS / "review-result" / "v1.json",
        SCHEMAS / "llm-output" / "v1.json",
        SCHEMAS / "queue" / "task-envelope.v1.json",
        SCHEMAS / "queue" / "dead" / "v1.json",
        SCHEMAS / "settings" / "v1.json",
    ],
)
def test_checked_in_json_schemas_are_valid(path: Path) -> None:
    _validator(path)


def _context_payload() -> dict[str, Any]:
    return _load_json(SCHEMAS / "examples" / "context.v1.json")


def test_context_payload_accepts_canonical_example() -> None:
    _validator(SCHEMAS / "context" / "v1.json").validate(_context_payload())


def test_context_port_carries_backend_identity_for_wire_assembly() -> None:
    expected = _context_payload()
    review_id, chunk_id = uuid.uuid4(), uuid.uuid4()
    expected.update(review_id=str(review_id), chunk_id=str(chunk_id))
    body = {
        k: v
        for k, v in expected.items()
        if k not in {"schema_version", "review_id", "chunk_id"}
    }
    context = BuiltContextPayload(
        schema_version=1, review_id=review_id, chunk_id=chunk_id, payload_body=body
    )
    assert context.to_wire() == expected
    _validator(SCHEMAS / "context" / "v1.json").validate(context.to_wire())


@pytest.mark.parametrize("reserved", ["schema_version", "review_id", "chunk_id"])
def test_context_body_must_not_duplicate_authoritative_envelope_fields(
    reserved: str,
) -> None:
    context = BuiltContextPayload(
        schema_version=1,
        review_id=uuid.uuid4(),
        chunk_id=uuid.uuid4(),
        payload_body={reserved: "spoofed"},
    )
    with pytest.raises(ValueError, match="envelope"):
        context.to_wire()


def _api_validator(name: str) -> Draft202012Validator:
    document = yaml.safe_load((ROOT / "openapi.yaml").read_text(encoding="utf-8"))
    return Draft202012Validator(
        {"$ref": f"#/components/schemas/{name}", "components": document["components"]}
    )


def test_repository_access_revalidation_has_safe_unavailable_response() -> None:
    api = yaml.safe_load((ROOT / "openapi.yaml").read_text(encoding="utf-8"))
    responses = api["paths"]["/api/v1/repositories"]["get"]["responses"]
    assert responses["503"]["$ref"] == "#/components/responses/ServiceUnavailable"


def test_repository_connection_contract_matches_ui_sprint_two_flow() -> None:
    api = yaml.safe_load((ROOT / "openapi.yaml").read_text(encoding="utf-8"))
    available = api["paths"]["/api/v1/repositories/available"]["get"]
    connect = api["paths"]["/api/v1/repositories/connect"]["post"]
    assert available["responses"]["200"]["content"]["application/json"]["schema"][
        "$ref"
    ].endswith("/AvailableRepositoryPage")
    assert {"200", "201", "401", "403", "422", "503"} <= connect["responses"].keys()
    assert connect["security"] == [{"cookieAuth": [], "csrfHeader": []}]
    _api_validator("ConnectRepositoryRequest").validate(
        {"provider": "github", "external_id": "42"}
    )
    with pytest.raises(ValidationError):
        _api_validator("ConnectRepositoryRequest").validate(
            {"provider": "github", "external_id": "42", "role": "admin"}
        )


def test_session_refresh_contract_requires_active_session_origin_and_csrf() -> None:
    api = yaml.safe_load((ROOT / "openapi.yaml").read_text(encoding="utf-8"))
    refresh = api["paths"]["/api/v1/auth/refresh"]["post"]
    assert refresh["security"] == [{"cookieAuth": [], "csrfHeader": []}]
    assert {"200", "401", "403", "503"} <= refresh["responses"].keys()
    assert "Set-Cookie" in refresh["responses"]["200"]["headers"]
    assert refresh["responses"]["200"]["content"]["application/json"]["schema"][
        "$ref"
    ].endswith("/Me")


def test_unknown_legacy_settings_have_explicit_safe_read_and_start_policy() -> None:
    api = yaml.safe_load((ROOT / "openapi.yaml").read_text(encoding="utf-8"))
    settings = api["paths"]["/api/v1/repositories/{id}/settings"]["get"]
    response = settings["responses"]["409"]
    assert response["$ref"] == "#/components/responses/LegacySettingsUnsupported"
    unsupported = api["components"]["responses"]["LegacySettingsUnsupported"]
    assert unsupported["headers"]["ETag"]["required"] is True
    example = unsupported["content"]["application/json"]["example"]
    assert example["code"] == "LEGACY_SETTINGS_UNSUPPORTED"
    _api_validator("Error").validate(example)
    start = api["paths"]["/api/v1/repositories/{id}/pull-requests/{number}/reviews"][
        "post"
    ]
    assert (
        "legacy_settings"
        in start["responses"]["409"]["content"]["application/json"]["examples"]
    )


def test_historical_settings_and_context_retain_their_actual_language() -> None:
    payload = _context_payload()
    payload["metadata"]["output_language"] = "en"
    _validator(SCHEMAS / "context" / "v1.json").validate(payload)
    settings = {
        "version": "old-version",
        "rules": {"instructions": []},
        "ignores": {"globs": []},
        "max_starts_per_hour": 10,
        "max_active_jobs": 2,
        "output_language": "en",
        "surrounding_lines": 5,
        "created_at": "2026-09-24T10:00:00Z",
    }
    _api_validator("RepositorySettings").validate(settings)
    replacement = {
        k: v for k, v in settings.items() if k not in {"version", "created_at"}
    }
    with pytest.raises(ValidationError):
        _api_validator("RepositorySettingsReplacement").validate(replacement)


def test_context_accepts_gateway_discussions_and_unknown_file_language() -> None:
    payload = _context_payload()
    payload["metadata"]["discussions"] = [
        {
            "author": "reviewer",
            "body": "Keep the error handling.",
            "path": "src/example.py",
            "line": 2,
        },
        {"author": "reviewer", "body": "General note", "path": None, "line": None},
    ]
    payload["files"][0]["diff"]["language"] = None
    _validator(SCHEMAS / "context" / "v1.json").validate(payload)


@pytest.mark.parametrize("path", ["./src/a.py", "src//a.py", "src/", "src/./a.py"])
def test_contract_paths_require_normalized_repository_relative_paths(path: str) -> None:
    payload = _context_payload()
    payload["files"][0]["diff"]["new_path"] = path
    with pytest.raises(ValidationError):
        _validator(SCHEMAS / "context" / "v1.json").validate(payload)
    result = _review_result()
    result["findings"][0]["path"] = path
    with pytest.raises(ValidationError):
        _validator(SCHEMAS / "review-result" / "v1.json").validate(result)
    with pytest.raises(ValidationError):
        _api_validator("DiffFile").validate(
            {"path": path, "previous_path": None, "status": "modified", "hunks": []}
        )


@pytest.mark.parametrize("added", [True, False])
def test_unified_diff_accepts_zero_start_on_empty_side(added: bool) -> None:
    payload = _context_payload()
    hunk = payload["files"][0]["diff"]["hunks"][0]
    hunk.update(
        old_start=0 if added else 1,
        old_count=0 if added else 1,
        new_start=1 if added else 0,
        new_count=1 if added else 0,
        lines=[
            {
                "kind": "added" if added else "deleted",
                "text": "x = 1",
                "old_line": None if added else 1,
                "new_line": 1 if added else None,
            }
        ],
    )
    _validator(SCHEMAS / "context" / "v1.json").validate(payload)
    public_hunk = {
        "old_start": hunk["old_start"],
        "old_lines": hunk["old_count"],
        "new_start": hunk["new_start"],
        "new_lines": hunk["new_count"],
        "lines": [
            {
                "kind": line["kind"],
                "content": line["text"],
                "old_line": line["old_line"],
                "new_line": line["new_line"],
            }
            for line in hunk["lines"]
        ],
    }
    _api_validator("DiffHunk").validate(public_hunk)


@pytest.mark.parametrize("side", ["old", "new"])
def test_nonempty_hunk_side_requires_positive_start(side: str) -> None:
    payload = _context_payload()
    hunk = payload["files"][0]["diff"]["hunks"][0]
    hunk[f"{side}_start"] = 0
    with pytest.raises(ValidationError):
        _validator(SCHEMAS / "context" / "v1.json").validate(payload)
    with pytest.raises(ValidationError):
        _api_validator("DiffHunk").validate(
            {
                "old_start": hunk["old_start"],
                "old_lines": hunk["old_count"],
                "new_start": hunk["new_start"],
                "new_lines": hunk["new_count"],
                "lines": [],
            }
        )


@pytest.mark.parametrize("kind", ["added", "deleted", "context"])
def test_public_diff_rejects_coordinates_inconsistent_with_kind(kind: str) -> None:
    line: dict[str, Any] = {
        "kind": kind,
        "content": "x",
        "old_line": None,
        "new_line": None,
    }
    if kind in {"added", "deleted"}:
        line.update(old_line=1, new_line=1)
    with pytest.raises(ValidationError):
        _api_validator("DiffLine").validate(line)


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
    return _load_json(SCHEMAS / "examples" / "review-result.v1.json")


def test_review_result_accepts_canonical_example_and_nullable_usage() -> None:
    validator = _validator(SCHEMAS / "review-result" / "v1.json")
    result = _review_result()
    validator.validate(result)
    result["usage"] = None
    validator.validate(result)


@pytest.mark.parametrize(
    "metadata",
    ["usage", "latency_ms", "schema_version", "review_id", "chunk_id", "status"],
)
def test_llm_output_schema_separates_model_output_from_backend_metadata(
    metadata: str,
) -> None:
    result = _review_result()
    output = {key: result[key] for key in ("summary", "findings", "limitations")}
    validator = _validator(SCHEMAS / "llm-output" / "v1.json")
    validator.validate(output)
    output[metadata] = "model-owned"
    with pytest.raises(ValidationError):
        validator.validate(output)


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
        ("/api/v1/auth/refresh", "post"),
        ("/api/v1/me", "get"),
        ("/api/v1/repositories", "get"),
        ("/api/v1/repositories/available", "get"),
        ("/api/v1/repositories/connect", "post"),
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
    assert settings_response["properties"]["output_language"]["type"] == "string"

    start_request = document["components"]["schemas"]["StartReviewRequest"]
    assert start_request["required"] == ["requested_head_sha"]
    start_responses = document["paths"][
        "/api/v1/repositories/{id}/pull-requests/{number}/reviews"
    ]["post"]["responses"]
    replay_ref = start_responses["200"]["content"]["application/json"]["schema"]
    accepted_ref = start_responses["202"]["content"]["application/json"]["schema"]
    assert replay_ref["$ref"].endswith("/StartReviewReplayResponse")
    assert accepted_ref["$ref"].endswith("/StartReviewResponse")
