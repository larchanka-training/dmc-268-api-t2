"""Persist transport generations, receipts and bounded recovery reservations.

Revision ID: 0011
Revises: 0010
"""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("outbox_event", sa.Column("broker_message_id", sa.Text(), nullable=True))
    op.add_column("outbox_event", sa.Column("dispatch_generation", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("outbox_event", sa.Column("dispatch_not_before", sa.DateTime(timezone=True), nullable=True))
    op.add_column("outbox_event", sa.Column("recovery_window_started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("outbox_event", sa.Column("recovery_send_count", sa.Integer(), nullable=False, server_default="0"))
    for name, condition in (
        ("ck_outbox_dispatch_generation", "dispatch_generation >= 0"),
        ("ck_outbox_recovery_count", "recovery_send_count >= 0"),
        ("ck_outbox_recovery_window", "(recovery_window_started_at IS NULL AND recovery_send_count = 0) OR (recovery_window_started_at IS NOT NULL AND recovery_send_count > 0)"),
    ):
        op.create_check_constraint(op.f(name), "outbox_event", condition)

    op.create_index("ix_outbox_analyze_cursor", "outbox_event", ["created_at", "id"], postgresql_where=sa.text("task_kind = 'analyze' AND quarantined_at IS NULL"))


def downgrade() -> None:
    op.drop_index("ix_outbox_analyze_cursor", table_name="outbox_event")
    for name in ("ck_outbox_recovery_window", "ck_outbox_recovery_count", "ck_outbox_dispatch_generation"):
        op.drop_constraint(op.f(name), "outbox_event", type_="check")
    for column in ("recovery_send_count", "recovery_window_started_at", "dispatch_not_before", "dispatch_generation", "broker_message_id"):
        op.drop_column("outbox_event", column)
