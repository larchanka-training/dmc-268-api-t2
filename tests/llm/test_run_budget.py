"""Shared per-run budget (SD §14, spec §4.5)."""

import pytest

from app.core.config import LLM, Ollama
from app.llm.budget import BudgetLimits, RunBudget
from app.llm.errors import BudgetExceededError

_LIMITS = BudgetLimits(
    max_requests=2,
    max_input_tokens=1000,
    max_output_tokens=500,
    deadline_s=600,
    max_format_repairs=1,
)


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def test_request_limit_is_checked_before_sending() -> None:
    budget = RunBudget(_LIMITS, clock=_Clock())
    budget.reserve(input_tokens=10, output_tokens=10)
    budget.reserve(input_tokens=10, output_tokens=10)

    with pytest.raises(BudgetExceededError):
        budget.reserve(input_tokens=10, output_tokens=10)


def test_reservation_is_replaced_by_actual_usage() -> None:
    budget = RunBudget(_LIMITS, clock=_Clock())
    reservation = budget.reserve(input_tokens=900, output_tokens=400)
    budget.settle(reservation, input_tokens=100, output_tokens=50)

    # 100 + 900 fits the 1000 input limit only because the first reservation
    # was replaced by the actual usage.
    budget.reserve(input_tokens=900, output_tokens=400)


def test_unknown_usage_charges_the_whole_reservation() -> None:
    budget = RunBudget(_LIMITS, clock=_Clock())
    reservation = budget.reserve(input_tokens=600, output_tokens=100)
    budget.settle(reservation, input_tokens=None, output_tokens=None)

    with pytest.raises(BudgetExceededError):
        budget.reserve(input_tokens=600, output_tokens=100)


def test_deadline_counts_from_run_start() -> None:
    clock = _Clock()
    budget = RunBudget(_LIMITS, clock=clock)

    clock.now += 100
    assert budget.remaining_s() == pytest.approx(500)

    clock.now += 500
    with pytest.raises(BudgetExceededError):
        budget.reserve(input_tokens=10, output_tokens=10)


def test_format_repair_is_allowed_once_per_run() -> None:
    budget = RunBudget(_LIMITS, clock=_Clock())

    assert budget.try_use_format_repair() is True
    assert budget.try_use_format_repair() is False


def test_state_can_be_saved_and_restored() -> None:
    clock = _Clock()
    budget = RunBudget(_LIMITS, clock=clock)
    budget.reserve(input_tokens=700, output_tokens=100)
    budget.try_use_format_repair()

    restored = RunBudget(_LIMITS, clock=clock, state=budget.state())

    assert restored.try_use_format_repair() is False
    with pytest.raises(BudgetExceededError):
        restored.reserve(input_tokens=400, output_tokens=10)


def test_limits_come_from_llm_settings() -> None:
    settings = LLM(
        primary_provider="ollama",
        fallback_enabled=False,
        ollama=Ollama(model="qwen"),
        run_max_requests=4,
        run_deadline_s=120,
    )

    limits = BudgetLimits.from_settings(settings)

    assert limits == BudgetLimits(
        max_requests=4,
        max_input_tokens=80_000,
        max_output_tokens=16_000,
        deadline_s=120,
        max_format_repairs=1,
    )
