from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "user"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class Repository(Base):
    __tablename__ = "repository"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    provider_name: Mapped[str] = mapped_column(Text, nullable=False)
    repo_name: Mapped[str] = mapped_column(Text, nullable=False)
    owner: Mapped[str] = mapped_column(Text, nullable=False)
    current_settings_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "repository_settings.id",
            name="fk_repository_current_settings",
            deferrable=True,
            initially="DEFERRED",
        ),
        nullable=False,
    )

    change_requests: Mapped[list[ChangeRequest]] = relationship(back_populates="repository")
    settings: Mapped[list[RepositorySettings]] = relationship(
        back_populates="repository",
        foreign_keys="RepositorySettings.repository_id",
    )
    current_settings: Mapped[RepositorySettings] = relationship(
        foreign_keys=[current_settings_id],
        post_update=True,
    )
    accesses: Mapped[list[RepositoryAccess]] = relationship(back_populates="repository")
    quota_usage: Mapped[list[QuotaUsage]] = relationship(back_populates="repository")


class RepositorySettings(Base):
    __tablename__ = "repository_settings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    repository_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("repository.id"), nullable=False
    )
    rules: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    ignores: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    max_starts_per_hour: Mapped[int] = mapped_column(Integer, nullable=False)
    max_active_jobs: Mapped[int] = mapped_column(Integer, nullable=False)
    output_language: Mapped[str] = mapped_column(Text, nullable=False)
    surrounding_lines: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    rules_digest: Mapped[str] = mapped_column(Text, nullable=False)

    repository: Mapped[Repository] = relationship(
        back_populates="settings",
        foreign_keys=[repository_id],
    )
    review_jobs: Mapped[list[ReviewJob]] = relationship(back_populates="repository_settings")

    __table_args__ = (
        CheckConstraint("max_starts_per_hour >= 0", name="ck_repository_settings_starts_nonnegative"),
        CheckConstraint("max_active_jobs >= 0", name="ck_repository_settings_active_nonnegative"),
        CheckConstraint("surrounding_lines >= 0", name="ck_repository_settings_surrounding_nonnegative"),
    )


class RepositoryAccess(Base):
    __tablename__ = "repository_access"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    repository_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("repository.id"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("user.id"), nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)

    repository: Mapped[Repository] = relationship(back_populates="accesses")
    user: Mapped[User] = relationship()

    __table_args__ = (
        UniqueConstraint("repository_id", "user_id", name="uq_repository_access_repository_user"),
        CheckConstraint("role IN ('reviewer','admin')", name="ck_repository_access_role"),
    )


class ChangeRequest(Base):
    __tablename__ = "change_request"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    repository_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("repository.id"), nullable=False
    )
    external_number: Mapped[str] = mapped_column(Text, nullable=False)
    source_repository_external_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_ref: Mapped[str] = mapped_column(Text, nullable=False)
    base_ref: Mapped[str] = mapped_column(Text, nullable=False)
    head_sha: Mapped[str] = mapped_column(Text, nullable=False)
    base_sha: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    current_status: Mapped[str] = mapped_column(Text, nullable=False)

    repository: Mapped[Repository] = relationship(back_populates="change_requests")
    review_jobs: Mapped[list[ReviewJob]] = relationship(back_populates="change_request")

    __table_args__ = (
        UniqueConstraint("repository_id", "external_number", name="uq_change_request_repository_number"),
    )


class ReviewJob(Base):
    __tablename__ = "review_job"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    change_request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("change_request.id"), nullable=False
    )
    repository_settings_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("repository_settings.id"), nullable=False
    )
    rules_digest: Mapped[str] = mapped_column(Text, nullable=False)
    trigger_type: Mapped[str] = mapped_column(Text, nullable=False)
    initiator_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id"), nullable=True
    )
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    snapshot_provider: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_head_sha: Mapped[str] = mapped_column(Text, nullable=False)
    base_sha: Mapped[str | None] = mapped_column(Text, nullable=True)
    merge_base_sha: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_diff_version: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    source_repository_external_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    base_repository_external_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    rerun_of: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("review_job.id"), nullable=True
    )
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    model_ref: Mapped[str] = mapped_column(Text, nullable=False)
    model_digest: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_settings: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_digest: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    stage: Mapped[str | None] = mapped_column(Text, nullable=True)
    queue_deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    analysis_deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    coverage: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    final_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    change_request: Mapped[ChangeRequest] = relationship(back_populates="review_jobs")
    repository_settings: Mapped[RepositorySettings] = relationship(back_populates="review_jobs")
    initiator_user: Mapped[User | None] = relationship(foreign_keys=[initiator_user_id])
    events: Mapped[list[ReviewEvent]] = relationship(back_populates="review_job")
    context_payloads: Mapped[list[ContextPayload]] = relationship(back_populates="review_job")
    publication: Mapped[Publication] = relationship(back_populates="review_job", uselist=False)
    task_lease: Mapped[TaskLease | None] = relationship(back_populates="review_job", uselist=False)
    outbox_events: Mapped[list[OutboxEvent]] = relationship(back_populates="review_job")

    __table_args__ = (
        CheckConstraint(
            "status IN ('QUEUED','RUNNING','COMPLETED','PARTIAL','FAILED','SKIPPED')",
            name="ck_review_job_status",
        ),
        CheckConstraint(
            "stage IS NULL OR stage IN ('snapshot','context','inference','validation','done')",
            name="ck_review_job_stage",
        ),
        Index("ix_review_job_status_retry_at", "status", "retry_at"),
    )


class ReviewEvent(Base):
    __tablename__ = "review_event"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    review_job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("review_job.id"), nullable=False
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    stage: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason_code: Mapped[str | None] = mapped_column(Text, nullable=True)

    review_job: Mapped[ReviewJob] = relationship(back_populates="events")

    __table_args__ = (
        CheckConstraint(
            "status IN ('QUEUED','RUNNING','COMPLETED','PARTIAL','FAILED','SKIPPED')",
            name="ck_review_event_status",
        ),
        CheckConstraint(
            "stage IS NULL OR stage IN ('snapshot','context','inference','validation','done')",
            name="ck_review_event_stage",
        ),
    )


class ContextPayload(Base):
    __tablename__ = "context_payload"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    review_job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("review_job.id"), nullable=False
    )
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    payload_body: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    review_job: Mapped[ReviewJob] = relationship(back_populates="context_payloads")
    chunk_result: Mapped[ChunkResult | None] = relationship(back_populates="context_payload", uselist=False)

    __table_args__ = (
        CheckConstraint("schema_version > 0", name="ck_context_payload_schema_version"),
    )


class ChunkResult(Base):
    __tablename__ = "chunk_result"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    context_payload_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("context_payload.id"), nullable=False, unique=True
    )
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    limitations: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    usage: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    context_payload: Mapped[ContextPayload] = relationship(back_populates="chunk_result")
    findings: Mapped[list[Finding]] = relationship(back_populates="chunk_result")

    __table_args__ = (
        CheckConstraint("schema_version > 0", name="ck_chunk_result_schema_version"),
        CheckConstraint("latency_ms >= 0", name="ck_chunk_result_latency_nonnegative"),
    )


class Finding(Base):
    __tablename__ = "finding"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    chunk_result_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chunk_result.id"), nullable=False
    )
    fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    side: Mapped[str] = mapped_column(Text, nullable=False)
    start_line: Mapped[int] = mapped_column(Integer, nullable=False)
    end_line: Mapped[int] = mapped_column(Integer, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[str] = mapped_column(Text, nullable=False)
    recommendation: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    chunk_result: Mapped[ChunkResult] = relationship(back_populates="findings")

    __table_args__ = (
        CheckConstraint("side IN ('OLD','NEW')", name="ck_finding_side"),
        CheckConstraint(
            "category IN ('security','correctness','performance','maintainability')",
            name="ck_finding_category",
        ),
        CheckConstraint(
            "severity IN ('critical','high','medium','low')",
            name="ck_finding_severity",
        ),
        CheckConstraint("start_line > 0", name="ck_finding_start_line_positive"),
        CheckConstraint("end_line >= start_line", name="ck_finding_line_range"),
    )


class Publication(Base):
    __tablename__ = "publication"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    review_job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("review_job.id"), nullable=False, unique=True
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    provider_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    review_job: Mapped[ReviewJob] = relationship(back_populates="publication")

    __table_args__ = (
        CheckConstraint(
            "status IN ('NOT_READY','PENDING','PUBLISHED','PARTIAL','FAILED','UNKNOWN','SKIPPED')",
            name="ck_publication_status",
        ),
    )


class OutboxEvent(Base):
    __tablename__ = "outbox_event"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    review_job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("review_job.id"), nullable=False
    )
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    task_kind: Mapped[str] = mapped_column(Text, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    trace_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    broker_published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    review_job: Mapped[ReviewJob] = relationship(back_populates="outbox_events")

    __table_args__ = (
        CheckConstraint("schema_version > 0", name="ck_outbox_schema_version"),
        CheckConstraint("task_kind IN ('analyze','publish')", name="ck_outbox_task_kind"),
        CheckConstraint("attempt > 0", name="ck_outbox_attempt_positive"),
        Index("ix_outbox_unpublished", "broker_published_at", "created_at"),
    )


class TaskLease(Base):
    __tablename__ = "task_lease"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    review_job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("review_job.id"), nullable=False, unique=True
    )
    owner: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)

    review_job: Mapped[ReviewJob] = relationship(back_populates="task_lease")

    __table_args__ = (
        CheckConstraint("fencing_token > 0", name="ck_task_lease_fencing_positive"),
        Index("ix_task_lease_expires_at", "expires_at"),
    )


class QuotaUsage(Base):
    __tablename__ = "quota_usage"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    repository_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("repository.id"), nullable=False
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("user.id"), nullable=True)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[int] = mapped_column(Integer, nullable=False)
    window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    repository: Mapped[Repository] = relationship(back_populates="quota_usage")
    user: Mapped[User | None] = relationship()

    __table_args__ = (
        CheckConstraint("type IN ('starts','active_jobs')", name="ck_quota_usage_type"),
        CheckConstraint("value >= 0", name="ck_quota_usage_value_nonnegative"),
        CheckConstraint(
            "(type = 'starts' AND user_id IS NOT NULL AND window_start IS NOT NULL) OR "
            "(type = 'active_jobs' AND user_id IS NULL AND window_start IS NULL)",
            name="ck_quota_usage_scope",
        ),
        Index(
            "uq_quota_starts_scope",
            "repository_id",
            "user_id",
            "type",
            "window_start",
            unique=True,
            postgresql_where=text("type = 'starts'"),
        ),
        Index(
            "uq_quota_active_scope",
            "repository_id",
            "type",
            unique=True,
            postgresql_where=text("type = 'active_jobs'"),
        ),
    )


class WebhookReceipt(Base):
    __tablename__ = "webhook_receipt"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider_name: Mapped[str] = mapped_column(Text, nullable=False)
    delivery_id: Mapped[str] = mapped_column(Text, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (
        UniqueConstraint("provider_name", "delivery_id", name="uq_webhook_receipt_provider_delivery"),
    )


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_record"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    request_hash: Mapped[str] = mapped_column(Text, nullable=False)
    review_job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("review_job.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    review_job: Mapped[ReviewJob] = relationship()

    __table_args__ = (
        UniqueConstraint("scope", "idempotency_key", name="uq_idempotency_scope_key"),
        Index("ix_idempotency_record_expires_at", "expires_at"),
    )
