"""Persisted inputs and authority for the publication application boundary."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class PersistedFinding:
    id: UUID
    fingerprint: str
    path: str
    side: str
    start_line: int
    end_line: int
    category: str
    severity: str
    title: str
    explanation: str
    evidence: str
    recommendation: str
    proposed_diff_fix: str | None
    rule_ref: str | None = None


@dataclass(frozen=True, slots=True)
class PublicationInput:
    review_id: UUID
    change_request_id: UUID
    owner: str
    name: str
    repository_external_id: str
    number: int
    head_sha: str
    base_sha: str
    merge_base_sha: str
    config_digest: str
    summary: str
    coverage: dict[str, Any]
    contexts: tuple[dict[str, Any], ...]
    findings: tuple[PersistedFinding, ...]


@dataclass(frozen=True, slots=True)
class PublicationAuthority:
    review_job_id: UUID
    owner: str
    fencing_token: int
    task_kind: str = "publish"


@dataclass(frozen=True, slots=True)
class PublicationClaim:
    review_id: UUID
    change_request_id: UUID
    operation_id: UUID
    authority: PublicationAuthority
    marker: str
    payload_hash: str


@dataclass(frozen=True, slots=True)
class PublicationState:
    review_id: UUID
    status: str
    provider_metadata: dict[str, Any] | None
    started_at: datetime | None
    deadline_at: datetime | None
    finished_at: datetime | None


@dataclass(frozen=True, slots=True)
class PublicationOperationSnapshot:
    review_id: UUID
    change_request_id: UUID
    operation_id: UUID
    owner: str
    fencing_token: int
    marker: str
    payload_hash: str
    state: str


class PublicationAuthorityError(RuntimeError):
    """The durable operation or its fencing authority is no longer current."""


class PublicationStore(Protocol):
    async def load(self, review_id: UUID) -> PublicationInput | None: ...

    async def begin(
        self,
        review_id: UUID,
        *,
        authority: PublicationAuthority,
        marker: str,
        payload_hash: str,
    ) -> PublicationClaim | None: ...

    async def check_authority(self, claim: PublicationClaim) -> bool: ...

    async def check_reconciliation_authority(
        self,
        review_id: UUID,
        authority: PublicationAuthority,
    ) -> bool: ...

    async def save(
        self,
        claim: PublicationClaim,
        *,
        status: str,
        provider_metadata: dict[str, object] | None,
    ) -> None: ...

    async def get(self, review_id: UUID) -> PublicationState | None: ...

    async def get_operation(
        self,
        review_id: UUID,
    ) -> PublicationOperationSnapshot | None: ...

    async def reconcile(
        self,
        review_id: UUID,
        *,
        authority: PublicationAuthority,
        status: str,
        confirmed_absent: bool,
        provider_metadata: dict[str, object] | None,
    ) -> None: ...
