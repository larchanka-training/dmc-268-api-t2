from http import HTTPStatus

import httpx

from app.main import create_app


async def test_rule_simulation_requires_authoritative_browser_session() -> None:
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/repositories/00000000-0000-0000-0000-000000000001/rule-simulations",
            follow_redirects=False,
        )

    assert response.status_code == HTTPStatus.UNAUTHORIZED
    assert response.headers["X-Request-Id"]
    assert response.json() == {
        "code": "AUTHENTICATION_REQUIRED",
        "message": "Authentication is required.",
        "request_id": response.headers["X-Request-Id"],
        "retryable": False,
        "retry_after": None,
    }
    assert "Location" not in response.headers
    assert "Set-Cookie" not in response.headers
