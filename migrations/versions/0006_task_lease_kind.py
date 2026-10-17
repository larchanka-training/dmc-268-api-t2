"""Bind task leases to analysis or publication authority.

Revision ID: 0006
Revises: 0005
"""

from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("task_lease", sa.Column("task_kind", sa.Text(), nullable=True))
    op.execute("UPDATE task_lease SET task_kind = 'analyze'")
    op.alter_column("task_lease", "task_kind", nullable=False)
    op.create_check_constraint(
        "ck_task_lease_task_kind", "task_lease", "task_kind IN ('analyze','publish')"
    )


def downgrade() -> None:
    op.drop_constraint("ck_task_lease_task_kind", "task_lease", type_="check")
    op.drop_column("task_lease", "task_kind")
