"""Durable PR-scoped publication authority across provider I/O."""

from uuid import UUID, uuid4

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class PublicationOperation(Base):
    __tablename__ = "publication_operation"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    change_request_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("change_request.id"), nullable=False
    )
    review_job_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("review_job.id"), nullable=False
    )
    owner: Mapped[str] = mapped_column(Text, nullable=False)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    marker: Mapped[str] = mapped_column(Text, nullable=False)
    payload_hash: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint("change_request_id", name="uq_publication_operation_change"),
        CheckConstraint("fencing_token > 0", name="fencing_positive"),
        CheckConstraint("state IN ('IN_FLIGHT','UNKNOWN','RESOLVED')", name="state"),
    )
