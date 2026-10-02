"""Enforce shared result/context ownership without coupling retention.

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_context_payload_review_job_id_id",
        "context_payload",
        ["review_job_id", "id"],
    )
    op.drop_constraint(
        "fk_chunk_result_context_payload", "chunk_result", type_="foreignkey"
    )
    op.create_foreign_key(
        "fk_chunk_result_context_payload_same_review",
        "chunk_result",
        "context_payload",
        ["review_job_id", "context_payload_id"],
        ["review_job_id", "id"],
        ondelete="SET NULL (context_payload_id)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_chunk_result_context_payload_same_review",
        "chunk_result",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_chunk_result_context_payload",
        "chunk_result",
        "context_payload",
        ["context_payload_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.drop_constraint(
        "uq_context_payload_review_job_id_id", "context_payload", type_="unique"
    )
