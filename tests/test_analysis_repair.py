"""Job-wide durable repair through fresh services, real PostgreSQL and mock HTTP."""

# ruff: noqa: PLR2004 -- literal cost and request expectations

import asyncio
import json
import uuid
from dataclasses import replace
from typing import Any

import httpx
import pytest
from sqlalchemy import delete, select, update
from test_analysis_accounting import Execution, freeze_test_policy, reserve
from test_frozen_configuration import PROMPTS, runtime_profiles

from app.application.analysis_ports import AccountingIdentityError
from app.application.ports import BuiltContextPayload
from app.db.models import (
    ChunkResult,
    ContextPayload,
    ReviewJob,
    ReviewLlmAttempt,
    TaskLease,
)
from app.llm.tokens import ConservativeTokenCounter
from app.review.configuration import (
    ExecutionConfiguration,
    FrozenConfigurationReader,
    build_frozen_gateway,
)
from app.review.port_adapter import ChunkReviewFailedError, LlmGatewayAdapter
from app.review.prompt import PromptBuilder
from app.review.service import ReviewService
from app.review.validation import FindingValidator

pytest_plugins = ("test_analysis_accounting",)


def adapter(
    execution: Execution,
    config: ExecutionConfiguration,
    http: httpx.AsyncClient,
    *,
    attempt: int = 1,
) -> LlmGatewayAdapter:
    return LlmGatewayAdapter(
        ReviewService(
            build_frozen_gateway(config, runtime_profiles(), http),
            PromptBuilder.load(
                PROMPTS, config.prompt_version, expected_digest=config.prompt_digest
            ),
            FindingValidator(),
            ConservativeTokenCounter(),
            config.service_settings,
            frozen_configuration=config,
        ),
        execution.budget(attempt),
    )


async def payload(
    execution: Execution, config: ExecutionConfiguration, data: dict[str, Any]
) -> BuiltContextPayload:
    body = {
        k: v
        for k, v in data.items()
        if k not in {"schema_version", "review_id", "chunk_id"}
    }
    body["metadata"] = dict(
        body["metadata"], rules_version=str(config.repository_settings_id)
    )
    result = BuiltContextPayload(1, config.review_id, execution.chunk_id, body)
    async with execution.factory.begin() as session:
        await session.execute(
            update(ContextPayload)
            .where(ContextPayload.id == execution.chunk_id)
            .values(payload_body=body)
        )
    return result


async def test_format_repair_is_spent_across_worker_and_service_restart(
    execution: Execution, payload_data: dict[str, Any]
) -> None:
    await execution.store().begin(task=execution.task, lease=execution.lease)
    config = await FrozenConfigurationReader(
        execution.factory, PROMPTS, runtime_profiles()
    ).resolve(execution.lease.review_job_id)
    context = await payload(execution, config, payload_data)
    calls = []

    def response(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        content = (
            json.dumps({"summary": "repaired", "findings": [], "limitations": []})
            if len(calls) == 2
            else "{}"
        )
        return httpx.Response(
            200,
            json={
                "model": "test-model",
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
                "choices": [{"message": {"content": content}}],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as http:
        assert (
            await adapter(execution, config, http).review(context)
        ).summary == "repaired"
        async with execution.factory.begin() as session:
            await session.execute(
                update(TaskLease)
                .where(TaskLease.review_job_id == config.review_id)
                .values(fencing_token=2)
            )
        execution.lease = replace(execution.lease, fencing_token=2)
        await execution.store().begin(task=execution.task, lease=execution.lease)
        with pytest.raises(ChunkReviewFailedError) as error:
            await adapter(execution, config, http, attempt=2).review(context)
        assert error.value.outcome.error_code == "invalid_llm_output"
    assert len(calls) == 3
    snapshot = await execution.budget(2).snapshot()
    assert snapshot.requests_used == 3
    assert snapshot.input_tokens_used == 300


async def test_crash_after_claim_before_ledger_never_restores_repair(
    execution: Execution,
) -> None:

    await execution.store().begin(task=execution.task, lease=execution.lease)
    repair = uuid.uuid4()
    assert await execution.budget().claim_format_repair(
        repair_id=repair, chunk_id=execution.chunk_id
    )
    assert (await execution.budget().snapshot()).requests_used == 0
    async with execution.factory.begin() as session:
        await session.execute(
            update(TaskLease)
            .where(TaskLease.review_job_id == execution.lease.review_job_id)
            .values(fencing_token=2)
        )
    execution.lease = replace(execution.lease, fencing_token=2)
    await execution.store().begin(task=execution.task, lease=execution.lease)
    assert not await execution.budget(2).claim_format_repair(
        repair_id=uuid.uuid4(), chunk_id=execution.chunk_id
    )
    with pytest.raises(AccountingIdentityError, match="repair_claim_mismatch"):
        await reserve(
            execution, execution.budget(2), operation_id=repair, call_kind="repair"
        )
    assert (await execution.budget(2).snapshot()).requests_used == 0


async def test_concurrent_repair_claims_only_grant_one_operation(
    execution: Execution,
) -> None:

    await execution.store().begin(task=execution.task, lease=execution.lease)
    decisions = await asyncio.gather(
        *(
            execution.budget().claim_format_repair(
                repair_id=uuid.uuid4(), chunk_id=execution.chunk_id
            )
            for _ in range(2)
        )
    )
    assert sorted(decisions) == [False, True]


async def test_completed_context_is_not_inferred_again_by_fresh_service(
    execution: Execution,
    payload_data: dict[str, Any],
) -> None:

    await execution.store().begin(task=execution.task, lease=execution.lease)
    config = await FrozenConfigurationReader(
        execution.factory, PROMPTS, runtime_profiles()
    ).resolve(execution.lease.review_job_id)
    context = await payload(execution, config, payload_data)
    calls = []

    def response(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "model": "test-model",
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"summary":"saved","findings":[],"limitations":[]}'
                            )
                        }
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as http:
        result = await adapter(execution, config, http).review(context)
        async with execution.factory.begin() as session:
            session.add(
                ChunkResult(
                    review_job_id=config.review_id,
                    context_payload_id=context.chunk_id,
                    schema_version=1,
                    summary=result.summary,
                    limitations=[],
                    usage=None,
                    latency_ms=1,
                )
            )
        try:
            with pytest.raises(
                AccountingIdentityError, match="context_already_completed"
            ):
                await adapter(execution, config, http).review(context)
            assert len(calls) == 1
        finally:
            async with execution.factory.begin() as session:
                await session.execute(
                    delete(ChunkResult).where(
                        ChunkResult.review_job_id == config.review_id
                    )
                )


@pytest.mark.parametrize("cap", ["requests", "input", "output", "repair"])
async def test_distinct_contexts_and_fresh_services_share_job_allowances(
    execution: Execution,
    payload_data: dict[str, Any],
    cap: str,
) -> None:

    async with execution.factory.begin() as session:
        job = await session.get(ReviewJob, execution.lease.review_job_id)
        assert job is not None
        policy = dict(job.model_settings)
        limits = dict(policy["limits"])
        if cap == "requests":
            limits["run_max_requests"] = 1
        elif cap == "input":
            limits["run_max_input_tokens"] = 9000
        elif cap == "output":
            limits["run_max_output_tokens"] = 2000
        policy["limits"] = limits
        freeze_test_policy(job, policy)
    await execution.store().begin(task=execution.task, lease=execution.lease)
    config = await FrozenConfigurationReader(
        execution.factory, PROMPTS, runtime_profiles()
    ).resolve(execution.lease.review_job_id)
    first = await payload(execution, config, payload_data)
    second_id = uuid.uuid4()
    async with execution.factory.begin() as session:
        session.add(
            ContextPayload(
                id=second_id,
                review_job_id=config.review_id,
                schema_version=1,
                payload_body=first.payload_body,
            )
        )
    second = replace(first, chunk_id=second_id)
    calls = []

    def response(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        content = (
            "{}"
            if cap == "repair" and len(calls) != 2
            else '{"summary":"ok","findings":[],"limitations":[]}'
        )
        return httpx.Response(
            200,
            json={
                "model": "test-model",
                "usage": {
                    "prompt_tokens": 9000 if cap == "input" else 100,
                    "completion_tokens": 2000 if cap == "output" else 20,
                },
                "choices": [{"message": {"content": content}}],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as http:
        assert (await adapter(execution, config, http).review(first)).summary == "ok"
        with pytest.raises(ChunkReviewFailedError) as error:
            await adapter(execution, config, http).review(second)
        assert (
            error.value.outcome.error_code
            == (
                {
                    "repair": "invalid_llm_output",
                    "requests": "budget_requests_exhausted",
                    "input": "budget_input_exhausted",
                    "output": "budget_output_exhausted",
                }[cap]
            )
        )
    snapshot = await execution.budget().snapshot()
    assert len(calls) == (3 if cap == "repair" else 1)
    assert snapshot.requests_used == len(calls)


@pytest.mark.parametrize("request_cap", [3, 4])
async def test_one_logical_repair_retries_and_falls_back_within_shared_cap(
    execution: Execution,
    payload_data: dict[str, Any],
    request_cap: int,
) -> None:

    async with execution.factory.begin() as session:
        job = await session.get(ReviewJob, execution.lease.review_job_id)
        assert job is not None
        policy = dict(job.model_settings)
        policy["limits"] = dict(policy["limits"], run_max_requests=request_cap)
        policy["routing"] = dict(
            policy["routing"],
            fallback=[
                {
                    "profile_ref": "ollama/review-v1",
                    "model_ref": "fallback-model",
                    "model_digest": None,
                }
            ],
        )
        freeze_test_policy(job, policy)
    await execution.store().begin(task=execution.task, lease=execution.lease)
    config = await FrozenConfigurationReader(
        execution.factory, PROMPTS, runtime_profiles()
    ).resolve(execution.lease.review_job_id)
    context = await payload(execution, config, payload_data)
    calls = []

    def response(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(
                200,
                json={
                    "model": "test-model",
                    "choices": [{"message": {"content": "{}"}}],
                },
            )
        if request.url.host == "llm.invalid":
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={
                "model": "fallback-model",
                "message": {
                    "content": (
                        '{"summary":"fallback repair","findings":[],"limitations":[]}'
                    )
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as http:
        if request_cap == 3:
            with pytest.raises(ChunkReviewFailedError) as error:
                await adapter(execution, config, http).review(context)
            assert error.value.outcome.error_code == "budget_requests_exhausted"
        else:
            assert (
                await adapter(execution, config, http).review(context)
            ).summary == "fallback repair"
    assert len(calls) == request_cap
    async with execution.factory() as session:
        job = await session.get(ReviewJob, config.review_id)
        rows = (
            await session.scalars(
                select(ReviewLlmAttempt).where(
                    ReviewLlmAttempt.review_job_id == config.review_id,
                    ReviewLlmAttempt.call_kind == "repair",
                )
            )
        ).all()
        assert job is not None and job.format_repair_id is not None
        assert len(rows) == request_cap - 1
        assert {row.operation_id for row in rows} == {job.format_repair_id}
