from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.domain.enums import PublicationStatus, ReviewStatus, TaskKind
from app.db.models import OutboxEvent, Publication, ReviewJob


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


@dataclass(frozen=True, slots=True)
class StartReviewAggregate:
    review_job: ReviewJob
    publication: Publication
    outbox_event: OutboxEvent


def build_start_review(command: StartReviewCommand) -> StartReviewAggregate:
    """Build the rows that must be persisted atomically for a new review.

    Access checks, quota reservation and Idempotency-Key handling belong to the
    surrounding application use case/UoW. This function only builds the rows
    whose fields are already fixed by ERD/System Design.
    """

    now = datetime.now(timezone.utc)
    review_id = uuid.uuid4()

    review_job = ReviewJob(
        id=review_id,
        change_request_id=command.change_request_id,
        repository_settings_id=command.repository_settings_id,
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
        status=ReviewStatus.QUEUED.value,
        stage=None,
        queue_deadline_at=now + timedelta(minutes=15),
        coverage={},
    )

    publication = Publication(
        id=uuid.uuid4(),
        review_job_id=review_id,
        status=PublicationStatus.NOT_READY.value,
        provider_metadata=None,
        created_at=now,
    )

    outbox_event = OutboxEvent(
        id=uuid.uuid4(),
        review_job_id=review_id,
        schema_version=1,
        task_kind=TaskKind.ANALYZE.value,
        attempt=1,
        trace_id=command.trace_id,
        created_at=now,
        broker_published_at=None,
    )

    return StartReviewAggregate(
        review_job=review_job,
        publication=publication,
        outbox_event=outbox_event,
    )
