"""Typed gateway errors (spec §4.3).

Every error carries a safe ``error_code``. Request and response bodies, prompts
and keys never go into error messages.
"""


class LLMGatewayError(Exception):
    error_code = "llm_error"

    def __init__(self, message: str = "") -> None:
        super().__init__(message or self.error_code)


class RateLimitedError(LLMGatewayError):
    """HTTP 429: the key/endpoint goes into cooldown."""

    error_code = "rate_limited"

    def __init__(self, retry_after: float | None = None) -> None:
        super().__init__()
        self.retry_after = retry_after


class TransientProviderError(LLMGatewayError):
    """HTTP 500/502/503/504, connect/read timeouts, protocol errors."""

    error_code = "provider_unavailable"

    def __init__(self, message: str = "", *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class AuthError(LLMGatewayError):
    """HTTP 401/403: the key is disabled until process restart."""

    error_code = "provider_auth_failed"


class ContextOverflowError(LLMGatewayError):
    """HTTP 400 caused by an oversized context: rebuild the chunk smaller."""

    error_code = "context_overflow"


class ProviderRequestError(LLMGatewayError):
    """Other 4xx: not retried; a configuration or request error."""

    error_code = "provider_request_error"


class SchemaValidationError(LLMGatewayError):
    """Response is not JSON or does not match the schema; the pipeline decides."""

    error_code = "invalid_llm_output"

    def __init__(self, details: list[str], raw_text: str) -> None:
        super().__init__()
        self.details = details
        # Kept only for the single format-repair request; never logged.
        self.raw_text = raw_text


class BudgetExceededError(LLMGatewayError):
    error_code = "budget_exceeded"


class LLMUnavailableError(LLMGatewayError):
    """All endpoints failed after the allowed attempts."""

    error_code = "llm_unavailable"

    def __init__(self, last_error_code: str) -> None:
        super().__init__(f"all providers failed, last error: {last_error_code}")
        self.last_error_code = last_error_code
