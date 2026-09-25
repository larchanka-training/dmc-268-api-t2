from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol, Sequence


@dataclass(frozen=True, slots=True)
class ChangeRequestSnapshot:
    provider_name: str
    requested_head_sha: str
    base_sha: str
    merge_base_sha: str
    source_repository_external_id: str
    base_repository_external_id: str
    provider_diff_version: dict[str, object]


@dataclass(frozen=True, slots=True)
class BuiltContextPayload:
    schema_version: int
    payload_body: dict[str, object]


@dataclass(frozen=True, slots=True)
class ReviewChunkResult:
    schema_version: int
    summary: str
    limitations: dict[str, object]
    usage: dict[str, object]
    latency_ms: int
    findings: Sequence[dict[str, object]]


class VcsPort(Protocol):
    async def capture_snapshot(self, *, review_job_id: uuid.UUID) -> ChangeRequestSnapshot: ...


class ContextBuilderPort(Protocol):
    async def build(self, *, review_job_id: uuid.UUID) -> Sequence[BuiltContextPayload]: ...


class LlmGatewayPort(Protocol):
    async def review(self, payload: BuiltContextPayload) -> ReviewChunkResult: ...


class PublisherPort(Protocol):
    async def publish(self, *, review_job_id: uuid.UUID) -> dict[str, object]: ...


class ReviewJobRepositoryPort(Protocol):
    async def get(self, review_job_id: uuid.UUID) -> object | None: ...


class UnitOfWorkPort(Protocol):
    review_jobs: ReviewJobRepositoryPort

    async def __aenter__(self) -> "UnitOfWorkPort": ...
    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None: ...
    async def commit(self) -> None: ...
    async def rollback(self) -> None: ...
