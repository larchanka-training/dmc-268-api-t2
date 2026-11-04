"""One irreversible format-repair claim per review job.

Revision ID: 0010
Revises: 0009
"""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("review_job", sa.Column("format_repair_id", sa.Uuid(), nullable=True))
    op.add_column("review_job", sa.Column("format_repair_claimed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("review_job", sa.Column("format_repair_fence", sa.BigInteger(), nullable=True))
    op.add_column("review_job", sa.Column("format_repair_chunk_id", sa.Uuid(), nullable=True))
    op.create_check_constraint(
        op.f("ck_review_job_repair_state"), "review_job",
        "(format_repair_id IS NULL AND format_repair_claimed_at IS NULL "
        "AND format_repair_fence IS NULL AND format_repair_chunk_id IS NULL) OR "
        "(format_repair_id IS NOT NULL AND format_repair_claimed_at IS NOT NULL "
        "AND format_repair_fence IS NOT NULL AND format_repair_fence >= 1 "
        "AND format_repair_chunk_id IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_review_job_repair_state"), "review_job", type_="check")
    for column in ("format_repair_chunk_id", "format_repair_fence", "format_repair_claimed_at", "format_repair_id"):
        op.drop_column("review_job", column)
