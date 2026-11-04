"""Align persistence with the authoritative contract foundation.

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("LOCK TABLE review_job, chunk_result IN ACCESS EXCLUSIVE MODE")
    op.execute(
        """
        DO $$
        DECLARE incompatible_id uuid;
        BEGIN
            SELECT id INTO incompatible_id FROM chunk_result
            WHERE (
                limitations = '{}'::jsonb
                OR (jsonb_typeof(limitations) = 'array'
                    AND NOT jsonb_path_exists(limitations, '$[*] ? (@.type() != "string")'))
                OR (CASE WHEN jsonb_typeof(limitations) = 'object' THEN
                        limitations - 'warnings' = '{}'::jsonb
                        AND jsonb_typeof(limitations->'warnings') = 'array'
                        AND NOT jsonb_path_exists(limitations->'warnings', '$[*] ? (@.type() != "string")')
                    ELSE false END)
            ) IS NOT TRUE
            OR (
                usage IS NULL OR usage = '{}'::jsonb
                OR (jsonb_typeof(usage) = 'object'
                    AND usage = jsonb_build_object(
                        'input_tokens', usage->'input_tokens',
                        'output_tokens', usage->'output_tokens')
                    AND jsonb_typeof(usage->'input_tokens') = 'number'
                    AND jsonb_typeof(usage->'output_tokens') = 'number'
                    AND usage->>'input_tokens' ~ '^[0-9]+$'
                    AND usage->>'output_tokens' ~ '^[0-9]+$')
            ) IS NOT TRUE
            ORDER BY id LIMIT 1;
            IF incompatible_id IS NOT NULL THEN
                RAISE EXCEPTION '0004 preflight: incompatible chunk_result id=%. Back up and explicitly convert limitations/usage to the v1 schema, then retry. No data was changed.', incompatible_id;
            END IF;
        END $$
        """
    )
    op.drop_constraint(
        op.f("ck_review_job_status_finished_at"), "review_job", type_="check"
    )
    op.drop_constraint("ck_review_job_status", "review_job", type_="check")
    op.drop_constraint("ck_review_job_stage", "review_job", type_="check")

    op.execute(
        """
        UPDATE review_job
        SET status = CASE stage
            WHEN 'snapshot' THEN 'FETCHING_DIFF'
            WHEN 'context' THEN 'PARSING_CONTEXT'
            WHEN 'inference' THEN 'LLM_PROCESSING'
            WHEN 'validation' THEN 'LLM_PROCESSING'
            WHEN 'done' THEN 'LLM_PROCESSING'
            ELSE 'FETCHING_DIFF'
        END
        WHERE status = 'RUNNING'
        """
    )
    op.drop_column("review_job", "stage")
    op.create_check_constraint(
        "ck_review_job_status",
        "review_job",
        "status IN ('QUEUED','FETCHING_DIFF','PARSING_CONTEXT','LLM_PROCESSING',"
        "'COMPLETED','PARTIAL','FAILED','SKIPPED')",
    )
    op.create_check_constraint(
        op.f("ck_review_job_status_finished_at"),
        "review_job",
        "(status IN ('QUEUED','FETCHING_DIFF','PARSING_CONTEXT','LLM_PROCESSING') "
        "AND finished_at IS NULL) OR "
        "(status IN ('COMPLETED','PARTIAL','FAILED','SKIPPED') "
        "AND finished_at IS NOT NULL)",
    )

    op.drop_constraint("ck_review_event_status", "review_event", type_="check")
    op.drop_constraint("ck_review_event_stage", "review_event", type_="check")
    op.alter_column("review_event", "stage", new_column_name="phase")
    op.execute(
        """
        UPDATE review_event
        SET status = CASE
            WHEN status = 'RUNNING' THEN CASE phase
                WHEN 'snapshot' THEN 'FETCHING_DIFF'
                WHEN 'context' THEN 'PARSING_CONTEXT'
                WHEN 'inference' THEN 'LLM_PROCESSING'
                WHEN 'validation' THEN 'LLM_PROCESSING'
                WHEN 'done' THEN 'LLM_PROCESSING'
                ELSE 'FETCHING_DIFF'
            END
            ELSE status
        END,
        phase = CASE phase
            WHEN 'snapshot' THEN 'FETCHING_DIFF'
            WHEN 'context' THEN 'PARSING_CONTEXT'
            WHEN 'inference' THEN 'LLM_PROCESSING'
            WHEN 'validation' THEN 'LLM_PROCESSING'
            WHEN 'done' THEN NULL
            ELSE NULL
        END
        """
    )
    op.add_column(
        "review_event",
        sa.Column("retryable", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "review_event",
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "review_event",
        sa.Column(
            "safe_details",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.alter_column("review_event", "retryable", server_default=None)
    op.alter_column("review_event", "attempt", server_default=None)
    op.create_check_constraint(
        "ck_review_event_status",
        "review_event",
        "status IN ('QUEUED','FETCHING_DIFF','PARSING_CONTEXT','LLM_PROCESSING',"
        "'COMPLETED','PARTIAL','FAILED','SKIPPED')",
    )
    op.create_check_constraint(
        "ck_review_event_phase",
        "review_event",
        "phase IS NULL OR phase IN "
        "('FETCHING_DIFF','PARSING_CONTEXT','LLM_PROCESSING')",
    )
    op.create_check_constraint(
        "ck_review_event_attempt_positive", "review_event", "attempt >= 1"
    )
    op.create_check_constraint(
        "ck_review_event_safe_details_object",
        "review_event",
        "safe_details IS NULL OR jsonb_typeof(safe_details) = 'object'",
    )

    op.alter_column("chunk_result", "usage", existing_type=postgresql.JSONB(), nullable=True)
    op.execute(
        "UPDATE chunk_result SET limitations = '[]'::jsonb "
        "WHERE limitations = '{}'::jsonb"
    )
    op.execute(
        "UPDATE chunk_result SET limitations = limitations->'warnings' "
        "WHERE jsonb_typeof(limitations) = 'object' "
        "AND limitations - 'warnings' = '{}'::jsonb "
        "AND jsonb_typeof(limitations->'warnings') = 'array' "
        "AND NOT jsonb_path_exists(limitations->'warnings', '$[*] ? (@.type() != \"string\")')"
    )
    op.execute("UPDATE chunk_result SET usage = NULL WHERE usage = '{}'::jsonb")
    op.add_column(
        "finding", sa.Column("proposed_diff_fix", sa.Text(), nullable=True)
    )

def downgrade() -> None:
    op.drop_column("finding", "proposed_diff_fix")
    op.execute(
        "UPDATE chunk_result SET limitations = '{}'::jsonb "
        "WHERE limitations = '[]'::jsonb"
    )
    op.execute(
        "UPDATE chunk_result SET limitations = jsonb_build_object('warnings', limitations) "
        "WHERE jsonb_typeof(limitations) = 'array'"
    )
    op.execute("UPDATE chunk_result SET usage = '{}'::jsonb WHERE usage IS NULL")
    op.alter_column("chunk_result", "usage", existing_type=postgresql.JSONB(), nullable=False)

    op.drop_constraint(
        "ck_review_event_safe_details_object", "review_event", type_="check"
    )
    op.drop_constraint("ck_review_event_attempt_positive", "review_event", type_="check")
    op.drop_constraint("ck_review_event_phase", "review_event", type_="check")
    op.drop_constraint("ck_review_event_status", "review_event", type_="check")
    op.execute(
        """
        UPDATE review_event
        SET status = CASE
                WHEN status IN ('FETCHING_DIFF','PARSING_CONTEXT','LLM_PROCESSING')
                    THEN 'RUNNING'
                ELSE status
            END,
            phase = CASE phase
                WHEN 'FETCHING_DIFF' THEN 'snapshot'
                WHEN 'PARSING_CONTEXT' THEN 'context'
                WHEN 'LLM_PROCESSING' THEN 'inference'
                ELSE phase
            END
        """
    )
    op.drop_column("review_event", "safe_details")
    op.drop_column("review_event", "attempt")
    op.drop_column("review_event", "retryable")
    op.alter_column("review_event", "phase", new_column_name="stage")
    op.create_check_constraint(
        "ck_review_event_status",
        "review_event",
        "status IN ('QUEUED','RUNNING','COMPLETED','PARTIAL','FAILED','SKIPPED')",
    )
    op.create_check_constraint(
        "ck_review_event_stage",
        "review_event",
        "stage IS NULL OR stage IN ('snapshot','context','inference','validation','done')",
    )

    op.drop_constraint(
        op.f("ck_review_job_status_finished_at"), "review_job", type_="check"
    )
    op.drop_constraint("ck_review_job_status", "review_job", type_="check")
    op.add_column("review_job", sa.Column("stage", sa.Text(), nullable=True))
    op.execute(
        """
        UPDATE review_job
        SET stage = CASE status
                WHEN 'FETCHING_DIFF' THEN 'snapshot'
                WHEN 'PARSING_CONTEXT' THEN 'context'
                WHEN 'LLM_PROCESSING' THEN 'inference'
                ELSE NULL
            END,
            status = 'RUNNING'
        WHERE status IN ('FETCHING_DIFF','PARSING_CONTEXT','LLM_PROCESSING')
        """
    )
    op.create_check_constraint(
        "ck_review_job_status",
        "review_job",
        "status IN ('QUEUED','RUNNING','COMPLETED','PARTIAL','FAILED','SKIPPED')",
    )
    op.create_check_constraint(
        "ck_review_job_stage",
        "review_job",
        "stage IS NULL OR stage IN ('snapshot','context','inference','validation','done')",
    )
    op.create_check_constraint(
        op.f("ck_review_job_status_finished_at"),
        "review_job",
        "(status IN ('QUEUED','RUNNING') AND finished_at IS NULL) OR "
        "(status IN ('COMPLETED','PARTIAL','FAILED','SKIPPED') "
        "AND finished_at IS NOT NULL)",
    )
