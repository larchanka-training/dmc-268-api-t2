"""Process-local circuit breaker for calls to overloaded external services.

State lives in memory: each worker process protects its own outbound calls.
Durable retry scheduling stays with the callers' stores.
"""

import time
from collections.abc import Callable
from typing import Literal

BreakerState = Literal["closed", "open", "half_open"]


class CircuitBreaker:
    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        reset_timeout_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._threshold = failure_threshold
        self._reset_s = reset_timeout_s
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None
        self._probe_at: float | None = None

    @property
    def state(self) -> BreakerState:
        if self._opened_at is None:
            return "closed"
        return "half_open" if self._probe_at is not None else "open"

    @property
    def retry_after_s(self) -> float:
        if self._opened_at is None:
            return 0.0
        return max(0.0, self._opened_at + self._reset_s - self._clock())

    def allow(self) -> bool:
        """Closed admits all calls; after the reset period one probe is admitted.

        A probe whose outcome is never recorded is replaced after another period.
        """
        if self._opened_at is None:
            return True
        now = self._clock()
        if self._probe_at is not None:
            if now < self._probe_at + self._reset_s:
                return False
        elif self.retry_after_s > 0:
            return False
        self._probe_at = now
        return True

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None
        self._probe_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._probe_at is not None or self._failures >= self._threshold:
            self._opened_at = self._clock()
            self._probe_at = None


class BreakerRegistry:
    """One breaker per external dependency, shared by short-lived clients."""

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        reset_timeout_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._threshold = failure_threshold
        self._reset_s = reset_timeout_s
        self._clock = clock
        self._breakers: dict[str, CircuitBreaker] = {}

    def get(self, key: str) -> CircuitBreaker:
        breaker = self._breakers.get(key)
        if breaker is None:
            breaker = CircuitBreaker(
                failure_threshold=self._threshold,
                reset_timeout_s=self._reset_s,
                clock=self._clock,
            )
            self._breakers[key] = breaker
        return breaker
