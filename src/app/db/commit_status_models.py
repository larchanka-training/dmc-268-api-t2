"""Last commit status confirmed by GitHub for one review cycle."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, Text, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CommitStatus(Base):
    __tablename__ = "commit_status"

    review_job_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        # A derived mirror of the review: cleanup of the review removes it too.
        ForeignKey("review_job.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # NULL until GitHub has accepted the first status for this review.
    posted_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    failed_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint(
            "posted_state IS NULL OR "
            "posted_state IN ('pending','success','failure','error')",
            name="posted_state",
        ),
        CheckConstraint("failed_attempts >= 0", name="failed_attempts_nonnegative"),
    )
