"""Retain optional repository rule references on canonical findings.

Revision ID: 0014
Revises: 0013
"""

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("finding", sa.Column("rule_ref", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("finding", "rule_ref")
