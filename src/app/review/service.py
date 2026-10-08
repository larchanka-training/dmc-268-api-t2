"""ReviewService: one chunk from ContextPayload to ReviewChunkResult (spec §8).

Expected provider, budget and format failures become a FAILED result with a
safe ``error_code``; programming and configuration errors propagate.
"""

import time
from collections.abc import Iterable
from dataclasses import dataclass, field

import structlog

from app.core.config import LLM
from app.llm.budget import RunBudget
from app.llm.client import ChatMessage, LLMCallResult, LLMRequest
from app.llm.errors import (
    ContextOverflowError,
    LLMGatewayError,
    SchemaValidationError,
)
from app.llm.gateway import LLMGateway
from app.llm.tokens import TokenCounter
from app.review.prompt import BuiltPrompt, PromptBuilder, PromptTooLargeError
from app.review.schemas.context import ContextPayload
from app.review.schemas.result import (
    ChunkStatus,
    LLMReviewOutput,
    ReviewChunkResult,
    Usage,
)
from app.review.validation import FindingValidator, ValidationOutcome

logger = structlog.get_logger(__name__)

_OVERFLOW_SHRINK = 0.75
_FALLBACK_NOTE = "Чанк обработан резервным провайдером ({}/{})."

# Previous invalid answer kept in the repair request (spec §8 step 3).
_REPAIR_ANSWER_CHARS = 4000
_REPAIR_MAX_ERRORS = 20
_REPAIR_INSTRUCTION = (
    "Предыдущий ответ не прошёл проверку JSON Schema. Ошибки:\n{errors}\n"
    "Верни исправленный ответ целиком: только JSON по схеме, без пояснений."
)


@dataclass
class _Attempt:
    """Per-chunk accounting across the main call, repair and rebuild."""

    started: float
    requests_before: int
    calls: list[LLMCallResult[LLMReviewOutput]] = field(default_factory=list)


class ReviewService:
    def __init__(
        self,
        gateway: LLMGateway,
        prompt_builder: PromptBuilder,
        validator: FindingValidator,
        token_counter: TokenCounter,
        settings: LLM,
    ) -> None:
        self._gateway = gateway
        self._prompts = prompt_builder
        self._validator = validator
        self._counter = token_counter
        self._settings = settings

    async def review_chunk(
        self, payload: ContextPayload, budget: RunBudget
    ) -> ReviewChunkResult:
        attempt = _Attempt(time.monotonic(), budget.state().requests_used)
        try:
            prompt = self._prompts.build(
                payload, max_input_tokens=self._settings.max_input_tokens
            )
        except PromptTooLargeError:
            return self._result(
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
                smaller = int(self._settings.max_input_tokens * _OVERFLOW_SHRINK)
                try:
                    prompt = self._prompts.build(payload, max_input_tokens=smaller)
                except PromptTooLargeError:
                    raise ContextOverflowError from None
                call = await self._generate(payload, prompt, budget)
        except LLMGatewayError as exc:
            return self._result(
                payload,
                budget,
                attempt,
                prompt.prompt_version,
                error_code=exc.error_code,
            )
        attempt.calls.append(call)
        limitations = [*prompt.limitations, *call.parsed.limitations]
        if call.provider != self._gateway.primary_provider:
            limitations.append(_FALLBACK_NOTE.format(call.provider, call.model))
        outcome = self._validator.validate(call.parsed.findings, payload)
        return self._result(
            payload,
            budget,
            attempt,
            prompt.prompt_version,
            summary=call.parsed.summary,
            outcome=outcome,
            limitations=limitations,
        )

    async def _generate(
        self, payload: ContextPayload, prompt: BuiltPrompt, budget: RunBudget
    ) -> LLMCallResult[LLMReviewOutput]:
        try:
            return await self._send(prompt.messages, prompt.estimated_tokens, budget)
        except SchemaValidationError as exc:
            if not budget.try_use_format_repair():
                raise
            return await self._repair(payload, exc, budget)

    async def _repair(
        self, payload: ContextPayload, error: SchemaValidationError, budget: RunBudget
    ) -> LLMCallResult[LLMReviewOutput]:
        """One repair request: original messages, the truncated invalid answer
        and the validation errors (locations and messages, no source code)."""
        errors = "\n".join(f"- {d}" for d in error.details[:_REPAIR_MAX_ERRORS])
        extra = [
            ChatMessage(
                role="assistant", content=error.raw_text[:_REPAIR_ANSWER_CHARS]
            ),
            ChatMessage(role="user", content=_REPAIR_INSTRUCTION.format(errors=errors)),
        ]
        extra_tokens = sum(self._counter.count(m.content) for m in extra)
        try:
            prompt = self._prompts.build(
                payload, max_input_tokens=self._settings.max_input_tokens - extra_tokens
            )
        except PromptTooLargeError:
            raise error from None
        return await self._send(
            [*prompt.messages, *extra], prompt.estimated_tokens + extra_tokens, budget
        )

    async def _send(
        self, messages: list[ChatMessage], input_tokens: int, budget: RunBudget
    ) -> LLMCallResult[LLMReviewOutput]:
        request = LLMRequest(
            messages=messages,
            max_output_tokens=self._settings.max_output_tokens,
            timeout_s=self._settings.request_timeout_s,
        )
        return await self._gateway.generate_structured(
            request, LLMReviewOutput, budget=budget, input_tokens=input_tokens
        )

    def _result(  # noqa: PLR0913
        self,
        payload: ContextPayload,
        budget: RunBudget,
        attempt: _Attempt,
        prompt_version: str,
        *,
        summary: str | None = None,
        outcome: ValidationOutcome | None = None,
        limitations: list[str] | None = None,
        error_code: str | None = None,
    ) -> ReviewChunkResult:
        last = attempt.calls[-1] if attempt.calls else None
        result = ReviewChunkResult(
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
                requests=budget.state().requests_used - attempt.requests_before,
                latency_ms=int((time.monotonic() - attempt.started) * 1000),
            ),
            model=last.model if last else self._configured_model(),
            model_digest=last.model_digest if last else None,
            provider=last.provider if last else self._gateway.primary_provider,
            prompt_version=prompt_version,
            error_code=error_code,
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
        if self._gateway.primary_provider == "eurorouter":
            return self._settings.eurorouter.model
        return self._settings.ollama.model


def _sum_known(values: Iterable[int | None]) -> int | None:
    known = [v for v in values if v is not None]
    return sum(known) if known else None
