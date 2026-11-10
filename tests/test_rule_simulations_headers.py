"""Preliminary HTTP header gates at the authenticated simulation boundary."""

from collections.abc import AsyncIterator
from dataclasses import replace
from http import HTTPStatus
from typing import NoReturn

import httpx
import pytest
from test_postgres_integration import PostgresHarness
from test_rule_simulations_access import SimulationScope, assert_simulation_response

from app.api import rule_simulations

pytest_plugins = ("test_rule_simulations_access",)


@pytest.fixture
def forbid_payload_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    # Explicit order probe: no substitute DTO or parser result.
    def forbidden(*args: object, **kwargs: object) -> NoReturn:
        pytest.fail("DTO validation must not execute before the header gate")

    monkeypatch.setattr(
        rule_simulations.SimulationRequest, "model_validate_json", forbidden
    )


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param([], id="missing"),
        pytest.param([("Content-Type", "")], id="empty"),
        pytest.param([("Content-Type", "text/plain")], id="other-media-type"),
        pytest.param([("Content-Type", "application/problem+json")], id="json-suffix"),
        pytest.param(
            [
                ("Content-Type", "application/json"),
                ("content-type", "application/json"),
            ],
            id="duplicate",
        ),
        pytest.param(
            [("Content-Type", "application/json, application/json")], id="combined"
        ),
        pytest.param(
            [("Content-Type", "application/json; charset=stage3b2-private")],
            id="unsupported-charset",
        ),
        pytest.param(
            [("Content-Type", "application/json; private=stage3b2-private")],
            id="unknown-parameter",
        ),
        pytest.param(
            [("Content-Type", "application/json; charset=utf-8; charset=utf-8")],
            id="duplicate-parameter",
        ),
        pytest.param(
            [("Content-Type", "application/json; charset=utf-8; private=secret")],
            id="additional-parameter",
        ),
        pytest.param([("Content-Type", "application/json;")], id="empty-parameter"),
        pytest.param([("Content-Type", "application/json; charset")], id="no-equals"),
        pytest.param(
            [("Content-Type", "application/json; charset=")], id="empty-charset"
        ),
        pytest.param([("Content-Type", "application /json")], id="bad-media-syntax"),
        pytest.param(
            [("Content-Type", "application/json\r\n")], id="control-character"
        ),
    ],
)
async def test_invalid_content_type_is_safe_validation_failure(  # noqa: PLR0913, PLR0917 -- HTTP fixtures and raw header vector
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    postgres: PostgresHarness,
    github_requests: list[httpx.Request],
    caplog: pytest.LogCaptureFixture,
    headers: list[tuple[str, str]],
    forbid_payload_validation: None,
) -> None:
    scope = replace(
        simulation_scope,
        headers={
            name: value
            for name, value in simulation_scope.headers.items()
            if name.lower() != "content-type"
        },
    )
    await assert_simulation_response(
        browser,
        scope,
        postgres,
        github_requests,
        scope.repository,
        HTTPStatus.UNPROCESSABLE_ENTITY,
        "VALIDATION_FAILED",
        "Request is invalid.",
        headers=headers,
    )
    assert "stage3b2-private" not in caplog.text


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param([], id="missing"),
        pytest.param([("Idempotency-Key", "")], id="empty"),
        pytest.param([("Idempotency-Key", " \t ")], id="whitespace"),
        pytest.param(
            [("Idempotency-Key", "same"), ("idempotency-key", "same")],
            id="duplicate-identical",
        ),
        pytest.param(
            [("Idempotency-Key", "one"), ("Idempotency-Key", "two")],
            id="duplicate-different",
        ),
        pytest.param([("Idempotency-Key", "stage3b2-private, second")], id="combined"),
        pytest.param([("Idempotency-Key", "key,")], id="trailing-comma"),
        pytest.param([("Idempotency-Key", ",key")], id="leading-comma"),
        pytest.param([("Idempotency-Key", '"one", "two"')], id="quoted-combined"),
    ],
)
async def test_invalid_idempotency_header_is_safe_validation_failure(  # noqa: PLR0913, PLR0917 -- HTTP fixtures and raw header vector
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    postgres: PostgresHarness,
    github_requests: list[httpx.Request],
    caplog: pytest.LogCaptureFixture,
    headers: list[tuple[str, str]],
    forbid_payload_validation: None,
) -> None:
    scope = replace(
        simulation_scope,
        headers={
            name: value
            for name, value in simulation_scope.headers.items()
            if name.lower() != "idempotency-key"
        },
    )
    await assert_simulation_response(
        browser,
        scope,
        postgres,
        github_requests,
        scope.repository,
        HTTPStatus.UNPROCESSABLE_ENTITY,
        "VALIDATION_FAILED",
        "Request is invalid.",
        headers=headers,
    )
    assert "stage3b2-private" not in caplog.text


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({"Content-Type": "application/json"}, id="plain-json"),
        pytest.param({"Content-Type": "application/json; charset=utf-8"}, id="utf8"),
        pytest.param({"cOnTent-TyPe": "ApPlIcAtIoN/JsOn; ChArSeT=UtF-8"}, id="case"),
        pytest.param(
            {"Content-Type": " \tapplication/json \t; \tcharset = utf-8 \t"},
            id="surrounding-and-parameter-whitespace",
        ),
        pytest.param(
            {"Content-Type": 'application/json; charset="UTF-8"'}, id="quoted-charset"
        ),
        pytest.param({"Idempotency-Key": "not-a-uuid"}, id="opaque-key"),
        pytest.param(
            {"Idempotency-Key": "01900000-0000-7000-8000-000000000001"},
            id="old-v7-no-freshness-policy",
        ),
        pytest.param(
            {"Idempotency-Key": "ffffffff-ffff-7fff-8fff-ffffffffffff"},
            id="future-v7-no-freshness-policy",
        ),
        pytest.param(
            {"Idempotency-Key": "00000000-0000-4000-8000-000000000001"}, id="uuid-v4"
        ),
        pytest.param(
            {"IDEMPOTENCY-KEY": " \topaque key\t "}, id="nonblank-with-whitespace"
        ),
    ],
)
async def test_valid_transport_headers_retain_temporary_unavailable(
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    postgres: PostgresHarness,
    github_requests: list[httpx.Request],
    headers: dict[str, str],
) -> None:
    # Preservation controls: these inputs already reached 503 before this stage.
    await assert_simulation_response(
        browser,
        simulation_scope,
        postgres,
        github_requests,
        simulation_scope.repository,
        HTTPStatus.SERVICE_UNAVAILABLE,
        "SERVICE_UNAVAILABLE",
        "Service is unavailable.",
        headers=headers,
    )


@pytest.mark.parametrize(
    "gate", ["origin", "csrf", "local-access", "github-admin", "body"]
)
async def test_access_and_bounded_body_precede_header_validation(  # noqa: PLR0913, PLR0917 -- HTTP fixtures and protection gate
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    postgres: PostgresHarness,
    github_requests: list[httpx.Request],
    provider: dict[str, httpx.Response],
    forbid_payload_validation: None,
    gate: str,
) -> None:
    headers = {"Content-Type": "text/stage3b2-private", "Idempotency-Key": ""}
    repository = simulation_scope.repository
    if gate == "origin":
        headers["Origin"] = "https://untrusted.test"
    elif gate == "csrf":
        headers["X-CSRF-Token"] = "wrong"
    elif gate == "local-access":
        repository = simulation_scope.foreign_repository
    elif gate == "github-admin":
        path = f"/repositories/{simulation_scope.external_id}"
        payload = provider[path].json()
        payload["permissions"]["admin"] = False
        provider[path] = httpx.Response(200, json=payload)

    consumed: list[int] = []

    async def body() -> AsyncIterator[bytes]:
        assert gate == "body", "Body must not be read before access checks"
        for chunk in (b"x" * 131072, b"x" * 131072, b"x"):
            consumed.append(len(chunk))
            yield chunk
        pytest.fail("Read beyond the first oversized chunk")

    if gate == "body":
        status, code, message = (
            HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            "SIMULATION_INPUT_TOO_LARGE",
            "Simulation input is too large.",
        )
    elif gate == "local-access":
        status, code, message = (
            HTTPStatus.NOT_FOUND,
            "RESOURCE_NOT_FOUND",
            "Resource was not found.",
        )
    else:
        status, code, message = (
            HTTPStatus.FORBIDDEN,
            "ACCESS_DENIED",
            "Access is denied.",
        )
    await assert_simulation_response(
        browser,
        simulation_scope,
        postgres,
        github_requests,
        repository,
        status,
        code,
        message,
        headers=headers,
        content=body(),
        github_read=gate in {"github-admin", "body"},
    )
    assert consumed == ([131072, 131072, 1] if gate == "body" else [])
