"""Run budget shared by every chunk of one review run (SD §14, spec §4.5)."""

import time
from collections.abc import Callable
from dataclasses import dataclass, replace

from app.core.config import LLM
from app.llm.errors import BudgetExceededError


@dataclass(frozen=True)
class BudgetLimits:
    max_requests: int
    max_input_tokens: int
    max_output_tokens: int
    deadline_s: float
    max_format_repairs: int = 1

    @classmethod
    def from_settings(cls, settings: LLM) -> BudgetLimits:
        return cls(
            max_requests=settings.run_max_requests,
            max_input_tokens=settings.run_max_input_tokens,
            max_output_tokens=settings.run_max_output_tokens,
            deadline_s=settings.run_deadline_s,
        )


@dataclass(frozen=True)
class BudgetState:
    """Serializable budget usage, so a later ticket can persist it (SD §9)."""

    started_at: float
    requests_used: int = 0
    input_tokens_used: int = 0
    output_tokens_used: int = 0
    format_repairs_used: int = 0


@dataclass(frozen=True)
class Reservation:
    input_tokens: int
    output_tokens: int


class RunBudget:
    """In-memory run budget.

    The clock is wall time so a saved state stays meaningful after a worker
    restart.
    """

    def __init__(
        self,
        limits: BudgetLimits,
        *,
        clock: Callable[[], float] = time.time,
        state: BudgetState | None = None,
    ) -> None:
        self._limits = limits
        self._clock = clock
        self._state = state or BudgetState(started_at=clock())

    def state(self) -> BudgetState:
        return self._state

    def reserve(self, *, input_tokens: int, output_tokens: int) -> Reservation:
        """Account one request and its token estimate before sending it."""
        limits, used = self._limits, self._state
        if self.remaining_s() <= 0:
            msg = "run deadline exceeded"
            raise BudgetExceededError(msg)
        if used.requests_used + 1 > limits.max_requests:
            msg = "run request limit exhausted"
            raise BudgetExceededError(msg)
        if used.input_tokens_used + input_tokens > limits.max_input_tokens:
            msg = "run input token limit exhausted"
            raise BudgetExceededError(msg)
        if used.output_tokens_used + output_tokens > limits.max_output_tokens:
            msg = "run output token limit exhausted"
            raise BudgetExceededError(msg)
        self._state = replace(
            used,
            requests_used=used.requests_used + 1,
            input_tokens_used=used.input_tokens_used + input_tokens,
            output_tokens_used=used.output_tokens_used + output_tokens,
        )
        return Reservation(input_tokens, output_tokens)

    def settle(
        self,
        reservation: Reservation,
        *,
        input_tokens: int | None,
        output_tokens: int | None,
    ) -> None:
        """Replace the reservation with actual usage; unknown usage keeps it."""
        used = self._state
        input_delta = (
            0 if input_tokens is None else input_tokens - reservation.input_tokens
        )
        output_delta = (
            0 if output_tokens is None else output_tokens - reservation.output_tokens
        )
        self._state = replace(
            used,
            input_tokens_used=used.input_tokens_used + input_delta,
            output_tokens_used=used.output_tokens_used + output_delta,
        )

    def try_use_format_repair(self) -> bool:
        """Consume the run's format-repair allowance; False when spent."""
        used = self._state
        if used.format_repairs_used >= self._limits.max_format_repairs:
            return False
        self._state = replace(used, format_repairs_used=used.format_repairs_used + 1)
        return True

    def remaining_s(self) -> float:
        """Seconds left until the run deadline (may be negative)."""
        return self._state.started_at + self._limits.deadline_s - self._clock()
