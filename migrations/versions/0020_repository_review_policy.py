"""Immutable repository review policy, NULL for unchanged historical identity."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("repository_settings", sa.Column("review_policy", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.execute("LOCK TABLE repository_settings IN ACCESS EXCLUSIVE MODE")
    if op.get_bind().scalar(sa.text(
        "SELECT EXISTS(SELECT 1 FROM repository_settings WHERE review_policy IS NOT NULL)"
    )):
        raise RuntimeError("Cannot downgrade with retained repository review policies")
    op.drop_column("repository_settings", "review_policy")
