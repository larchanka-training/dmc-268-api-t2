"""Keep confirmed transport receipts and persisted dispatch throttle only.

Revision ID: 0012
Revises: 0011
"""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name in (
        "ck_outbox_recovery_window",
        "ck_outbox_recovery_count",
        "ck_outbox_dispatch_generation",
    ):
        op.drop_constraint(op.f(name), "outbox_event", type_="check")
    for column in (
        "recovery_send_count",
        "recovery_window_started_at",
        "dispatch_generation",
    ):
        op.drop_column("outbox_event", column)


def downgrade() -> None:
    # Historical counters cannot be recovered. Old dispatchers must stay stopped
    # until rollback is complete; retained receipts/throttles remain authoritative.
    op.add_column("outbox_event", sa.Column("dispatch_generation", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("outbox_event", sa.Column("recovery_window_started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("outbox_event", sa.Column("recovery_send_count", sa.Integer(), nullable=False, server_default="0"))
    for name, condition in (
        ("ck_outbox_dispatch_generation", "dispatch_generation >= 0"),
        ("ck_outbox_recovery_count", "recovery_send_count >= 0"),
        ("ck_outbox_recovery_window", "(recovery_window_started_at IS NULL AND recovery_send_count = 0) OR (recovery_window_started_at IS NOT NULL AND recovery_send_count > 0)"),
    ):
        op.create_check_constraint(op.f(name), "outbox_event", condition)
