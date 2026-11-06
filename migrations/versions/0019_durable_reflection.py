"""Explicit frozen model policy2; no historical policy backfill."""

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def _policy_shape(versions: str) -> None:
    op.drop_constraint(
        op.f("ck_review_job_frozen_settings_shape"), "review_job", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_review_job_frozen_settings_shape"),
        "review_job",
        "frozen_configuration_version IS NULL OR COALESCE(jsonb_typeof(model_settings) = 'object' "
        f"AND model_settings->>'schema_version' IN ({versions}), false)",
    )


def _call_kinds(kinds: str) -> None:
    op.drop_constraint(
        op.f("ck_review_llm_attempt_kind"), "review_llm_attempt", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_review_llm_attempt_kind"),
        "review_llm_attempt",
        f"call_kind IN ({kinds})",
    )


def upgrade() -> None:
    _policy_shape("'1', '2'")
    _call_kinds("'initial', 'repair', 'overflow_rebuild', 'reflection'")


def downgrade() -> None:
    # Narrowing CHECKs rejects retained policy2/Reflection rows; operators must
    # keep the forward schema while such history remains.
    _call_kinds("'initial', 'repair', 'overflow_rebuild'")
    _policy_shape("'1'")
