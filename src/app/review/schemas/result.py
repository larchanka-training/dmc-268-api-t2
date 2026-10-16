"""Model output (`LLMReviewOutput`) and the internal chunk outcome.

`ReviewChunkOutcome` keeps backend-only provenance (status, provider, model,
request count, fingerprints). The canonical wire result is
`app.application.ports.ReviewChunkResult`, built by `review.port_adapter`.
"""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.review.schemas.finding import LLMFinding, ValidatedFinding


class LLMReviewOutput(BaseModel):
    """The only schema passed to the model as structured-output format (spec §3.2).

    Every field is required (nullable where optional) to stay compatible with
    OpenAI-style ``strict`` JSON Schema.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: str = Field(max_length=2000)
    findings: list[LLMFinding] = Field(max_length=50)
    limitations: list[str] = Field(max_length=10)


class ChunkStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"


class Usage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    input_tokens: int | None = Field(ge=0)  # None when the provider gave no usage
    output_tokens: int | None = Field(ge=0)
    requests: int = Field(ge=0)  # actual requests, retries and repair included
    latency_ms: int = Field(ge=0)


class ReviewChunkOutcome(BaseModel):
    """Internal chunk outcome filled by the backend after validation (SD §7)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[2] = 2
    review_id: str
    chunk_id: str
    status: ChunkStatus
    summary: str | None  # None when FAILED
    findings: list[ValidatedFinding]
    rejected_findings_count: int = Field(ge=0)
    limitations: list[str]
    usage: Usage
    model: str
    model_digest: str | None
    provider: str
    prompt_version: str
    error_code: str | None  # safe code when FAILED, never model output text
