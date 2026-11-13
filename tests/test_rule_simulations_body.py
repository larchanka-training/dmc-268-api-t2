"""Transport body limits at the real authenticated simulation HTTP boundary."""

from collections.abc import AsyncIterator, Iterator
from http import HTTPStatus

import httpx
import pytest
import sentry_sdk
from test_postgres_integration import PostgresHarness
from test_rule_simulations_access import (
    VALID_SIMULATION_BODY,
    SimulationScope,
    assert_simulation_response,
)

pytest_plugins = ("test_rule_simulations_access",)


@pytest.fixture(autouse=True)
def isolate_stream_from_sentry() -> Iterator[None]:
    # Other tests can leave Sentry's default FastAPI integration enabled: it
    # pre-reads JSON bodies and swallows the stream's read-ahead AssertionError.
    # Restore only the body-capture option; creating/binding a client also
    # mutates SDK metadata and global scope attributes.
    with pytest.MonkeyPatch.context() as patch:
        patch.setitem(sentry_sdk.get_client().options, "max_request_body_size", "never")
        yield


@pytest.mark.parametrize("content_length", [None, "1", "262144", "not-a-number"])
async def test_exact_limit_utf8_stream_is_buffered_before_json_validation(
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    postgres: PostgresHarness,
    github_requests: list[httpx.Request],
    content_length: str | None,
) -> None:
    prefix = b'{"input":{"kind":"code","path":"x.py","source":"'
    rules = (
        b'"},"rules":{"format":"rules-md/1",'
        b'"markdown":"# Security\\n## SEC-1: Boundary\\nCheck the input."}}'
    )
    # Keep source and rules within their caps; pad JSON outside string fields.
    encoded = prefix + ("я" * 32768).encode() + rules
    encoded += b" " * (262144 - len(encoded))
    assert len(encoded) == 262144  # noqa: PLR2004 -- specified UTF-8 byte boundary

    async def body() -> AsyncIterator[bytes]:
        # Split a UTF-8 code point across chunks: decode only after buffering.
        split = len(prefix) + 1
        yield encoded[:split]
        yield encoded[split : split + 70000]
        yield encoded[split + 70000 :]

    headers = {"Content-Type": "application/json"}
    if content_length is not None:
        headers["Content-Length"] = content_length
    await assert_simulation_response(
        browser,
        simulation_scope,
        postgres,
        github_requests,
        simulation_scope.repository,
        HTTPStatus.SERVICE_UNAVAILABLE,
        "SERVICE_UNAVAILABLE",
        "Service is unavailable.",
        content=body(),
        headers=headers,
    )


async def test_exactly_262144_bytes_reaches_temporary_unavailable_handler(
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    postgres: PostgresHarness,
    github_requests: list[httpx.Request],
) -> None:
    body = VALID_SIMULATION_BODY + b" " * (262144 - len(VALID_SIMULATION_BODY))
    assert len(body) == 262144  # noqa: PLR2004 -- specified byte boundary
    await assert_simulation_response(
        browser,
        simulation_scope,
        postgres,
        github_requests,
        simulation_scope.repository,
        HTTPStatus.SERVICE_UNAVAILABLE,
        "SERVICE_UNAVAILABLE",
        "Service is unavailable.",
        content=body,
        headers={"Content-Type": "application/json"},
    )


async def test_262145_bytes_is_canonical_input_too_large_without_side_effects(
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    postgres: PostgresHarness,
    github_requests: list[httpx.Request],
) -> None:
    body = b'{"source":"' + b"x" * 262131 + b'"}\n'
    assert len(body) == 262145  # noqa: PLR2004 -- specified first rejected byte
    await assert_simulation_response(
        browser,
        simulation_scope,
        postgres,
        github_requests,
        simulation_scope.repository,
        HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
        "SIMULATION_INPUT_TOO_LARGE",
        "Simulation input is too large.",
        content=body,
        headers={"Content-Type": "application/json"},
    )


@pytest.mark.parametrize("content_length", [None, "1", "262144", "not-a-number"])
async def test_actual_stream_bytes_are_bounded_without_trusting_content_length(
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    postgres: PostgresHarness,
    github_requests: list[httpx.Request],
    content_length: str | None,
) -> None:
    consumed: list[int] = []

    async def body() -> AsyncIterator[bytes]:
        # A JSON string occupies exactly 262144 UTF-8 bytes before the space.
        # Counting decoded characters or checking chunks separately misses it.
        for chunk in (b'"', ("я" * 65535).encode(), ("я" * 65536).encode(), b'"', b" "):
            consumed.append(len(chunk))
            yield chunk
        raise AssertionError("Body was read beyond the first oversized chunk")

    headers = {"Content-Type": "application/json"}
    if content_length is not None:
        headers["Content-Length"] = content_length
    await assert_simulation_response(
        browser,
        simulation_scope,
        postgres,
        github_requests,
        simulation_scope.repository,
        HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
        "SIMULATION_INPUT_TOO_LARGE",
        "Simulation input is too large.",
        content=body(),
        headers=headers,
    )
    assert consumed == [1, 131070, 131072, 1, 1]
