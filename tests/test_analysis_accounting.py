"""Durable accounting through public stores and real PostgreSQL transactions."""

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    AsyncSessionTransaction,
    async_sessionmaker,
)
from test_analysis_handler import BASE, HEAD, github_http
from test_frozen_configuration import PROMPTS, runtime_profiles
from test_postgres_integration import (
    PostgresHarness,
    _create_change_request,
    _create_repository,
    _create_settings,
)

from app.adapters.analysis_handler import PostgresAnalysisHandler
from app.application.analysis_authority import (
    LiveAnalysisAuthority,
    current_analysis_authority,
)
from app.application.analysis_ports import (
    AccountingIdentityError,
    CallPermit,
    ReviewRunBudgetPort,
)
from app.application.ports import (
    BuiltContextPayload,
    LeaseLostError,
    QueueTaskEnvelope,
    StartReviewCommand,
    TaskLeaseSnapshot,
)
from app.application.use_cases.start_review import compute_config_digest, start_review
from app.db.analysis_attempts import PostgresAnalysisAttemptStore
from app.db.llm_accounting import PostgresReviewRunBudget
from app.db.models import (
    ChangeRequest,
    ChunkResult,
    ContextPayload,
    OutboxEvent,
    Publication,
    Repository,
    RepositorySettings,
    ReviewEvent,
    ReviewJob,
    ReviewLlmAttempt,
    TaskLease,
)
from app.db.task_leases import PostgresTaskLeaseStore
from app.db.uow import SqlAlchemyUnitOfWork
from app.db.worker_tasks import QueueIdentityError
from app.domain.enums import ReviewReasonCode
from app.domain.frozen_configuration import (
    FrozenConfigurationError,
    ModelSettings,
    RepositoryInstructions,
    admission_policy,
)
from app.domain.settings import compute_repository_settings_digest, compute_rules_digest
from app.llm.client import ChatMessage, LLMRequest
from app.llm.errors import BudgetExceededError, SchemaValidationError
from app.llm.gateway import Endpoint, LLMGateway, ProviderPool
from app.llm.openai_compatible import OpenAICompatibleClient
from app.llm.tokens import ConservativeTokenCounter
from app.review.configuration import FrozenConfigurationReader, build_frozen_gateway
from app.review.port_adapter import ChunkReviewFailedError, LlmGatewayAdapter
from app.review.prompt import PromptBuilder
from app.review.schemas.context import ContextPayload as ReviewContextPayload
from app.review.schemas.result import LLMReviewOutput
from app.review.service import ReviewService
from app.review.validation import FindingValidator
from app.vcs.github import GithubClient

pytest_plugins = ("test_postgres_integration",)


def freeze_test_policy(job: ReviewJob, policy: dict[str, Any]) -> None:
    """Legitimate fixture changes materialize a consistent frozen identity."""
    ModelSettings.from_stored(policy)
    job.model_settings = policy
    job.config_digest = compute_config_digest(
        StartReviewCommand(
            job.change_request_id,
            job.repository_settings_id,
            job.requested_head_sha,
            job.rules_digest,
            job.trigger_type,
            job.initiator_user_id,
            job.model_ref,
            job.model_digest,
            job.model_settings,
            job.prompt_version,
            job.prompt_digest,
            "fixture",
        )
    )


@dataclass
class Execution:
    factory: async_sessionmaker[AsyncSession]
    task: QueueTaskEnvelope
    lease: TaskLeaseSnapshot
    chunk_id: uuid.UUID

    def budget(self, attempt: int = 1) -> PostgresReviewRunBudget:
        return PostgresReviewRunBudget(
            self.factory, task=self.task, lease=self.lease, analysis_attempt=attempt
        )

    def store(self) -> PostgresAnalysisAttemptStore:
        return PostgresAnalysisAttemptStore(self.factory)


@pytest_asyncio.fixture
async def execution(postgres: PostgresHarness) -> AsyncIterator[Execution]:
    async with postgres.engine.begin() as connection:
        repo = await _create_repository(postgres, connection)
        settings = await _create_settings(postgres, connection, repo)
        change = await _create_change_request(postgres, connection, repo)
        await connection.execute(
            update(RepositorySettings)
            .where(RepositorySettings.id == settings)
            .values(rules_digest=compute_rules_digest({"instructions": []}))
        )
    factory = async_sessionmaker(postgres.engine, expire_on_commit=False)
    async with factory() as session:
        saved = await session.get(RepositorySettings, settings)
        assert saved is not None
        settings_digest = compute_repository_settings_digest(
            id=saved.id,
            repository_id=saved.repository_id,
            rules=saved.rules,
            ignores=saved.ignores,
            output_language=saved.output_language,
            surrounding_lines=saved.surrounding_lines,
        )
    bundle = PromptBuilder.load(Path(".agents/reviewer"), "v1")
    assert bundle.prompt_digest is not None
    review = await start_review(
        StartReviewCommand(
            change,
            settings,
            "head-sha",
            compute_rules_digest({"instructions": []}),
            "manual",
            None,
            "test-model",
            None,
            admission_policy(settings_digest).model_dump(mode="json"),
            "v1",
            bundle.prompt_digest,
            "trace-accounting",
            frozen_configuration_version=1,
        ),
        unit_of_work=SqlAlchemyUnitOfWork(factory),
    )
    postgres.track("review_job", review)
    async with factory() as session:
        event = (
            await session.scalars(
                select(OutboxEvent).where(OutboxEvent.review_job_id == review)
            )
        ).one()
        task = QueueTaskEnvelope(
            1, str(event.id), str(review), "analyze", 1, event.trace_id
        )
    lease = await PostgresTaskLeaseStore(factory).acquire(
        review, owner="first", task_kind="analyze"
    )
    assert lease is not None
    try:
        chunk_id = uuid.uuid4()
        async with factory.begin() as session:
            session.add(
                ContextPayload(
                    id=chunk_id, review_job_id=review, schema_version=1, payload_body={}
                )
            )
        yield Execution(factory, task, lease, chunk_id)
    finally:
        async with factory.begin() as session:
            if (
                "review_llm_attempt"
                in __import__("app.db.base", fromlist=["Base"]).Base.metadata.tables
            ):
                await session.execute(
                    text("DELETE FROM review_llm_attempt WHERE review_job_id=:review"),
                    {"review": review},
                )
            for table in (
                ReviewEvent,
                ContextPayload,
                OutboxEvent,
                Publication,
                TaskLease,
            ):
                await session.execute(
                    delete(table).where(table.review_job_id == review)
                )


async def test_begin_uses_db_time_and_frozen_initial_deadline(
    execution: Execution,
) -> None:
    async with execution.factory() as session:
        before = await session.scalar(select(func.clock_timestamp()))
    decision = await execution.store().begin(task=execution.task, lease=execution.lease)
    async with execution.factory() as session:
        after = await session.scalar(select(func.clock_timestamp()))
    assert decision.accepted
    assert decision.analysis_attempt == 1
    assert before is not None and after is not None
    assert decision.started_at is not None and decision.analysis_deadline_at is not None
    assert before <= decision.started_at <= after
    assert decision.analysis_deadline_at - decision.started_at == timedelta(seconds=600)


async def test_begin_same_fence_is_idempotent_across_store_restart(
    execution: Execution,
) -> None:
    first = await execution.store().begin(task=execution.task, lease=execution.lease)
    restarted = await execution.store().begin(
        task=execution.task, lease=execution.lease
    )
    assert restarted == first


async def test_begin_legacy_configuration_has_no_execution_permission(
    execution: Execution,
) -> None:
    async with execution.factory.begin() as session:
        await session.execute(
            update(ReviewJob)
            .where(ReviewJob.id == execution.lease.review_job_id)
            .values(frozen_configuration_version=None)
        )
    decision = await execution.store().begin(task=execution.task, lease=execution.lease)
    assert not decision.accepted
    assert decision.error_code == "historical_budget_unknown"
    assert decision.analysis_attempt == 0
    assert decision.started_at is None and decision.analysis_deadline_at is None


async def test_begin_unsupported_policy_is_terminal_decision(
    execution: Execution,
) -> None:
    async with execution.factory.begin() as session:
        job = await session.get(ReviewJob, execution.lease.review_job_id)
        assert job is not None
        policy = dict(job.model_settings)
        policy.pop("limits")
        job.model_settings = policy
    decision = await execution.store().begin(task=execution.task, lease=execution.lease)
    assert not decision.accepted
    assert decision.error_code == "unsupported_model_settings"
    assert decision.analysis_attempt == 0


async def test_begin_rejects_valid_policy_with_stale_config_digest(
    execution: Execution,
) -> None:
    async with execution.factory.begin() as session:
        job = await session.get(ReviewJob, execution.lease.review_job_id)
        assert job is not None
        policy = dict(job.model_settings)
        policy["limits"] = dict(policy["limits"], run_max_requests=1)
        ModelSettings.from_stored(policy)  # A valid policy, but not the admitted one.
        job.model_settings = policy
    decision = await execution.store().begin(task=execution.task, lease=execution.lease)
    assert not decision.accepted
    assert decision.error_code == "config_digest_mismatch"
    assert decision.analysis_attempt == 0
    assert decision.started_at is None and decision.analysis_deadline_at is None
    async with execution.factory() as session:
        job = await session.get(ReviewJob, execution.lease.review_job_id)
        assert job is not None and job.analysis_attempts == 0


async def test_begin_rejects_non_authoritative_outbox(execution: Execution) -> None:

    with pytest.raises(QueueIdentityError):
        await execution.store().begin(
            task=replace(execution.task, trace_id="forged"), lease=execution.lease
        )


async def test_begin_expired_deadline_does_not_start_new_execution(
    execution: Execution,
) -> None:
    async with execution.factory.begin() as session:
        await session.execute(
            update(ReviewJob)
            .where(ReviewJob.id == execution.lease.review_job_id)
            .values(analysis_deadline_at=func.clock_timestamp() - timedelta(seconds=1))
        )
    decision = await execution.store().begin(task=execution.task, lease=execution.lease)
    assert not decision.accepted
    assert decision.error_code == "analysis_deadline_exceeded"
    assert decision.analysis_attempt == 0


async def test_begin_expired_queue_deadline_is_not_admitted(
    execution: Execution,
) -> None:
    async with execution.factory.begin() as session:
        await session.execute(
            update(ReviewJob)
            .where(ReviewJob.id == execution.lease.review_job_id)
            .values(queue_deadline_at=func.clock_timestamp() - timedelta(seconds=1))
        )
    decision = await execution.store().begin(task=execution.task, lease=execution.lease)
    assert not decision.accepted
    assert decision.error_code == "queue_deadline_exceeded"
    assert decision.analysis_attempt == 0


async def test_begin_execution_cap_survives_new_fences(execution: Execution) -> None:

    first = await execution.store().begin(task=execution.task, lease=execution.lease)
    for fence in (2, 3, 4):
        async with execution.factory.begin() as session:
            await session.execute(
                update(TaskLease)
                .where(TaskLease.review_job_id == execution.lease.review_job_id)
                .values(fencing_token=fence)
            )
        decision = await execution.store().begin(
            task=execution.task, lease=replace(execution.lease, fencing_token=fence)
        )
        assert decision.started_at == first.started_at
        assert decision.analysis_deadline_at == first.analysis_deadline_at
        if fence < 4:  # noqa: PLR2004 -- first rejected execution
            assert decision.accepted and decision.analysis_attempt == fence
        else:
            assert not decision.accepted
            assert decision.error_code == "analysis_attempts_exhausted"
            assert decision.analysis_attempt == 3  # noqa: PLR2004 -- frozen execution cap


async def wait_for_lock(factory: async_sessionmaker[AsyncSession], table: str) -> None:
    async with asyncio.timeout(5), factory() as session:
        while not await session.scalar(
            text(
                "SELECT EXISTS(SELECT 1 FROM pg_stat_activity "
                "WHERE pid <> pg_backend_pid() AND wait_event_type='Lock' "
                "AND cardinality(pg_blocking_pids(pid)) > 0 "
                "AND query LIKE '%' || :name || '%')"
            ),
            {"name": table},
        ):
            await session.commit()
            await asyncio.sleep(0.01)


async def expire_soon(execution: Execution, expiry: str) -> datetime:
    boundary = func.clock_timestamp() + timedelta(milliseconds=300)
    if expiry == "deadline":
        statement = (
            update(ReviewJob)
            .where(ReviewJob.id == execution.lease.review_job_id)
            .values(analysis_deadline_at=boundary)
            .returning(ReviewJob.analysis_deadline_at)
        )
    else:
        statement = (
            update(TaskLease)
            .where(TaskLease.review_job_id == execution.lease.review_job_id)
            .values(expires_at=boundary)
            .returning(TaskLease.expires_at)
        )
    async with execution.factory.begin() as session:
        expiration = await session.scalar(statement)
        assert isinstance(expiration, datetime)
        return expiration


async def wait_until_expired(
    factory: async_sessionmaker[AsyncSession], expiration: datetime
) -> None:
    async with asyncio.timeout(5), factory() as session:
        while True:
            now = await session.scalar(select(func.clock_timestamp()))
            assert isinstance(now, datetime)
            if now >= expiration:
                return
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("expiry", ["deadline", "lease"])
async def test_begin_outbox_lock_wait_rechecks_db_clock(
    execution: Execution, expiry: str
) -> None:
    expiration = await expire_soon(execution, expiry)
    async with execution.factory.begin() as blocker:
        await blocker.execute(
            select(OutboxEvent)
            .where(OutboxEvent.id == uuid.UUID(execution.task.event_id))
            .with_for_update()
        )
        pending = asyncio.create_task(
            execution.store().begin(task=execution.task, lease=execution.lease)
        )
        await wait_for_lock(execution.factory, "outbox_event")
        await wait_until_expired(execution.factory, expiration)
    if expiry == "lease":
        with pytest.raises(LeaseLostError):
            await pending
    else:
        decision = await pending
        assert not decision.accepted
        assert decision.error_code == "analysis_deadline_exceeded"


async def reserve(
    execution: Execution,
    budget: ReviewRunBudgetPort,
    *,
    reservation_id: uuid.UUID | None = None,
    operation_id: uuid.UUID | None = None,
    **changes: Any,
) -> CallPermit:
    values: dict[str, Any] = {
        "reservation_id": reservation_id or uuid.uuid4(),
        "chunk_id": execution.chunk_id,
        "operation_id": operation_id or uuid.uuid4(),
        "call_kind": "initial",
        "provider_profile_ref": "eurorouter/review-v1",
        "model_ref": "test-model",
        "model_digest": None,
        "input_tokens": 100,
        "output_tokens": 2000,
    }
    return await budget.reserve_call(**(values | changes))


async def test_reserve_rejects_valid_policy_changed_after_accepted_begin(
    execution: Execution,
) -> None:
    decision = await execution.store().begin(task=execution.task, lease=execution.lease)
    assert decision.accepted
    budget = execution.budget()
    permit = await reserve(execution, budget)
    async with execution.factory.begin() as session:
        job = await session.get(ReviewJob, execution.lease.review_job_id)
        assert job is not None
        policy = dict(job.model_settings)
        policy["limits"] = dict(
            policy["limits"], run_max_requests=9, max_input_tokens=11000
        )
        ModelSettings.from_stored(policy)
        job.model_settings = policy
    with pytest.raises(FrozenConfigurationError, match="config_digest_mismatch"):
        await reserve(execution, budget, input_tokens=10500)
    snapshot = await budget.snapshot()
    assert (
        snapshot.requests_used,
        snapshot.input_tokens_used,
        snapshot.output_tokens_used,
    ) == (1, 100, 2000)
    # Already authorized spend remains settleable under its original live fence.
    settled = await budget.settle_call(
        reservation_id=permit.reservation_id,
        input_tokens=40,
        output_tokens=None,
        outcome="SUCCEEDED",
        error_code=None,
        latency_ms=7,
    )
    assert (
        settled.requests_used,
        settled.input_tokens_used,
        settled.output_tokens_used,
    ) == (1, 40, 2000)


async def test_last_slot_is_durable_and_serialized_between_restarted_budgets(
    execution: Execution,
) -> None:

    async with execution.factory.begin() as session:
        job = await session.get(ReviewJob, execution.lease.review_job_id)
        assert job is not None
        policy = dict(job.model_settings)
        policy["limits"] = dict(policy["limits"], run_max_requests=1)
        freeze_test_policy(job, policy)
    await execution.store().begin(task=execution.task, lease=execution.lease)
    async with execution.factory.begin() as blocker:
        await blocker.execute(
            select(ReviewJob)
            .where(ReviewJob.id == execution.lease.review_job_id)
            .with_for_update()
        )
        left = asyncio.create_task(reserve(execution, execution.budget()))
        right = asyncio.create_task(reserve(execution, execution.budget()))
        # Both callers must actually reach the PostgreSQL serialization barrier.
        async with asyncio.timeout(5), execution.factory() as observer:
            while (
                await observer.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE wait_event_type='Lock' "
                        "AND cardinality(pg_blocking_pids(pid)) > 0 "
                        "AND query LIKE '%review_job%'"
                    )
                )
                < 2  # noqa: PLR2004 -- both competing callers
            ):
                for pending in (left, right):
                    if pending.done():
                        pending.result()
                await observer.commit()
                await asyncio.sleep(0.01)
    outcomes = await asyncio.gather(left, right, return_exceptions=True)
    assert sum(isinstance(result, BudgetExceededError) for result in outcomes) == 1
    permit = next(
        result for result in outcomes if not isinstance(result, BaseException)
    )
    assert permit.reservation_id is not None
    snapshot = await execution.budget().snapshot()
    assert (
        snapshot.requests_used,
        snapshot.input_tokens_used,
        snapshot.output_tokens_used,
    ) == (1, 100, 2000)
    with pytest.raises(BudgetExceededError):
        await reserve(execution, execution.budget())  # Crash before send cannot refund.


@pytest.mark.parametrize("defect", ["legacy", "attempt", "deadline", "terminal"])
async def test_reservation_requires_current_durable_execution(
    execution: Execution, defect: str
) -> None:

    await execution.store().begin(task=execution.task, lease=execution.lease)
    async with execution.factory.begin() as session:
        cases: dict[str, dict[str, Any]] = {
            "legacy": {"frozen_configuration_version": None},
            "attempt": {"analysis_attempts": 2},
            "deadline": {
                "analysis_deadline_at": func.clock_timestamp() - timedelta(seconds=1)
            },
            "terminal": {"status": "FAILED", "finished_at": func.clock_timestamp()},
        }
        values = cases[defect]
        await session.execute(
            update(ReviewJob)
            .where(ReviewJob.id == execution.lease.review_job_id)
            .values(**values)
        )
    with pytest.raises((LeaseLostError, FrozenConfigurationError, BudgetExceededError)):
        await reserve(execution, execution.budget())
    assert (await execution.budget().snapshot()).requests_used == 0


async def test_reservation_uuid_reuse_never_returns_another_permit(
    execution: Execution,
) -> None:

    await execution.store().begin(task=execution.task, lease=execution.lease)
    identity = uuid.uuid4()
    await reserve(execution, execution.budget(), reservation_id=identity)
    with pytest.raises(AccountingIdentityError):
        await reserve(execution, execution.budget(), reservation_id=identity)
    assert (await execution.budget().snapshot()).requests_used == 1


@pytest.mark.parametrize(
    "defect",
    [
        "chunk",
        "profile",
        "model",
        "output",
        "per_call_input",
        "run_input",
        "run_output",
        "repair",
    ],
)
async def test_reservation_validates_frozen_request_policy(
    execution: Execution, defect: str
) -> None:

    if defect in {"run_input", "run_output"}:
        async with execution.factory.begin() as session:
            job = await session.get(ReviewJob, execution.lease.review_job_id)
            assert job is not None
            policy = dict(job.model_settings)
            policy["limits"] = dict(
                policy["limits"],
                **{
                    "run_max_input_tokens"
                    if defect == "run_input"
                    else "run_max_output_tokens": 50
                },
            )
            freeze_test_policy(job, policy)
    await execution.store().begin(task=execution.task, lease=execution.lease)
    cases: dict[str, dict[str, Any]] = {
        "chunk": {"chunk_id": uuid.uuid4()},
        "profile": {"provider_profile_ref": "runtime/new"},
        "model": {"model_ref": "runtime-model"},
        "output": {"output_tokens": 1},
        "per_call_input": {"input_tokens": 10001},
        "run_input": {},
        "run_output": {},
        "repair": {"call_kind": "repair"},
    }
    changes = cases[defect]
    with pytest.raises((AccountingIdentityError, BudgetExceededError)):
        await reserve(execution, execution.budget(), **changes)
    assert (await execution.budget().snapshot()).requests_used == 0


@pytest.mark.parametrize("defect", ["chunk", "kind", "execution", "cap", "rebuild"])
async def test_logical_operation_cannot_change_identity_or_exceed_frozen_cap(
    execution: Execution, defect: str
) -> None:

    await execution.store().begin(task=execution.task, lease=execution.lease)
    operation = uuid.uuid4()
    kind = "overflow_rebuild" if defect in {"kind", "rebuild"} else "initial"
    await reserve(execution, execution.budget(), operation_id=operation, call_kind=kind)
    changes: dict[str, Any] = {"operation_id": operation, "call_kind": kind}
    if defect == "chunk":
        other = uuid.uuid4()
        async with execution.factory.begin() as session:
            session.add(
                ContextPayload(
                    id=other,
                    review_job_id=execution.lease.review_job_id,
                    schema_version=1,
                    payload_body={},
                )
            )
        changes["chunk_id"] = other
    elif defect == "kind":
        changes["call_kind"] = "initial"
    elif defect == "execution":
        async with execution.factory.begin() as session:
            await session.execute(
                update(TaskLease)
                .where(TaskLease.review_job_id == execution.lease.review_job_id)
                .values(fencing_token=2)
            )
        execution.lease = replace(execution.lease, fencing_token=2)
        await execution.store().begin(task=execution.task, lease=execution.lease)
    elif defect == "cap":
        await reserve(execution, execution.budget(), operation_id=operation)
        await reserve(execution, execution.budget(), operation_id=operation)
    elif defect == "rebuild":
        changes["operation_id"] = uuid.uuid4()
    with pytest.raises((AccountingIdentityError, BudgetExceededError)):
        await reserve(
            execution,
            execution.budget(attempt=2 if defect == "execution" else 1),
            **changes,
        )


@pytest.mark.parametrize("expiry", ["deadline", "lease"])
async def test_reservation_ledger_row_wait_rechecks_clock(
    execution: Execution, expiry: str
) -> None:

    await execution.store().begin(task=execution.task, lease=execution.lease)
    permit = await reserve(execution, execution.budget())
    expiration = await expire_soon(execution, expiry)
    async with execution.factory.begin() as blocker:
        await blocker.execute(
            text("SELECT id FROM review_llm_attempt WHERE id=:id FOR UPDATE"),
            {"id": permit.reservation_id},
        )
        pending = asyncio.create_task(reserve(execution, execution.budget()))
        await wait_for_lock(execution.factory, "review_llm_attempt")
        await wait_until_expired(execution.factory, expiration)
    with pytest.raises((LeaseLostError, BudgetExceededError)):
        await pending
    assert (await execution.budget().snapshot()).requests_used == 1


async def test_gateway_commits_before_http_and_restart_does_not_reset_allowance(
    execution: Execution,
) -> None:

    async with execution.factory() as session:
        job = await session.get(ReviewJob, execution.lease.review_job_id)
        assert job is not None
        policy = dict(job.model_settings)
        policy["limits"] = dict(policy["limits"], run_max_requests=1)
    # Update the complete frozen fixture identity, rather than using runtime overrides.
    async with execution.factory.begin() as session:
        job = await session.get(ReviewJob, execution.lease.review_job_id)
        assert job is not None
        job.model_settings = policy
        job.config_digest = compute_config_digest(
            StartReviewCommand(
                job.change_request_id,
                job.repository_settings_id,
                job.requested_head_sha,
                job.rules_digest,
                job.trigger_type,
                job.initiator_user_id,
                job.model_ref,
                job.model_digest,
                job.model_settings,
                job.prompt_version,
                job.prompt_digest,
                "fixture",
            )
        )
    await execution.store().begin(task=execution.task, lease=execution.lease)
    config = await FrozenConfigurationReader(
        execution.factory, PROMPTS, runtime_profiles()
    ).resolve(execution.lease.review_job_id)
    requests = []

    async def response(request: httpx.Request) -> httpx.Response:
        # A separate transaction observes the committed debit before any HTTP reply.
        assert (await execution.budget().snapshot()).requests_used == 1
        requests.append(request)
        return httpx.Response(
            200,
            json={"model": "test-model", "choices": [{"message": {"content": "{}"}}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as http:
        for expected in (SchemaValidationError, BudgetExceededError):
            gateway = build_frozen_gateway(config, runtime_profiles(), http)
            with pytest.raises(expected):
                await gateway.generate_structured(
                    LLMRequest(
                        messages=[ChatMessage(role="user", content="review")],
                        max_output_tokens=2000,
                        timeout_s=2,
                    ),
                    LLMReviewOutput,
                    budget=execution.budget(),
                    input_tokens=100,
                    chunk_id=execution.chunk_id,
                    operation_id=uuid.uuid4(),
                    call_kind="initial",
                )
    assert len(requests) == 1


async def test_settlement_persists_actual_cost_separate_from_result_uow(
    execution: Execution,
) -> None:
    await execution.store().begin(task=execution.task, lease=execution.lease)
    permit = await reserve(execution, execution.budget())
    snapshot = await execution.budget().settle_call(
        reservation_id=permit.reservation_id,
        input_tokens=40,
        output_tokens=20,
        outcome="SUCCEEDED",
        error_code=None,
        latency_ms=7,
    )
    # Simulate a later result transaction failing; the cost transaction has committed.
    async with execution.factory() as session, session.begin():
        await session.execute(
            update(ReviewJob)
            .where(ReviewJob.id == execution.lease.review_job_id)
            .values(final_summary="uncommitted result")
        )
        await session.rollback()
    restarted = await execution.budget().snapshot()
    assert (
        snapshot.requests_used,
        snapshot.input_tokens_used,
        snapshot.output_tokens_used,
    ) == (1, 40, 20)
    assert (
        restarted.requests_used,
        restarted.input_tokens_used,
        restarted.output_tokens_used,
    ) == (1, 40, 20)


async def test_settlement_duplicate_is_idempotent_and_conflict_is_rejected(
    execution: Execution,
) -> None:

    await execution.store().begin(task=execution.task, lease=execution.lease)
    permit = await reserve(execution, execution.budget())
    values: dict[str, Any] = {
        "reservation_id": permit.reservation_id,
        "input_tokens": 40,
        "output_tokens": 20,
        "outcome": "SUCCEEDED",
        "error_code": None,
        "latency_ms": 7,
    }
    await execution.budget().settle_call(**values)
    snapshot = await execution.budget().settle_call(**values)
    assert (snapshot.input_tokens_used, snapshot.output_tokens_used) == (40, 20)
    with pytest.raises(AccountingIdentityError):
        await execution.budget().settle_call(**(values | {"input_tokens": 1}))
    assert (await execution.budget().snapshot()).input_tokens_used == 40  # noqa: PLR2004 -- contract test vector


async def test_gateway_settles_success_before_returning_output(
    execution: Execution,
) -> None:

    await execution.store().begin(task=execution.task, lease=execution.lease)
    config = await FrozenConfigurationReader(
        execution.factory, PROMPTS, runtime_profiles()
    ).resolve(execution.lease.review_job_id)

    def response(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "test-model",
                "usage": {"prompt_tokens": 40, "completion_tokens": 20},
                "choices": [
                    {
                        "message": {
                            "content": '{"summary":"ok","findings":[],"limitations":[]}'
                        }
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as http:
        result = await build_frozen_gateway(
            config, runtime_profiles(), http
        ).generate_structured(
            LLMRequest(
                messages=[ChatMessage(role="user", content="review")],
                max_output_tokens=2000,
                timeout_s=2,
            ),
            LLMReviewOutput,
            budget=execution.budget(),
            input_tokens=100,
            chunk_id=execution.chunk_id,
            operation_id=uuid.uuid4(),
            call_kind="initial",
        )
    assert result.parsed.summary == "ok"
    snapshot = await execution.budget().snapshot()
    assert (snapshot.input_tokens_used, snapshot.output_tokens_used) == (40, 20)


@pytest.mark.parametrize("invalid", [None, True, -1, "0", 1.5, 2**63])
async def test_invalid_or_unknown_usage_preserves_each_reserved_dimension(
    execution: Execution, invalid: Any
) -> None:
    await execution.store().begin(task=execution.task, lease=execution.lease)
    permit = await reserve(execution, execution.budget())
    snapshot = await execution.budget().settle_call(
        reservation_id=permit.reservation_id,
        input_tokens=invalid,
        output_tokens=20,
        outcome="SUCCEEDED",
        error_code=None,
        latency_ms=7,
    )
    assert (snapshot.input_tokens_used, snapshot.output_tokens_used) == (100, 20)


@pytest.mark.parametrize("provider", ["eurorouter", "ollama"])
async def test_schema_invalid_paid_reply_preserves_known_usage(
    execution: Execution, provider: str
) -> None:

    async with execution.factory.begin() as session:
        job = await session.get(ReviewJob, execution.lease.review_job_id)
        assert job is not None
        policy = dict(job.model_settings)
        policy["routing"] = dict(
            policy["routing"], primary_profile_ref=f"{provider}/review-v1"
        )
        job.model_settings = policy
        job.config_digest = compute_config_digest(
            StartReviewCommand(
                job.change_request_id,
                job.repository_settings_id,
                job.requested_head_sha,
                job.rules_digest,
                job.trigger_type,
                job.initiator_user_id,
                job.model_ref,
                job.model_digest,
                job.model_settings,
                job.prompt_version,
                job.prompt_digest,
                "fixture",
            )
        )
    await execution.store().begin(task=execution.task, lease=execution.lease)
    config = await FrozenConfigurationReader(
        execution.factory, PROMPTS, runtime_profiles()
    ).resolve(execution.lease.review_job_id)
    body = {
        "model": "test-model",
        "usage": {"prompt_tokens": 40, "completion_tokens": 20},
        "prompt_eval_count": 40,
        "eval_count": 20,
        "choices": [{"message": {"content": "{}"}}],
        "message": {"content": "{}"},
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    ) as http:
        with pytest.raises(SchemaValidationError) as failure:
            await build_frozen_gateway(
                config, runtime_profiles(), http
            ).generate_structured(
                LLMRequest(
                    messages=[ChatMessage(role="user", content="review")],
                    max_output_tokens=2000,
                    timeout_s=2,
                ),
                LLMReviewOutput,
                budget=execution.budget(),
                input_tokens=100,
                chunk_id=execution.chunk_id,
                operation_id=uuid.uuid4(),
                call_kind="initial",
            )
    snapshot = await execution.budget().snapshot()
    assert (snapshot.input_tokens_used, snapshot.output_tokens_used) == (40, 20)
    assert (failure.value.input_tokens, failure.value.output_tokens) == (40, 20)


@pytest.mark.parametrize("version", ["v1", "v2"])
@pytest.mark.parametrize("per_call_limit", [10000, 3000])
async def test_service_reserves_complete_frozen_input_before_gateway_http(
    execution: Execution,
    payload_data: dict[str, Any],
    version: str,
    per_call_limit: int,
) -> None:
    bundle = PromptBuilder.load(PROMPTS, version)
    async with execution.factory.begin() as session:
        job = await session.get(ReviewJob, execution.lease.review_job_id)
        assert job is not None and bundle.prompt_digest is not None
        job.prompt_version = version
        job.prompt_digest = bundle.prompt_digest
        policy = dict(job.model_settings)
        policy["limits"] = dict(policy["limits"], max_input_tokens=per_call_limit)
        freeze_test_policy(job, policy)
    assert (
        await execution.store().begin(task=execution.task, lease=execution.lease)
    ).accepted
    config = await FrozenConfigurationReader(
        execution.factory, PROMPTS, runtime_profiles()
    ).resolve(execution.lease.review_job_id)
    body = {
        k: v
        for k, v in payload_data.items()
        if k not in {"schema_version", "review_id", "chunk_id"}
    }
    body["metadata"] = dict(
        body["metadata"], rules_version=str(config.repository_settings_id)
    )
    body["budget"] = dict(body["budget"], limit_input_tokens=per_call_limit)
    payload = BuiltContextPayload(1, config.review_id, execution.chunk_id, body)
    prompt = bundle.build(
        ReviewContextPayload.model_validate(payload.to_wire()),
        max_input_tokens=per_call_limit,
    )
    # Frozen schema: 1459 UTF-8 bytes => ceil(1459/3)=487 => ceil(487*1.15)=561.
    # This independent literal catches omitting the schema or counting it twice.
    expected_input = prompt.estimated_tokens + 561
    assert prompt.estimated_tokens <= per_call_limit
    assert (expected_input <= per_call_limit) == (per_call_limit == 10000)  # noqa: PLR2004 -- frozen full-input boundary
    requests = []

    async def response(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert json.loads(request.content)["response_format"]["json_schema"][
            "schema"
        ] == json.loads(bundle.output_schema_json)
        snapshot = await execution.budget().snapshot()
        assert snapshot.requests_used == 1
        if expected_input <= per_call_limit:
            assert snapshot.unknown_input_tokens == expected_input
        return httpx.Response(
            200,
            json={
                "model": "test-model",
                "choices": [
                    {
                        "message": {
                            "content": '{"summary":"ok","findings":[],"limitations":[]}'
                        }
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as http:
        adapter = LlmGatewayAdapter(
            ReviewService(
                build_frozen_gateway(config, runtime_profiles(), http),
                bundle,
                FindingValidator(),
                ConservativeTokenCounter(),
                config.service_settings,
                frozen_configuration=config,
            ),
            execution.budget(),
        )
        if expected_input > per_call_limit:
            with pytest.raises(ChunkReviewFailedError) as failure:
                await adapter.review(payload)
            assert failure.value.outcome.error_code == "budget_exceeded"
            assert requests == []
            assert (await execution.budget().snapshot()).requests_used == 0
        else:
            assert (await adapter.review(payload)).summary == "ok"
            assert len(requests) == 1
            snapshot = await execution.budget().snapshot()
            assert snapshot.unknown_input_tokens == expected_input
            assert snapshot.unknown_output_tokens == 2000  # noqa: PLR2004 -- frozen output reserve


async def test_actual_overrun_blocks_restarted_service_without_clamping(
    execution: Execution, payload_data: dict[str, Any]
) -> None:

    await execution.store().begin(task=execution.task, lease=execution.lease)
    config = await FrozenConfigurationReader(
        execution.factory, PROMPTS, runtime_profiles()
    ).resolve(execution.lease.review_job_id)
    body = dict(payload_data)
    body.pop("review_id")
    body.pop("chunk_id")
    body.pop("schema_version")
    body["metadata"] = dict(
        body["metadata"], rules_version=str(config.repository_settings_id)
    )
    payload = BuiltContextPayload(1, config.review_id, execution.chunk_id, body)
    requests = []

    def response(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "model": "test-model",
                "usage": {"prompt_tokens": 90000, "completion_tokens": 17000},
                "choices": [
                    {
                        "message": {
                            "content": '{"summary":"ok","findings":[],"limitations":[]}'
                        }
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as http:

        def adapter() -> LlmGatewayAdapter:
            return LlmGatewayAdapter(
                ReviewService(
                    build_frozen_gateway(config, runtime_profiles(), http),
                    PromptBuilder.load(
                        PROMPTS,
                        config.prompt_version,
                        expected_digest=config.prompt_digest,
                        repository_instructions=RepositoryInstructions(
                            instructions=config.instructions
                        ),
                    ),
                    FindingValidator(),
                    ConservativeTokenCounter(),
                    config.service_settings,
                    frozen_configuration=config,
                ),
                execution.budget(),
            )

        assert (await adapter().review(payload)).summary == "ok"
        snapshot = await execution.budget().snapshot()
        assert (snapshot.input_tokens_used, snapshot.output_tokens_used) == (
            90000,
            17000,
        )
        with pytest.raises(ChunkReviewFailedError) as failure:
            await adapter().review(payload)
    assert failure.value.outcome.error_code == "budget_exceeded"
    assert len(requests) == 1


@pytest.mark.parametrize("actor", ["stale", "successor"])
async def test_settlement_requires_original_live_fence(
    execution: Execution, actor: str
) -> None:

    await execution.store().begin(task=execution.task, lease=execution.lease)
    old_budget = execution.budget()
    permit = await reserve(execution, old_budget)
    leases = PostgresTaskLeaseStore(execution.factory)
    assert await leases.release(execution.lease)
    successor = await leases.acquire(
        execution.lease.review_job_id, owner="successor", task_kind="analyze"
    )
    assert successor is not None and successor.fencing_token == 2  # noqa: PLR2004 -- contract test vector
    await execution.store().begin(task=execution.task, lease=successor)
    acting_budget = (
        old_budget
        if actor == "stale"
        else replace(execution, lease=successor).budget(attempt=2)
    )
    with pytest.raises((LeaseLostError, AccountingIdentityError)):
        await acting_budget.settle_call(
            reservation_id=permit.reservation_id,
            input_tokens=40,
            output_tokens=20,
            outcome="SUCCEEDED",
            error_code=None,
            latency_ms=7,
        )
    snapshot = await acting_budget.snapshot()
    assert (
        snapshot.requests_used,
        snapshot.input_tokens_used,
        snapshot.output_tokens_used,
    ) == (1, 100, 2000)
    with pytest.raises(LeaseLostError):
        await reserve(execution, old_budget)


async def test_handler_discards_paid_answer_after_lease_changes(
    execution: Execution,
) -> None:

    async with execution.factory.begin() as session:
        job = await session.get(ReviewJob, execution.lease.review_job_id)
        assert job is not None
        job.requested_head_sha = HEAD
        job.config_digest = compute_config_digest(
            StartReviewCommand(
                job.change_request_id,
                job.repository_settings_id,
                job.requested_head_sha,
                job.rules_digest,
                job.trigger_type,
                job.initiator_user_id,
                job.model_ref,
                job.model_digest,
                job.model_settings,
                job.prompt_version,
                job.prompt_digest,
                "fixture",
            )
        )
        await session.execute(
            update(Repository)
            .where(Repository.id == job.repository_id)
            .values(owner="team", repo_name="api", external_id="42")
        )
        await session.execute(
            update(ChangeRequest)
            .where(ChangeRequest.id == job.change_request_id)
            .values(
                external_number="13",
                head_sha=HEAD,
                base_sha=BASE,
                source_repository_external_id="43",
            )
        )
        await session.execute(
            delete(ContextPayload).where(ContextPayload.review_job_id == job.id)
        )
    config = await FrozenConfigurationReader(
        execution.factory, PROMPTS, runtime_profiles()
    ).resolve(execution.lease.review_job_id)
    response_started, release_response = asyncio.Event(), asyncio.Event()

    async def response(request: httpx.Request) -> httpx.Response:
        response_started.set()
        await release_response.wait()
        return httpx.Response(
            200,
            json={
                "model": "test-model",
                "usage": {"prompt_tokens": 40, "completion_tokens": 20},
                "choices": [
                    {
                        "message": {
                            "content": '{"summary":"ok","findings":[],"limitations":[]}'
                        }
                    }
                ],
            },
        )

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(response)) as http,
        httpx.AsyncClient(transport=github_http()) as github,
    ):
        gateway = build_frozen_gateway(config, runtime_profiles(), http)
        adapter = LlmGatewayAdapter(
            ReviewService(
                gateway,
                PromptBuilder.load(
                    PROMPTS,
                    config.prompt_version,
                    expected_digest=config.prompt_digest,
                    repository_instructions=RepositoryInstructions(
                        instructions=config.instructions
                    ),
                ),
                FindingValidator(),
                ConservativeTokenCounter(),
                config.service_settings,
                frozen_configuration=config,
            ),
            execution.budget(),
        )
        handler = PostgresAnalysisHandler(
            execution.factory,
            GithubClient(github),
            adapter,
            schemas_dir=Path("schemas"),
            limit_input_tokens=10000,
            reserved_output_tokens=2000,
        )
        pending = asyncio.create_task(handler.handle(execution.task, execution.lease))
        try:
            await asyncio.wait_for(response_started.wait(), timeout=5)
            before = await execution.budget().snapshot()
            leases = PostgresTaskLeaseStore(execution.factory)
            assert await leases.release(execution.lease)
            successor = await leases.acquire(
                config.review_id, owner="new-worker", task_kind="analyze"
            )
            assert successor is not None and successor.fencing_token == 2  # noqa: PLR2004 -- contract test vector
            release_response.set()
            with pytest.raises(LeaseLostError):
                await pending
            after = await execution.budget().snapshot()
            assert (
                after.requests_used,
                after.input_tokens_used,
                after.output_tokens_used,
            ) == (
                before.requests_used,
                before.input_tokens_used,
                before.output_tokens_used,
            )
            async with execution.factory() as session:
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(ChunkResult)
                        .where(ChunkResult.review_job_id == config.review_id)
                    )
                    == 0
                )
            with pytest.raises(LeaseLostError):
                await reserve(execution, execution.budget())
        finally:
            release_response.set()
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("expiry", ["deadline", "lease"])
@pytest.mark.parametrize("lock_kind", ["relation", "row"])
async def test_settlement_lock_wait_uses_current_lease_but_allows_late_accounting(
    execution: Execution, expiry: str, lock_kind: str
) -> None:

    await execution.store().begin(task=execution.task, lease=execution.lease)
    permit = await reserve(execution, execution.budget())
    expiration = await expire_soon(execution, expiry)
    async with execution.factory.begin() as blocker:
        if lock_kind == "relation":
            await blocker.execute(text("LOCK TABLE review_llm_attempt IN SHARE MODE"))
        else:
            await blocker.execute(
                text("SELECT id FROM review_llm_attempt WHERE id=:id FOR UPDATE"),
                {"id": permit.reservation_id},
            )
        pending = asyncio.create_task(
            execution.budget().settle_call(
                reservation_id=permit.reservation_id,
                input_tokens=40,
                output_tokens=20,
                outcome="SUCCEEDED",
                error_code=None,
                latency_ms=7,
            )
        )
        await wait_for_lock(execution.factory, "review_llm_attempt")
        await wait_until_expired(execution.factory, expiration)
    if expiry == "lease":
        with pytest.raises(LeaseLostError):
            await pending
        snapshot = await execution.budget().snapshot()
        assert (snapshot.input_tokens_used, snapshot.output_tokens_used) == (100, 2000)
    else:
        snapshot = await pending
        assert (snapshot.input_tokens_used, snapshot.output_tokens_used) == (40, 20)


async def test_snapshot_distinguishes_known_actuals_and_unknown_reserves(
    execution: Execution,
) -> None:
    await execution.store().begin(task=execution.task, lease=execution.lease)
    first = await reserve(execution, execution.budget())
    await reserve(execution, execution.budget())
    snapshot = await execution.budget().settle_call(
        reservation_id=first.reservation_id,
        input_tokens=40,
        output_tokens=None,
        outcome="ERROR",
        error_code="invalid_llm_output",
        latency_ms=7,
    )
    assert (snapshot.input_tokens_used, snapshot.output_tokens_used) == (140, 4000)
    assert (snapshot.actual_input_tokens, snapshot.actual_output_tokens) == (40, 0)
    assert (snapshot.unknown_input_tokens, snapshot.unknown_output_tokens) == (
        100,
        4000,
    )


async def test_ambiguous_commit_receipt_never_authorizes_http(
    execution: Execution,
) -> None:

    class LostCommitReceipt(AsyncSessionTransaction):
        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            traceback: TracebackType | None,
        ) -> None:
            await super().__aexit__(exc_type, exc, traceback)
            if exc_type is None:
                raise ConnectionError("commit receipt lost")

    class ReceiptLostSession(AsyncSession):
        def begin(self) -> AsyncSessionTransaction:
            return LostCommitReceipt(self)

    await execution.store().begin(task=execution.task, lease=execution.lease)
    config = await FrozenConfigurationReader(
        execution.factory, PROMPTS, runtime_profiles()
    ).resolve(execution.lease.review_job_id)
    failing_factory = async_sessionmaker[AsyncSession](
        execution.factory.kw["bind"], class_=ReceiptLostSession, expire_on_commit=False
    )
    failing_budget = PostgresReviewRunBudget(
        failing_factory, task=execution.task, lease=execution.lease, analysis_attempt=1
    )
    requests = []

    def response(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as http:
        with pytest.raises(ConnectionError):
            await build_frozen_gateway(
                config, runtime_profiles(), http
            ).generate_structured(
                LLMRequest(
                    messages=[ChatMessage(role="user", content="review")],
                    max_output_tokens=2000,
                    timeout_s=2,
                ),
                LLMReviewOutput,
                budget=failing_budget,
                input_tokens=100,
                chunk_id=execution.chunk_id,
                operation_id=uuid.uuid4(),
                call_kind="initial",
            )
    assert requests == []
    snapshot = await execution.budget().snapshot()
    assert (
        snapshot.requests_used,
        snapshot.input_tokens_used,
        snapshot.output_tokens_used,
    ) == (1, 100, 2000)


@pytest.mark.parametrize("phase", ["reserve", "settle"])
async def test_crash_at_db_boundary_preserves_only_committed_reservation(
    execution: Execution, phase: str
) -> None:

    await execution.store().begin(task=execution.task, lease=execution.lease)
    config = await FrozenConfigurationReader(
        execution.factory, PROMPTS, runtime_profiles()
    ).resolve(execution.lease.review_job_id)
    started, release_response = asyncio.Event(), asyncio.Event()

    async def response(request: httpx.Request) -> httpx.Response:
        started.set()
        await release_response.wait()
        return httpx.Response(
            200,
            json={
                "model": "test-model",
                "usage": {"prompt_tokens": 40, "completion_tokens": 20},
                "choices": [
                    {
                        "message": {
                            "content": '{"summary":"ok","findings":[],"limitations":[]}'
                        }
                    }
                ],
            },
        )

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(response)) as http,
        execution.factory.begin() as blocker,
    ):
        if phase == "reserve":
            await blocker.execute(text("LOCK TABLE review_llm_attempt IN SHARE MODE"))
        pending = asyncio.create_task(
            build_frozen_gateway(config, runtime_profiles(), http).generate_structured(
                LLMRequest(
                    messages=[ChatMessage(role="user", content="review")],
                    max_output_tokens=2000,
                    timeout_s=2,
                ),
                LLMReviewOutput,
                budget=execution.budget(),
                input_tokens=100,
                chunk_id=execution.chunk_id,
                operation_id=uuid.uuid4(),
                call_kind="initial",
            )
        )
        try:
            if phase == "settle":
                await asyncio.wait_for(started.wait(), 5)
                await blocker.execute(
                    text("LOCK TABLE review_llm_attempt IN SHARE MODE")
                )
                release_response.set()
            await wait_for_lock(execution.factory, "review_llm_attempt")
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        finally:
            release_response.set()
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
    snapshot = await execution.budget().snapshot()
    if phase == "reserve":
        assert not started.is_set() and snapshot.requests_used == 0
    else:
        assert (
            snapshot.requests_used,
            snapshot.input_tokens_used,
            snapshot.output_tokens_used,
        ) == (1, 100, 2000)
        assert snapshot.actual_input_tokens == snapshot.actual_output_tokens == 0
        # Redelivery pays for a fresh call, preserving the orphaned row.
        await reserve(execution, execution.budget())
        assert (await execution.budget().snapshot()).requests_used == 2  # noqa: PLR2004 -- contract test vector


@pytest.mark.parametrize("expiry", ["deadline", "lease"])
async def test_unique_index_wait_cannot_issue_expired_permit(
    execution: Execution, expiry: str
) -> None:

    await execution.store().begin(task=execution.task, lease=execution.lease)
    other_id, event_id, chunk_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with execution.factory.begin() as session:
        original = await session.get(ReviewJob, execution.lease.review_job_id)
        assert original is not None
        values = {
            column.name: getattr(original, column.name)
            for column in ReviewJob.__table__.columns
        }
        values.update(
            id=other_id,
            requested_head_sha="other-head",
            started_at=None,
            analysis_deadline_at=None,
            analysis_attempts=0,
            analysis_attempt_fence=None,
        )
        other_job = ReviewJob(**values)
        freeze_test_policy(other_job, other_job.model_settings)
        session.add(other_job)
        await session.flush()
        session.add(
            OutboxEvent(
                id=event_id,
                review_job_id=other_id,
                schema_version=1,
                task_kind="analyze",
                attempt=1,
                trace_id="other-trace",
            )
        )
        session.add(
            ContextPayload(
                id=chunk_id, review_job_id=other_id, schema_version=1, payload_body={}
            )
        )
    other_lease = await PostgresTaskLeaseStore(execution.factory).acquire(
        other_id, owner="other", task_kind="analyze"
    )
    assert other_lease is not None
    other = Execution(
        execution.factory,
        QueueTaskEnvelope(1, str(event_id), str(other_id), "analyze", 1, "other-trace"),
        other_lease,
        chunk_id,
    )
    pending = None
    try:
        await other.store().begin(task=other.task, lease=other.lease)
        expiration = await expire_soon(other, expiry)
        identity = uuid.uuid4()
        async with execution.factory() as blocker:
            await blocker.begin()
            blocker.add(
                ReviewLlmAttempt(
                    id=identity,
                    review_job_id=execution.lease.review_job_id,
                    chunk_id=execution.chunk_id,
                    operation_id=uuid.uuid4(),
                    call_kind="initial",
                    analysis_attempt=1,
                    lease_owner=execution.lease.owner,
                    fencing_token=execution.lease.fencing_token,
                    provider_profile_ref="eurorouter/review-v1",
                    model_ref="test-model",
                    model_digest=None,
                    reserved_input_tokens=100,
                    reserved_output_tokens=2000,
                )
            )
            await blocker.flush()
            pending = asyncio.create_task(
                reserve(other, other.budget(), reservation_id=identity)
            )
            await wait_for_lock(execution.factory, "INSERT INTO review_llm_attempt")
            await wait_until_expired(execution.factory, expiration)
            await (
                blocker.rollback()
            )  # UUID becomes free only after B's authority expires.
        with pytest.raises((LeaseLostError, BudgetExceededError)):
            await pending
        assert (await other.budget().snapshot()).requests_used == 0
    finally:
        if pending is not None:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        async with execution.factory.begin() as session:
            for table in (ReviewLlmAttempt, ContextPayload, OutboxEvent, TaskLease):
                await session.execute(
                    delete(table).where(table.review_job_id == other_id)
                )
            await session.execute(delete(ReviewJob).where(ReviewJob.id == other_id))


async def test_deadline_rejection_precedes_future_retry_gate(
    execution: Execution,
) -> None:
    await execution.store().begin(task=execution.task, lease=execution.lease)
    async with execution.factory.begin() as session:
        await session.execute(
            update(ReviewJob)
            .where(ReviewJob.id == execution.lease.review_job_id)
            .values(
                retry_at=func.clock_timestamp() + timedelta(minutes=1),
                analysis_deadline_at=func.clock_timestamp() - timedelta(seconds=1),
            )
        )
    decision = await execution.store().begin(task=execution.task, lease=execution.lease)
    assert not decision.accepted and decision.error_code == "analysis_deadline_exceeded"
    assert decision.analysis_attempt == 1


@pytest.mark.parametrize("loss", ["fence", "signal", "deadline"])
async def test_gateway_stops_after_503_when_authority_changes(
    execution: Execution,
    loss: str,
) -> None:

    await execution.store().begin(task=execution.task, lease=execution.lease)
    signal = LiveAnalysisAuthority(execution.lease, started=time.monotonic())
    calls = []

    async def response(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(503)

    async def backoff(delay: float) -> None:
        if loss == "signal":
            signal.cancel()
        elif loss == "deadline":
            async with execution.factory.begin() as session:
                await session.execute(
                    update(ReviewJob)
                    .where(ReviewJob.id == execution.lease.review_job_id)
                    .values(
                        analysis_deadline_at=func.clock_timestamp()
                        - timedelta(seconds=1)
                    )
                )
        else:
            async with execution.factory.begin() as session:
                await session.execute(
                    update(TaskLease)
                    .where(TaskLease.review_job_id == execution.lease.review_job_id)
                    .values(fencing_token=2)
                )

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as http:
        client = OpenAICompatibleClient(
            name="eurorouter",
            http=http,
            base_url="https://llm.invalid/v1",
            model="test-model",
            api_key="test",
        )
        gateway = LLMGateway(
            [
                ProviderPool(
                    "eurorouter",
                    [Endpoint(client, expected_model="test-model")],
                    120,
                    profile_ref="eurorouter/review-v1",
                )
            ],
            sleep=backoff,
        )
        token = current_analysis_authority.set(signal)
        try:
            with pytest.raises(
                BudgetExceededError if loss == "deadline" else LeaseLostError
            ):
                await gateway.generate_structured(
                    LLMRequest(
                        messages=[ChatMessage(role="user", content="review")],
                        max_output_tokens=2000,
                        timeout_s=120,
                    ),
                    LLMReviewOutput,
                    budget=execution.budget(),
                    input_tokens=100,
                    chunk_id=execution.chunk_id,
                    operation_id=uuid.uuid4(),
                )
        finally:
            current_analysis_authority.reset(token)
    assert len(calls) == 1
    assert (await execution.budget().snapshot()).requests_used == 1


async def test_reservation_round_trip_consumes_local_deadline_before_dispatch(
    execution: Execution,
) -> None:

    clock = [0.0]

    class DelayedReceipt(AsyncSessionTransaction):
        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            traceback: TracebackType | None,
        ) -> None:
            await super().__aexit__(exc_type, exc, traceback)
            clock[0] = 601.0

    class DelayedSession(AsyncSession):
        def begin(self) -> AsyncSessionTransaction:
            return DelayedReceipt(self)

    await execution.store().begin(task=execution.task, lease=execution.lease)
    factory = async_sessionmaker[AsyncSession](
        execution.factory.kw["bind"], class_=DelayedSession, expire_on_commit=False
    )
    budget = PostgresReviewRunBudget(
        factory, task=execution.task, lease=execution.lease, analysis_attempt=1
    )
    requests = []

    def response(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "model": "test-model",
                "choices": [
                    {
                        "message": {
                            "content": '{"summary":"ok","findings":[],"limitations":[]}'
                        }
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as http:
        client = OpenAICompatibleClient(
            name="eurorouter",
            http=http,
            base_url="https://llm.invalid/v1",
            model="test-model",
            api_key="test",
        )
        gateway = LLMGateway(
            [
                ProviderPool(
                    "eurorouter",
                    [Endpoint(client, expected_model="test-model")],
                    120,
                    profile_ref="eurorouter/review-v1",
                )
            ],
            clock=lambda: clock[0],
        )
        with pytest.raises(BudgetExceededError):
            await gateway.generate_structured(
                LLMRequest(
                    messages=[ChatMessage(role="user", content="review")],
                    max_output_tokens=2000,
                    timeout_s=120,
                ),
                LLMReviewOutput,
                budget=budget,
                input_tokens=100,
                chunk_id=execution.chunk_id,
                operation_id=uuid.uuid4(),
            )
    assert requests == []
    assert (await execution.budget().snapshot()).requests_used == 1


@pytest.mark.parametrize("answer", ["valid", "invalid"])
async def test_locally_cancelled_paid_response_cannot_settle_or_retry(
    execution: Execution, answer: str
) -> None:
    await execution.store().begin(task=execution.task, lease=execution.lease)
    config = await FrozenConfigurationReader(
        execution.factory, PROMPTS, runtime_profiles()
    ).resolve(execution.lease.review_job_id)
    signal = LiveAnalysisAuthority(execution.lease, started=time.monotonic())
    calls = []

    async def response(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        signal.cancel()  # renewal failure races a provider returning usable output
        return httpx.Response(
            200,
            json={
                "model": "test-model",
                "usage": {"prompt_tokens": 40, "completion_tokens": 20},
                "choices": [
                    {
                        "message": {
                            "content": (
                                "{}"
                                if answer == "invalid"
                                else '{"summary":"ok","findings":[],"limitations":[]}'
                            )
                        }
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as http:
        token = current_analysis_authority.set(signal)
        try:
            with pytest.raises(LeaseLostError):
                await build_frozen_gateway(
                    config, runtime_profiles(), http
                ).generate_structured(
                    LLMRequest(
                        messages=[ChatMessage(role="user", content="review")],
                        max_output_tokens=2000,
                        timeout_s=120,
                    ),
                    LLMReviewOutput,
                    budget=execution.budget(),
                    input_tokens=100,
                    chunk_id=execution.chunk_id,
                    operation_id=uuid.uuid4(),
                )
        finally:
            current_analysis_authority.reset(token)
    snapshot = await execution.budget().snapshot()
    assert len(calls) == 1
    assert (
        snapshot.requests_used,
        snapshot.input_tokens_used,
        snapshot.output_tokens_used,
    ) == (1, 100, 2000)


@pytest.mark.parametrize("operation", ["reserve", "repair"])
async def test_future_retry_at_denies_direct_durable_reservation(
    execution: Execution, operation: str
) -> None:

    await execution.store().begin(task=execution.task, lease=execution.lease)
    async with execution.factory.begin() as session:
        await session.execute(
            update(ReviewJob)
            .where(ReviewJob.id == execution.lease.review_job_id)
            .values(status="LLM_PROCESSING")
        )
    assert await execution.store().defer_retry(
        task=execution.task,
        lease=execution.lease,
        reason=ReviewReasonCode.LLM_UNAVAILABLE,
        error_code="provider_unavailable",
        delay_s=20,
    )
    with pytest.raises(BudgetExceededError, match="retry deferred"):
        if operation == "reserve":
            await reserve(execution, execution.budget())
        else:
            await execution.budget().claim_format_repair(
                repair_id=uuid.uuid4(), chunk_id=execution.chunk_id
            )
    assert (await execution.budget().snapshot()).requests_used == 0


@pytest.mark.parametrize("operation", ["repair", "defer"])
@pytest.mark.parametrize("expiry", ["lease", "deadline"])
async def test_repair_and_defer_lock_wait_resample_authority(
    execution: Execution,
    operation: str,
    expiry: str,
) -> None:

    await execution.store().begin(task=execution.task, lease=execution.lease)
    async with execution.factory.begin() as session:
        await session.execute(
            update(ReviewJob)
            .where(ReviewJob.id == execution.lease.review_job_id)
            .values(status="LLM_PROCESSING")
        )
    expiration = await expire_soon(execution, expiry)
    async with execution.factory.begin() as blocker:
        await blocker.execute(text("LOCK TABLE review_job IN SHARE MODE"))
        if operation == "repair":
            pending = asyncio.create_task(
                execution.budget().claim_format_repair(
                    repair_id=uuid.uuid4(), chunk_id=execution.chunk_id
                )
            )
        else:
            pending = asyncio.create_task(
                execution.store().defer_retry(
                    task=execution.task,
                    lease=execution.lease,
                    reason=ReviewReasonCode.LLM_UNAVAILABLE,
                    error_code="provider_unavailable",
                    delay_s=2,
                )
            )
        await wait_for_lock(execution.factory, "review_job")
        await wait_until_expired(execution.factory, expiration)
    if operation == "defer" and expiry == "deadline":
        assert await pending is False
    else:
        with pytest.raises((LeaseLostError, BudgetExceededError)):
            await pending
    async with execution.factory() as session:
        job = await session.get(ReviewJob, execution.lease.review_job_id)
        assert job is not None and job.retry_at is None and job.format_repair_id is None
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ReviewEvent)
                .where(ReviewEvent.review_job_id == job.id)
            )
            == 0
        )
