"""Safe rules diagnostics through authenticated HTTP and the real rules parser."""

import builtins
import io
import json
import os
from collections.abc import Callable
from http import HTTPStatus
from typing import Any, cast

import httpx
import pytest
from test_postgres_integration import PostgresHarness
from test_rule_simulations_access import (
    SimulationScope,
    assert_simulation_response,
)
from test_rule_simulations_rules import rules_body

from app.api.rule_simulations import project_rules_diagnostics
from app.context.rules import ParsedRules

pytest_plugins = ("test_rule_simulations_access",)


async def test_duplicate_rule_reference_has_safe_diagnostic(
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    postgres: PostgresHarness,
    github_requests: list[httpx.Request],
    caplog: pytest.LogCaptureFixture,
) -> None:
    markdown = (
        "# Security\n## SEC-1: private-title\nprivate-body\n"
        "## SEC-1: private-secret-token\nprivate-source"
    )
    await assert_simulation_response(
        browser,
        simulation_scope,
        postgres,
        github_requests,
        simulation_scope.repository,
        HTTPStatus.UNPROCESSABLE_ENTITY,
        "VALIDATION_FAILED",
        "Request is invalid.",
        content=rules_body(markdown),
        headers={"Content-Type": "application/json"},
        expected_extra={
            "diagnostics": [
                {
                    "code": "RULES_DUPLICATE_REF",
                    "line": 4,
                    "message": "Rule references must be unique.",
                }
            ],
            "diagnostics_truncated": False,
        },
    )
    assert "private-" not in caplog.text
    assert "duplicate_ref:4" not in caplog.text


@pytest.mark.parametrize(
    ("markdown", "code", "line", "message"),
    [
        (
            "# private-unsupported\n# Security\n## SEC-1: private-title\nbody",
            "RULES_INVALID_SECTION",
            1,
            "Unsupported top-level rules section.",
        ),
        (
            "# Security\n## SEC-1: private-title\n- severity: private-secret",
            "RULES_INVALID_SEVERITY",
            3,
            "Use a supported severity value.",
        ),
        (
            "# Security\n## SEC-1: private-title\nbody\n# Ignore\n- private-secret?",
            "RULES_INVALID_IGNORE",
            5,
            "Use the supported Ignore entry format.",
        ),
        (
            "# Security\n## SEC-1: private-title\n```python\nprivate-source",
            "RULES_UNCLOSED_FENCE",
            None,
            "Close the Markdown code fence.",
        ),
        (
            "x" * 65537,
            "RULES_TOO_LARGE",
            None,
            "Rules exceed the UTF-8 byte limit.",
        ),
        (
            "# Security\n" + "".join(f"## SEC-{i}: x\n" for i in range(1, 3001)),
            "RULES_CANONICAL_LIMIT",
            None,
            "Parsed rules exceed the instruction limit.",
        ),
    ],
    ids=["section", "severity", "ignore", "fence", "file-limit", "canonical-limit"],
)
async def test_known_parser_diagnostic_mapping(  # noqa: PLR0913, PLR0917 -- HTTP fixtures and literal contract vector
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    postgres: PostgresHarness,
    github_requests: list[httpx.Request],
    caplog: pytest.LogCaptureFixture,
    markdown: str,
    code: str,
    line: int | None,
    message: str,
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
        content=rules_body(markdown),
        headers={"Content-Type": "application/json"},
        expected_extra={
            "diagnostics": [{"code": code, "line": line, "message": message}],
            "diagnostics_truncated": False,
        },
    )
    assert "private-" not in caplog.text
    for internal in (
        "invalid_structure:",
        "invalid_severity:",
        "invalid_ignore:",
        "unclosed_fence:",
        "file_too_large:",
        "combined_rule_limit:",
    ):
        assert internal not in caplog.text


@pytest.mark.parametrize(
    "markdown",
    [
        "",
        "# Security\n",
        "# Security\n## SEC-1: private-title\nbody\n# Ignore\n- SEC-1",
    ],
    ids=["empty", "heading-only", "all-ignored"],
)
async def test_rules_without_active_criteria_have_safe_diagnostic(
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    postgres: PostgresHarness,
    github_requests: list[httpx.Request],
    markdown: str,
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
        content=rules_body(markdown),
        headers={"Content-Type": "application/json"},
        expected_extra={
            "diagnostics": [
                {
                    "code": "RULES_NO_ACTIVE_CRITERIA",
                    "line": None,
                    "message": "Add at least one active rule criterion.",
                }
            ],
            "diagnostics_truncated": False,
        },
    )


async def test_parser_diagnostics_keep_their_order(
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    postgres: PostgresHarness,
    github_requests: list[httpx.Request],
) -> None:
    markdown = (
        "# Security\n## SEC-1: private-title\nprivate-body\n"
        "## SEC-1: private-duplicate\n## SEC-2: private-bad\n"
        "- severity: private-secret\n# Ignore\n- private-ignore?"
    )
    await assert_simulation_response(
        browser,
        simulation_scope,
        postgres,
        github_requests,
        simulation_scope.repository,
        HTTPStatus.UNPROCESSABLE_ENTITY,
        "VALIDATION_FAILED",
        "Request is invalid.",
        content=rules_body(markdown),
        headers={"Content-Type": "application/json"},
        expected_extra={
            "diagnostics": [
                {
                    "code": "RULES_DUPLICATE_REF",
                    "line": 4,
                    "message": "Rule references must be unique.",
                },
                {
                    "code": "RULES_INVALID_SEVERITY",
                    "line": 6,
                    "message": "Use a supported severity value.",
                },
                {
                    "code": "RULES_INVALID_IGNORE",
                    "line": 8,
                    "message": "Use the supported Ignore entry format.",
                },
            ],
            "diagnostics_truncated": False,
        },
    )


@pytest.mark.parametrize("count", [20, 21, 200], ids=["at-cap", "one-over", "many"])
async def test_diagnostics_count_is_bounded_and_truncation_is_truthful(
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    postgres: PostgresHarness,
    github_requests: list[httpx.Request],
    count: int,
) -> None:
    markdown = "# Security\n## SEC-1: private-title\n" + (
        "## SEC-1: private-duplicate\n" * count
    )
    await assert_simulation_response(
        browser,
        simulation_scope,
        postgres,
        github_requests,
        simulation_scope.repository,
        HTTPStatus.UNPROCESSABLE_ENTITY,
        "VALIDATION_FAILED",
        "Request is invalid.",
        content=rules_body(markdown),
        headers={"Content-Type": "application/json"},
        expected_extra={
            "diagnostics": [
                {
                    "code": "RULES_DUPLICATE_REF",
                    "line": line,
                    "message": "Rule references must be unique.",
                }
                for line in range(3, 23)
            ],
            "diagnostics_truncated": count > 20,  # noqa: PLR2004 -- public diagnostics cap
        },
    )


def test_unknown_parser_code_is_projected_without_internal_text() -> None:
    diagnostics, truncated = project_rules_diagnostics(
        ParsedRules((), "invalid", ("private-secret-unknown:7",))
    )
    assert diagnostics == [
        {
            "code": "RULES_INVALID",
            "line": None,
            "message": "Rules could not be validated.",
        }
    ]
    assert truncated is False


@pytest.mark.parametrize(
    "raw",
    [
        "private-secret",
        "duplicate_ref:7:private-secret",
        "duplicate_ref:-1",
        "duplicate_ref:+1",
        "duplicate_ref: 1",
        "duplicate_ref:1.5",
        "duplicate_ref:\u0661",
        "duplicate_ref:" + "9" * 5000,
        None,
        7,
        {"private-secret": "private-source"},
    ],
    ids=[
        "no-colon",
        "extra-colon",
        "negative",
        "plus",
        "whitespace",
        "fraction",
        "unicode-digit",
        "huge-number",
        "null",
        "integer",
        "mapping",
    ],
)
def test_corrupt_internal_diagnostic_has_safe_fallback(raw: object) -> None:
    diagnostics, truncated = project_rules_diagnostics(
        ParsedRules((), "invalid", (cast(str, raw),))
    )
    assert diagnostics == [
        {
            "code": "RULES_INVALID",
            "line": None,
            "message": "Rules could not be validated.",
        }
    ]
    assert truncated is False


@pytest.mark.parametrize("state", ["invalid", "private-unknown-state"])
def test_invalid_parser_state_without_diagnostics_uses_generic_fallback(
    state: str,
) -> None:
    diagnostics, truncated = project_rules_diagnostics(ParsedRules((), state))
    assert diagnostics == [
        {
            "code": "RULES_INVALID",
            "line": None,
            "message": "Rules could not be validated.",
        }
    ]
    assert truncated is False


def test_diagnostics_json_byte_budget_is_enforced_before_count_cap() -> None:
    # Adversarial internal coordinates exercise the independent 8192-byte gate.
    # Real parser coordinates are much smaller; its known codes are tested via HTTP.
    raw = "duplicate_ref:" + "1" + "0" * 4199
    diagnostics, truncated = project_rules_diagnostics(
        ParsedRules((), "invalid", (raw, raw, raw))
    )
    assert diagnostics == [
        {
            "code": "RULES_DUPLICATE_REF",
            "line": 10**4199,
            "message": "Rule references must be unique.",
        }
    ]
    assert truncated is True
    assert len(json.dumps(diagnostics, ensure_ascii=False).encode("utf-8")) <= 8192  # noqa: PLR2004 -- contract budget


async def test_diagnostics_preserve_source_privacy_and_do_not_read_user_path(  # noqa: PLR0913, PLR0917 -- HTTP and security boundary fixtures
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    postgres: PostgresHarness,
    github_requests: list[httpx.Request],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = "playground/private-sensitive.py"

    def guard(opener: Callable[..., Any]) -> Callable[..., Any]:
        def guarded(file: Any, *args: Any, **kwargs: Any) -> Any:
            assert str(file) != path, "User path must never be opened"
            return opener(file, *args, **kwargs)

        return guarded

    # Boundary probes for Python file reads; not a general syscall trace.
    monkeypatch.setattr(builtins, "open", guard(builtins.open))
    monkeypatch.setattr(io, "open", guard(io.open))
    monkeypatch.setattr(os, "open", guard(os.open))
    content = json.loads(
        rules_body(
            "# Security\n## SEC-1: private-title\nprivate-body\n"
            "## SEC-1: private-secret-api-key\nprivate-criterion-body"
        )
    )
    content["input"] = {
        "kind": "code",
        "path": path,
        "source": "private-source-secret-token",
    }
    await assert_simulation_response(
        browser,
        simulation_scope,
        postgres,
        github_requests,
        simulation_scope.repository,
        HTTPStatus.UNPROCESSABLE_ENTITY,
        "VALIDATION_FAILED",
        "Request is invalid.",
        content=json.dumps(content).encode(),
        headers={"Content-Type": "application/json"},
        expected_extra={
            "diagnostics": [
                {
                    "code": "RULES_DUPLICATE_REF",
                    "line": 4,
                    "message": "Rule references must be unique.",
                }
            ],
            "diagnostics_truncated": False,
        },
    )
    assert "private-" not in caplog.text
    assert "duplicate_ref:4" not in caplog.text
