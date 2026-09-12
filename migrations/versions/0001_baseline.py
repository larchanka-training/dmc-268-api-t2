"""baseline

Revision ID: 0001
Revises:
Create Date: 2026-01-01 00:00:00

"""
from collections.abc import Sequence

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Baseline: no schema yet, first models will chain from here."""


def downgrade() -> None:
    """No-op for baseline."""
