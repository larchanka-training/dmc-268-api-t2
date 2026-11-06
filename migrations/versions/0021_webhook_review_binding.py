"""Durable automatic receipt binding, independent of expiring request keys."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("webhook_receipt", sa.Column("review_job_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key("fk_webhook_receipt_review_job", "webhook_receipt", "review_job", ["review_job_id"], ["id"])


def downgrade() -> None:
    op.execute("LOCK TABLE webhook_receipt IN ACCESS EXCLUSIVE MODE")
    if op.get_bind().scalar(sa.text(
        "SELECT EXISTS(SELECT 1 FROM webhook_receipt WHERE review_job_id IS NOT NULL)"
    )):
        raise RuntimeError("Cannot downgrade with retained automatic review bindings")
    op.drop_constraint("fk_webhook_receipt_review_job", "webhook_receipt", type_="foreignkey")
    op.drop_column("webhook_receipt", "review_job_id")
