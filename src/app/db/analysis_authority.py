"""Shared lock order for analysis accounting, with time sampled after locks."""

from datetime import datetime

from sqlalchemy import exists, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.application.ports import (
    LeaseLostError,
    QueueTaskEnvelope,
    StartReviewCommand,
    TaskLeaseSnapshot,
)
from app.application.use_cases.start_review import compute_config_digest
from app.db.models import ReviewJob, TaskLease
from app.db.task_leases import lock_review_job, lock_task_lease
from app.db.worker_tasks import QueueIdentityError, validate_task_identity
from app.domain.frozen_configuration import FrozenConfigurationError


def validate_config_identity(job: ReviewJob) -> None:
    """Check the admitted identity using the authoritative locked job values."""
    command = StartReviewCommand(
        change_request_id=job.change_request_id,
        repository_settings_id=job.repository_settings_id,
        requested_head_sha=job.requested_head_sha,
        rules_digest=job.rules_digest,
        trigger_type=job.trigger_type,
        initiator_user_id=job.initiator_user_id,
        model_ref=job.model_ref,
        model_digest=job.model_digest,
        model_settings=job.model_settings,
        prompt_version=job.prompt_version,
        prompt_digest=job.prompt_digest,
        trace_id="accounting-identity",
    )
    if compute_config_digest(command) != job.config_digest:
        raise FrozenConfigurationError("config_digest_mismatch")


def live_fence(lease: TaskLeaseSnapshot) -> ColumnElement[bool]:
    return (
        exists()
        .where(
            TaskLease.review_job_id == lease.review_job_id,
            TaskLease.task_kind == "analyze",
            TaskLease.owner == lease.owner,
            TaskLease.fencing_token == lease.fencing_token,
            TaskLease.expires_at > func.clock_timestamp(),
        )
        .correlate(None)
    )


async def lock_analysis(
    session: AsyncSession,
    task: QueueTaskEnvelope,
    claim: TaskLeaseSnapshot,
    *,
    ledger: bool = False,
) -> tuple[ReviewJob, TaskLease, datetime]:
    if (
        task.task_kind != "analyze"
        or claim.task_kind != "analyze"
        or task.review_id != str(claim.review_job_id)
    ):
        raise LeaseLostError("Analysis task and lease do not match")
    job = await lock_review_job(session, claim.review_job_id)
    lease = await lock_task_lease(session, claim.review_job_id)
    if job is None or lease is None:
        raise LeaseLostError("Analysis lease is no longer current")
    if not await validate_task_identity(session, task):
        raise QueueIdentityError("Queue identity is quarantined")
    await session.execute(text("LOCK TABLE review_job IN ROW EXCLUSIVE MODE"))
    if ledger:
        await session.execute(
            text("LOCK TABLE review_llm_attempt IN ROW EXCLUSIVE MODE")
        )
    now = await session.scalar(select(func.clock_timestamp()))
    assert now is not None
    if (
        lease.owner != claim.owner
        or lease.task_kind != "analyze"
        or lease.fencing_token != claim.fencing_token
        or lease.expires_at <= now
    ):
        raise LeaseLostError("Analysis lease is no longer current")
    return job, lease, now
