"""Persist quarantine for invalid outbox events separately from publication.

Revision ID: 0007
Revises: 0006
"""

from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "outbox_event",
        sa.Column("quarantined_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "outbox_event", sa.Column("quarantine_reason_code", sa.Text(), nullable=True)
    )
    op.create_check_constraint(
        op.f("ck_outbox_quarantine_pair"),
        "outbox_event",
        "(quarantined_at IS NULL) = (quarantine_reason_code IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_outbox_delivery_state"),
        "outbox_event",
        "broker_published_at IS NULL OR quarantined_at IS NULL",
    )
    op.create_check_constraint(
        op.f("ck_outbox_quarantine_reason_code"),
        "outbox_event",
        "quarantine_reason_code IS NULL OR quarantine_reason_code IN "
        "('UNSUPPORTED_SCHEMA_VERSION','UNKNOWN_TASK_KIND','INVALID_ENVELOPE')",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_outbox_quarantine_reason_code"), "outbox_event", type_="check"
    )
    op.drop_constraint(op.f("ck_outbox_delivery_state"), "outbox_event", type_="check")
    op.drop_constraint(
        op.f("ck_outbox_quarantine_pair"), "outbox_event", type_="check"
    )
    op.drop_column("outbox_event", "quarantine_reason_code")
    op.drop_column("outbox_event", "quarantined_at")
