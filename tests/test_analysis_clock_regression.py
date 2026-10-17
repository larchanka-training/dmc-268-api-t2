"""Durable settlement remains valid when PostgreSQL wall time moves backwards."""

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, update
from test_analysis_accounting import Execution, reserve

from app.db.models import ReviewLlmAttempt

pytest_plugins = ("test_analysis_accounting",)


@pytest.mark.parametrize("known_usage", [True, False], ids=["known", "unknown"])
async def test_settlement_after_clock_regression_preserves_debit_and_idempotency(
    execution: Execution, known_usage: bool
) -> None:
    decision = await execution.store().begin(task=execution.task, lease=execution.lease)
    assert decision.accepted
    budget = execution.budget()
    permit = await reserve(execution, budget)
    # reserve_call has committed: a separate connection sees the charged row.
    async with execution.factory() as observer:
        row = await observer.get(ReviewLlmAttempt, permit.reservation_id)
        assert row is not None and row.state == "RESERVED" and row.settled_at is None
    before = await budget.snapshot()
    assert (
        before.requests_used,
        before.input_tokens_used,
        before.output_tokens_used,
    ) == (1, 100, 2000)
    # Fixture simulation of a DB clock regression AFTER a committed reservation.
    # Change only this test's RESERVED timestamp, never the real host/database clock.
    async with execution.factory.begin() as fixture:
        future_reservation = await fixture.scalar(
            update(ReviewLlmAttempt)
            .where(
                ReviewLlmAttempt.id == permit.reservation_id,
                ReviewLlmAttempt.state == "RESERVED",
            )
            .values(reserved_at=func.clock_timestamp() + timedelta(hours=1))
            .returning(ReviewLlmAttempt.reserved_at)
        )
    assert future_reservation is not None
    async with execution.factory() as observer:
        now = await observer.scalar(select(func.clock_timestamp()))
        assert now is not None and now < future_reservation

    values: dict[str, Any] = {
        "reservation_id": permit.reservation_id,
        "input_tokens": 40 if known_usage else None,
        "output_tokens": 7 if known_usage else None,
        "outcome": "SUCCEEDED" if known_usage else "ERROR",
        "error_code": None if known_usage else "provider_timeout",
        "latency_ms": 5,
    }
    settled = await budget.settle_call(**values)
    assert (
        settled.requests_used,
        settled.input_tokens_used,
        settled.output_tokens_used,
    ) == ((1, 40, 7) if known_usage else (1, 100, 2000))
    async with execution.factory() as observer:
        row = await observer.get(ReviewLlmAttempt, permit.reservation_id)
        assert row is not None
        assert row.reserved_at == future_reservation
        assert row.settled_at == future_reservation
        assert row.state == values["outcome"]

    duplicate = await execution.budget().settle_call(**values)
    assert (
        duplicate.requests_used,
        duplicate.input_tokens_used,
        duplicate.output_tokens_used,
    ) == ((1, 40, 7) if known_usage else (1, 100, 2000))
    async with execution.factory() as observer:
        row = await observer.get(ReviewLlmAttempt, permit.reservation_id)
        assert row is not None and row.settled_at == future_reservation
