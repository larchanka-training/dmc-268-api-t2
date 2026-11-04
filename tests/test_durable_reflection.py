"""Durable Reflection at admission, accounting and actual worker boundaries."""

# ruff: noqa: PLR2004 -- fixed accounting/retry/fence contract vectors

import asyncio
from dataclasses import replace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select
from test_analysis_accounting import Execution, freeze_test_policy
from test_frozen_configuration import PROMPTS
from test_postgres_integration import PostgresHarness

from app.application.analysis_ports import AccountingIdentityError
from app.application.ports import LeaseLostError, StartReviewCommand
from app.application.use_cases.start_review import start_review
from app.db.models import ContextPayload, OutboxEvent, Publication, ReviewJob
from app.db.task_leases import PostgresTaskLeaseStore
from app.db.uow import SqlAlchemyUnitOfWork
from app.llm.errors import BudgetExceededError
from app.review.prompt import PromptBuilder, ReflectionBundle

pytest_plugins = ("test_postgres_integration", "test_analysis_accounting")


def policy_two(values: dict[str, Any], *, allowance: int = 1) -> dict[str, Any]:
    return values | {
        "schema_version": 2,
        "reflection": {
            "max_reflections_per_run": allowance,
            "max_reflections_per_chunk": 1,
            "failure_mode": "fail_open",
            "verifier_version": "v1",
            "verifier_digest": ReflectionBundle.load(
                PROMPTS / "reflection", "v1"
            ).digest,
        },
    }


async def test_admission_persists_explicit_policy_two_and_preserves_old_job(
    execution: Execution,
    postgres: PostgresHarness,
) -> None:
    async with execution.factory() as session:
        old = await session.get(ReviewJob, execution.lease.review_job_id)
        assert old is not None
        historical = dict(old.model_settings)
        policy = policy_two(historical)
        bundle = PromptBuilder.load(PROMPTS, "v4")
        assert bundle.prompt_digest is not None
        command = StartReviewCommand(
            old.change_request_id,
            old.repository_settings_id,
            old.requested_head_sha,
            old.rules_digest,
            "manual",
            None,
            old.model_ref,
            None,
            policy,
            "v4",
            bundle.prompt_digest,
            "reflection-test",
            frozen_configuration_version=1,
        )
    review = await start_review(
        command, unit_of_work=SqlAlchemyUnitOfWork(execution.factory)
    )
    postgres.track("review_job", review)
    async with execution.factory() as session:
        saved = await session.get(ReviewJob, review)
        assert saved is not None and saved.model_settings == policy
        old = await session.get(ReviewJob, execution.lease.review_job_id)
        assert old is not None and old.model_settings == historical
        for publication in await session.scalars(
            select(Publication).where(Publication.review_job_id == review)
        ):
            postgres.track("publication", publication.id)
        for event in await session.scalars(
            select(OutboxEvent).where(OutboxEvent.review_job_id == review)
        ):
            postgres.track("outbox_event", event.id)


async def enable_reflection(execution: Execution, *, allowance: int = 1) -> None:
    async with execution.factory.begin() as session:
        job = await session.get(ReviewJob, execution.lease.review_job_id)
        assert job is not None
        job.prompt_version = "v4"
        digest = PromptBuilder.load(PROMPTS, "v4").prompt_digest
        assert digest is not None
        job.prompt_digest = digest
        freeze_test_policy(job, policy_two(job.model_settings, allowance=allowance))


async def test_durable_reflection_reservation_is_committed_and_counted(
    execution: Execution,
) -> None:

    await enable_reflection(execution)
    assert (
        await execution.store().begin(task=execution.task, lease=execution.lease)
    ).accepted
    permit = await execution.budget().reserve_call(
        reservation_id=uuid4(),
        chunk_id=execution.chunk_id,
        operation_id=uuid4(),
        call_kind="reflection",
        provider_profile_ref="eurorouter/review-v1",
        model_ref="test-model",
        model_digest=None,
        input_tokens=100,
        output_tokens=2000,
    )
    assert permit.reservation_id is not None
    snapshot = await execution.budget().snapshot()
    assert (
        snapshot.requests_used,
        snapshot.unknown_input_tokens,
        snapshot.unknown_output_tokens,
    ) == (1, 100, 2000)


async def test_historical_policy_cannot_reserve_reflection(
    execution: Execution,
) -> None:

    assert (
        await execution.store().begin(task=execution.task, lease=execution.lease)
    ).accepted
    with pytest.raises(BudgetExceededError, match="reflection"):
        await execution.budget().reserve_call(
            reservation_id=uuid4(),
            chunk_id=execution.chunk_id,
            operation_id=uuid4(),
            call_kind="reflection",
            provider_profile_ref="eurorouter/review-v1",
            model_ref="test-model",
            model_digest=None,
            input_tokens=100,
            output_tokens=2000,
        )
    assert (await execution.budget().snapshot()).requests_used == 0


async def reserve_reflection(
    execution: Execution, *, chunk_id: Any = None, operation_id: Any = None
) -> Any:
    return await execution.budget().reserve_call(
        reservation_id=uuid4(),
        chunk_id=chunk_id or execution.chunk_id,
        operation_id=operation_id or uuid4(),
        call_kind="reflection",
        provider_profile_ref="eurorouter/review-v1",
        model_ref="test-model",
        model_digest=None,
        input_tokens=100,
        output_tokens=2000,
    )


async def test_restarted_budget_cannot_verify_same_chunk_again(
    execution: Execution,
) -> None:

    await enable_reflection(execution, allowance=2)
    assert (
        await execution.store().begin(task=execution.task, lease=execution.lease)
    ).accepted
    await reserve_reflection(execution)
    with pytest.raises(BudgetExceededError, match="reflection"):
        await reserve_reflection(execution)
    assert (await execution.budget().snapshot()).requests_used == 1


async def test_global_reflection_allowance_is_atomic_between_chunks(
    execution: Execution,
) -> None:

    await enable_reflection(execution)
    assert (
        await execution.store().begin(task=execution.task, lease=execution.lease)
    ).accepted
    other = uuid4()
    async with execution.factory.begin() as session:
        session.add(
            ContextPayload(
                id=other,
                review_job_id=execution.lease.review_job_id,
                schema_version=1,
                payload_body={},
            )
        )
    results = await asyncio.gather(
        reserve_reflection(execution),
        reserve_reflection(execution, chunk_id=other),
        return_exceptions=True,
    )
    assert sum(isinstance(result, BudgetExceededError) for result in results) == 1
    assert (await execution.budget().snapshot()).requests_used == 1


async def test_reflection_provider_retries_share_one_logical_allowance(
    execution: Execution,
) -> None:

    await enable_reflection(execution)
    assert (
        await execution.store().begin(task=execution.task, lease=execution.lease)
    ).accepted
    operation = uuid4()
    for _ in range(3):
        await reserve_reflection(execution, operation_id=operation)
    with pytest.raises(BudgetExceededError, match="operation request limit"):
        await reserve_reflection(execution, operation_id=operation)
    assert (await execution.budget().snapshot()).requests_used == 3


async def test_new_fence_cannot_reset_reflection_or_settle_old_receipt(
    execution: Execution,
) -> None:

    await enable_reflection(execution)
    assert (
        await execution.store().begin(task=execution.task, lease=execution.lease)
    ).accepted
    permit = await reserve_reflection(execution)
    leases = PostgresTaskLeaseStore(execution.factory)
    assert await leases.release(execution.lease)
    successor = await leases.acquire(
        execution.lease.review_job_id, owner="successor", task_kind="analyze"
    )
    assert successor is not None
    assert (
        await execution.store().begin(task=execution.task, lease=successor)
    ).analysis_attempt == 2
    restarted = replace(execution, lease=successor)
    with pytest.raises(BudgetExceededError, match="reflection"):
        await restarted.budget(attempt=2).reserve_call(
            reservation_id=uuid4(),
            chunk_id=execution.chunk_id,
            operation_id=uuid4(),
            call_kind="reflection",
            provider_profile_ref="eurorouter/review-v1",
            model_ref="test-model",
            model_digest=None,
            input_tokens=100,
            output_tokens=2000,
        )
    for budget in (execution.budget(), restarted.budget(attempt=2)):
        with pytest.raises((LeaseLostError, AccountingIdentityError)):
            await budget.settle_call(
                reservation_id=permit.reservation_id,
                input_tokens=40,
                output_tokens=20,
                outcome="SUCCEEDED",
                error_code=None,
                latency_ms=7,
            )
    snapshot = await restarted.budget(attempt=2).snapshot()
    assert (
        snapshot.requests_used,
        snapshot.unknown_input_tokens,
        snapshot.unknown_output_tokens,
    ) == (1, 100, 2000)
