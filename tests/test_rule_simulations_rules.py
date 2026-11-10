# ruff: noqa: RUF001 -- intentional Cyrillic rule titles and instructions
"""Draft rules validation at the authenticated simulation HTTP boundary."""

import json
from http import HTTPStatus
from typing import NoReturn

import httpx
import pytest
from sqlalchemy import update
from test_postgres_integration import PostgresHarness
from test_rule_simulations_access import (
    VALID_GIT_DIFF,
    VALID_SIMULATION_BODY,
    SimulationScope,
    assert_simulation_response,
)

from app.api import rule_simulations
from app.context.rules import parse_rules
from app.db import models

pytest_plugins = ("test_rule_simulations_access",)

VALID_RULES = "# Security\n## SEC-1: Безопасность\nПроверяй входные данные."
NO_ACTIVE_DIAGNOSTIC: dict[str, object] = {
    "code": "RULES_NO_ACTIVE_CRITERIA",
    "line": None,
    "message": "Add at least one active rule criterion.",
}
# Independent literal expectations from the ADR, not the production projection.
EXPECTED_PARSER_DIAGNOSTICS: dict[str, dict[str, object]] = {
    "invalid_structure:1": {
        "code": "RULES_INVALID_SECTION",
        "line": 1,
        "message": "Unsupported top-level rules section.",
    },
    "unclosed_fence:0": {
        "code": "RULES_UNCLOSED_FENCE",
        "line": None,
        "message": "Close the Markdown code fence.",
    },
    "file_too_large:0": {
        "code": "RULES_TOO_LARGE",
        "line": None,
        "message": "Rules exceed the UTF-8 byte limit.",
    },
    "combined_rule_limit:0": {
        "code": "RULES_CANONICAL_LIMIT",
        "line": None,
        "message": "Parsed rules exceed the instruction limit.",
    },
    "duplicate_ref:4": {
        "code": "RULES_DUPLICATE_REF",
        "line": 4,
        "message": "Rule references must be unique.",
    },
    "invalid_severity:5": {
        "code": "RULES_INVALID_SEVERITY",
        "line": 5,
        "message": "Use a supported severity value.",
    },
    "invalid_ignore:5": {
        "code": "RULES_INVALID_IGNORE",
        "line": 5,
        "message": "Use the supported Ignore entry format.",
    },
    "invalid_severity:3": {
        "code": "RULES_INVALID_SEVERITY",
        "line": 3,
        "message": "Use a supported severity value.",
    },
}


def rules_body(
    markdown: str, kind: str = "code", *, ensure_ascii: bool = False
) -> bytes:
    payload = json.loads(VALID_SIMULATION_BODY)
    if kind == "diff":
        payload["input"] = {"kind": "diff", "unified_diff": VALID_GIT_DIFF}
    payload["rules"]["markdown"] = markdown
    return json.dumps(payload, ensure_ascii=ensure_ascii).encode()


async def assert_rules_response(  # noqa: PLR0913, PLR0917 -- HTTP fixtures and explicit expected outcome
    browser: httpx.AsyncClient,
    scope: SimulationScope,
    postgres: PostgresHarness,
    github_requests: list[httpx.Request],
    markdown: str,
    status: HTTPStatus,
    *,
    kind: str = "code",
    ensure_ascii: bool = False,
    diagnostics: list[dict[str, object]] | None = None,
) -> None:
    invalid = status == HTTPStatus.UNPROCESSABLE_ENTITY
    await assert_simulation_response(
        browser,
        scope,
        postgres,
        github_requests,
        scope.repository,
        status,
        "VALIDATION_FAILED" if invalid else "SERVICE_UNAVAILABLE",
        "Request is invalid." if invalid else "Service is unavailable.",
        content=rules_body(markdown, kind, ensure_ascii=ensure_ascii),
        headers={"Content-Type": "application/json"},
        expected_extra=(
            {
                "diagnostics": diagnostics or [NO_ACTIVE_DIAGNOSTIC],
                "diagnostics_truncated": False,
            }
            if invalid
            else None
        ),
    )


@pytest.mark.parametrize("markdown", ["", " \t\r\n"], ids=["empty", "whitespace"])
async def test_empty_draft_rules_are_rejected(
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    postgres: PostgresHarness,
    github_requests: list[httpx.Request],
    markdown: str,
) -> None:
    parsed = parse_rules(markdown, db_instructions=())
    assert parsed.state == "empty"
    assert parsed.instructions == ()
    await assert_rules_response(
        browser,
        simulation_scope,
        postgres,
        github_requests,
        markdown,
        HTTPStatus.UNPROCESSABLE_ENTITY,
    )


@pytest.mark.parametrize(
    "markdown",
    [
        pytest.param("# Security\n", id="heading-only"),
        pytest.param("stage2e-private prose without criteria", id="unrecognized"),
        pytest.param(VALID_RULES + "\n# Ignore\n- SEC-1\n", id="all-ignored"),
    ],
)
async def test_draft_rules_need_an_active_criterion(
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    postgres: PostgresHarness,
    github_requests: list[httpx.Request],
    markdown: str,
) -> None:
    parsed = parse_rules(markdown, db_instructions=())
    assert parsed.state == "valid"
    assert parsed.diagnostics == ()
    assert parsed.instructions == ()
    await assert_rules_response(
        browser,
        simulation_scope,
        postgres,
        github_requests,
        markdown,
        HTTPStatus.UNPROCESSABLE_ENTITY,
    )


@pytest.mark.parametrize(
    ("markdown", "diagnostic"),
    [
        pytest.param(
            "# stage2e-private unsupported\n" + VALID_RULES,
            "invalid_structure:1",
            id="unsupported-section",
        ),
        pytest.param(
            VALID_RULES + "\n```python\nstage2e-private",
            "unclosed_fence:0",
            id="unclosed-fence",
        ),
        pytest.param("x" * 65537, "file_too_large:0", id="file-overflow"),
        pytest.param(
            "# Security\n" + "".join(f"## SEC-{i}: x\n" for i in range(1, 3001)),
            "combined_rule_limit:0",
            id="canonical-instructions-overflow",
        ),
    ],
)
async def test_invalid_parser_state_is_safe_validation_failure(  # noqa: PLR0913, PLR0917 -- explicit HTTP fixtures and parser vector
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    postgres: PostgresHarness,
    github_requests: list[httpx.Request],
    caplog: pytest.LogCaptureFixture,
    markdown: str,
    diagnostic: str,
) -> None:
    parsed = parse_rules(markdown, db_instructions=())
    assert parsed.state == "invalid"
    assert parsed.diagnostics == (diagnostic,)
    assert len(rules_body(markdown)) < 262144  # noqa: PLR2004 -- HTTP cap must not mask parser limit
    await assert_rules_response(
        browser,
        simulation_scope,
        postgres,
        github_requests,
        markdown,
        HTTPStatus.UNPROCESSABLE_ENTITY,
        diagnostics=[EXPECTED_PARSER_DIAGNOSTICS[diagnostic]],
    )
    assert "stage2e-private" not in caplog.text
    assert diagnostic not in caplog.text


@pytest.mark.parametrize(
    ("markdown", "diagnostic"),
    [
        pytest.param(
            VALID_RULES + "\n## SEC-1: stage2e-private duplicate\nReplace.",
            "duplicate_ref:4",
            id="duplicate-ref",
        ),
        pytest.param(
            VALID_RULES
            + "\n## SEC-2: stage2e-private bad metadata\n- severity: blocker\nBad.",
            "invalid_severity:5",
            id="invalid-severity",
        ),
        pytest.param(
            VALID_RULES + "\n# Ignore\n- SEC-1?\n",
            "invalid_ignore:5",
            id="malformed-ignore",
        ),
        pytest.param(
            "# Style\n## STYLE-1: stage2e-private bad\n- severity: HIGH\n"
            + VALID_RULES,
            "invalid_severity:3",
            id="bad-criterion-before-valid",
        ),
    ],
)
async def test_valid_parser_state_with_diagnostics_is_rejected(  # noqa: PLR0913, PLR0917 -- explicit HTTP fixtures and parser vector
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    postgres: PostgresHarness,
    github_requests: list[httpx.Request],
    caplog: pytest.LogCaptureFixture,
    markdown: str,
    diagnostic: str,
) -> None:
    parsed = parse_rules(markdown, db_instructions=())
    assert parsed.state == "valid"
    assert parsed.instructions
    assert parsed.diagnostics == (diagnostic,)
    await assert_rules_response(
        browser,
        simulation_scope,
        postgres,
        github_requests,
        markdown,
        HTTPStatus.UNPROCESSABLE_ENTITY,
        diagnostics=[EXPECTED_PARSER_DIAGNOSTICS[diagnostic]],
    )
    assert "stage2e-private" not in caplog.text
    assert diagnostic not in caplog.text


RICH_RULES = (
    "# Security\n## SEC-1: Секреты\n- severity: high\nНе раскрывай секреты.\n"
    "# Performance\n## PERF-1: Запросы\nОграничивай число запросов.\n"
    "# Style\n## STYLE-1: Пример\n```markdown\n# Not a section\n"
    "## SEC-999: Only example data\n```\nПроверяй имена."
)
RICH_INSTRUCTIONS = (
    "[SEC-1] Security: Секреты\nНе раскрывай секреты.",
    "[PERF-1] Performance: Запросы\nОграничивай число запросов.",
    "[STYLE-1] Style: Пример\n```markdown\n# Not a section\n"
    "## SEC-999: Only example data\n```\nПроверяй имена.",
)


@pytest.mark.parametrize("kind", ["code", "diff"])
@pytest.mark.parametrize(
    "rich", [False, True], ids=["one-security-rule", "all-sections"]
)
async def test_valid_user_rules_retain_temporary_unavailable(  # noqa: PLR0913, PLR0917 -- explicit HTTP fixtures and request modes
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    postgres: PostgresHarness,
    github_requests: list[httpx.Request],
    kind: str,
    rich: bool,
) -> None:
    # Preservation control: valid requests already ended in 503 before Stage2e.
    markdown = RICH_RULES if rich else VALID_RULES
    parsed = parse_rules(markdown, db_instructions=())
    assert parsed.state == "valid"
    assert parsed.diagnostics == ()
    assert parsed.instructions == (
        RICH_INSTRUCTIONS
        if rich
        else ("[SEC-1] Security: Безопасность\nПроверяй входные данные.",)
    )
    assert parsed.severities == ((("SEC-1", "high"),) if rich else ())
    await assert_rules_response(
        browser,
        simulation_scope,
        postgres,
        github_requests,
        markdown,
        HTTPStatus.SERVICE_UNAVAILABLE,
        kind=kind,
    )


@pytest.mark.parametrize("kind", ["code", "diff"])
async def test_ignore_keeps_only_the_named_unexcluded_criteria(
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    postgres: PostgresHarness,
    github_requests: list[httpx.Request],
    kind: str,
) -> None:
    markdown = RICH_RULES + "\n# Ignore\n- SEC-1\n- STYLE-1\n"
    parsed = parse_rules(markdown, db_instructions=())
    assert parsed.state == "valid"
    assert parsed.diagnostics == ()
    assert parsed.instructions == (RICH_INSTRUCTIONS[1],)
    assert parsed.severities == ()
    await assert_rules_response(
        browser,
        simulation_scope,
        postgres,
        github_requests,
        markdown,
        HTTPStatus.SERVICE_UNAVAILABLE,
        kind=kind,
    )


def byte_boundary_rules(target: int, unit: str) -> str:
    # Ignored introductory prose pads the file without inflating canonical rules.
    remaining = target - len(VALID_RULES.encode()) - 1
    repeats, tail = divmod(remaining, len(unit.encode()))
    markdown = unit * repeats + " " * tail + "\n" + VALID_RULES
    assert len(markdown.encode()) == target
    return markdown


@pytest.mark.parametrize("size", [65536, 65537], ids=["at-cap", "first-rejected-byte"])
@pytest.mark.parametrize(
    "unit", ["x", "я", "😀"], ids=["ascii", "utf8-two", "utf8-four"]
)
@pytest.mark.parametrize(
    "ensure_ascii", [False, True], ids=["raw-utf8", "json-escapes"]
)
async def test_parser_file_limit_counts_decoded_utf8_bytes(  # noqa: PLR0913, PLR0917 -- explicit HTTP fixtures and byte vectors
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    postgres: PostgresHarness,
    github_requests: list[httpx.Request],
    size: int,
    unit: str,
    ensure_ascii: bool,
) -> None:
    markdown = byte_boundary_rules(size, unit)
    parsed = parse_rules(markdown, db_instructions=())
    accepted = size == 65536  # noqa: PLR2004 -- specified parser boundary
    assert parsed.state == ("valid" if accepted else "invalid")
    assert parsed.diagnostics == (() if accepted else ("file_too_large:0",))
    assert len(rules_body(markdown, ensure_ascii=ensure_ascii)) < 262144  # noqa: PLR2004 -- parser cap must be reached first
    await assert_rules_response(
        browser,
        simulation_scope,
        postgres,
        github_requests,
        markdown,
        HTTPStatus.SERVICE_UNAVAILABLE if accepted else HTTPStatus.UNPROCESSABLE_ENTITY,
        ensure_ascii=ensure_ascii,
        diagnostics=[EXPECTED_PARSER_DIAGNOSTICS["file_too_large:0"]],
    )


@pytest.mark.parametrize("total", [65536, 65537], ids=["at-cap", "first-rejected-char"])
async def test_canonical_instruction_limit_is_reachable_below_transport_caps(
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    postgres: PostgresHarness,
    github_requests: list[httpx.Request],
    total: int,
) -> None:
    base = "# Security\n" + "".join(f"## SEC-{i}: x\n" for i in range(1, 2801))
    assert sum(map(len, parse_rules(base).instructions)) == 63293  # noqa: PLR2004 -- worked canonical size
    markdown = base + "x" * (total - 63293)
    assert len(markdown.encode()) < 65536  # noqa: PLR2004 -- canonical cap reached first
    accepted = total == 65536  # noqa: PLR2004 -- canonical cap
    parsed = parse_rules(markdown, db_instructions=())
    assert parsed.state == ("valid" if accepted else "invalid")
    assert parsed.diagnostics == (() if accepted else ("combined_rule_limit:0",))
    if accepted:
        assert sum(map(len, parsed.instructions)) == total
    await assert_rules_response(
        browser,
        simulation_scope,
        postgres,
        github_requests,
        markdown,
        HTTPStatus.SERVICE_UNAVAILABLE if accepted else HTTPStatus.UNPROCESSABLE_ENTITY,
        diagnostics=[EXPECTED_PARSER_DIAGNOSTICS["combined_rule_limit:0"]],
    )


@pytest.mark.parametrize(
    ("markdown", "status"),
    [
        pytest.param("", HTTPStatus.UNPROCESSABLE_ENTITY, id="empty"),
        pytest.param(
            VALID_RULES + "\n# Ignore\n- SEC-1\n",
            HTTPStatus.UNPROCESSABLE_ENTITY,
            id="all-ignored",
        ),
        pytest.param(VALID_RULES, HTTPStatus.SERVICE_UNAVAILABLE, id="same-ref-as-db"),
    ],
)
async def test_saved_repository_rules_never_supply_playground_criteria(  # noqa: PLR0913, PLR0917 -- explicit HTTP fixtures and request
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    postgres: PostgresHarness,
    github_requests: list[httpx.Request],
    markdown: str,
    status: HTTPStatus,
) -> None:
    async with postgres.engine.begin() as connection:
        await connection.execute(
            update(models.RepositorySettings)
            .where(
                models.RepositorySettings.repository_id == simulation_scope.repository
            )
            .values(rules={"instructions": ["[SEC-1] stage2e-private DB criterion"]})
        )
    await assert_rules_response(
        browser,
        simulation_scope,
        postgres,
        github_requests,
        markdown,
        status,
    )


@pytest.fixture
def forbid_rules_parser(monkeypatch: pytest.MonkeyPatch) -> None:
    # Explicit AC-7 execution-boundary probe; never substitute a parsed result.
    def forbidden(markdown: str, db_instructions: tuple[str, ...] = ()) -> NoReturn:
        pytest.fail("Rules parser must not execute for this request")

    monkeypatch.setattr(rule_simulations, "parse_rules", forbidden)


@pytest.mark.parametrize(
    ("body", "status", "code", "message"),
    [
        pytest.param(
            b"x" * 262145,
            HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            "SIMULATION_INPUT_TOO_LARGE",
            "Simulation input is too large.",
            id="oversized-http-body",
        ),
        pytest.param(
            b'{"input":',
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "VALIDATION_FAILED",
            "Request is invalid.",
            id="invalid-json",
        ),
        pytest.param(
            VALID_SIMULATION_BODY.replace(b'"kind":"code"', b'"kind":"diff"'),
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "VALIDATION_FAILED",
            "Request is invalid.",
            id="strict-dto",
        ),
        pytest.param(
            VALID_SIMULATION_BODY.replace(b"rules-md/1", b"rules-md/2"),
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "VALIDATION_FAILED",
            "Request is invalid.",
            id="unsupported-format",
        ),
        pytest.param(
            json.dumps(
                {
                    "input": {"kind": "diff", "unified_diff": "x" * 131073},
                    "rules": {"format": "rules-md/1", "markdown": VALID_RULES},
                }
            ).encode(),
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "VALIDATION_FAILED",
            "Request is invalid.",
            id="diff-byte-cap",
        ),
        pytest.param(
            rules_body(VALID_RULES, "diff").replace(b"diff --git", b"broken --git", 1),
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "VALIDATION_FAILED",
            "Request is invalid.",
            id="malformed-diff",
        ),
    ],
)
async def test_transport_dto_and_diff_reject_before_rules_parser(  # noqa: PLR0913, PLR0917 -- explicit HTTP fixtures and response contract
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    postgres: PostgresHarness,
    github_requests: list[httpx.Request],
    forbid_rules_parser: None,
    body: bytes,
    status: HTTPStatus,
    code: str,
    message: str,
) -> None:
    await assert_simulation_response(
        browser,
        simulation_scope,
        postgres,
        github_requests,
        simulation_scope.repository,
        status,
        code,
        message,
        content=body,
    )


@pytest.mark.parametrize("gate", ["local-access", "github-admin", "csrf", "origin"])
async def test_access_rejection_precedes_rules_parser(  # noqa: PLR0913, PLR0917 -- explicit HTTP fixtures and access gate
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    postgres: PostgresHarness,
    github_requests: list[httpx.Request],
    provider: dict[str, httpx.Response],
    forbid_rules_parser: None,
    gate: str,
) -> None:
    repository = simulation_scope.repository
    headers = {}
    if gate == "local-access":
        repository = simulation_scope.foreign_repository
    elif gate == "github-admin":
        path = f"/repositories/{simulation_scope.external_id}"
        payload = provider[path].json()
        payload["permissions"]["admin"] = False
        provider[path] = httpx.Response(200, json=payload)
    elif gate == "csrf":
        headers["X-CSRF-Token"] = "wrong"
    else:
        headers["Origin"] = "https://untrusted.test"
    not_found = gate == "local-access"
    await assert_simulation_response(
        browser,
        simulation_scope,
        postgres,
        github_requests,
        repository,
        HTTPStatus.NOT_FOUND if not_found else HTTPStatus.FORBIDDEN,
        "RESOURCE_NOT_FOUND" if not_found else "ACCESS_DENIED",
        "Resource was not found." if not_found else "Access is denied.",
        github_read=gate == "github-admin",
        content=rules_body(VALID_RULES),
        headers=headers,
    )


async def test_unauthorized_request_does_not_execute_rules_parser(
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    github_requests: list[httpx.Request],
    forbid_rules_parser: None,
) -> None:
    browser.cookies.clear()
    before = list(github_requests)
    response = await browser.post(
        simulation_scope.url(simulation_scope.repository),
        headers=simulation_scope.headers,
        content=rules_body(VALID_RULES),
        follow_redirects=False,
    )
    assert response.status_code == HTTPStatus.UNAUTHORIZED
    assert response.json() == {
        "code": "AUTHENTICATION_REQUIRED",
        "message": "Authentication is required.",
        "request_id": response.headers["X-Request-Id"],
        "retryable": False,
        "retry_after": None,
    }
    assert "Set-Cookie" not in response.headers
    assert "Location" not in response.headers
    assert github_requests == before
