"""ReviewService: one chunk from ContextPayload to ReviewChunkOutcome (spec §8).

Expected provider, budget and format failures become a FAILED result with a
safe ``error_code``; programming and configuration errors propagate.
"""

import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import structlog

from app.application.analysis_ports import ReviewRunBudgetPort
from app.core.config import LLM
from app.domain.frozen_configuration import ExecutionSettings
from app.llm.budget import RunBudget
from app.llm.client import ChatMessage, LLMCallResult, LLMRequest
from app.llm.errors import (
    ContextOverflowError,
    LLMGatewayError,
    SchemaValidationError,
)
from app.llm.gateway import LLMGateway
from app.llm.tokens import ConservativeTokenCounter, TokenCounter
from app.review.configuration import ExecutionConfiguration, FrozenConfigurationError
from app.review.prompt import BuiltPrompt, PromptBuilder, PromptTooLargeError
from app.review.schemas.context import ContextPayload
from app.review.schemas.result import (
    ChunkStatus,
    LLMReviewOutput,
    ReviewChunkOutcome,
    Usage,
)
from app.review.validation import FindingValidator, ValidationOutcome

logger = structlog.get_logger(__name__)

_FALLBACK_NOTE = "Чанк обработан резервным провайдером ({}/{})."


@dataclass
class _Attempt:
    """Per-chunk accounting across the main call, repair and rebuild."""

    started: float
    requests_before: int
    calls: list[LLMCallResult[LLMReviewOutput]] = field(default_factory=list)


class ReviewService:
    def __init__(  # noqa: PLR0913 -- service ports plus frozen execution authority
        self,
        gateway: LLMGateway,
        prompt_builder: PromptBuilder,
        validator: FindingValidator,
        token_counter: TokenCounter,
        settings: LLM | ExecutionSettings,
        *,
        frozen_configuration: ExecutionConfiguration | None = None,
    ) -> None:
        self._gateway = gateway
        self._prompts = prompt_builder
        self._validator = validator
        self._counter = token_counter
        self._settings = settings
        self._frozen = frozen_configuration

    async def review_chunk(
        self, payload: ContextPayload, budget: RunBudget | ReviewRunBudgetPort
    ) -> ReviewChunkOutcome:
        self._prompts.ensure_verified()
        if isinstance(self._settings, ExecutionSettings):
            if self._frozen is None or self._settings != self._frozen.service_settings:
                raise FrozenConfigurationError("execution_configuration_required")
            if (self._prompts.prompt_version, self._prompts.prompt_digest) != (
                self._frozen.prompt_version,
                self._frozen.prompt_digest,
            ):
                raise FrozenConfigurationError("unsupported_prompt_bundle")
            if self._prompts.repository_instructions != self._frozen.instructions:
                raise FrozenConfigurationError("repository_instructions_mismatch")
            self._frozen.validate_context(payload)
        attempt = _Attempt(time.monotonic(), await _requests_used(budget))
        try:
            prompt = self._prompts.build(
                payload, max_input_tokens=self._settings.max_input_tokens
            )
        except PromptTooLargeError:
            return await self._result(
                payload,
                budget,
                attempt,
                self._prompts.prompt_version,
                error_code="prompt_too_large",
            )
        try:
            try:
                call = await self._generate(payload, prompt, budget)
            except ContextOverflowError:
                # One rebuild with a smaller limit: metadata shrinks, the diff
                # stays; a still-too-large diff is re-chunked by the caller.
                smaller = self._prompts.overflow_input_limit(
                    self._settings.max_input_tokens
                )
                try:
                    prompt = self._prompts.build(payload, max_input_tokens=smaller)
                except PromptTooLargeError:
                    raise ContextOverflowError from None
                call = await self._generate(
                    payload, prompt, budget, call_kind="overflow_rebuild"
                )
        except LLMGatewayError as exc:
            return await self._result(
                payload,
                budget,
                attempt,
                prompt.prompt_version,
                error_code=exc.error_code,
                retryable=exc.retryable,
                root_error_code=getattr(exc, "last_error_code", exc.error_code),
            )
        attempt.calls.append(call)
        limitations = [*prompt.limitations, *call.parsed.limitations]
        if call.provider != self._gateway.primary_provider:
            limitations.append(_FALLBACK_NOTE.format(call.provider, call.model))
        outcome = self._validator.validate(call.parsed.findings, payload)
        return await self._result(
            payload,
            budget,
            attempt,
            prompt.prompt_version,
            summary=call.parsed.summary,
            outcome=outcome,
            limitations=limitations,
        )

    async def _generate(
        self,
        payload: ContextPayload,
        prompt: BuiltPrompt,
        budget: RunBudget | ReviewRunBudgetPort,
        *,
        call_kind: str = "initial",
    ) -> LLMCallResult[LLMReviewOutput]:
        try:
            return await self._send(
                prompt.messages,
                prompt.estimated_tokens,
                budget,
                payload,
                call_kind=call_kind,
            )
        except SchemaValidationError as exc:
            repair_id = uuid4()
            allowed = (
                budget.try_use_format_repair()
                if isinstance(budget, RunBudget)
                else await budget.claim_format_repair(
                    repair_id=repair_id, chunk_id=UUID(payload.chunk_id)
                )
            )
            if not allowed:
                raise
            return await self._repair(payload, exc, budget, repair_id=repair_id)

    async def _repair(
        self,
        payload: ContextPayload,
        error: SchemaValidationError,
        budget: RunBudget | ReviewRunBudgetPort,
        *,
        repair_id: UUID,
    ) -> LLMCallResult[LLMReviewOutput]:
        """One repair request: original messages, the truncated invalid answer
        and the validation errors (locations and messages, no source code)."""
        try:
            prompt = self._prompts.build_repair(
                payload,
                raw_text=error.raw_text,
                details=error.details,
                max_input_tokens=self._settings.max_input_tokens,
            )
        except PromptTooLargeError:
            raise error from None
        return await self._send(
            prompt.messages,
            prompt.estimated_tokens,
            budget,
            payload,
            call_kind="repair",
            operation_id=repair_id,
        )

    async def _send(  # noqa: PLR0913 -- paid operation identity
        self,
        messages: list[ChatMessage],
        input_tokens: int,
        budget: RunBudget | ReviewRunBudgetPort,
        payload: ContextPayload,
        *,
        call_kind: str,
        operation_id: UUID | None = None,
    ) -> LLMCallResult[LLMReviewOutput]:
        request = LLMRequest(
            messages=messages,
            temperature=self._settings.temperature,
            max_output_tokens=self._settings.max_output_tokens,
            timeout_s=self._settings.request_timeout_s,
            output_schema_json=self._prompts.output_schema_json,
        )
        # The verified renderer estimates messages; schema is a separate input.
        counter = ConservativeTokenCounter() if self._frozen else self._counter
        input_tokens += counter.count(self._prompts.output_schema_json)
        return await self._gateway.generate_structured(
            request,
            LLMReviewOutput,
            budget=budget,
            input_tokens=input_tokens,
            chunk_id=UUID(payload.chunk_id)
            if not isinstance(budget, RunBudget)
            else None,
            operation_id=operation_id or uuid4(),
            call_kind=call_kind,
        )

    async def _result(  # noqa: PLR0913
        self,
        payload: ContextPayload,
        budget: RunBudget | ReviewRunBudgetPort,
        attempt: _Attempt,
        prompt_version: str,
        *,
        summary: str | None = None,
        outcome: ValidationOutcome | None = None,
        limitations: list[str] | None = None,
        error_code: str | None = None,
        retryable: bool = False,
        root_error_code: str | None = None,
    ) -> ReviewChunkOutcome:
        last = attempt.calls[-1] if attempt.calls else None
        result = ReviewChunkOutcome(
            review_id=payload.review_id,
            chunk_id=payload.chunk_id,
            status=ChunkStatus.FAILED if error_code else ChunkStatus.COMPLETED,
            summary=summary,
            findings=outcome.accepted if outcome else [],
            rejected_findings_count=outcome.rejected_count if outcome else 0,
            limitations=limitations or [],
            usage=Usage(
                input_tokens=_sum_known(c.input_tokens for c in attempt.calls),
                output_tokens=_sum_known(c.output_tokens for c in attempt.calls),
                requests=(await _requests_used(budget)) - attempt.requests_before,
                latency_ms=int((time.monotonic() - attempt.started) * 1000),
            ),
            model=last.model if last else self._configured_model(),
            model_digest=last.model_digest if last else None,
            provider=last.provider if last else self._gateway.primary_provider,
            prompt_version=prompt_version,
            error_code=error_code,
            retryable=retryable,
            root_error_code=root_error_code,
        )
        logger.info(
            "review_chunk_finished",
            review_id=result.review_id,
            chunk_id=result.chunk_id,
            status=result.status,
            provider=result.provider,
            model=result.model,
            requests=result.usage.requests,
            duration_ms=result.usage.latency_ms,
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
            error_code=result.error_code,
            findings_count=len(result.findings),
            rejected_findings_count=result.rejected_findings_count,
        )
        return result

    def _configured_model(self) -> str:
        if self._frozen is not None:
            return self._frozen.model_ref
        if self._gateway.primary_provider == "eurorouter":
            return self._settings.eurorouter.model
        return self._settings.ollama.model


def _sum_known(values: Iterable[int | None]) -> int | None:
    known = [v for v in values if v is not None]
    return sum(known) if known else None


async def _requests_used(budget: RunBudget | ReviewRunBudgetPort) -> int:
    if isinstance(budget, RunBudget):
        return budget.state().requests_used
    return (await budget.snapshot()).requests_used
