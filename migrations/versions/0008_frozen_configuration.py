"""Nullable frozen configuration marker; no historical backfill."""

from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "review_job",
        sa.Column("frozen_configuration_version", sa.SmallInteger(), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_review_job_frozen_configuration_version"),
        "review_job",
        "frozen_configuration_version IS NULL OR frozen_configuration_version = 1",
    )
    op.create_check_constraint(
        op.f("ck_review_job_frozen_settings_shape"),
        "review_job",
        "frozen_configuration_version IS NULL OR COALESCE(jsonb_typeof(model_settings) = 'object' AND model_settings->>'schema_version' = '1', false)",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_review_job_frozen_settings_shape"), "review_job", type_="check"
    )
    op.drop_constraint(
        op.f("ck_review_job_frozen_configuration_version"), "review_job", type_="check"
    )
    op.drop_column("review_job", "frozen_configuration_version")
