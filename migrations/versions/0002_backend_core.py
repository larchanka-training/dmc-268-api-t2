"""Create backend domain core schema.

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    # current_settings_id is created before repository_settings; its FK is added below.
    op.create_table(
        "repository",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("provider_name", sa.Text(), nullable=False),
        sa.Column("repo_name", sa.Text(), nullable=False),
        sa.Column("owner", sa.Text(), nullable=False),
        sa.Column("current_settings_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "repository_settings",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("repository_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("rules", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("ignores", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("max_starts_per_hour", sa.Integer(), nullable=False),
        sa.Column("max_active_jobs", sa.Integer(), nullable=False),
        sa.Column("output_language", sa.Text(), nullable=False),
        sa.Column("surrounding_lines", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rules_digest", sa.Text(), nullable=False),
        sa.CheckConstraint("max_starts_per_hour >= 0", name="ck_repository_settings_starts_nonnegative"),
        sa.CheckConstraint("max_active_jobs >= 0", name="ck_repository_settings_active_nonnegative"),
        sa.CheckConstraint("surrounding_lines >= 0", name="ck_repository_settings_surrounding_nonnegative"),
        sa.ForeignKeyConstraint(["repository_id"], ["repository.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_foreign_key(
        "fk_repository_current_settings",
        "repository",
        "repository_settings",
        ["current_settings_id"],
        ["id"],
        deferrable=True,
        initially="DEFERRED",
    )

    op.create_table(
        "repository_access",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("repository_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.CheckConstraint("role IN ('reviewer','admin')", name="ck_repository_access_role"),
        sa.ForeignKeyConstraint(["repository_id"], ["repository.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("repository_id", "user_id", name="uq_repository_access_repository_user"),
    )

    op.create_table(
        "change_request",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("repository_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("external_number", sa.Text(), nullable=False),
        sa.Column("source_repository_external_id", sa.Text(), nullable=True),
        sa.Column("source_ref", sa.Text(), nullable=False),
        sa.Column("base_ref", sa.Text(), nullable=False),
        sa.Column("head_sha", sa.Text(), nullable=False),
        sa.Column("base_sha", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("current_status", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["repository_id"], ["repository.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("repository_id", "external_number", name="uq_change_request_repository_number"),
    )

    op.create_table(
        "review_job",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("change_request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("repository_settings_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("rules_digest", sa.Text(), nullable=False),
        sa.Column("trigger_type", sa.Text(), nullable=False),
        sa.Column("initiator_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("snapshot_provider", sa.Text(), nullable=True),
        sa.Column("requested_head_sha", sa.Text(), nullable=False),
        sa.Column("base_sha", sa.Text(), nullable=True),
        sa.Column("merge_base_sha", sa.Text(), nullable=True),
        sa.Column("provider_diff_version", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("source_repository_external_id", sa.Text(), nullable=True),
        sa.Column("base_repository_external_id", sa.Text(), nullable=True),
        sa.Column("rerun_of", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("model_ref", sa.Text(), nullable=False),
        sa.Column("model_digest", sa.Text(), nullable=True),
        sa.Column("model_settings", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("prompt_digest", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("stage", sa.Text(), nullable=True),
        sa.Column("queue_deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("analysis_deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("coverage", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("final_summary", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('QUEUED','RUNNING','COMPLETED','PARTIAL','FAILED','SKIPPED')",
            name="ck_review_job_status",
        ),
        sa.CheckConstraint(
            "stage IS NULL OR stage IN ('snapshot','context','inference','validation','done')",
            name="ck_review_job_stage",
        ),
        sa.ForeignKeyConstraint(["change_request_id"], ["change_request.id"]),
        sa.ForeignKeyConstraint(["initiator_user_id"], ["user.id"]),
        sa.ForeignKeyConstraint(["repository_settings_id"], ["repository_settings.id"]),
        sa.ForeignKeyConstraint(["rerun_of"], ["review_job.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_review_job_status_retry_at", "review_job", ["status", "retry_at"], unique=False)

    op.create_table(
        "review_event",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("review_job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("stage", sa.Text(), nullable=True),
        sa.Column("reason_code", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('QUEUED','RUNNING','COMPLETED','PARTIAL','FAILED','SKIPPED')",
            name="ck_review_event_status",
        ),
        sa.CheckConstraint(
            "stage IS NULL OR stage IN ('snapshot','context','inference','validation','done')",
            name="ck_review_event_stage",
        ),
        sa.ForeignKeyConstraint(["review_job_id"], ["review_job.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "context_payload",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("review_job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("payload_body", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("schema_version > 0", name="ck_context_payload_schema_version"),
        sa.ForeignKeyConstraint(["review_job_id"], ["review_job.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "chunk_result",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("context_payload_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("limitations", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("usage", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("schema_version > 0", name="ck_chunk_result_schema_version"),
        sa.CheckConstraint("latency_ms >= 0", name="ck_chunk_result_latency_nonnegative"),
        sa.ForeignKeyConstraint(["context_payload_id"], ["context_payload.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("context_payload_id"),
    )

    op.create_table(
        "finding",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chunk_result_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("start_line", sa.Integer(), nullable=False),
        sa.Column("end_line", sa.Integer(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=False),
        sa.Column("recommendation", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("side IN ('OLD','NEW')", name="ck_finding_side"),
        sa.CheckConstraint(
            "category IN ('security','correctness','performance','maintainability')",
            name="ck_finding_category",
        ),
        sa.CheckConstraint("severity IN ('critical','high','medium','low')", name="ck_finding_severity"),
        sa.CheckConstraint("start_line > 0", name="ck_finding_start_line_positive"),
        sa.CheckConstraint("end_line >= start_line", name="ck_finding_line_range"),
        sa.ForeignKeyConstraint(["chunk_result_id"], ["chunk_result.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "publication",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("review_job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("provider_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('NOT_READY','PENDING','PUBLISHED','PARTIAL','FAILED','UNKNOWN','SKIPPED')",
            name="ck_publication_status",
        ),
        sa.ForeignKeyConstraint(["review_job_id"], ["review_job.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("review_job_id"),
    )

    op.create_table(
        "outbox_event",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("review_job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("task_kind", sa.Text(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("trace_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("broker_published_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("schema_version > 0", name="ck_outbox_schema_version"),
        sa.CheckConstraint("task_kind IN ('analyze','publish')", name="ck_outbox_task_kind"),
        sa.CheckConstraint("attempt > 0", name="ck_outbox_attempt_positive"),
        sa.ForeignKeyConstraint(["review_job_id"], ["review_job.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_outbox_unpublished", "outbox_event", ["broker_published_at", "created_at"], unique=False)

    op.create_table(
        "task_lease",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("review_job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("fencing_token > 0", name="ck_task_lease_fencing_positive"),
        sa.ForeignKeyConstraint(["review_job_id"], ["review_job.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("review_job_id"),
    )
    op.create_index("ix_task_lease_expires_at", "task_lease", ["expires_at"], unique=False)

    op.create_table(
        "quota_usage",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("repository_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("value", sa.Integer(), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("type IN ('starts','active_jobs')", name="ck_quota_usage_type"),
        sa.CheckConstraint("value >= 0", name="ck_quota_usage_value_nonnegative"),
        sa.CheckConstraint(
            "(type = 'starts' AND user_id IS NOT NULL AND window_start IS NOT NULL) OR "
            "(type = 'active_jobs' AND user_id IS NULL AND window_start IS NULL)",
            name="ck_quota_usage_scope",
        ),
        sa.ForeignKeyConstraint(["repository_id"], ["repository.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_quota_starts_scope",
        "quota_usage",
        ["repository_id", "user_id", "type", "window_start"],
        unique=True,
        postgresql_where=sa.text("type = 'starts'"),
    )
    op.create_index(
        "uq_quota_active_scope",
        "quota_usage",
        ["repository_id", "type"],
        unique=True,
        postgresql_where=sa.text("type = 'active_jobs'"),
    )

    op.create_table(
        "webhook_receipt",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider_name", sa.Text(), nullable=False),
        sa.Column("delivery_id", sa.Text(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider_name", "delivery_id", name="uq_webhook_receipt_provider_delivery"),
    )

    op.create_table(
        "idempotency_record",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("review_job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["review_job_id"], ["review_job.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("scope", "idempotency_key", name="uq_idempotency_scope_key"),
    )
    op.create_index("ix_idempotency_record_expires_at", "idempotency_record", ["expires_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_idempotency_record_expires_at", table_name="idempotency_record")
    op.drop_table("idempotency_record")
    op.drop_table("webhook_receipt")

    op.drop_index("uq_quota_active_scope", table_name="quota_usage")
    op.drop_index("uq_quota_starts_scope", table_name="quota_usage")
    op.drop_table("quota_usage")

    op.drop_index("ix_task_lease_expires_at", table_name="task_lease")
    op.drop_table("task_lease")

    op.drop_index("ix_outbox_unpublished", table_name="outbox_event")
    op.drop_table("outbox_event")
    op.drop_table("publication")
    op.drop_table("finding")
    op.drop_table("chunk_result")
    op.drop_table("context_payload")
    op.drop_table("review_event")

    op.drop_index("ix_review_job_status_retry_at", table_name="review_job")
    op.drop_table("review_job")
    op.drop_table("change_request")
    op.drop_table("repository_access")

    op.drop_constraint("fk_repository_current_settings", "repository", type_="foreignkey")
    op.drop_table("repository_settings")
    op.drop_table("repository")
    op.drop_table("user")
