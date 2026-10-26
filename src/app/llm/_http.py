"""HTTP plumbing shared by provider clients: one request, typed errors."""

import json
import re
from typing import Any

import httpx
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JSONSchemaValidationError
from pydantic import BaseModel, ValidationError

from app.llm.errors import (
    AuthError,
    ContextOverflowError,
    ProviderRequestError,
    ProviderTimeoutError,
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
    except httpx.TimeoutException:
        raise ProviderTimeoutError from None
    except (
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
        raise TransientProviderError(_status_detail(response), status_code=status)
    if status == 400 and _is_context_overflow(response):  # noqa: PLR2004
        msg = "HTTP 400: context overflow"
        raise ContextOverflowError(msg)
    raise ProviderRequestError(_status_detail(response))


def _status_detail(response: httpx.Response) -> str:
    msg = f"HTTP {response.status_code}"
    if code := _provider_error_code(response):
        msg = f"{msg} ({code})"
    return msg


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


def parse_structured[T: BaseModel](  # noqa: PLR0913 -- schema and usage metadata
    content: str,
    response_model: type[T],
    *,
    output_schema_json: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    latency_ms: int | None = None,
) -> T:
    """Validate model output; errors list locations and messages, not input."""
    if output_schema_json is not None:
        try:
            payload = json.loads(content)
            Draft202012Validator(json.loads(output_schema_json)).validate(payload)
        except json.JSONDecodeError:
            details = ["<root>: invalid JSON"]
        except JSONSchemaValidationError as exc:
            # Instance paths and jsonschema messages may include model text or
            # arbitrary extra property names. Only the trusted schema path and
            # validator keyword are safe to include in format-repair details.
            location = ".".join(str(part) for part in exc.absolute_schema_path)
            required = exc.validator_value
            instance = exc.instance
            if (
                exc.validator == "required"
                and not exc.absolute_path
                and isinstance(required, list)
                and isinstance(instance, dict)
            ):
                # Preserve historical repair vectors for missing output fields.
                # Names and order come from the trusted schema, never payload.
                details = [
                    f"{name}: Field required"
                    for name in required
                    if name not in instance
                ]
            else:
                details = [f"schema.{location}: {exc.validator} validation failed"]
        else:
            details = []
        if details:
            raise SchemaValidationError(
                details,
                content,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_ms=latency_ms,
            ) from None
    try:
        return response_model.model_validate_json(content)
    except ValidationError as exc:
        details = [
            f"{'.'.join(str(p) for p in error['loc']) or '<root>'}: {error['msg']}"
            for error in exc.errors(include_input=False, include_url=False)
        ]
        raise SchemaValidationError(
            details,
            content,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
        ) from None
