"""Ports for mirroring review cycles to GitHub commit statuses."""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

# Failed posts after which a cycle is parked; the backoff spans ~10-20 min by then.
MAX_FAILED_ATTEMPTS = 10


@dataclass(frozen=True, slots=True)
class CommitStatusTarget:
    review_id: UUID
    owner: str
    name: str
    head_sha: str
    review_status: str
    critical_findings: int
    posted_state: str | None
    failed_attempts: int


class CommitStatusStore(Protocol):
    async def due(self, *, limit: int) -> list[CommitStatusTarget]: ...

    async def record_posted(self, review_id: UUID, state: str) -> None: ...

    async def record_failure(
        self, review_id: UUID, *, retry_after_s: float
    ) -> None: ...

    async def park(self, review_id: UUID) -> None:
        """Stop retrying a cycle: GitHub will not accept its status."""
        ...


class CommitStatusWriter(Protocol):
    async def create_commit_status(  # noqa: PLR0913 -- one GitHub status payload
        self,
        owner: str,
        name: str,
        sha: str,
        *,
        state: str,
        description: str,
        context: str,
        target_url: str | None,
    ) -> None: ...
