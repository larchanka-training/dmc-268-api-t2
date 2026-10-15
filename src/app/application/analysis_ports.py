"""Internal execution/accounting decisions; never part of queue wire payloads."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.application.ports import QueueTaskEnvelope, TaskLeaseSnapshot
from app.domain.enums import ReviewReasonCode


@dataclass(frozen=True)
class BeginAnalysisDecision:
    accepted: bool
    analysis_attempt: int
    started_at: datetime | None
    analysis_deadline_at: datetime | None
    error_code: str | None = None


class AnalysisAttemptStore(Protocol):
    async def begin(
        self, *, task: QueueTaskEnvelope, lease: TaskLeaseSnapshot
    ) -> BeginAnalysisDecision: ...

    async def defer_retry(
        self,
        *,
        task: QueueTaskEnvelope,
        lease: TaskLeaseSnapshot,
        reason: ReviewReasonCode,
        error_code: str,
        delay_s: float,
    ) -> bool: ...


@dataclass(frozen=True)
class DurableBudgetSnapshot:
    requests_used: int
    input_tokens_used: int
    output_tokens_used: int
    analysis_deadline_at: datetime | None
    sampled_at: datetime
    actual_input_tokens: int
    actual_output_tokens: int
    unknown_input_tokens: int
    unknown_output_tokens: int


@dataclass(frozen=True)
class CallPermit:
    reservation_id: UUID
    sampled_at: datetime
    lease_expires_at: datetime
    analysis_deadline_at: datetime


class AccountingIdentityError(ValueError):
    """A reservation or operation identity cannot be reused."""


class ReviewRunBudgetPort(Protocol):
    async def claim_format_repair(self, *, repair_id: UUID, chunk_id: UUID) -> bool: ...

    async def snapshot(self) -> DurableBudgetSnapshot: ...
    async def remaining_s(self) -> float: ...
    async def reserve_call(  # noqa: PLR0913 -- immutable HTTP identity
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
    ) -> CallPermit: ...

    async def settle_call(  # noqa: PLR0913 -- immutable cost receipt
        self,
        *,
        reservation_id: UUID,
        input_tokens: int | None,
        output_tokens: int | None,
        outcome: str,
        error_code: str | None,
        latency_ms: int | None,
    ) -> DurableBudgetSnapshot: ...
