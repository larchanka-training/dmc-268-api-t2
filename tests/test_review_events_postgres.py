"""Durable event stream behavior against migrated PostgreSQL."""

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from http import HTTPStatus

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import delete, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from starlette.types import Message

from app.api import review_stream
from app.api.review_stream import StreamPrincipal, get_current_user
from app.db.models import (
    ChangeRequest,
    Repository,
    RepositoryAccess,
    RepositorySettings,
    ReviewEvent,
    ReviewJob,
    User,
)
from app.db.review_events import SqlAlchemyReviewEvents, append_review_event
from app.main import create_app

_URL = os.environ.get("DMC268_TEST_DATABASE_URL")
if not _URL:
    pytest.skip("set DMC268_TEST_DATABASE_URL", allow_module_level=True)


@dataclass(frozen=True)
class EventScope:
    sessions: async_sessionmaker[AsyncSession]
    review_id: uuid.UUID
    user_id: uuid.UUID
    repository_id: uuid.UUID


@pytest_asyncio.fixture
async def event_scope() -> AsyncIterator[EventScope]:
    assert _URL is not None
    engine = create_async_engine(_URL, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    repository_id, settings_id, change_id, review_id, user_id = (
        uuid.uuid4() for _ in range(5)
    )
    now = datetime.now(UTC)
    async with sessions.begin() as session:
        await session.execute(insert(User).values(id=user_id))
        await session.execute(
            insert(Repository).values(
                id=repository_id,
                external_id=repository_id.hex,
                provider_name="github",
                repo_name="events",
                owner="test",
            )
        )
        await session.execute(
            insert(RepositoryAccess).values(
                repository_id=repository_id,
                user_id=user_id,
                role="reviewer",
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
                title="events",
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
                status="QUEUED",
                queue_deadline_at=now + timedelta(minutes=15),
            )
        )
    try:
        yield EventScope(sessions, review_id, user_id, repository_id)
    finally:
        async with sessions.begin() as session:
            for model, criterion in (
                (ReviewEvent, ReviewEvent.review_job_id == review_id),
                (ReviewJob, ReviewJob.id == review_id),
                (ChangeRequest, ChangeRequest.id == change_id),
                (RepositoryAccess, RepositoryAccess.repository_id == repository_id),
                (RepositorySettings, RepositorySettings.id == settings_id),
                (Repository, Repository.id == repository_id),
                (User, User.id == user_id),
            ):
                await session.execute(delete(model).where(criterion))
        await engine.dispose()


@pytest.fixture
def authorized_app(event_scope: EventScope) -> FastAPI:

    async def active() -> bool:
        return True

    app = create_app()
    app.state.review_event_sessions = event_scope.sessions
    app.dependency_overrides[get_current_user] = lambda: StreamPrincipal(
        event_scope.user_id,
        active,
    )
    return app


async def _collect_stream(
    app: FastAPI,
    review_id: uuid.UUID,
    on_body: Callable[[bytes], Awaitable[bool]],
) -> list[bytes]:
    disconnected = asyncio.Event()
    initial_request = True
    bodies: list[bytes] = []

    async def receive() -> Message:
        nonlocal initial_request
        if initial_request:
            initial_request = False
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        if chunk := message.get("body", b""):
            bodies.append(chunk)
            if await on_body(chunk):
                disconnected.set()

    path = f"/api/v1/reviews/{review_id}/events/stream"
    await asyncio.wait_for(
        app(
            {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": path,
                "raw_path": path.encode(),
                "query_string": b"",
                "root_path": "",
                "headers": [],
                "client": ("127.0.0.1", 1),
                "server": ("test", 80),
            },
            receive,
            send,
        ),
        timeout=4,
    )
    return bodies


async def test_appended_event_can_be_replayed_with_historical_publication_status(
    event_scope: EventScope,
) -> None:

    async with event_scope.sessions.begin() as session:
        event = await append_review_event(
            session,
            event_scope.review_id,
            event_type="analysis.queued",
            publication_status="NOT_READY",
        )
    page = await SqlAlchemyReviewEvents(event_scope.sessions).read(
        event_scope.review_id,
        event_scope.user_id,
        after=0,
    )

    assert event.sequence == 1
    assert [
        (row.sequence, row.status, row.publication_status) for row in page.events
    ] == [(1, "QUEUED", "NOT_READY")]
    assert page.snapshot.sequence == 1
    assert page.snapshot.status == "QUEUED"


async def test_revoked_repository_access_cannot_read_events(
    event_scope: EventScope,
) -> None:

    async with event_scope.sessions.begin() as session:
        await session.execute(
            delete(RepositoryAccess).where(
                RepositoryAccess.user_id == event_scope.user_id,
            )
        )
    with pytest.raises(PermissionError):
        await SqlAlchemyReviewEvents(event_scope.sessions).read(
            event_scope.review_id,
            event_scope.user_id,
            after=0,
        )


async def test_authenticated_stream_starts_from_authoritative_terminal_snapshot(
    event_scope: EventScope,
) -> None:

    async with event_scope.sessions.begin() as session:
        await session.execute(
            update(ReviewJob)
            .where(ReviewJob.id == event_scope.review_id)
            .values(status="FAILED", finished_at=datetime.now(UTC))
        )

    async def active() -> bool:
        return True

    app = create_app()
    app.state.review_event_sessions = event_scope.sessions
    app.dependency_overrides[get_current_user] = lambda: StreamPrincipal(
        event_scope.user_id,
        active,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            f"/api/v1/reviews/{event_scope.review_id}/events/stream"
        )

    assert response.status_code == HTTPStatus.OK
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-accel-buffering"] == "no"
    data = json.loads(response.text.split("data: ", 1)[1].split("\n", 1)[0])
    assert data == {
        "schema_version": 1,
        "review_id": str(event_scope.review_id),
        "sequence": 0,
        "status": "FAILED",
        "publication_status": "SKIPPED",
    }


async def test_reconnect_replays_only_committed_events_after_last_event_id(
    event_scope: EventScope,
) -> None:

    for status in ("QUEUED", "FETCHING_DIFF", "FAILED"):
        async with event_scope.sessions.begin() as session:
            await session.execute(
                update(ReviewJob)
                .where(ReviewJob.id == event_scope.review_id)
                .values(
                    status=status,
                    finished_at=datetime.now(UTC) if status == "FAILED" else None,
                )
            )
            await append_review_event(
                session,
                event_scope.review_id,
                event_type="analysis.changed",
                publication_status="SKIPPED" if status == "FAILED" else "NOT_READY",
            )

    async def active() -> bool:
        return True

    app = create_app()
    app.state.review_event_sessions = event_scope.sessions
    app.dependency_overrides[get_current_user] = lambda: StreamPrincipal(
        event_scope.user_id,
        active,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            f"/api/v1/reviews/{event_scope.review_id}/events/stream",
            headers={"Last-Event-ID": f"v1:{event_scope.review_id}:1"},
        )
    messages = [
        json.loads(part.split("data: ", 1)[1])
        for part in response.text.strip().split("\n\n")
    ]
    assert [row["sequence"] for row in messages] == [3, 2, 3]
    assert [row["publication_status"] for row in messages] == [
        "SKIPPED",
        "NOT_READY",
        "SKIPPED",
    ]
    assert f"id: v1:{event_scope.review_id}:2\n" in response.text


async def test_malformed_last_event_id_is_a_safe_validation_error(
    event_scope: EventScope,
    authorized_app: FastAPI,
) -> None:

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=authorized_app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.get(
            f"/api/v1/reviews/{event_scope.review_id}/events/stream",
            headers={"Last-Event-ID": "invalid-cursor"},
        )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()["code"] == "VALIDATION_FAILED"


async def test_live_stream_delivers_a_newly_committed_analysis_transition(
    event_scope: EventScope,
    authorized_app: FastAPI,
) -> None:

    disconnected = asyncio.Event()
    bodies: list[bytes] = []
    initial_request = True

    async def receive() -> Message:
        nonlocal initial_request
        if initial_request:
            initial_request = False
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        chunk = message.get("body", b"")
        if chunk:
            bodies.append(chunk)
        if b"event: review.snapshot" in chunk and len(bodies) == 1:
            async with event_scope.sessions.begin() as session:
                await session.execute(
                    update(ReviewJob)
                    .where(ReviewJob.id == event_scope.review_id)
                    .values(status="FETCHING_DIFF")
                )
                await append_review_event(
                    session,
                    event_scope.review_id,
                    event_type="analysis.changed",
                    publication_status="NOT_READY",
                )
        if b"event: review.event" in chunk:
            disconnected.set()

    path = f"/api/v1/reviews/{event_scope.review_id}/events/stream"
    await asyncio.wait_for(
        authorized_app(
            {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": path,
                "raw_path": path.encode(),
                "query_string": b"",
                "root_path": "",
                "headers": [],
                "client": ("127.0.0.1", 1),
                "server": ("test", 80),
            },
            receive,
            send,
        ),
        timeout=4,
    )
    assert b"event: review.event" in b"".join(bodies)
    assert b'"status": "FETCHING_DIFF"' in b"".join(bodies)


async def test_session_revoked_midstream_prevents_later_event_delivery(
    event_scope: EventScope,
    authorized_app: FastAPI,
) -> None:

    active_session = True

    async def active() -> bool:
        return active_session

    authorized_app.dependency_overrides[get_current_user] = lambda: StreamPrincipal(
        event_scope.user_id,
        active,
    )

    async def revoke_then_commit(chunk: bytes) -> bool:
        nonlocal active_session
        if not active_session:
            return True
        active_session = False
        async with event_scope.sessions.begin() as session:
            await session.execute(
                update(ReviewJob)
                .where(
                    ReviewJob.id == event_scope.review_id,
                )
                .values(status="FETCHING_DIFF")
            )
            await append_review_event(
                session,
                event_scope.review_id,
                event_type="analysis.changed",
                publication_status="NOT_READY",
            )
        return False

    bodies = await _collect_stream(
        authorized_app, event_scope.review_id, revoke_then_commit
    )
    assert len(bodies) == 1
    assert b'"status": "QUEUED"' in bodies[0]


async def test_inaccessible_review_returns_safe_not_found_before_streaming(
    event_scope: EventScope,
    authorized_app: FastAPI,
) -> None:

    async with event_scope.sessions.begin() as session:
        await session.execute(
            delete(RepositoryAccess).where(
                RepositoryAccess.user_id == event_scope.user_id,
            )
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=authorized_app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.get(
            f"/api/v1/reviews/{event_scope.review_id}/events/stream",
        )
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json()["code"] == "RESOURCE_NOT_FOUND"


async def test_replay_reports_a_gap_in_retained_history(
    event_scope: EventScope,
) -> None:

    async with event_scope.sessions.begin() as session:
        for _ in range(3):
            await append_review_event(
                session,
                event_scope.review_id,
                event_type="analysis.changed",
                publication_status="NOT_READY",
            )
        await session.execute(
            delete(ReviewEvent).where(
                ReviewEvent.review_job_id == event_scope.review_id,
                ReviewEvent.sequence == 2,  # noqa: PLR2004 - intentional missing middle event
            )
        )
    with pytest.raises(LookupError, match="replay"):
        await SqlAlchemyReviewEvents(event_scope.sessions).read(
            event_scope.review_id,
            event_scope.user_id,
            after=1,
        )


async def test_unavailable_replay_cursor_returns_conflict_requiring_resync(
    event_scope: EventScope,
    authorized_app: FastAPI,
) -> None:

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=authorized_app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.get(
            f"/api/v1/reviews/{event_scope.review_id}/events/stream",
            headers={"Last-Event-ID": f"v1:{event_scope.review_id}:100"},
        )
    assert response.status_code == HTTPStatus.CONFLICT
    assert response.json()["code"] == "EVENT_REPLAY_UNAVAILABLE"


async def test_idle_stream_sends_heartbeat_without_creating_a_domain_event(
    event_scope: EventScope,
    authorized_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    monkeypatch.setattr(review_stream, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(review_stream, "HEARTBEAT_SECONDS", 0.02, raising=False)

    async def stop_at_heartbeat(chunk: bytes) -> bool:
        return chunk.startswith(b": heartbeat")

    bodies = await _collect_stream(
        authorized_app, event_scope.review_id, stop_at_heartbeat
    )
    assert any(chunk == b": heartbeat\n\n" for chunk in bodies)
    page = await SqlAlchemyReviewEvents(event_scope.sessions).read(
        event_scope.review_id,
        event_scope.user_id,
        after=0,
    )
    assert page.events == ()
    assert page.snapshot.sequence == 0


async def test_repository_access_revocation_closes_an_open_stream_cleanly(
    event_scope: EventScope,
    authorized_app: FastAPI,
) -> None:
    async def revoke(chunk: bytes) -> bool:
        async with event_scope.sessions.begin() as session:
            await session.execute(
                delete(RepositoryAccess).where(
                    RepositoryAccess.user_id == event_scope.user_id,
                )
            )
        return False

    bodies = await _collect_stream(authorized_app, event_scope.review_id, revoke)
    assert len(bodies) == 1


async def test_unknown_legacy_reason_code_is_not_exposed_in_public_events(
    event_scope: EventScope,
) -> None:

    async with event_scope.sessions.begin() as session:
        await append_review_event(
            session,
            event_scope.review_id,
            event_type="analysis.changed",
            publication_status="NOT_READY",
        )
        await session.execute(
            update(ReviewEvent)
            .where(
                ReviewEvent.review_job_id == event_scope.review_id,
            )
            .values(reason_code="legacy-provider-error:secret-value")
        )
    page = await SqlAlchemyReviewEvents(event_scope.sessions).read(
        event_scope.review_id,
        event_scope.user_id,
        after=0,
    )
    assert page.events[0].reason_code is None


async def test_stream_event_timestamp_uses_rfc3339_wire_format(
    event_scope: EventScope,
    authorized_app: FastAPI,
) -> None:

    async with event_scope.sessions.begin() as session:
        await session.execute(
            update(ReviewJob)
            .where(
                ReviewJob.id == event_scope.review_id,
            )
            .values(status="FAILED", finished_at=datetime.now(UTC))
        )
        await append_review_event(
            session,
            event_scope.review_id,
            event_type="analysis.failed",
            publication_status="SKIPPED",
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=authorized_app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            f"/api/v1/reviews/{event_scope.review_id}/events/stream",
            headers={"Last-Event-ID": f"v1:{event_scope.review_id}:0"},
        )
    payload = json.loads(response.text.strip().split("\n\n")[-1].split("data: ", 1)[1])
    assert "T" in payload["occurred_at"]


async def test_event_append_captures_current_status_even_with_cached_orm_job(
    event_scope: EventScope,
) -> None:
    async with event_scope.sessions.begin() as session:
        cached = await session.get(ReviewJob, event_scope.review_id)
        assert cached is not None
        async with event_scope.sessions.begin() as writer:
            await writer.execute(
                update(ReviewJob)
                .where(
                    ReviewJob.id == event_scope.review_id,
                )
                .values(status="FETCHING_DIFF")
            )
        event = await append_review_event(
            session,
            event_scope.review_id,
            event_type="analysis.changed",
            publication_status="NOT_READY",
        )
    assert event.status == "FETCHING_DIFF"


async def test_event_writer_preserves_internal_phase_without_public_exposure(
    event_scope: EventScope,
) -> None:
    async with event_scope.sessions.begin() as session:
        await session.execute(
            update(ReviewJob)
            .where(
                ReviewJob.id == event_scope.review_id,
            )
            .values(status="LLM_PROCESSING")
        )
        event = await append_review_event(
            session,
            event_scope.review_id,
            event_type="FAILURE",
            publication_status="NOT_READY",
            phase="LLM_PROCESSING",
        )
    async with event_scope.sessions() as session:
        audit_phase = await session.scalar(
            select(ReviewEvent.phase).where(
                ReviewEvent.id == event.id,
            )
        )
    assert audit_phase == "LLM_PROCESSING"
    page = await SqlAlchemyReviewEvents(event_scope.sessions).read(
        event_scope.review_id,
        event_scope.user_id,
        after=0,
    )
    assert "phase" not in asdict(page.events[0])
