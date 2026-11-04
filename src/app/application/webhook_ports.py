"""Durable GitHub intake records, independent of review-job execution."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from app.application.github_ports import GithubFailure, GithubRequest


class InboxUnavailable(Exception):  # noqa: N818 - application boundary failure
    """Intake could not reach a durable persistence decision."""


@dataclass(frozen=True, slots=True)
class WebhookDelivery:
    id: UUID
    status: str
    request: GithubRequest
    snapshot: dict[str, Any] | None
    attempts: int
    raw_diff: str | None
    reason_code: str | None
    ignore_globs: tuple[str, ...]


class WebhookInbox(Protocol):
    async def accept(self, delivery_id: str, request: GithubRequest) -> UUID | None: ...
    async def get(self, receipt_id: UUID) -> WebhookDelivery | None: ...
    async def claim(
        self, receipt_id: UUID, *, now: datetime | None = None
    ) -> WebhookDelivery | None: ...
    async def complete(
        self, claim: WebhookDelivery, snapshot: dict[str, Any], raw_diff: str
    ) -> bool: ...
    async def fail(
        self,
        claim: WebhookDelivery,
        failure: GithubFailure,
        *,
        now: datetime | None = None,
    ) -> None: ...
    async def pending(
        self, *, now: datetime | None = None, limit: int = 100
    ) -> list[UUID]: ...
