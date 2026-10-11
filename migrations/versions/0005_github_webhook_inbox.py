"""Persist GitHub webhook intake without creating review jobs.

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "webhook_receipt",
        sa.Column(
            "processing_status", sa.Text(), server_default="IGNORED", nullable=False
        ),
    )
    op.add_column(
        "webhook_receipt", sa.Column("request_data", postgresql.JSONB(), nullable=True)
    )
    op.add_column(
        "webhook_receipt", sa.Column("snapshot_data", postgresql.JSONB(), nullable=True)
    )
    op.add_column(
        "webhook_receipt",
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column("webhook_receipt", sa.Column("raw_diff", sa.Text(), nullable=True))
    op.add_column(
        "webhook_receipt",
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "webhook_receipt",
        sa.Column("retry_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("webhook_receipt", sa.Column("reason_code", sa.Text(), nullable=True))
    op.create_index(
        "ix_webhook_receipt_pending",
        "webhook_receipt",
        ["received_at", "id"],
        postgresql_where=sa.text("processing_status IN ('PENDING','PROCESSING')"),
    )


def downgrade() -> None:
    op.drop_index("ix_webhook_receipt_pending", table_name="webhook_receipt")
    op.drop_column("webhook_receipt", "reason_code")
    op.drop_column("webhook_receipt", "retry_at")
    op.drop_column("webhook_receipt", "lease_until")
    op.drop_column("webhook_receipt", "raw_diff")
    op.drop_column("webhook_receipt", "attempts")
    op.drop_column("webhook_receipt", "snapshot_data")
    op.drop_column("webhook_receipt", "request_data")
    op.drop_column("webhook_receipt", "processing_status")
