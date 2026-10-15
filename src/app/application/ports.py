from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from types import TracebackType
from typing import Literal, Protocol

from app.domain.enums import (
    FindingCategory,
    FindingSeverity,
    FindingSide,
    PublicationStatus,
    ReviewStatus,
)

type TaskKind = Literal["analyze", "publish"]
MAX_QUEUE_ENVELOPE_BYTES = 16 * 1024


type QueueEnvelopeReason = Literal[
    "UNSUPPORTED_SCHEMA_VERSION", "UNKNOWN_TASK_KIND", "INVALID_ENVELOPE"
]


class QueueEnvelopeValidationError(ValueError):
    """Permanent canonical-envelope rejection before any broker I/O.

    Queue adapters must reserve this error for validation, never transport errors.
    Only the safe contract code may be persisted as a quarantine reason.
    """

    def __init__(
        self,
        reason_code: QueueEnvelopeReason,
        message: str = "Invalid queue task envelope",
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class QueueTaskEnvelope:
    schema_version: int
    event_id: str
    review_id: str
    task_kind: TaskKind
    attempt: int
    trace_id: str

    def to_json(self) -> str:
        """Encode the bounded canonical wire representation; validate on enqueue."""
        payload = json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"))
        if len(payload.encode("utf-8")) > MAX_QUEUE_ENVELOPE_BYTES:
            raise QueueEnvelopeValidationError(
                "INVALID_ENVELOPE", "Queue task envelope exceeds 16 KiB"
            )
        return payload


@dataclass(frozen=True, slots=True)
class QueueDelivery:
    message_id: str
    envelope: QueueTaskEnvelope


@dataclass(frozen=True, slots=True)
class QueueClaim:
    """PEL scan position and at most one delivery; deleted IDs are diagnostic only.

    Cursor 0-0 ends a pass. An empty delivery can still advance the cursor.
    """

    cursor: str
    delivery: QueueDelivery | None
    deleted_ids: tuple[str, ...] = ()


class QueuePort(Protocol):
    async def enqueue(self, envelope: QueueTaskEnvelope) -> str:
        """Confirm enqueue or reject before I/O via QueueEnvelopeValidationError."""
        ...

    async def receive(
        self, *, task_kind: TaskKind, consumer_name: str
    ) -> QueueDelivery | None: ...

    async def reclaim(
        self, *, consumer_name: str, cursor: str, min_idle_time: int = 90_000
    ) -> QueueClaim:
        """Claim at most one pending analyze delivery; caller owns scan cursor."""
        ...

    async def ack(self, delivery: QueueDelivery) -> bool: ...


@dataclass(frozen=True, slots=True)
class ChangeRequestSnapshot:
    provider_name: str
    requested_head_sha: str
    base_sha: str
    merge_base_sha: str
    source_repository_external_id: str
    base_repository_external_id: str
    provider_diff_version: dict[str, object]


@dataclass(frozen=True, slots=True)
class BuiltContextPayload:
    schema_version: int
    review_id: uuid.UUID
    chunk_id: uuid.UUID
    payload_body: dict[str, object]

    def to_wire(self) -> dict[str, object]:
        if {"schema_version", "review_id", "chunk_id"} & self.payload_body.keys():
            raise ValueError("payload_body must not contain envelope fields")
        return {
            **self.payload_body,
            "schema_version": self.schema_version,
            "review_id": str(self.review_id),
            "chunk_id": str(self.chunk_id),
        }


@dataclass(frozen=True, slots=True)
class PullRequestAuthor:
    login: str


@dataclass(frozen=True, slots=True)
class PullRequestRef:
    repository_external_id: str
    ref: str
    sha: str


@dataclass(frozen=True, slots=True)
class OpenPullRequest:
    number: int
    title: str
    author: PullRequestAuthor
    state: Literal["OPEN"]
    source: PullRequestRef
    base: PullRequestRef


@dataclass(frozen=True, slots=True)
class OpenPullRequestPage:
    items: Sequence[OpenPullRequest]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class TokenUsage:
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class ReviewFinding:
    path: str
    side: FindingSide
    start_line: int
    end_line: int
    category: FindingCategory
    severity: FindingSeverity
    title: str
    explanation: str
    evidence: str
    recommendation: str
    proposed_diff_fix: str | None


@dataclass(frozen=True, slots=True)
class ReviewChunkResult:
    schema_version: int
    summary: str
    limitations: list[str]
    usage: TokenUsage | None
    latency_ms: int
    findings: Sequence[ReviewFinding]


@dataclass(frozen=True, slots=True)
class StartReviewCommand:
    change_request_id: uuid.UUID
    repository_settings_id: uuid.UUID
    requested_head_sha: str
    rules_digest: str
    trigger_type: str
    initiator_user_id: uuid.UUID | None
    model_ref: str
    model_digest: str | None
    model_settings: dict[str, object]
    prompt_version: str
    prompt_digest: str
    trace_id: str
    rerun_of: uuid.UUID | None = None
    frozen_configuration_version: int | None = None


@dataclass(frozen=True, slots=True)
class ReviewJobSnapshot:
    id: uuid.UUID
    repository_id: uuid.UUID
    change_request_id: uuid.UUID
    repository_settings_id: uuid.UUID
    requested_head_sha: str
    config_digest: str
    status: str
    created_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True, slots=True)
class TaskLeaseSnapshot:
    review_job_id: uuid.UUID
    owner: str
    expires_at: datetime
    fencing_token: int
    task_kind: TaskKind
    sampled_at: datetime | None = None


class LeaseLostError(Exception):
    """The worker no longer has authority to persist this review."""


@dataclass(frozen=True, slots=True)
class PublicationSnapshot:
    review_job_id: uuid.UUID
    status: str
    provider_metadata: dict[str, object] | None
    started_at: datetime | None
    deadline_at: datetime | None
    finished_at: datetime | None


class PublicationRepositoryPort(Protocol):
    async def get(self, review_job_id: uuid.UUID) -> PublicationSnapshot | None: ...

    async def prepare(self, lease: TaskLeaseSnapshot) -> None:
        """Prepare PENDING in the analysis completion UoW; never commit here."""
        ...

    async def update(
        self,
        lease: TaskLeaseSnapshot,
        *,
        status: PublicationStatus,
        provider_metadata: dict[str, object] | None,
    ) -> None:
        """Fence progress/outcome in this UoW; refuse lost authority.

        PENDING progress requires a future deadline. Terminal outcomes of an
        already-started attempt may be recorded after deadline with a live
        publish lease. The caller must start provider I/O before deadline;
        this method neither starts an attempt nor verifies its start time.
        """
        ...


class TaskLeasePort(Protocol):
    async def acquire(
        self, review_job_id: uuid.UUID, *, owner: str, task_kind: TaskKind
    ) -> TaskLeaseSnapshot | None: ...

    async def renew(self, lease: TaskLeaseSnapshot) -> TaskLeaseSnapshot | None: ...

    async def release(self, lease: TaskLeaseSnapshot) -> bool: ...


class DurableTaskOutcome(Enum):
    """Explicit handler assertion; the worker must verify committed DB state."""

    COMPLETED = "completed"


class TaskHandlerPort(Protocol):
    async def handle(
        self, task: QueueTaskEnvelope, lease: TaskLeaseSnapshot
    ) -> DurableTaskOutcome | None:
        """Use fenced UoW writes; report COMPLETED only after durable commit.

        The orchestrator supplies no provider call-start authority. An analysis
        handler must check live authority/deadlines before external I/O. None
        means recovery is required, even when the callback returned without error.
        """
        ...


class VcsPort(Protocol):
    async def list_open_pull_requests(
        self,
        *,
        repository_id: uuid.UUID,
        cursor: str | None,
        limit: int,
    ) -> OpenPullRequestPage: ...

    async def capture_snapshot(
        self, *, review_job_id: uuid.UUID
    ) -> ChangeRequestSnapshot: ...


class ContextBuilderPort(Protocol):
    async def build(
        self, *, review_job_id: uuid.UUID
    ) -> Sequence[BuiltContextPayload]: ...


class LlmGatewayPort(Protocol):
    async def review(self, payload: BuiltContextPayload) -> ReviewChunkResult: ...


class PublisherPort(Protocol):
    async def publish(self, *, review_job_id: uuid.UUID) -> dict[str, object]: ...


class ReviewJobRepositoryPort(Protocol):
    async def get(self, review_job_id: uuid.UUID) -> ReviewJobSnapshot | None: ...

    async def add_start_review(
        self, command: StartReviewCommand, *, config_digest: str
    ) -> uuid.UUID: ...

    async def update_status(
        self, lease: TaskLeaseSnapshot, *, status: ReviewStatus
    ) -> None:
        """Fence and write in this UoW transaction; raise LeaseLostError on refusal.

        Commit/rollback before external I/O. Other worker persistence methods must
        use the same transaction-local locking and verification, never a prior
        standalone check.
        """
        ...


class UnitOfWorkPort(Protocol):
    review_jobs: ReviewJobRepositoryPort
    publications: PublicationRepositoryPort

    async def __aenter__(self) -> UnitOfWorkPort: ...
    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...
    async def commit(self) -> None: ...
    async def rollback(self) -> None: ...
