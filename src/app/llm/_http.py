"""HTTP plumbing shared by provider clients: one request, typed errors."""

import re
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from app.llm.errors import (
    AuthError,
    ContextOverflowError,
    ProviderRequestError,
    RateLimitedError,
    SchemaValidationError,
    TransientProviderError,
)

_SAFE_CODE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_TRANSIENT_STATUSES = frozenset({500, 502, 503, 504})
# Substrings providers use for "prompt does not fit the context window".
# Adjust after real provider testing (spec §4.3).
_OVERFLOW_MARKERS = (
    "context_length_exceeded",
    "context length",
    "context window",
    "too many tokens",
    "prompt is too long",
)


async def post_json(
    http: httpx.AsyncClient,
    url: str,
    body: dict[str, Any],
    *,
    timeout_s: float,
    headers: dict[str, str] | None = None,
) -> Any:
    """POST ``body`` once and return the decoded JSON of a 2xx response."""
    try:
        response = await http.post(url, json=body, headers=headers, timeout=timeout_s)
    except (
        httpx.TimeoutException,
        httpx.NetworkError,
        httpx.RemoteProtocolError,
    ) as exc:
        msg = type(exc).__name__
        raise TransientProviderError(msg) from None
    _raise_for_status(response)
    try:
        return response.json()
    except ValueError:
        msg = "provider returned a non-JSON body"
        raise TransientProviderError(msg, status_code=response.status_code) from None


def _raise_for_status(response: httpx.Response) -> None:
    status = response.status_code
    if status < 400:  # noqa: PLR2004
        return
    if status == 429:  # noqa: PLR2004
        raise RateLimitedError(_retry_after(response))
    if status in {401, 403}:
        msg = f"HTTP {status}"
        raise AuthError(msg)
    if status in _TRANSIENT_STATUSES:
        msg = f"HTTP {status}"
        raise TransientProviderError(msg, status_code=status)
    if status == 400 and _is_context_overflow(response):  # noqa: PLR2004
        msg = "HTTP 400: context overflow"
        raise ContextOverflowError(msg)
    msg = f"HTTP {status}"
    if code := _provider_error_code(response):
        msg = f"{msg} ({code})"
    raise ProviderRequestError(msg)


def _provider_error_code(response: httpx.Response) -> str | None:
    """Short machine code from an OpenAI-style error body. The human-readable
    message is dropped: it may quote request content."""
    try:
        error = response.json().get("error")
    except ValueError, AttributeError:
        return None
    if not isinstance(error, dict):
        return None
    for key in ("code", "type"):
        value = error.get(key)
        if isinstance(value, str) and _SAFE_CODE.fullmatch(value):
            return value
    return None


def _retry_after(response: httpx.Response) -> float | None:
    try:
        return max(0.0, float(response.headers["Retry-After"]))
    except KeyError, ValueError:
        return None


def _is_context_overflow(response: httpx.Response) -> bool:
    text = response.text.lower()
    return any(marker in text for marker in _OVERFLOW_MARKERS)


def parse_structured[T: BaseModel](content: str, response_model: type[T]) -> T:
    """Validate model output; errors list locations and messages, not input."""
    try:
        return response_model.model_validate_json(content)
    except ValidationError as exc:
        details = [
            f"{'.'.join(str(p) for p in error['loc']) or '<root>'}: {error['msg']}"
            for error in exc.errors(include_input=False, include_url=False)
        ]
        raise SchemaValidationError(details, content) from None
