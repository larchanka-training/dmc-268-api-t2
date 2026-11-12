"""Last GitHub commit status confirmed per review cycle, with retry backoff."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "commit_status",
        sa.Column("review_job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("posted_state", sa.Text(), nullable=True),
        sa.Column("failed_attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "posted_state IS NULL OR posted_state IN ('pending','success','failure','error')",
            name="ck_commit_status_posted_state",
        ),
        sa.CheckConstraint("failed_attempts >= 0", name="ck_commit_status_failed_attempts_nonnegative"),
        sa.ForeignKeyConstraint(["review_job_id"], ["review_job.id"], name="fk_commit_status_review_job_id_review_job", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("review_job_id", name="pk_commit_status"),
    )


def downgrade() -> None:
    op.drop_table("commit_status")
