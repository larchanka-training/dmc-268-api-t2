"""GitHub browser identity, one-use OAuth flows and authoritative sessions.

Revision ID: 0013
Revises: 0012
"""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Legacy users remain unchanged; GitHub identity is linked on first OAuth.
    op.create_table(
        "github_oauth_flow",
        sa.Column("state_hash", sa.Text(), nullable=False),
        sa.Column("browser_hash", sa.Text(), nullable=False),
        sa.Column("return_to", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("state_hash", name=op.f("pk_github_oauth_flow")),
    )
    op.create_table(
        "github_identity",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("login", sa.Text(), nullable=False),
        sa.Column("encrypted_token", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], name=op.f("fk_github_identity_user_id_user")),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_github_identity")),
        sa.UniqueConstraint("external_id", name=op.f("uq_github_identity_external_id")),
    )
    op.create_table(
        "auth_session",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("csrf_token", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], name=op.f("fk_auth_session_user_id_user")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_session")),
    )


def downgrade() -> None:
    op.drop_table("auth_session")
    op.drop_table("github_identity")
    op.drop_table("github_oauth_flow")
