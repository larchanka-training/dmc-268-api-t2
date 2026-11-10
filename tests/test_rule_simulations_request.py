"""Bounded JSON/DTO validation at the authenticated simulation HTTP boundary."""

import json
from http import HTTPStatus
from pathlib import Path

import httpx
import pytest
import yaml
from test_postgres_integration import PostgresHarness
from test_rule_simulations_access import (
    VALID_GIT_DIFF,
    VALID_SIMULATION_BODY,
    SimulationScope,
    assert_simulation_response,
)

pytest_plugins = ("test_rule_simulations_access",)


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b'{"source":"private-source",', id="truncated-object"),
        pytest.param(b'{"markdown":"private-rules",}', id="trailing-comma"),
        pytest.param(b"", id="empty-body"),
        pytest.param(b'{"source":"\xff"}', id="invalid-utf8"),
        pytest.param(b"[" * 1000 + b"0" + b"]" * 1000, id="excessive-nesting"),
    ],
)
async def test_malformed_json_is_safe_canonical_validation_failure(
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    postgres: PostgresHarness,
    github_requests: list[httpx.Request],
    body: bytes,
) -> None:
    await assert_simulation_response(
        browser,
        simulation_scope,
        postgres,
        github_requests,
        simulation_scope.repository,
        HTTPStatus.UNPROCESSABLE_ENTITY,
        "VALIDATION_FAILED",
        "Request is invalid.",
        content=body,
        headers={"Content-Type": "application/json"},
    )


@pytest.mark.parametrize("body", [b"[]", b"null", b'"private-source"', b"123"])
async def test_top_level_must_be_an_object(
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    postgres: PostgresHarness,
    github_requests: list[httpx.Request],
    body: bytes,
) -> None:
    await assert_simulation_response(
        browser,
        simulation_scope,
        postgres,
        github_requests,
        simulation_scope.repository,
        HTTPStatus.UNPROCESSABLE_ENTITY,
        "VALIDATION_FAILED",
        "Request is invalid.",
        content=body,
        headers={"Content-Type": "application/json"},
    )


@pytest.mark.parametrize("level", ["top", "code", "diff", "rules"])
@pytest.mark.parametrize(
    "field",
    ["unexpected", "model", "api_key", "system_prompt", "provider", "budget", "quota"],
)
async def test_unknown_fields_are_forbidden_at_every_level(  # noqa: PLR0913, PLR0917 -- explicit HTTP fixtures and request vectors
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    postgres: PostgresHarness,
    github_requests: list[httpx.Request],
    level: str,
    field: str,
) -> None:
    payload = json.loads(VALID_SIMULATION_BODY)
    if level == "diff":
        payload["input"] = {"kind": "diff", "unified_diff": VALID_GIT_DIFF}
    target = (
        payload if level == "top" else payload["rules" if level == "rules" else "input"]
    )
    target[field] = "private-untrusted-value"
    await assert_simulation_response(
        browser,
        simulation_scope,
        postgres,
        github_requests,
        simulation_scope.repository,
        HTTPStatus.UNPROCESSABLE_ENTITY,
        "VALIDATION_FAILED",
        "Request is invalid.",
        content=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )


@pytest.mark.parametrize(
    "input_value",
    [
        pytest.param(
            {"kind": "unknown", "path": "a.py", "source": "x"}, id="unknown-kind"
        ),
        pytest.param({"path": "a.py", "source": "x"}, id="missing-kind"),
        pytest.param({"kind": 1, "path": "a.py", "source": "x"}, id="kind-number"),
        pytest.param(
            {"kind": "code", "path": "a.py", "source": "x", "unified_diff": "x"},
            id="code-with-diff",
        ),
        pytest.param(
            {"kind": "diff", "unified_diff": "x", "source": "x"}, id="diff-with-source"
        ),
        pytest.param(
            {"kind": "diff", "unified_diff": "x", "path": "a.py"}, id="diff-with-path"
        ),
        pytest.param({"kind": "code", "source": "x"}, id="missing-path"),
        pytest.param({"kind": "code", "path": "a.py"}, id="missing-source"),
        pytest.param({"kind": "diff"}, id="missing-unified-diff"),
        pytest.param({"kind": "code", "path": 123, "source": "x"}, id="path-number"),
        pytest.param({"kind": "code", "path": None, "source": "x"}, id="path-null"),
        pytest.param(
            {"kind": "code", "path": "a.py", "source": 123}, id="source-number"
        ),
        pytest.param(
            {"kind": "code", "path": "a.py", "source": True}, id="source-bool"
        ),
        pytest.param(
            {"kind": "code", "path": "a.py", "source": None}, id="source-null"
        ),
        pytest.param({"kind": "code", "path": "a.py", "source": []}, id="source-array"),
        pytest.param(
            {"kind": "code", "path": "a.py", "source": {}}, id="source-object"
        ),
        pytest.param({"kind": "diff", "unified_diff": 123}, id="diff-number"),
        pytest.param({"kind": "diff", "unified_diff": None}, id="diff-null"),
    ],
)
async def test_input_kind_selects_strict_required_fields(
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    postgres: PostgresHarness,
    github_requests: list[httpx.Request],
    input_value: object,
) -> None:
    payload = json.loads(VALID_SIMULATION_BODY)
    payload["input"] = input_value
    await assert_simulation_response(
        browser,
        simulation_scope,
        postgres,
        github_requests,
        simulation_scope.repository,
        HTTPStatus.UNPROCESSABLE_ENTITY,
        "VALIDATION_FAILED",
        "Request is invalid.",
        content=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )


@pytest.mark.parametrize(
    "rules",
    [
        pytest.param(
            {"format": "rules-md/2", "markdown": "private-rules"}, id="unknown-format"
        ),
        pytest.param({"markdown": "private-rules"}, id="missing-format"),
        pytest.param({"format": "rules-md/1"}, id="missing-markdown"),
        pytest.param({"format": 1, "markdown": "private-rules"}, id="format-number"),
        pytest.param({"format": None, "markdown": "private-rules"}, id="format-null"),
        pytest.param({"format": "rules-md/1", "markdown": 123}, id="markdown-number"),
        pytest.param({"format": "rules-md/1", "markdown": False}, id="markdown-bool"),
        pytest.param({"format": "rules-md/1", "markdown": None}, id="markdown-null"),
        pytest.param({"format": "rules-md/1", "markdown": []}, id="markdown-array"),
        pytest.param({"format": "rules-md/1", "markdown": {}}, id="markdown-object"),
    ],
)
async def test_rules_require_known_format_and_strict_markdown(
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    postgres: PostgresHarness,
    github_requests: list[httpx.Request],
    rules: object,
) -> None:
    payload = json.loads(VALID_SIMULATION_BODY)
    payload["rules"] = rules
    await assert_simulation_response(
        browser,
        simulation_scope,
        postgres,
        github_requests,
        simulation_scope.repository,
        HTTPStatus.UNPROCESSABLE_ENTITY,
        "VALIDATION_FAILED",
        "Request is invalid.",
        content=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )


@pytest.mark.parametrize(
    ("input_value", "markdown"),
    [
        pytest.param(
            {
                "kind": "code",
                "path": "playground/example.py",
                "source": "print('Hello')",
            },
            "# Security\n## SEC-1: Example\nCheck the input.",
            id="code",
        ),
        pytest.param(
            {"kind": "diff", "unified_diff": VALID_GIT_DIFF},
            "# Security\n## SEC-1: Example\nCheck the input.",
            id="diff",
        ),
        pytest.param(
            {"kind": "code", "path": "valid.py", "source": "x"},
            "# Performance\n## PERF-1: Bound calls\nUse timeouts.",
            id="code-with-performance-rules",
        ),
        pytest.param(
            {"kind": "diff", "unified_diff": VALID_GIT_DIFF},
            "# Style\n## STYLE-1: Имена\nИспользовать понятные имена.",  # noqa: RUF001 -- intentional Cyrillic rule content
            id="diff-with-unicode-style-rules",
        ),
    ],
)
async def test_structurally_valid_request_remains_unavailable_without_simulation(  # noqa: PLR0913, PLR0917 -- explicit HTTP fixtures and accepted request vectors
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    postgres: PostgresHarness,
    github_requests: list[httpx.Request],
    input_value: dict[str, str],
    markdown: str,
) -> None:
    await assert_simulation_response(
        browser,
        simulation_scope,
        postgres,
        github_requests,
        simulation_scope.repository,
        HTTPStatus.SERVICE_UNAVAILABLE,
        "SERVICE_UNAVAILABLE",
        "Service is unavailable.",
        content=json.dumps(
            {
                "input": input_value,
                "rules": {"format": "rules-md/1", "markdown": markdown},
            },
            ensure_ascii=False,
        ).encode(),
        headers={"Content-Type": "application/json"},
    )


@pytest.mark.parametrize("example", ["code", "diff"])
async def test_authoritative_examples_reach_canonical_temporary_503(
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    postgres: PostgresHarness,
    github_requests: list[httpx.Request],
    example: str,
) -> None:
    document = yaml.safe_load(
        (Path(__file__).parents[1] / "openapi.yaml").read_text(encoding="utf-8")
    )
    examples = document["paths"]["/api/v1/repositories/{id}/rule-simulations"]["post"][
        "requestBody"
    ]["content"]["application/json"]["examples"]
    await assert_simulation_response(
        browser,
        simulation_scope,
        postgres,
        github_requests,
        simulation_scope.repository,
        HTTPStatus.SERVICE_UNAVAILABLE,
        "SERVICE_UNAVAILABLE",
        "Service is unavailable.",
        content=json.dumps(examples[example]["value"]).encode(),
    )
