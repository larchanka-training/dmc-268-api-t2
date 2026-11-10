"""Origin/CSRF characterization at the real simulation HTTP boundary."""

from http import HTTPStatus

import httpx
import pytest
from test_rule_simulations_access import VALID_SIMULATION_BODY, SimulationScope

pytest_plugins = ("test_rule_simulations_access",)


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param([("X-CSRF-Token", "correct")], id="missing-origin"),
        pytest.param(
            [("Origin", "https://evil.test"), ("X-CSRF-Token", "correct")],
            id="origin-outside-allowlist",
        ),
        pytest.param([("Origin", "https://frontend.test")], id="missing-csrf"),
        pytest.param(
            [("Origin", "https://frontend.test"), ("X-CSRF-Token", "wrong")],
            id="wrong-csrf",
        ),
        pytest.param(
            [
                ("Origin", "https://frontend.test"),
                ("Origin", "https://frontend.test"),
                ("X-CSRF-Token", "correct"),
            ],
            id="duplicate-origin",
        ),
        pytest.param(
            [
                ("Origin", "https://frontend.test"),
                ("X-CSRF-Token", "correct"),
                ("X-CSRF-Token", "correct"),
            ],
            id="duplicate-csrf",
        ),
    ],
)
async def test_rule_simulation_denies_unverified_origin_or_csrf_without_side_effects(
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    github_requests: list[httpx.Request],
    headers: list[tuple[str, str]],
) -> None:
    before = await browser.get("/api/v1/me")
    assert before.status_code == HTTPStatus.OK
    identity = before.json()
    endpoint = simulation_scope.url(simulation_scope.repository)
    # Control: valid evidence reaches the existing Stage 1a fail-closed handler.
    verified = await browser.post(
        endpoint,
        headers={
            "Origin": "https://frontend.test",
            "X-CSRF-Token": identity["csrf_token"],
            "Content-Type": "application/json",
            "Idempotency-Key": "preliminary-opaque-key",
        },
        content=VALID_SIMULATION_BODY,
        follow_redirects=False,
    )
    assert verified.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert verified.json()["code"] == "SERVICE_UNAVAILABLE"
    cookies_before = dict(browser.cookies)
    github_before = list(github_requests)

    response = await browser.post(
        endpoint,
        headers=[
            (key, identity["csrf_token"] if value == "correct" else value)
            for key, value in headers
        ],
        follow_redirects=False,
    )

    assert response.status_code == HTTPStatus.FORBIDDEN
    assert response.headers["X-Request-Id"]
    assert response.json() == {
        "code": "ACCESS_DENIED",
        "message": "Access is denied.",
        "request_id": response.headers["X-Request-Id"],
        "retryable": False,
        "retry_after": None,
    }
    assert "Location" not in response.headers
    assert "Set-Cookie" not in response.headers
    assert dict(browser.cookies) == cookies_before
    after = await browser.get("/api/v1/me")
    assert after.status_code == HTTPStatus.OK
    assert after.json() == identity
    assert github_requests == github_before
