"""Durable analysis execution state and HTTP accounting.

Revision ID: 0009
Revises: 0008
"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "review_job",
        sa.Column(
            "analysis_attempts", sa.Integer(), nullable=False, server_default="0"
        ),
    )
    op.add_column(
        "review_job",
        sa.Column("analysis_attempt_fence", sa.BigInteger(), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_review_job_analysis_attempt_state"),
        "review_job",
        "(analysis_attempts = 0 AND analysis_attempt_fence IS NULL) OR (analysis_attempts > 0 AND analysis_attempt_fence IS NOT NULL AND analysis_attempt_fence >= 1)",
    )

    op.create_table(
        "review_llm_attempt",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "review_job_id", sa.Uuid(), sa.ForeignKey("review_job.id"), nullable=False
        ),
        sa.Column("chunk_id", sa.Uuid(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("call_kind", sa.Text(), nullable=False),
        sa.Column("analysis_attempt", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.Text(), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("provider_profile_ref", sa.Text(), nullable=False),
        sa.Column("model_ref", sa.Text(), nullable=False),
        sa.Column("model_digest", sa.Text(), nullable=True),
        sa.Column("reserved_input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("reserved_output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("actual_input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("actual_output_tokens", sa.BigInteger(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False, server_default="RESERVED"),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column(
            "reserved_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "call_kind IN ('initial', 'repair', 'overflow_rebuild')",
            name=op.f("ck_review_llm_attempt_kind"),
        ),
        sa.CheckConstraint(
            "analysis_attempt >= 1 AND fencing_token >= 1 AND length(lease_owner) > 0 AND length(provider_profile_ref) > 0 AND length(model_ref) > 0",
            name=op.f("ck_review_llm_attempt_identity"),
        ),
        sa.CheckConstraint(
            "reserved_input_tokens >= 0 AND reserved_output_tokens > 0 AND (actual_input_tokens IS NULL OR actual_input_tokens >= 0) AND (actual_output_tokens IS NULL OR actual_output_tokens >= 0)",
            name=op.f("ck_review_llm_attempt_tokens"),
        ),
        sa.CheckConstraint(
            "(state = 'RESERVED' AND settled_at IS NULL AND actual_input_tokens IS NULL AND actual_output_tokens IS NULL AND error_code IS NULL AND latency_ms IS NULL) OR (state = 'SUCCEEDED' AND settled_at IS NOT NULL AND error_code IS NULL) OR (state = 'ERROR' AND settled_at IS NOT NULL AND error_code IS NOT NULL)",
            name=op.f("ck_review_llm_attempt_state"),
        ),
        sa.CheckConstraint(
            "error_code IS NULL OR error_code ~ '^[a-z0-9_.-]{1,64}$'",
            name=op.f("ck_review_llm_attempt_error_code"),
        ),
        sa.CheckConstraint(
            "settled_at IS NULL OR settled_at >= reserved_at",
            name=op.f("ck_review_llm_attempt_time"),
        ),
        sa.CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0",
            name=op.f("ck_review_llm_attempt_latency"),
        ),
    )
    op.create_index(
        "ix_review_llm_attempt_review_operation",
        "review_llm_attempt",
        ["review_job_id", "operation_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_review_llm_attempt_review_operation", table_name="review_llm_attempt"
    )
    op.drop_table("review_llm_attempt")
    op.drop_constraint(
        op.f("ck_review_job_analysis_attempt_state"), "review_job", type_="check"
    )
    op.drop_column("review_job", "analysis_attempt_fence")
    op.drop_column("review_job", "analysis_attempts")
