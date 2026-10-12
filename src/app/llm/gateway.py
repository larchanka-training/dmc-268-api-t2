"""LLMGateway: routing across provider endpoints (spec §4.4).

Cooldowns live in process memory: v1 runs one analyze worker with
concurrency=1 (SD §9, §14).
"""

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import structlog
from pydantic import BaseModel

from app.llm.budget import RunBudget
from app.llm.client import BaseLLMClient, LLMCallResult, LLMRequest
from app.llm.errors import (
    AuthError,
    BudgetExceededError,
    LLMGatewayError,
    LLMUnavailableError,
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


@dataclass
class ProviderPool:
    """One provider and its endpoints (one per API key), used round-robin."""

    name: str
    endpoints: list[Endpoint]
    timeout_s: float
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

    async def generate_structured[T: BaseModel](
        self,
        request: LLMRequest,
        response_model: type[T],
        *,
        budget: RunBudget,
        input_tokens: int,
    ) -> LLMCallResult[T]:
        attempts = 0
        last_error: LLMGatewayError | None = None
        for pool in self._pools:
            transient_failures = 0
            while (
                attempts < MAX_ATTEMPTS_PER_CALL
                and transient_failures < MAX_TRANSIENT_PER_PROVIDER
            ):
                endpoint = pool.next_available(self._clock())
                if endpoint is None:
                    break
                reservation = budget.reserve(
                    input_tokens=input_tokens, output_tokens=request.max_output_tokens
                )
                attempts += 1
                # A failed attempt has no usage: its whole reservation stays
                # charged (SD §14).
                timeout_s = min(request.timeout_s, pool.timeout_s, budget.remaining_s())
                try:
                    result = await endpoint.client.generate_structured(
                        request.model_copy(update={"timeout_s": timeout_s}),
                        response_model,
                    )
                except RateLimitedError as exc:
                    _log_failure(pool.name, attempts, exc)
                    endpoint.cooldown_until = self._clock() + max(
                        exc.retry_after or 0.0, self._backoff(attempts)
                    )
                    last_error = exc
                    continue
                except AuthError as exc:
                    _log_failure(pool.name, attempts, exc)
                    endpoint.disabled = True
                    last_error = exc
                    continue
                except TransientProviderError as exc:
                    _log_failure(pool.name, attempts, exc)
                    last_error = exc
                    transient_failures += 1
                    if transient_failures < MAX_TRANSIENT_PER_PROVIDER:
                        await self._wait(self._backoff(attempts), budget)
                    continue
                except LLMGatewayError as exc:  # not retried: the caller decides
                    _log_failure(pool.name, attempts, exc)
                    raise
                logger.info(
                    "llm_attempt_succeeded",
                    provider=pool.name,
                    model=result.model,
                    attempt=attempts,
                    duration_ms=result.latency_ms,
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                )
                budget.settle(
                    reservation,
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                )
                return result
        raise LLMUnavailableError(
            last_error.error_code if last_error else "no_available_endpoint"
        )

    async def _wait(self, delay: float, budget: RunBudget) -> None:
        if delay >= budget.remaining_s():
            msg = "backoff does not fit the run deadline"
            raise BudgetExceededError(msg)
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
