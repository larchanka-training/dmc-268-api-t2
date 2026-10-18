"""Strict persisted policy v1. Defaults are materialized only at new admission."""

import json
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PositiveLimit = Annotated[int, Field(gt=0, le=2_147_483_647)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Selector = Annotated[str, Field(min_length=1, max_length=256, pattern=r"^\S+$")]
ProfileRef = Literal["eurorouter/review-v1", "ollama/review-v1"]


class FrozenConfigurationError(ValueError):
    """Safe pre-inference rejection; never mutates job lifecycle state."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class FrozenDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    @field_validator(
        "schema_version",
        "analysis_duration_ms",
        "worker_max_attempts",
        "provider_max_attempts_per_operation",
        "max_transient_per_provider",
        "max_format_repairs",
        "max_overflow_rebuilds_per_chunk",
        mode="before",
        check_fields=False,
    )
    @classmethod
    def exact_integer_literals(cls, value: Any) -> Any:
        # Pydantic Literal equality alone accepts bool/float as integer values.
        if type(value) is not int:
            raise ValueError("fixed integer policy requires an integer")
        return value


class Generation(FrozenDTO):
    temperature: float = Field(ge=0, le=2, allow_inf_nan=False)


class ModelRoute(FrozenDTO):
    profile_ref: ProfileRef
    model_ref: Selector
    model_digest: Digest | None


class Routing(FrozenDTO):
    primary_profile_ref: ProfileRef
    fallback: tuple[ModelRoute, ...] = Field(max_length=1)


class Limits(FrozenDTO):
    context_window: PositiveLimit
    max_input_tokens: PositiveLimit
    max_output_tokens: PositiveLimit
    request_timeout_ms: Annotated[int, Field(gt=0, le=120000)]
    run_max_requests: PositiveLimit
    run_max_input_tokens: PositiveLimit
    run_max_output_tokens: PositiveLimit
    max_chunks: PositiveLimit
    analysis_duration_ms: Literal[600000]

    @model_validator(mode="after")
    def fits_context(self) -> Self:
        if self.max_input_tokens + self.max_output_tokens > self.context_window:
            raise ValueError("input/output limits exceed context window")
        return self


class RetryPolicy(FrozenDTO):
    worker_max_attempts: Literal[3]
    provider_max_attempts_per_operation: Literal[3]
    max_transient_per_provider: Literal[2]
    max_format_repairs: Literal[1]
    max_overflow_rebuilds_per_chunk: Literal[1]
    backoff_policy: Literal["review-exponential-v1"]


class ModelSettings(FrozenDTO):
    schema_version: Literal[1]
    generation: Generation
    routing: Routing
    limits: Limits
    retry: RetryPolicy
    token_estimator_version: Literal["review-conservative-v1"]
    repository_settings_digest: Digest

    @classmethod
    def from_stored(cls, values: dict[str, object]) -> Self:
        # JSON lists become immutable tuples. Python strict mode would reject the
        # canonical persisted array. No defaults exist on the persisted DTO.
        return cls.model_validate_json(json.dumps(values, allow_nan=False))


def admission_policy(
    repository_settings_digest: str,
    *,
    primary_profile_ref: ProfileRef = "eurorouter/review-v1",
    temperature: float = 0.0,
) -> ModelSettings:
    """Server-owned v1 defaults, never used to interpret historical jobs."""
    return ModelSettings(
        schema_version=1,
        generation=Generation(temperature=temperature),
        routing=Routing(primary_profile_ref=primary_profile_ref, fallback=()),
        limits=Limits(
            context_window=16384,
            max_input_tokens=10000,
            max_output_tokens=2000,
            request_timeout_ms=120000,
            run_max_requests=8,
            run_max_input_tokens=80000,
            run_max_output_tokens=16000,
            max_chunks=5,
            analysis_duration_ms=600000,
        ),
        retry=RetryPolicy(
            worker_max_attempts=3,
            provider_max_attempts_per_operation=3,
            max_transient_per_provider=2,
            max_format_repairs=1,
            max_overflow_rebuilds_per_chunk=1,
            backoff_policy="review-exponential-v1",
        ),
        token_estimator_version="review-conservative-v1",
        repository_settings_digest=repository_settings_digest,
    )


class ExecutionModel(FrozenDTO):
    model: str


class ExecutionSettings(FrozenDTO):
    """Small immutable settings view consumed by ReviewService and test budgets."""

    eurorouter: ExecutionModel
    ollama: ExecutionModel
    context_window: int
    max_input_tokens: int
    max_output_tokens: int
    request_timeout_s: float
    run_max_requests: int
    run_max_input_tokens: int
    run_max_output_tokens: int
    run_deadline_s: float
    max_chunks: int
    temperature: float


MAX_REPOSITORY_INSTRUCTION_CHARS = 65536


class RepositoryInstructions(FrozenDTO):
    instructions: tuple[Annotated[str, Field(min_length=1)], ...]

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if (
            sum(len(instruction) for instruction in self.instructions)
            > MAX_REPOSITORY_INSTRUCTION_CHARS
        ):
            raise ValueError("repository instructions exceed supported size")
        return self
