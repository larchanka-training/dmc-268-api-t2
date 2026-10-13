"""Provider-neutral request/result types and the client interface (spec §4.1)."""

from abc import ABC, abstractmethod
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ChatMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    role: Literal["system", "user", "assistant"]
    content: str


class LLMRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    messages: list[ChatMessage]
    temperature: float = Field(
        default=0.0, ge=0, le=2, allow_inf_nan=False, strict=True
    )
    max_output_tokens: int = Field(gt=0)
    timeout_s: float = Field(gt=0)
    # Immutable serialized provider schema supplied by a verified prompt bundle.
    # Generic/test callers may still use their response model's schema.
    output_schema_json: str | None = None


class LLMCallResult[T: BaseModel](BaseModel):
    model_config = ConfigDict(frozen=True)

    parsed: T
    raw_text_len: int  # size only: the text itself is never logged
    input_tokens: int | None
    output_tokens: int | None
    model: str
    # Actual response selector, distinct from the legacy configured-model fallback.
    reported_model: str | None = None
    model_digest: str | None
    provider: str
    latency_ms: int


class BaseLLMClient(ABC):
    """One provider endpoint. Sends exactly one HTTP request per call: retries
    and failover belong to ``LLMGateway``."""

    name: str

    @abstractmethod
    async def generate_structured[T: BaseModel](
        self, request: LLMRequest, response_model: type[T]
    ) -> LLMCallResult[T]: ...
