"""Canonical errors for authenticated API reads."""

from fastapi import Request
from starlette.responses import JSONResponse
from structlog.contextvars import get_contextvars


class ApiError(Exception):
    def __init__(  # noqa: PLR0913 -- canonical error envelope plus optional HTTP headers
        self,
        status: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        retry_after: int | None = None,
        headers: dict[str, str] | None = None,
        **extra: object,
    ) -> None:
        self.status = status
        self.code = code
        self.message = message
        self.retryable = retryable
        self.retry_after = retry_after
        self.extra = extra
        self.headers = headers


async def api_error_response(request: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, ApiError)
    content: dict[str, object] = {
        "code": error.code,
        "message": error.message,
        "request_id": get_contextvars()["request_id"],
        "retryable": error.retryable,
        "retry_after": error.retry_after,
    }
    content.update(error.extra)
    return JSONResponse(
        status_code=error.status,
        content=content,
        headers=error.headers,
    )
