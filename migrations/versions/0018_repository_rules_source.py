"""Immutable base-rules provenance, null for historical settings."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("repository_settings", sa.Column("rules_source", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("repository_settings", "rules_source")
