from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from pydantic import ValidationError
from sqlalchemy import exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ports import (
    LeaseLostError,
    PublicationSnapshot,
    ReviewJobSnapshot,
    StartReviewCommand,
    TaskLeaseSnapshot,
)
from app.db.models import ChangeRequest, OutboxEvent, Publication, ReviewJob, TaskLease
from app.db.task_leases import lock_owned_lease, lock_publication
from app.domain.enums import PublicationStatus, ReviewStatus, TaskKind
from app.domain.frozen_configuration import FrozenConfigurationError, ModelSettings


class SqlAlchemyReviewJobRepository:
    """Persist review aggregates without owning the surrounding transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, review_job_id: uuid.UUID) -> ReviewJobSnapshot | None:
        review_job = await self._session.get(ReviewJob, review_job_id)
        if review_job is None:
            return None
        return ReviewJobSnapshot(
            id=review_job.id,
            repository_id=review_job.repository_id,
            change_request_id=review_job.change_request_id,
            repository_settings_id=review_job.repository_settings_id,
            requested_head_sha=review_job.requested_head_sha,
            config_digest=review_job.config_digest,
            status=review_job.status,
            created_at=review_job.created_at,
            finished_at=review_job.finished_at,
        )

    async def add_start_review(
        self, command: StartReviewCommand, *, config_digest: str
    ) -> uuid.UUID:
        if command.frozen_configuration_version is not None:
            if (
                type(command.frozen_configuration_version) is not int
                or command.frozen_configuration_version != 1
            ):
                raise FrozenConfigurationError(
                    "unsupported_frozen_configuration_version"
                )
            try:
                ModelSettings.from_stored(command.model_settings)
            except ValidationError, TypeError, ValueError:
                raise FrozenConfigurationError("unsupported_model_settings") from None
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
                    rerun_of=command.rerun_of,
                    model_ref=command.model_ref,
                    model_digest=command.model_digest,
                    model_settings=command.model_settings,
                    frozen_configuration_version=command.frozen_configuration_version,
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

    async def update_status(
        self, lease: TaskLeaseSnapshot, *, status: ReviewStatus
    ) -> None:
        locked = await lock_owned_lease(self._session, lease)
        if (
            locked is None
            or locked[1].task_kind != "analyze"
            or locked[0].finished_at is not None
        ):
            raise LeaseLostError("Review lease is no longer current")
        _, _, now = locked
        terminal = status in {
            ReviewStatus.COMPLETED,
            ReviewStatus.PARTIAL,
            ReviewStatus.FAILED,
            ReviewStatus.SKIPPED,
        }
        updated = await self._session.scalar(
            update(ReviewJob)
            .where(
                ReviewJob.id == lease.review_job_id,
                ReviewJob.finished_at.is_(None),
                exists().where(
                    TaskLease.review_job_id == lease.review_job_id,
                    TaskLease.owner == lease.owner,
                    TaskLease.task_kind == "analyze",
                    TaskLease.fencing_token == lease.fencing_token,
                    TaskLease.expires_at > func.clock_timestamp(),
                ),
            )
            .values(status=status.value, finished_at=now if terminal else None)
            .returning(ReviewJob.id)
        )
        if updated is None:
            raise LeaseLostError("Review lease expired before the write")


class SqlAlchemyPublicationRepository:
    """Publication writes share the caller's review UoW and fencing locks."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, review_job_id: uuid.UUID) -> PublicationSnapshot | None:
        publication = await self._session.scalar(
            select(Publication)
            .where(Publication.review_job_id == review_job_id)
            .execution_options(populate_existing=True)
        )
        if publication is None:
            return None
        return PublicationSnapshot(
            review_job_id=publication.review_job_id,
            status=publication.status,
            provider_metadata=publication.provider_metadata,
            started_at=publication.started_at,
            deadline_at=publication.deadline_at,
            finished_at=publication.finished_at,
        )

    async def prepare(self, lease: TaskLeaseSnapshot) -> None:
        locked = await lock_owned_lease(self._session, lease)
        if (
            locked is None
            or locked[1].task_kind != "analyze"
            or locked[0].status not in {"COMPLETED", "PARTIAL"}
            or locked[0].finished_at is None
        ):
            raise LeaseLostError("Analysis lease is no longer current")
        publication = await lock_publication(self._session, lease.review_job_id)
        if publication is None or publication.status != "NOT_READY":
            raise LeaseLostError("Publication is not eligible for preparation")
        now = (await self._session.execute(select(func.clock_timestamp()))).scalar_one()
        updated = await self._session.scalar(
            update(Publication)
            .where(
                Publication.review_job_id == lease.review_job_id,
                Publication.status == "NOT_READY",
                exists().where(
                    ReviewJob.id == lease.review_job_id,
                    ReviewJob.status.in_(("COMPLETED", "PARTIAL")),
                    ReviewJob.finished_at.is_not(None),
                ),
                exists().where(
                    TaskLease.review_job_id == lease.review_job_id,
                    TaskLease.owner == lease.owner,
                    TaskLease.task_kind == "analyze",
                    TaskLease.fencing_token == lease.fencing_token,
                    TaskLease.expires_at > func.clock_timestamp(),
                ),
            )
            .values(
                status=PublicationStatus.PENDING.value,
                started_at=now,
                deadline_at=now + timedelta(minutes=5),
                finished_at=None,
            )
            .returning(Publication.id)
        )
        if updated is None:
            raise LeaseLostError(
                "Analysis lease expired before publication preparation"
            )

    async def update(
        self,
        lease: TaskLeaseSnapshot,
        *,
        status: PublicationStatus,
        provider_metadata: dict[str, object] | None,
    ) -> None:
        locked = await lock_owned_lease(self._session, lease)
        if (
            locked is None
            or locked[1].task_kind != "publish"
            or status == PublicationStatus.NOT_READY
        ):
            raise LeaseLostError("Publication lease is no longer current")
        publication = await lock_publication(self._session, lease.review_job_id)
        now = (await self._session.execute(select(func.clock_timestamp()))).scalar_one()
        progress = status == PublicationStatus.PENDING
        if (
            locked[0].status not in {"COMPLETED", "PARTIAL"}
            or locked[0].finished_at is None
            or publication is None
            or publication.status != "PENDING"
            or publication.deadline_at is None
            or (progress and publication.deadline_at <= now)
        ):
            raise LeaseLostError("Publication is no longer eligible")
        updated = await self._session.scalar(
            update(Publication)
            .where(
                Publication.review_job_id == lease.review_job_id,
                Publication.status == "PENDING",
                Publication.deadline_at > func.clock_timestamp()
                if progress
                else Publication.deadline_at.is_not(None),
                exists().where(
                    ReviewJob.id == lease.review_job_id,
                    ReviewJob.status.in_(("COMPLETED", "PARTIAL")),
                    ReviewJob.finished_at.is_not(None),
                ),
                exists().where(
                    TaskLease.review_job_id == lease.review_job_id,
                    TaskLease.owner == lease.owner,
                    TaskLease.task_kind == "publish",
                    TaskLease.fencing_token == lease.fencing_token,
                    TaskLease.expires_at > func.clock_timestamp(),
                ),
            )
            .values(
                status=status.value,
                provider_metadata=provider_metadata,
                finished_at=None if progress else func.clock_timestamp(),
            )
            .returning(Publication.id)
        )
        if updated is None:
            raise LeaseLostError(
                "Publication lease or deadline expired before the write"
            )
