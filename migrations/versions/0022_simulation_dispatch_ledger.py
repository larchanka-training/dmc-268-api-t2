"""Authoritative Playground dispatch history; never discard consumed claims."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "simulation_dispatch_control",
        sa.Column("id", sa.SmallInteger(), primary_key=True),
        sa.Column("blocked", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("hmac_config_id", sa.Text(), nullable=True),
        sa.CheckConstraint("id = 1", name="singleton"),
        sa.CheckConstraint("hmac_config_id ~ '^[0-9a-f]{64}$'", name="config_format"),
    )
    op.execute("INSERT INTO simulation_dispatch_control(id) VALUES (1)")
    op.create_table(
        "simulation_dispatch_claim",
        sa.Column("operation_digest", sa.Text(), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("repository_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("authorized_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("clock_timestamp()")),
        sa.Column("state", sa.Text(), nullable=False, server_default="DISPATCH_AUTHORIZED"),
        sa.CheckConstraint("operation_digest ~ '^[0-9a-f]{64}$'", name="digest_format"),
        sa.CheckConstraint("state IN ('DISPATCH_AUTHORIZED', 'COMPLETED', 'UNKNOWN')", name="state"),
    )
    op.create_index("ix_simulation_dispatch_claim_authorized_at", "simulation_dispatch_claim", ["authorized_at"])
    op.create_index("ix_simulation_dispatch_claim_user_time", "simulation_dispatch_claim", ["user_id", "authorized_at"])
    op.create_index("ix_simulation_dispatch_claim_repository_time", "simulation_dispatch_claim", ["repository_id", "authorized_at"])


def downgrade() -> None:
    op.execute("LOCK TABLE simulation_dispatch_claim IN ACCESS EXCLUSIVE MODE")
    if op.get_bind().scalar(sa.text("SELECT EXISTS(SELECT 1 FROM simulation_dispatch_claim)")):
        raise RuntimeError("Cannot downgrade with retained simulation dispatch claims")
    if op.get_bind().scalar(sa.text("SELECT blocked FROM simulation_dispatch_control WHERE id=1")):
        raise RuntimeError("Cannot downgrade a blocked simulation ledger")
    op.drop_table("simulation_dispatch_claim")
    op.drop_table("simulation_dispatch_control")
