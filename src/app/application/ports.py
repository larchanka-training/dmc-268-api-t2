from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Protocol


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


@dataclass(frozen=True, slots=True)
class StartReviewCommand:
    change_request_id: uuid.UUID
    repository_settings_id: uuid.UUID
    requested_head_sha: str
    rules_digest: str
    trigger_type: str
    initiator_user_id: uuid.UUID | None
    model_ref: str
    model_digest: str | None
    model_settings: dict[str, object]
    prompt_version: str
    prompt_digest: str
    trace_id: str


class VcsPort(Protocol):
    async def capture_snapshot(
        self, *, review_job_id: uuid.UUID
    ) -> ChangeRequestSnapshot: ...


class ContextBuilderPort(Protocol):
    async def build(
        self, *, review_job_id: uuid.UUID
    ) -> Sequence[BuiltContextPayload]: ...


class LlmGatewayPort(Protocol):
    async def review(self, payload: BuiltContextPayload) -> ReviewChunkResult: ...


class PublisherPort(Protocol):
    async def publish(self, *, review_job_id: uuid.UUID) -> dict[str, object]: ...


class ReviewJobRepositoryPort(Protocol):
    async def get(self, review_job_id: uuid.UUID) -> object | None: ...

    async def add_start_review(
        self, command: StartReviewCommand, *, config_digest: str
    ) -> uuid.UUID: ...


class UnitOfWorkPort(Protocol):
    review_jobs: ReviewJobRepositoryPort

    async def __aenter__(self) -> UnitOfWorkPort: ...
    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...
    async def commit(self) -> None: ...
    async def rollback(self) -> None: ...
