"""Sequence durable review events and preserve publication status per event.

Revision ID: 0008
Revises: 0005 (retarget to PR #29's 0007 when that branch is integrated)
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("review_event", sa.Column("sequence", sa.BigInteger(), nullable=True))
    op.add_column("review_event", sa.Column("publication_status", sa.Text(), nullable=True))
    # Legacy publication state cannot be reconstructed; NULL means unavailable.
    op.execute("""
        UPDATE review_event AS event SET sequence = numbered.sequence
        FROM (
            SELECT id, row_number() OVER (
                PARTITION BY review_job_id ORDER BY occurred_at, id
            ) AS sequence FROM review_event
        ) AS numbered WHERE event.id = numbered.id
    """)
    op.alter_column("review_event", "sequence", nullable=False)
    op.create_unique_constraint("uq_review_event_sequence", "review_event", ["review_job_id", "sequence"])
    op.create_check_constraint("ck_review_event_sequence_positive", "review_event", "sequence >= 1")
    op.create_check_constraint(
        "ck_review_event_publication_status", "review_event",
        "publication_status IS NULL OR publication_status IN "
        "('NOT_READY','PENDING','PUBLISHED','PARTIAL','FAILED','UNKNOWN','SKIPPED')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_review_event_publication_status", "review_event", type_="check")
    op.drop_constraint("ck_review_event_sequence_positive", "review_event", type_="check")
    op.drop_constraint("uq_review_event_sequence", "review_event", type_="unique")
    op.drop_column("review_event", "publication_status")
    op.drop_column("review_event", "sequence")
