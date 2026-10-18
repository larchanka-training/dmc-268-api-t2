"""Real browser session authority at the public SSE boundary."""

import asyncio
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from http import HTTPStatus
from typing import Any
from uuid import UUID, uuid4

import httpx
import jwt
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import delete, event, func, insert, text, update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from starlette.types import Message, Receive, Scope, Send

from app.core.config import Settings, get_settings
from app.db.base import Base
from app.db.models import (
    AuthSession,
    ChangeRequest,
    GithubIdentity,
    Repository,
    RepositoryAccess,
    RepositorySettings,
    ReviewJob,
    User,
)
from app.db.review_events import append_review_event
from app.main import create_app

_URL = os.environ.get("DMC268_TEST_DATABASE_URL")
if not _URL:
    pytest.skip("set DMC268_TEST_DATABASE_URL", allow_module_level=True)


@dataclass(frozen=True)
class StreamAuthScope:
    app: FastAPI
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    review_id: UUID
    session_id: UUID
    user_id: UUID
    token: str


@pytest_asyncio.fixture
async def stream_auth() -> AsyncIterator[StreamAuthScope]:
    assert _URL is not None
    schema = f"stream_auth_{uuid4().hex}"
    admin = create_async_engine(_URL, poolclass=NullPool)
    async with admin.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        _URL,
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.25,
        connect_args={"server_settings": {"search_path": schema}},
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    user_id, session_id, repository_id, settings_id, change_id, review_id = (
        uuid4() for _ in range(6)
    )
    now = datetime.now(UTC)
    settings = Settings.model_validate(
        {"auth": {"jwt_secret": "stream-test-signing-key-at-least-32-bytes"}}
    )
    token = jwt.encode(
        {
            "sub": str(user_id),
            "sid": str(session_id),
            "iat": now,
            "exp": now + timedelta(minutes=5),
            "iss": "dmc268",
            "aud": "dmc268-browser",
        },
        settings.auth.jwt_secret.get_secret_value(),
        algorithm="HS256",
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions.begin() as session:
            await session.execute(insert(User).values(id=user_id))
            await session.execute(
                insert(GithubIdentity).values(
                    user_id=user_id,
                    external_id="12345",
                    login="stream-user",
                    encrypted_token="opaque-unused-token",
                )
            )
            await session.execute(
                insert(AuthSession).values(
                    id=session_id,
                    user_id=user_id,
                    csrf_token="unused-for-read-only-stream",
                    expires_at=now + timedelta(minutes=5),
                )
            )
            await session.execute(
                insert(Repository).values(
                    id=repository_id,
                    external_id="100",
                    provider_name="github",
                    repo_name="stream-auth",
                    owner="test",
                )
            )
            await session.execute(
                insert(RepositoryAccess).values(
                    repository_id=repository_id, user_id=user_id, role="reviewer"
                )
            )
            await session.execute(
                insert(RepositorySettings).values(
                    id=settings_id,
                    repository_id=repository_id,
                    rules={"instructions": []},
                    ignores={"globs": []},
                    max_starts_per_hour=10,
                    max_active_jobs=4,
                    output_language="ru",
                    surrounding_lines=5,
                    rules_digest="rules",
                )
            )
            await session.execute(
                insert(ChangeRequest).values(
                    id=change_id,
                    repository_id=repository_id,
                    external_number="42",
                    source_ref="feature",
                    base_ref="main",
                    head_sha="a" * 40,
                    base_sha="b" * 40,
                    title="stream-auth",
                    description="",
                    current_status="open",
                )
            )
            await session.execute(
                insert(ReviewJob).values(
                    id=review_id,
                    repository_id=repository_id,
                    change_request_id=change_id,
                    repository_settings_id=settings_id,
                    config_digest="config",
                    rules_digest="rules",
                    trigger_type="manual",
                    requested_head_sha="a" * 40,
                    model_ref="test-model",
                    prompt_version="v1",
                    prompt_digest="prompt",
                    status="FAILED",
                    queue_deadline_at=now + timedelta(minutes=15),
                    finished_at=now,
                )
            )
        app = create_app()
        app.state.review_event_sessions = sessions
        app.dependency_overrides[get_settings] = lambda: settings
        yield StreamAuthScope(
            app, engine, sessions, review_id, session_id, user_id, token
        )
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


async def test_active_cookie_opens_stream_without_holding_database_session(
    stream_auth: StreamAuthScope,
) -> None:
    """AC-SSE-AUTH-01: real session authority shares a one-connection pool safely."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stream_auth.app),
        base_url="https://test",
        cookies={"dmc_session": stream_auth.token},
    ) as client:
        response = await asyncio.wait_for(
            client.get(f"/api/v1/reviews/{stream_auth.review_id}/events/stream"),
            timeout=3,
        )

    assert response.status_code == HTTPStatus.OK
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text.count("event: review.snapshot") == 1
    assert f'"review_id": "{stream_auth.review_id}"' in response.text
    assert '"status": "FAILED"' in response.text
    assert stream_auth.token not in response.text
    assert "csrf_token" not in response.text


async def test_auth_database_outage_before_headers_remains_retryable_503(
    stream_auth: StreamAuthScope,
) -> None:
    """AC-SSE-AUTH-02: rechecking authority never calls an outage a logout."""
    reads = 0

    def fail_second_auth_read(*args: Any) -> None:
        nonlocal reads
        if "FROM auth_session" in args[2]:
            reads += 1
            if reads == 2:  # noqa: PLR2004
                raise OSError("synthetic-private-database-diagnostic")

    event.listen(
        stream_auth.engine.sync_engine, "before_cursor_execute", fail_second_auth_read
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=stream_auth.app),
            base_url="https://test",
            cookies={"dmc_session": stream_auth.token},
        ) as client:
            response = await client.get(
                f"/api/v1/reviews/{stream_auth.review_id}/events/stream"
            )
    finally:
        event.remove(
            stream_auth.engine.sync_engine,
            "before_cursor_execute",
            fail_second_auth_read,
        )

    assert reads == 2  # noqa: PLR2004
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json()["code"] == "SERVICE_UNAVAILABLE"
    assert response.json()["retryable"] is True
    assert "synthetic-private" not in response.text
    assert "review.snapshot" not in response.text


async def test_auth_database_outage_after_first_frame_closes_stream_cleanly(
    stream_auth: StreamAuthScope,
) -> None:
    """AC-SSE-AUTH-03: authority outages cannot leak or crash an open stream."""
    async with stream_auth.sessions.begin() as session:
        await session.execute(
            update(ReviewJob)
            .where(ReviewJob.id == stream_auth.review_id)
            .values(status="QUEUED", finished_at=None)
        )
    first_frame_sent = False

    def fail_auth_after_frame(*args: Any) -> None:
        if first_frame_sent and "FROM auth_session" in args[2]:
            raise OSError("synthetic-private-runtime-diagnostic")

    async def fail_after_first_frame(
        scope: Scope, receive: Receive, send: Send
    ) -> None:
        async def checked_send(message: Message) -> None:
            nonlocal first_frame_sent
            if message.get("body"):
                first_frame_sent = True
            await send(message)

        await stream_auth.app(scope, receive, checked_send)

    event.listen(
        stream_auth.engine.sync_engine, "before_cursor_execute", fail_auth_after_frame
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=fail_after_first_frame),
            base_url="https://test",
            cookies={"dmc_session": stream_auth.token},
        ) as client:
            response = await asyncio.wait_for(
                client.get(f"/api/v1/reviews/{stream_auth.review_id}/events/stream"),
                timeout=4,
            )
    finally:
        event.remove(
            stream_auth.engine.sync_engine,
            "before_cursor_execute",
            fail_auth_after_frame,
        )

    assert first_frame_sent
    assert response.status_code == HTTPStatus.OK
    assert response.text.count("event: review.snapshot") == 1
    assert "synthetic-private" not in response.text


@pytest.mark.parametrize(
    "state", ["revoked", "expired_db", "identity_removed", "expired_jwt"]
)
async def test_live_cookie_authority_is_rechecked_before_new_data(
    stream_auth: StreamAuthScope,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    """Regression coverage of the delegated browser verifier during live polls."""
    async with stream_auth.sessions.begin() as session:
        await session.execute(
            update(ReviewJob)
            .where(ReviewJob.id == stream_auth.review_id)
            .values(status="QUEUED", finished_at=None)
        )
    first_frame_sent = False

    if state == "expired_jwt":
        # Only the external JWT clock moves; PostgreSQL's session remains active.
        class JwtClock:
            @staticmethod
            def now(tz: tzinfo | None = None) -> datetime:
                return datetime.now(tz) + timedelta(
                    minutes=10 if first_frame_sent else 0
                )

        monkeypatch.setattr(jwt.api_jwt, "datetime", JwtClock)

    async def lose_authority_after_frame(
        scope: Scope, receive: Receive, send: Send
    ) -> None:
        async def checked_send(message: Message) -> None:
            nonlocal first_frame_sent
            if message.get("body") and not first_frame_sent:
                async with stream_auth.sessions.begin() as session:
                    if state == "revoked":
                        await session.execute(
                            update(AuthSession)
                            .where(AuthSession.id == stream_auth.session_id)
                            .values(revoked_at=func.clock_timestamp())
                        )
                    elif state == "expired_db":
                        await session.execute(
                            update(AuthSession)
                            .where(AuthSession.id == stream_auth.session_id)
                            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
                        )
                    elif state == "identity_removed":
                        await session.execute(
                            delete(GithubIdentity).where(
                                GithubIdentity.user_id == stream_auth.user_id
                            )
                        )
                    await session.execute(
                        update(ReviewJob)
                        .where(ReviewJob.id == stream_auth.review_id)
                        .values(status="FAILED", finished_at=func.clock_timestamp())
                    )
                    await append_review_event(
                        session,
                        stream_auth.review_id,
                        event_type="analysis.failed",
                        publication_status="SKIPPED",
                    )
                first_frame_sent = True
            await send(message)

        await stream_auth.app(scope, receive, checked_send)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=lose_authority_after_frame),
        base_url="https://test",
        cookies={"dmc_session": stream_auth.token},
    ) as client:
        response = await asyncio.wait_for(
            client.get(f"/api/v1/reviews/{stream_auth.review_id}/events/stream"),
            timeout=4,
        )

    assert first_frame_sent
    assert response.status_code == HTTPStatus.OK
    assert response.text.count("event: review.snapshot") == 1
    assert '"status": "QUEUED"' in response.text
    assert '"status": "FAILED"' not in response.text
    assert "event: review.event" not in response.text
