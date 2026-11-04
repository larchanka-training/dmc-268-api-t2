"""Model output (`LLMReviewOutput`) and the internal chunk outcome.

`ReviewChunkOutcome` keeps backend-only provenance (status, provider, model,
request count, fingerprints). The canonical wire result is
`app.application.ports.ReviewChunkResult`, built by `review.port_adapter`.
"""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.review.schemas.finding import LLMFinding, LLMFindingV1, ValidatedFinding


class LLMReviewOutputV1(BaseModel):
    """Historical provider v1 wire output, without rule references or defaults."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: str = Field(max_length=2000)
    findings: list[LLMFindingV1] = Field(max_length=50)
    limitations: list[str] = Field(max_length=10)


class LLMReviewOutput(BaseModel):
    """Provider v2 output with required nullable rule references.

    Every field is required (nullable where optional) to stay compatible with
    OpenAI-style ``strict`` JSON Schema.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: str = Field(max_length=2000)
    findings: list[LLMFinding] = Field(max_length=50)
    limitations: list[str] = Field(max_length=10)


def provider_output_model(
    schema_path: str,
) -> type[LLMReviewOutputV1] | type[LLMReviewOutput]:
    """Select by the explicit provider schema identity; never infer from fields."""
    if schema_path == "schemas/llm-output/v1.json":
        return LLMReviewOutputV1
    if schema_path == "schemas/llm-output/v2.json":
        return LLMReviewOutput
    raise ValueError(f"unsupported provider schema: {schema_path}")


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

    schema_version: Literal[1, 2] = 1
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

    retryable: bool = False
    root_error_code: str | None = None
