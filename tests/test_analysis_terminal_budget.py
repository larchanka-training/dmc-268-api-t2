"""Precise durable deadline refusal, independent of exception text."""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, update
from test_analysis_accounting import Execution, reserve

from app.db.models import ReviewJob
from app.llm.errors import BudgetExceededError

pytest_plugins = ("test_analysis_accounting",)


@pytest.mark.parametrize("operation", ["reserve", "repair"])
async def test_durable_deadline_error_has_precise_code(
    execution: Execution, operation: str
) -> None:
    await execution.store().begin(task=execution.task, lease=execution.lease)
    async with execution.factory.begin() as session:
        await session.execute(
            update(ReviewJob)
            .where(ReviewJob.id == execution.lease.review_job_id)
            .values(analysis_deadline_at=func.clock_timestamp() - timedelta(seconds=1))
        )
    with pytest.raises(BudgetExceededError) as failure:
        if operation == "reserve":
            await reserve(execution, execution.budget())
        else:
            await execution.budget().claim_format_repair(
                repair_id=uuid.uuid4(), chunk_id=execution.chunk_id
            )
    assert failure.value.error_code == "analysis_deadline_exceeded"
