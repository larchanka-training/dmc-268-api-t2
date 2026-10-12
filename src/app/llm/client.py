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
    temperature: float = 0.0  # SD §7
    max_output_tokens: int = Field(gt=0)
    timeout_s: float = Field(gt=0)


class LLMCallResult[T: BaseModel](BaseModel):
    model_config = ConfigDict(frozen=True)

    parsed: T
    raw_text_len: int  # size only: the text itself is never logged
    input_tokens: int | None
    output_tokens: int | None
    model: str
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
