"""Short committed PostgreSQL transactions; no HTTP/result UoW ownership."""

from uuid import UUID

from sqlalchemy import exists, func, insert, literal, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.analysis_ports import (
    AccountingIdentityError,
    CallPermit,
    DurableBudgetSnapshot,
)
from app.application.ports import LeaseLostError, QueueTaskEnvelope, TaskLeaseSnapshot
from app.db.analysis_authority import (
    live_fence,
    lock_analysis,
    validate_config_identity,
)
from app.db.models import ChunkResult, ContextPayload, ReviewJob, ReviewLlmAttempt
from app.domain.frozen_configuration import FrozenConfigurationError, ModelSettings
from app.llm.client import known_token_usage
from app.llm.errors import BudgetExceededError


class PostgresReviewRunBudget:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        task: QueueTaskEnvelope,
        lease: TaskLeaseSnapshot,
        analysis_attempt: int,
    ) -> None:
        self._factory = factory
        self._task = task
        self._lease = lease
        self._attempt = analysis_attempt

    async def remaining_s(self) -> float:
        snapshot = await self.snapshot()
        if snapshot.analysis_deadline_at is None:
            raise BudgetExceededError("analysis execution has not begun")
        return (snapshot.analysis_deadline_at - snapshot.sampled_at).total_seconds()

    async def snapshot(self) -> DurableBudgetSnapshot:
        async with self._factory() as session:
            return await self._snapshot(session)

    async def _snapshot(self, session: AsyncSession) -> DurableBudgetSnapshot:
        requests, actual_inputs, actual_outputs, unknown_inputs, unknown_outputs = (
            await session.execute(
                select(
                    func.count(ReviewLlmAttempt.id),
                    func.coalesce(func.sum(ReviewLlmAttempt.actual_input_tokens), 0),
                    func.coalesce(func.sum(ReviewLlmAttempt.actual_output_tokens), 0),
                    func.coalesce(
                        func.sum(ReviewLlmAttempt.reserved_input_tokens).filter(
                            ReviewLlmAttempt.actual_input_tokens.is_(None)
                        ),
                        0,
                    ),
                    func.coalesce(
                        func.sum(ReviewLlmAttempt.reserved_output_tokens).filter(
                            ReviewLlmAttempt.actual_output_tokens.is_(None)
                        ),
                        0,
                    ),
                ).where(ReviewLlmAttempt.review_job_id == self._lease.review_job_id)
            )
        ).one()
        deadline = await session.scalar(
            select(ReviewJob.analysis_deadline_at).where(
                ReviewJob.id == self._lease.review_job_id
            )
        )
        now = await session.scalar(select(func.clock_timestamp()))
        assert now is not None
        return DurableBudgetSnapshot(
            requests,
            int(actual_inputs + unknown_inputs),
            int(actual_outputs + unknown_outputs),
            deadline,
            now,
            int(actual_inputs),
            int(actual_outputs),
            int(unknown_inputs),
            int(unknown_outputs),
        )

    async def reserve_call(  # noqa: PLR0913, PLR0912, PLR0915 -- complete immutable HTTP provenance
        self,
        *,
        reservation_id: UUID,
        chunk_id: UUID,
        operation_id: UUID,
        call_kind: str,
        provider_profile_ref: str,
        model_ref: str,
        model_digest: str | None,
        input_tokens: int,
        output_tokens: int,
    ) -> CallPermit:
        async with self._factory.begin() as session:
            job, lease, now = await lock_analysis(
                session, self._task, self._lease, ledger=True
            )
            if job.frozen_configuration_version != 1:
                raise FrozenConfigurationError("historical_budget_unknown")
            if (
                job.finished_at is not None
                or job.analysis_attempts != self._attempt
                or job.analysis_attempt_fence != self._lease.fencing_token
            ):
                raise LeaseLostError("Analysis execution is no longer current")
            if job.analysis_deadline_at is None or job.analysis_deadline_at <= now:
                raise BudgetExceededError("analysis deadline exceeded")
            if job.retry_at is not None and job.retry_at > now:
                raise BudgetExceededError("analysis retry deferred")
            if await session.get(ReviewLlmAttempt, reservation_id) is not None:
                raise AccountingIdentityError("reservation_already_exists")
            policy = ModelSettings.from_stored(job.model_settings)
            validate_config_identity(job)
            rows = (
                await session.scalars(
                    select(ReviewLlmAttempt)
                    .where(ReviewLlmAttempt.review_job_id == job.id)
                    .order_by(ReviewLlmAttempt.id)
                    .with_for_update()
                )
            ).all()
            operation = [row for row in rows if row.operation_id == operation_id]
            if any(
                (row.chunk_id, row.call_kind, row.analysis_attempt, row.fencing_token)
                != (chunk_id, call_kind, self._attempt, self._lease.fencing_token)
                for row in operation
            ):
                raise AccountingIdentityError("operation_identity_mismatch")
            if len(operation) >= policy.retry.provider_max_attempts_per_operation:
                raise BudgetExceededError("operation request limit exhausted")
            if call_kind == "overflow_rebuild" and any(
                row.chunk_id == chunk_id
                and row.call_kind == "overflow_rebuild"
                and row.operation_id != operation_id
                for row in rows
            ):
                raise AccountingIdentityError("overflow_rebuild_already_used")
            limits = policy.limits
            if not await session.scalar(
                select(ContextPayload.id).where(
                    ContextPayload.id == chunk_id,
                    ContextPayload.review_job_id == job.id,
                )
            ):
                raise AccountingIdentityError("context_identity_mismatch")
            if await session.scalar(
                select(ChunkResult.id).where(
                    ChunkResult.context_payload_id == chunk_id,
                    ChunkResult.review_job_id == job.id,
                )
            ):
                raise AccountingIdentityError("context_already_completed")
            routes = {
                (policy.routing.primary_profile_ref, job.model_ref, job.model_digest),
                *(
                    (route.profile_ref, route.model_ref, route.model_digest)
                    for route in policy.routing.fallback
                ),
            }
            if (provider_profile_ref, model_ref, model_digest) not in routes:
                raise AccountingIdentityError("model_route_mismatch")
            if call_kind == "repair" and (
                job.format_repair_id,
                job.format_repair_fence,
                job.format_repair_chunk_id,
            ) != (operation_id, self._lease.fencing_token, chunk_id):
                raise AccountingIdentityError("repair_claim_mismatch")
            if call_kind not in {"initial", "overflow_rebuild", "repair"}:
                raise AccountingIdentityError("unsupported_call_kind")
            if (
                type(input_tokens) is not int
                or type(output_tokens) is not int
                or input_tokens < 0
                or input_tokens > limits.max_input_tokens
                or output_tokens != limits.max_output_tokens
            ):
                raise BudgetExceededError("invalid per-call token reservation")
            used = await self._snapshot(session)
            if used.input_tokens_used + input_tokens > limits.run_max_input_tokens:
                raise BudgetExceededError("run input token limit exhausted")
            if used.output_tokens_used + output_tokens > limits.run_max_output_tokens:
                raise BudgetExceededError("run output token limit exhausted")
            if used.requests_used >= policy.limits.run_max_requests:
                raise BudgetExceededError("run request limit exhausted")
            final_now = await session.scalar(select(func.clock_timestamp()))
            assert final_now is not None
            if lease.expires_at <= final_now:
                raise LeaseLostError("Reservation lease expired during lock wait")
            if job.analysis_deadline_at <= final_now:
                raise BudgetExceededError("analysis deadline exceeded")
            row_id = await session.scalar(
                insert(ReviewLlmAttempt)
                .from_select(
                    [
                        "id",
                        "review_job_id",
                        "chunk_id",
                        "operation_id",
                        "call_kind",
                        "analysis_attempt",
                        "lease_owner",
                        "fencing_token",
                        "provider_profile_ref",
                        "model_ref",
                        "model_digest",
                        "reserved_input_tokens",
                        "reserved_output_tokens",
                        "reserved_at",
                    ],
                    select(
                        literal(reservation_id),
                        literal(job.id),
                        literal(chunk_id),
                        literal(operation_id),
                        literal(call_kind),
                        literal(self._attempt),
                        literal(self._lease.owner),
                        literal(self._lease.fencing_token),
                        literal(provider_profile_ref),
                        literal(model_ref),
                        literal(model_digest),
                        literal(input_tokens),
                        literal(output_tokens),
                        func.clock_timestamp(),
                    ).where(
                        ReviewJob.id == job.id,
                        ReviewJob.finished_at.is_(None),
                        ReviewJob.analysis_attempts == self._attempt,
                        ReviewJob.analysis_attempt_fence == self._lease.fencing_token,
                        ReviewJob.analysis_deadline_at > func.clock_timestamp(),
                        (
                            ReviewJob.retry_at.is_(None)
                            | (ReviewJob.retry_at <= func.clock_timestamp())
                        ),
                        live_fence(self._lease),
                    ),
                )
                .returning(ReviewLlmAttempt.id)
            )
            if row_id is None:
                raise LeaseLostError("Reservation authority expired")
            # A competing UUID can delay INSERT at the unique index even after
            # relation/row locks. Admission is finalized only after that wait.
            admitted_at = await session.scalar(
                update(ReviewLlmAttempt)
                .where(
                    ReviewLlmAttempt.id == row_id,
                    live_fence(self._lease),
                    exists().where(
                        ReviewJob.id == job.id,
                        ReviewJob.finished_at.is_(None),
                        ReviewJob.analysis_attempts == self._attempt,
                        ReviewJob.analysis_attempt_fence == self._lease.fencing_token,
                        ReviewJob.analysis_deadline_at > func.clock_timestamp(),
                        (
                            ReviewJob.retry_at.is_(None)
                            | (ReviewJob.retry_at <= func.clock_timestamp())
                        ),
                    ),
                )
                .values(reserved_at=func.clock_timestamp())
                .returning(ReviewLlmAttempt.reserved_at)
            )
            if admitted_at is None:
                raise LeaseLostError("Reservation authority expired during insert")
            assert job.analysis_deadline_at is not None
            permit = CallPermit(
                row_id, admitted_at, lease.expires_at, job.analysis_deadline_at
            )
        # Returning only after __aexit__ confirms the commit: no ambiguous permit.
        return permit  # noqa: RET504 -- return only after confirmed transaction exit

    async def claim_format_repair(self, *, repair_id: UUID, chunk_id: UUID) -> bool:
        async with self._factory.begin() as session:
            job, _, now = await lock_analysis(session, self._task, self._lease)
            if job.frozen_configuration_version != 1:
                raise FrozenConfigurationError("historical_budget_unknown")
            ModelSettings.from_stored(job.model_settings)
            validate_config_identity(job)
            if (
                job.finished_at is not None
                or job.analysis_attempts != self._attempt
                or job.analysis_attempt_fence != self._lease.fencing_token
            ):
                raise LeaseLostError("Analysis execution is no longer current")
            if job.analysis_deadline_at is None or job.analysis_deadline_at <= now:
                raise BudgetExceededError("analysis deadline exceeded")
            if job.retry_at is not None and job.retry_at > now:
                raise BudgetExceededError("analysis retry deferred")
            if not await session.scalar(
                select(ContextPayload.id).where(
                    ContextPayload.id == chunk_id,
                    ContextPayload.review_job_id == job.id,
                )
            ):
                raise AccountingIdentityError("context_identity_mismatch")
            if job.format_repair_id is not None:
                return False
            changed = await session.scalar(
                update(ReviewJob)
                .where(
                    ReviewJob.id == job.id,
                    ReviewJob.finished_at.is_(None),
                    ReviewJob.analysis_attempts == self._attempt,
                    ReviewJob.analysis_attempt_fence == self._lease.fencing_token,
                    ReviewJob.analysis_deadline_at > func.clock_timestamp(),
                    (
                        ReviewJob.retry_at.is_(None)
                        | (ReviewJob.retry_at <= func.clock_timestamp())
                    ),
                    ReviewJob.format_repair_id.is_(None),
                    live_fence(self._lease),
                )
                .values(
                    format_repair_id=repair_id,
                    format_repair_claimed_at=func.clock_timestamp(),
                    format_repair_fence=self._lease.fencing_token,
                    format_repair_chunk_id=chunk_id,
                )
                .returning(ReviewJob.id)
            )
            if changed is None:
                raise LeaseLostError("Repair claim authority expired")
        return True

    async def settle_call(  # noqa: PLR0913 -- identified immutable settlement
        self,
        *,
        reservation_id: UUID,
        input_tokens: int | None,
        output_tokens: int | None,
        outcome: str,
        error_code: str | None,
        latency_ms: int | None,
    ) -> DurableBudgetSnapshot:
        input_tokens = known_token_usage(input_tokens)
        output_tokens = known_token_usage(output_tokens)
        async with self._factory.begin() as session:
            job, lease, _ = await lock_analysis(
                session, self._task, self._lease, ledger=True
            )
            row = await session.scalar(
                select(ReviewLlmAttempt)
                .where(
                    ReviewLlmAttempt.id == reservation_id,
                    ReviewLlmAttempt.review_job_id == self._lease.review_job_id,
                )
                .with_for_update()
            )
            if row is None:
                raise AccountingIdentityError("reservation_not_found")
            final_now = await session.scalar(select(func.clock_timestamp()))
            assert final_now is not None
            if lease.expires_at <= final_now:
                raise LeaseLostError("Settlement lease expired during lock wait")
            if (
                job.analysis_attempts != self._attempt
                or job.analysis_attempt_fence != self._lease.fencing_token
            ):
                raise LeaseLostError("Analysis execution is no longer current")
            if (row.analysis_attempt, row.lease_owner, row.fencing_token) != (
                self._attempt,
                self._lease.owner,
                self._lease.fencing_token,
            ):
                raise AccountingIdentityError("reservation_execution_mismatch")
            if row.state != "RESERVED":
                if (
                    row.actual_input_tokens,
                    row.actual_output_tokens,
                    row.state,
                    row.error_code,
                    row.latency_ms,
                ) != (input_tokens, output_tokens, outcome, error_code, latency_ms):
                    raise AccountingIdentityError("conflicting_settlement")
                return await self._snapshot(session)
            settled = await session.scalar(
                update(ReviewLlmAttempt)
                .where(
                    ReviewLlmAttempt.id == row.id,
                    live_fence(self._lease),
                    exists().where(
                        ReviewJob.id == self._lease.review_job_id,
                        ReviewJob.analysis_attempts == self._attempt,
                        ReviewJob.analysis_attempt_fence == self._lease.fencing_token,
                    ),
                )
                .values(
                    actual_input_tokens=input_tokens,
                    actual_output_tokens=output_tokens,
                    state=outcome,
                    error_code=error_code,
                    latency_ms=latency_ms,
                    settled_at=func.clock_timestamp(),
                )
                .returning(ReviewLlmAttempt.id)
            )
            if settled is None:
                raise LeaseLostError("Settlement authority expired")
            snapshot = await self._snapshot(session)
        return snapshot  # noqa: RET504 -- return only after confirmed transaction exit
