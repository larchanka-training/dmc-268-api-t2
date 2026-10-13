"""Safe persisted state used by review history and the SSE adapter."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


class EventReplayUnavailableError(LookupError):
    """The requested replay cursor is outside retained contiguous history."""


class ReviewAccessDeniedError(PermissionError):
    """The review is absent or the principal has no repository access."""


@dataclass(frozen=True)
class ReviewEventSnapshot:
    review_id: UUID
    sequence: int
    status: str
    publication_status: str


@dataclass(frozen=True)
class PersistedReviewEvent:
    id: UUID
    review_id: UUID
    sequence: int
    occurred_at: datetime
    event_type: str
    status: str
    publication_status: str | None
    reason_code: str | None
    retryable: bool


@dataclass(frozen=True)
class ReviewEventPage:
    snapshot: ReviewEventSnapshot
    events: tuple[PersistedReviewEvent, ...]
