"""Repository bot-integration health marker (AC6)."""

from alembic import op
import sqlalchemy as sa

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "repository",
        sa.Column(
            "integration_status",
            sa.Text(),
            nullable=False,
            server_default="healthy",
        ),
    )
    op.create_check_constraint(
        "ck_repository_integration_status",
        "repository",
        "integration_status IN ('healthy','bot_denied')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_repository_integration_status", "repository", type_="check")
    op.drop_column("repository", "integration_status")
