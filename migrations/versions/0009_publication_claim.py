"""Durable serialization and operation identity for publication write-back."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "publication_operation",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("change_request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("review_job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner", sa.Text(), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("marker", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_publication_operation"),
        sa.ForeignKeyConstraint(["change_request_id"], ["change_request.id"]),
        sa.ForeignKeyConstraint(["review_job_id"], ["review_job.id"]),
        sa.UniqueConstraint("change_request_id", name="uq_publication_operation_change"),
        sa.CheckConstraint("fencing_token > 0", name="fencing_positive"),
        sa.CheckConstraint("state IN ('IN_FLIGHT','UNKNOWN','RESOLVED')", name="state"),
    )


def downgrade() -> None:
    op.drop_table("publication_operation")
