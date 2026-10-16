"""Review SSE entry point; session validation is supplied by auth #29."""

import asyncio
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import asdict, dataclass
from time import monotonic
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.encoders import jsonable_encoder
from sqlalchemy.exc import SQLAlchemyError
from starlette.responses import JSONResponse, StreamingResponse
from structlog.contextvars import get_contextvars

from app.application.review_events import (
    EventReplayUnavailableError,
    ReviewAccessDeniedError,
)
from app.db.review_events import SqlAlchemyReviewEvents
from app.db.session import async_session_factory

router = APIRouter()
POLL_SECONDS = 1.0
HEARTBEAT_SECONDS = 15.0
_TERMINAL = {"COMPLETED", "PARTIAL", "FAILED", "SKIPPED"}


@dataclass(frozen=True)
class StreamPrincipal:
    user_id: UUID
    revalidate: Callable[[], Awaitable[bool]]


async def get_current_user(request: Request) -> StreamPrincipal | None:
    """Fail closed until #29 wires its active DB session dependency here."""
    return None


def get_review_events(request: Request) -> SqlAlchemyReviewEvents:
    sessions = getattr(
        request.app.state, "review_event_sessions", async_session_factory
    )
    return SqlAlchemyReviewEvents(sessions)


def _error(
    request: Request, status: int, code: str, message: str, *, retryable: bool = False
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "code": code,
            "message": message,
            "request_id": get_contextvars().get("request_id", ""),
            "retryable": retryable,
            "retry_after": 30 if retryable else None,
        },
    )


@router.get("/api/v1/reviews/{review_id}/events/stream", response_model=None)
async def review_stream(  # noqa: PLR0911 - explicit pre-header failure responses
    review_id: UUID,
    request: Request,
    principal: Annotated[StreamPrincipal | None, Depends(get_current_user)],
    events: Annotated[SqlAlchemyReviewEvents, Depends(get_review_events)],
) -> JSONResponse | StreamingResponse:
    if principal is not None and await principal.revalidate():
        last_event_id = request.headers.get("last-event-id")
        if last_event_id is not None and not re.fullmatch(
            f"v1:{review_id}:[0-9]{{1,19}}", last_event_id
        ):
            return _error(request, 422, "VALIDATION_FAILED", "Invalid event cursor.")
        after = int(last_event_id.rsplit(":", 1)[1]) if last_event_id else 0
        try:
            page = await events.read(
                review_id,
                principal.user_id,
                after=after if last_event_id else None,
            )
        except ReviewAccessDeniedError:
            return _error(request, 404, "RESOURCE_NOT_FOUND", "Review is unavailable.")
        except EventReplayUnavailableError:
            return _error(
                request,
                409,
                "EVENT_REPLAY_UNAVAILABLE",
                "Reconnect without an event cursor.",
            )
        except SQLAlchemyError, OSError:
            return _error(
                request,
                503,
                "SERVICE_UNAVAILABLE",
                "Service is temporarily unavailable.",
                retryable=True,
            )

        if not await principal.revalidate():
            return _error(
                request, 401, "AUTHENTICATION_REQUIRED", "Authentication is required."
            )

        async def body() -> AsyncIterator[str]:  # noqa: PLR0911 - stop on lost access/disconnect
            if not await principal.revalidate():
                return
            payload = {"schema_version": 1, **asdict(page.snapshot)}
            yield (
                "event: review.snapshot\ndata: "
                + json.dumps(jsonable_encoder(payload))
                + "\n\n"
            )
            current = page
            cursor = after if last_event_id else page.snapshot.sequence
            replay = bool(last_event_id)
            heartbeat_at = monotonic()
            while True:
                for event in current.events if replay else ():
                    if not await principal.revalidate():
                        return
                    payload = {"schema_version": 1, **asdict(event)}
                    yield (
                        f"id: v1:{review_id}:{event.sequence}\n"
                        "event: review.event\ndata: "
                        + json.dumps(jsonable_encoder(payload))
                        + "\n\n"
                    )
                    cursor = event.sequence
                if (
                    current.snapshot.status in _TERMINAL
                    and (
                        current.snapshot.publication_status
                        not in {"NOT_READY", "PENDING"}
                        or (
                            current.snapshot.status in {"FAILED", "SKIPPED"}
                            and current.snapshot.publication_status == "NOT_READY"
                        )
                    )
                    and cursor >= current.snapshot.sequence
                ):
                    return
                await asyncio.sleep(POLL_SECONDS)
                if await request.is_disconnected():
                    return
                if not await principal.revalidate():
                    return
                previous = current.snapshot
                try:
                    current = await events.read(
                        review_id, principal.user_id, after=cursor
                    )
                except ReviewAccessDeniedError:
                    return
                if not await principal.revalidate():
                    return
                if current.snapshot != previous:
                    payload = {"schema_version": 1, **asdict(current.snapshot)}
                    yield (
                        "event: review.snapshot\ndata: "
                        + json.dumps(jsonable_encoder(payload))
                        + "\n\n"
                    )
                if monotonic() - heartbeat_at >= HEARTBEAT_SECONDS:
                    yield ": heartbeat\n\n"
                    heartbeat_at = monotonic()
                replay = True

        return StreamingResponse(
            body(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
    return JSONResponse(
        status_code=401,
        content={
            "code": "AUTHENTICATION_REQUIRED",
            "message": "Authentication is required.",
            "request_id": get_contextvars().get("request_id", ""),
            "retryable": False,
            "retry_after": None,
        },
    )
