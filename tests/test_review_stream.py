"""SSE through the public HTTP/ASGI boundary."""

import uuid
from http import HTTPStatus
from pathlib import Path

import httpx
import yaml
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.review_stream import StreamPrincipal, get_current_user
from app.main import create_app


def test_review_stream_fails_closed_until_session_auth_is_wired() -> None:
    with TestClient(create_app()) as client:
        response = client.get(f"/api/v1/reviews/{uuid.uuid4()}/events/stream")

    assert response.status_code == HTTPStatus.UNAUTHORIZED
    assert response.json()["code"] == "AUTHENTICATION_REQUIRED"


def test_expired_session_is_rejected_before_reading_review_data() -> None:

    async def inactive() -> bool:
        return False

    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: StreamPrincipal(
        uuid.uuid4(), inactive
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(f"/api/v1/reviews/{uuid.uuid4()}/events/stream")
    assert response.status_code == HTTPStatus.UNAUTHORIZED
    assert response.json()["code"] == "AUTHENTICATION_REQUIRED"


def test_stream_error_returns_the_correlated_request_id() -> None:
    with TestClient(create_app()) as client:
        response = client.get(
            f"/api/v1/reviews/{uuid.uuid4()}/events/stream",
            headers={"X-Request-Id": "sse-auth-401"},
        )
    assert response.json()["request_id"] == "sse-auth-401"


def test_openapi_adds_sse_without_replacing_json_event_history() -> None:
    contract = yaml.safe_load((Path(__file__).parents[1] / "openapi.yaml").read_text())
    history = contract["paths"]["/api/v1/reviews/{id}/events"]["get"]
    stream = contract["paths"]["/api/v1/reviews/{id}/events/stream"]["get"]
    assert "application/json" in history["responses"]["200"]["content"]
    assert "text/event-stream" in stream["responses"]["200"]["content"]
    assert stream["security"] == [{"cookieAuth": []}]
    assert stream["x-sse-events"]["review.event"] == {
        "$ref": "#/components/schemas/ReviewStreamEvent"
    }


async def test_database_outage_returns_a_safe_retryable_service_error() -> None:
    async def active() -> bool:
        return True

    app = create_app()
    engine = create_async_engine(
        "postgresql+asyncpg://unused:unused@127.0.0.1:1/unavailable"
    )
    app.state.review_event_sessions = async_sessionmaker(engine)
    app.dependency_overrides[get_current_user] = lambda: StreamPrincipal(
        uuid.uuid4(), active
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            response = await client.get(f"/api/v1/reviews/{uuid.uuid4()}/events/stream")
        assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
        assert response.json()["code"] == "SERVICE_UNAVAILABLE"
        assert response.json()["retryable"] is True
        assert "unused" not in response.text
    finally:
        await engine.dispose()
