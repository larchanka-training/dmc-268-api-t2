from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ports import StartReviewCommand
from app.db.models import ChangeRequest, OutboxEvent, Publication, ReviewJob
from app.domain.enums import PublicationStatus, ReviewStatus, TaskKind


class SqlAlchemyReviewJobRepository:
    """Persist review aggregates without owning the surrounding transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, review_job_id: uuid.UUID) -> ReviewJob | None:
        return await self._session.get(ReviewJob, review_job_id)

    async def add_start_review(
        self, command: StartReviewCommand, *, config_digest: str
    ) -> uuid.UUID:
        repository_id = await self._session.scalar(
            select(ChangeRequest.repository_id).where(
                ChangeRequest.id == command.change_request_id
            )
        )
        if repository_id is None:
            raise LookupError(
                f"ChangeRequest {command.change_request_id} was not found"
            )

        now = datetime.now(UTC)
        review_id = uuid.uuid4()
        self._session.add_all(
            [
                ReviewJob(
                    id=review_id,
                    repository_id=repository_id,
                    change_request_id=command.change_request_id,
                    repository_settings_id=command.repository_settings_id,
                    config_digest=config_digest,
                    rules_digest=command.rules_digest,
                    trigger_type=command.trigger_type,
                    initiator_user_id=command.initiator_user_id,
                    requested_head_sha=command.requested_head_sha,
                    model_ref=command.model_ref,
                    model_digest=command.model_digest,
                    model_settings=command.model_settings,
                    prompt_version=command.prompt_version,
                    prompt_digest=command.prompt_digest,
                    created_at=now,
                    finished_at=None,
                    status=ReviewStatus.QUEUED.value,
                    queue_deadline_at=now + timedelta(minutes=15),
                    coverage={},
                ),
                Publication(
                    id=uuid.uuid4(),
                    review_job_id=review_id,
                    status=PublicationStatus.NOT_READY.value,
                    provider_metadata=None,
                    created_at=now,
                ),
                OutboxEvent(
                    id=uuid.uuid4(),
                    review_job_id=review_id,
                    schema_version=1,
                    task_kind=TaskKind.ANALYZE.value,
                    attempt=1,
                    trace_id=command.trace_id,
                    created_at=now,
                    broker_published_at=None,
                ),
            ]
        )
        return review_id
