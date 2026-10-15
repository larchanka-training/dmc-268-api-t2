"""Factory execution authority is mandatory; durable budget is not injectable."""

import time
from dataclasses import replace

import httpx
import pytest
from test_analysis_accounting import Execution
from test_frozen_configuration import PROMPTS, runtime_profiles

from app.application.analysis_authority import (
    LiveAnalysisAuthority,
    current_analysis_authority,
)
from app.application.ports import LeaseLostError
from app.db.task_leases import PostgresTaskLeaseStore
from app.review.execution import FrozenAnalysisExecutionFactory

pytest_plugins = ("test_analysis_accounting",)


async def test_factory_rejects_detached_execution_authority(
    execution: Execution,
) -> None:
    await execution.store().begin(task=execution.task, lease=execution.lease)
    authority = LiveAnalysisAuthority(execution.lease, started=time.monotonic())
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(500))
    ) as http:
        factory = FrozenAnalysisExecutionFactory(
            execution.factory, PROMPTS, runtime_profiles(), http
        )
        with pytest.raises(LeaseLostError):
            await factory.create(
                task=execution.task,
                lease=execution.lease,
                analysis_attempt=1,
                authority=authority,
            )


async def test_factory_rejects_non_authoritative_analysis_attempt(
    execution: Execution,
) -> None:
    await execution.store().begin(task=execution.task, lease=execution.lease)
    authority = LiveAnalysisAuthority(execution.lease, started=time.monotonic())
    token = current_analysis_authority.set(authority)
    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(500))
        ) as http:
            factory = FrozenAnalysisExecutionFactory(
                execution.factory, PROMPTS, runtime_profiles(), http
            )
            with pytest.raises(LeaseLostError):
                await factory.create(
                    task=execution.task,
                    lease=execution.lease,
                    analysis_attempt=2,
                    authority=authority,
                )
    finally:
        current_analysis_authority.reset(token)


async def test_factory_rejects_lease_without_accepted_execution_fence(
    execution: Execution,
) -> None:

    await execution.store().begin(task=execution.task, lease=execution.lease)
    leases = PostgresTaskLeaseStore(execution.factory)
    assert await leases.release(execution.lease)
    successor = await leases.acquire(
        execution.lease.review_job_id, owner="not-yet-begun", task_kind="analyze"
    )
    assert successor is not None
    authority = LiveAnalysisAuthority(successor, started=time.monotonic())
    token = current_analysis_authority.set(authority)
    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(500))
        ) as http:
            factory = FrozenAnalysisExecutionFactory(
                execution.factory, PROMPTS, runtime_profiles(), http
            )
            with pytest.raises(LeaseLostError):
                await factory.create(
                    task=execution.task,
                    lease=successor,
                    analysis_attempt=1,
                    authority=authority,
                )
    finally:
        current_analysis_authority.reset(token)


async def test_factory_binds_current_signal_to_the_same_lease(
    execution: Execution,
) -> None:

    await execution.store().begin(task=execution.task, lease=execution.lease)
    other = replace(execution.lease, owner="another-execution")
    authority = LiveAnalysisAuthority(other, started=time.monotonic())
    token = current_analysis_authority.set(authority)
    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(500))
        ) as http:
            factory = FrozenAnalysisExecutionFactory(
                execution.factory, PROMPTS, runtime_profiles(), http
            )
            with pytest.raises(LeaseLostError):
                await factory.create(
                    task=execution.task,
                    lease=execution.lease,
                    analysis_attempt=1,
                    authority=authority,
                )
    finally:
        current_analysis_authority.reset(token)
