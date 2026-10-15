"""LLMGateway: routing across provider endpoints (spec §4.4).

Cooldowns live in process memory: v1 runs one analyze worker with
concurrency=1 (SD §9, §14).
"""

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import structlog
from pydantic import BaseModel

from app.application.analysis_authority import current_analysis_authority
from app.application.analysis_ports import AccountingIdentityError, ReviewRunBudgetPort
from app.application.ports import LeaseLostError
from app.llm.budget import RunBudget
from app.llm.client import BaseLLMClient, LLMCallResult, LLMRequest
from app.llm.errors import (
    AuthError,
    BudgetExceededError,
    LLMGatewayError,
    LLMUnavailableError,
    ModelIdentityError,
    ProviderTimeoutError,
    RateLimitedError,
    TransientProviderError,
)

logger = structlog.get_logger(__name__)

Sleep = Callable[[float], Awaitable[None]]

MAX_ATTEMPTS_PER_CALL = 3  # SD §9: up to three attempts per stage
# Transient failures tolerated on one provider before failing over; together
# with MAX_ATTEMPTS_PER_CALL this leaves the fallback a try.
MAX_TRANSIENT_PER_PROVIDER = 2
BACKOFF_BASE_S = 1.0
BACKOFF_CAP_S = 20.0


@dataclass
class Endpoint:
    client: BaseLLMClient
    cooldown_until: float = 0.0
    disabled: bool = False  # 401/403: until process restart
    expected_model: str | None = None
    expected_digest: str | None = None


@dataclass
class ProviderPool:
    """One provider and its endpoints (one per API key), used round-robin."""

    name: str
    endpoints: list[Endpoint]
    timeout_s: float
    profile_ref: str | None = None
    _next: int = field(default=0, init=False)

    def next_available(self, now: float) -> Endpoint | None:
        for offset in range(len(self.endpoints)):
            index = (self._next + offset) % len(self.endpoints)
            endpoint = self.endpoints[index]
            if not endpoint.disabled and endpoint.cooldown_until <= now:
                self._next = (index + 1) % len(self.endpoints)
                return endpoint
        return None


class LLMGateway:
    def __init__(
        self,
        pools: list[ProviderPool],
        *,
        sleep: Sleep = asyncio.sleep,
        rng: random.Random | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._pools = pools
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._clock = clock

    @property
    def primary_provider(self) -> str:
        return self._pools[0].name

    async def generate_structured[T: BaseModel](  # noqa: PLR0913, PLR0912, PLR0915 -- routing and one fenced payment per HTTP
        self,
        request: LLMRequest,
        response_model: type[T],
        *,
        budget: RunBudget | ReviewRunBudgetPort,
        input_tokens: int,
        chunk_id: UUID | None = None,
        operation_id: UUID | None = None,
        call_kind: str = "initial",
    ) -> LLMCallResult[T]:
        attempts = 0
        last_error: LLMGatewayError | None = None
        saw_retryable = False
        for pool in self._pools:
            transient_failures = 0
            while (
                attempts < MAX_ATTEMPTS_PER_CALL
                and transient_failures < MAX_TRANSIENT_PER_PROVIDER
            ):
                endpoint = pool.next_available(self._clock())
                if endpoint is None:
                    break
                authority = current_analysis_authority.get()
                if authority is not None:
                    authority.check()
                reservation = None
                permit = None
                if isinstance(budget, RunBudget):
                    reservation = budget.reserve(
                        input_tokens=input_tokens,
                        output_tokens=request.max_output_tokens,
                    )
                else:
                    if (
                        chunk_id is None
                        or operation_id is None
                        or pool.profile_ref is None
                        or endpoint.expected_model is None
                    ):
                        raise AccountingIdentityError("durable_call_identity_required")
                    reserve_started = self._clock()
                    permit = await budget.reserve_call(
                        reservation_id=uuid4(),
                        chunk_id=chunk_id,
                        operation_id=operation_id,
                        call_kind=call_kind,
                        provider_profile_ref=pool.profile_ref,
                        model_ref=endpoint.expected_model,
                        model_digest=endpoint.expected_digest,
                        input_tokens=input_tokens,
                        output_tokens=request.max_output_tokens,
                    )
                attempts += 1
                # A failed attempt has no usage: its whole reservation stays
                # charged (SD §14).
                if permit is None:
                    remaining = await _remaining_s(budget)
                else:
                    elapsed = self._clock() - reserve_started
                    remaining = (
                        permit.analysis_deadline_at - permit.sampled_at
                    ).total_seconds() - elapsed
                    if remaining <= 0:
                        raise BudgetExceededError(
                            "analysis deadline exceeded before dispatch",
                            error_code="analysis_deadline_exceeded",
                        )
                    if (
                        permit.lease_expires_at - permit.sampled_at
                    ).total_seconds() <= elapsed:
                        raise LeaseLostError("Lease expired before dispatch")
                timeout_s = min(request.timeout_s, pool.timeout_s, remaining)
                try:
                    try:
                        try:
                            async with asyncio.timeout(timeout_s):
                                if authority is not None:
                                    authority.check()
                                result = await endpoint.client.generate_structured(
                                    request.model_copy(update={"timeout_s": timeout_s}),
                                    response_model,
                                )
                        except TimeoutError:
                            raise ProviderTimeoutError from None
                        if authority is not None:
                            authority.check()
                    except LLMGatewayError as exc:
                        if authority is not None:
                            authority.check()
                        if isinstance(budget, RunBudget):
                            assert reservation is not None
                            budget.settle(
                                reservation,
                                input_tokens=exc.input_tokens,
                                output_tokens=exc.output_tokens,
                            )
                        else:
                            assert permit is not None
                            await budget.settle_call(
                                reservation_id=permit.reservation_id,
                                input_tokens=exc.input_tokens,
                                output_tokens=exc.output_tokens,
                                outcome="ERROR",
                                error_code=exc.error_code,
                                latency_ms=exc.latency_ms,
                            )
                        raise
                except RateLimitedError as exc:
                    _log_failure(pool.name, attempts, exc)
                    endpoint.cooldown_until = self._clock() + max(
                        exc.retry_after or 0.0, self._backoff(attempts)
                    )
                    last_error = exc
                    saw_retryable |= exc.retryable
                    continue
                except AuthError as exc:
                    _log_failure(pool.name, attempts, exc)
                    endpoint.disabled = True
                    last_error = exc
                    saw_retryable |= exc.retryable
                    continue
                except TransientProviderError as exc:
                    _log_failure(pool.name, attempts, exc)
                    last_error = exc
                    saw_retryable = True
                    transient_failures += 1
                    if transient_failures < MAX_TRANSIENT_PER_PROVIDER:
                        await self._wait(self._backoff(attempts), budget)
                    continue
                except LLMGatewayError as exc:  # not retried: the caller decides
                    _log_failure(pool.name, attempts, exc)
                    raise
                if isinstance(budget, RunBudget):
                    assert reservation is not None
                    budget.settle(
                        reservation,
                        input_tokens=result.input_tokens,
                        output_tokens=result.output_tokens,
                    )
                else:
                    assert permit is not None
                    await budget.settle_call(
                        reservation_id=permit.reservation_id,
                        input_tokens=result.input_tokens,
                        output_tokens=result.output_tokens,
                        outcome="SUCCEEDED",
                        error_code=None,
                        latency_ms=result.latency_ms,
                    )
                if (
                    endpoint.expected_model is not None
                    and result.reported_model != endpoint.expected_model
                ):
                    raise ModelIdentityError
                logger.info(
                    "llm_attempt_succeeded",
                    provider=pool.name,
                    model=result.model,
                    attempt=attempts,
                    duration_ms=result.latency_ms,
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                )
                return result
        raise LLMUnavailableError(
            last_error.error_code if last_error else "no_available_endpoint",
            retryable=saw_retryable or last_error is None,
        )

    async def _wait(
        self, delay: float, budget: RunBudget | ReviewRunBudgetPort
    ) -> None:
        if delay >= await _remaining_s(budget):
            msg = "backoff does not fit the run deadline"
            raise BudgetExceededError(msg, error_code="analysis_deadline_exceeded")
        await self._sleep(delay)

    def _backoff(self, attempt: int) -> float:
        delay = min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2.0**attempt)
        return delay * self._rng.uniform(0.5, 1.0)


def _log_failure(provider: str, attempt: int, error: LLMGatewayError) -> None:
    logger.warning(
        "llm_attempt_failed",
        provider=provider,
        attempt=attempt,
        error_code=error.error_code,
        # Messages hold only status/exception names and provider codes,
        # never request or response bodies.
        detail=str(error),
    )


async def _remaining_s(budget: RunBudget | ReviewRunBudgetPort) -> float:
    if isinstance(budget, RunBudget):
        return budget.remaining_s()
    return await budget.remaining_s()
