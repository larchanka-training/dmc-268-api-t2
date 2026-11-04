"""SSE lifecycle regressions with the actual publication row and PostgreSQL."""

import asyncio
import json
from http import HTTPStatus
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from starlette.types import Message, Receive, Scope, Send
from test_publications_postgres import seed_review

from app.api.review_stream import StreamPrincipal, get_current_user
from app.db.models import Publication, RepositoryAccess, ReviewJob, User
from app.db.review_events import append_review_event
from app.main import create_app

pytest_plugins = ("test_publications_postgres",)


async def _seed_stream(
    engine: AsyncEngine,
    payload: dict[str, Any],
) -> tuple[FastAPI, UUID, UUID]:
    review_id, _ = await seed_review(engine, payload)
    user_id = uuid4()
    async with engine.begin() as connection:
        repository_id = await connection.scalar(
            select(ReviewJob.repository_id).where(ReviewJob.id == review_id)
        )
        await connection.execute(insert(User).values(id=user_id))
        await connection.execute(
            insert(RepositoryAccess).values(
                repository_id=repository_id,
                user_id=user_id,
                role="reviewer",
            )
        )
    app = create_app()
    app.state.review_event_sessions = async_sessionmaker(engine, expire_on_commit=False)
    return app, review_id, user_id


@pytest.mark.parametrize("status", ["FAILED", "SKIPPED"])
async def test_failed_analysis_with_not_ready_publication_finishes_stream(
    publication_engine: AsyncEngine,
    payload_data: dict[str, Any],
    status: str,
) -> None:
    """AC-SSE-REVIEW-01: a real NOT_READY row never keeps a non-run open."""
    app, review_id, user_id = await _seed_stream(publication_engine, payload_data)
    async with publication_engine.begin() as connection:
        await connection.execute(
            update(ReviewJob)
            .where(
                ReviewJob.id == review_id,
            )
            .values(status=status)
        )
        await connection.execute(
            update(Publication)
            .where(
                Publication.review_job_id == review_id,
            )
            .values(
                status="NOT_READY", started_at=None, deadline_at=None, finished_at=None
            )
        )

    async def active() -> bool:
        return True

    app.dependency_overrides[get_current_user] = lambda: StreamPrincipal(
        user_id, active
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await asyncio.wait_for(
            client.get(f"/api/v1/reviews/{review_id}/events/stream"),
            timeout=1,
        )

    assert response.status_code == HTTPStatus.OK
    snapshot = json.loads(response.text.split("data: ", 1)[1].split("\n", 1)[0])
    assert snapshot["status"] == status
    assert snapshot["publication_status"] == "NOT_READY"


async def test_session_expiring_during_initial_read_returns_401_before_stream(
    publication_engine: AsyncEngine,
    payload_data: dict[str, Any],
) -> None:
    """AC-SSE-REVIEW-02: initial database I/O cannot outlive session authority."""
    app, review_id, user_id = await _seed_stream(publication_engine, payload_data)
    async with publication_engine.begin() as connection:
        await connection.execute(
            update(Publication)
            .where(
                Publication.review_job_id == review_id,
            )
            .values(status="PUBLISHED", finished_at=func.now())
        )
    checks = 0

    async def expires_after_first_check() -> bool:
        nonlocal checks
        checks += 1
        return checks == 1

    app.dependency_overrides[get_current_user] = lambda: StreamPrincipal(
        user_id,
        expires_after_first_check,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(f"/api/v1/reviews/{review_id}/events/stream")

    assert response.status_code == HTTPStatus.UNAUTHORIZED
    assert response.json()["code"] == "AUTHENTICATION_REQUIRED"
    assert "review.snapshot" not in response.text


async def test_session_revoked_after_headers_prevents_first_data_frame(
    publication_engine: AsyncEngine,
    payload_data: dict[str, Any],
) -> None:
    """AC-SSE-REVIEW-03: header delivery cannot authorize a later data yield."""
    app, review_id, user_id = await _seed_stream(publication_engine, payload_data)
    async with publication_engine.begin() as connection:
        await connection.execute(
            update(Publication)
            .where(
                Publication.review_job_id == review_id,
            )
            .values(status="PUBLISHED", finished_at=func.now())
        )
    active_session = True

    async def active() -> bool:
        return active_session

    app.dependency_overrides[get_current_user] = lambda: StreamPrincipal(
        user_id, active
    )

    async def revoke_at_headers(scope: Scope, receive: Receive, send: Send) -> None:
        async def checked_send(message: Message) -> None:
            nonlocal active_session
            if message["type"] == "http.response.start":
                active_session = False
            await send(message)

        await app(scope, receive, checked_send)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=revoke_at_headers),
        base_url="http://test",
    ) as client:
        response = await client.get(f"/api/v1/reviews/{review_id}/events/stream")

    assert active_session is False
    assert response.status_code == HTTPStatus.OK
    assert response.content == b""


async def test_session_expiring_during_poll_prevents_changed_snapshot_delivery(
    publication_engine: AsyncEngine,
    payload_data: dict[str, Any],
) -> None:
    """AC-SSE-REVIEW-04: every data snapshot needs authority after its DB read."""
    app, review_id, user_id = await _seed_stream(publication_engine, payload_data)
    sessions = async_sessionmaker(publication_engine, expire_on_commit=False)
    async with sessions.begin() as session:
        await session.execute(
            update(ReviewJob)
            .where(
                ReviewJob.id == review_id,
            )
            .values(status="LLM_PROCESSING", finished_at=None)
        )
        await session.execute(
            update(Publication)
            .where(
                Publication.review_job_id == review_id,
            )
            .values(status="NOT_READY", started_at=None, deadline_at=None)
        )
    active_session = True
    first_snapshot_sent = False
    revoke_scheduled = False

    def revoke() -> None:
        nonlocal active_session
        active_session = False

    async def active() -> bool:
        nonlocal revoke_scheduled
        if first_snapshot_sent and not revoke_scheduled:
            revoke_scheduled = True
            asyncio.get_running_loop().call_soon(revoke)
        return active_session

    app.dependency_overrides[get_current_user] = lambda: StreamPrincipal(
        user_id, active
    )

    async def commit_after_first_frame(
        scope: Scope, receive: Receive, send: Send
    ) -> None:
        async def checked_send(message: Message) -> None:
            nonlocal first_snapshot_sent
            if message.get("body") and not first_snapshot_sent:
                async with sessions.begin() as session:
                    await session.execute(
                        update(ReviewJob)
                        .where(
                            ReviewJob.id == review_id,
                        )
                        .values(status="FAILED", finished_at=func.now())
                    )
                    await append_review_event(
                        session,
                        review_id,
                        event_type="analysis.failed",
                        publication_status="NOT_READY",
                    )
                first_snapshot_sent = True
            await send(message)

        await app(scope, receive, checked_send)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=commit_after_first_frame),
        base_url="http://test",
    ) as client:
        response = await client.get(f"/api/v1/reviews/{review_id}/events/stream")

    assert active_session is False
    assert response.text.count("event: review.snapshot") == 1
    assert '"status": "LLM_PROCESSING"' in response.text
    assert '"status": "FAILED"' not in response.text
