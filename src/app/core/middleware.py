"""ASGI middleware tagging every request with a Request ID."""

import re
from uuid import uuid4

import structlog
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_HEADER = "x-request-id"
_SAFE_REQUEST_ID = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")


class RequestIdMiddleware:
    """Bind a Request ID to the logging context and echo it back to the client."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap the ASGI application."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Bind the Request ID for the duration of the request."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = Headers(scope=scope).get(REQUEST_ID_HEADER, "")
        request_id = incoming if _SAFE_REQUEST_ID.fullmatch(incoming) else uuid4().hex
        structlog.contextvars.bind_contextvars(request_id=request_id)

        async def send_with_request_id(message: Message) -> None:
            """Add the Request ID header to the outgoing response."""
            if message["type"] == "http.response.start":
                headers = MutableHeaders(raw=message["headers"])
                headers.append(REQUEST_ID_HEADER, request_id)
                message["headers"] = headers.raw
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            structlog.contextvars.unbind_contextvars("request_id")
