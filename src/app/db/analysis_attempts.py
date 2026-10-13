"""The only owner of the initial analysis clock and execution counter."""

from datetime import timedelta

from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.analysis_ports import BeginAnalysisDecision
from app.application.ports import LeaseLostError, QueueTaskEnvelope, TaskLeaseSnapshot
from app.db.analysis_authority import (
    live_fence,
    lock_analysis,
    validate_config_identity,
)
from app.db.models import ReviewJob
from app.domain.frozen_configuration import FrozenConfigurationError, ModelSettings


class PostgresAnalysisAttemptStore:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def begin(
        self, *, task: QueueTaskEnvelope, lease: TaskLeaseSnapshot
    ) -> BeginAnalysisDecision:
        async with self._factory.begin() as session:
            job, _, now = await lock_analysis(session, task, lease)
            if job.frozen_configuration_version is None:
                return BeginAnalysisDecision(
                    False,
                    job.analysis_attempts,
                    job.started_at,
                    job.analysis_deadline_at,
                    "historical_budget_unknown",
                )
            try:
                policy = ModelSettings.from_stored(job.model_settings)
                validate_config_identity(job)
            except (ValueError, TypeError) as exc:
                return BeginAnalysisDecision(
                    False,
                    job.analysis_attempts,
                    job.started_at,
                    job.analysis_deadline_at,
                    exc.reason
                    if isinstance(exc, FrozenConfigurationError)
                    else "unsupported_model_settings",
                )
            if job.analysis_deadline_at is not None and job.analysis_deadline_at <= now:
                return BeginAnalysisDecision(
                    False,
                    job.analysis_attempts,
                    job.started_at,
                    job.analysis_deadline_at,
                    "analysis_deadline_exceeded",
                )
            if job.started_at is None and job.queue_deadline_at <= now:
                return BeginAnalysisDecision(
                    False,
                    job.analysis_attempts,
                    job.started_at,
                    job.analysis_deadline_at,
                    "queue_deadline_exceeded",
                )
            if (
                job.analysis_attempt_fence != lease.fencing_token
                and job.analysis_attempts >= policy.retry.worker_max_attempts
            ):
                return BeginAnalysisDecision(
                    False,
                    job.analysis_attempts,
                    job.started_at,
                    job.analysis_deadline_at,
                    "analysis_attempts_exhausted",
                )
            started_at = job.started_at or now
            deadline = job.analysis_deadline_at or now + timedelta(
                milliseconds=policy.limits.analysis_duration_ms
            )
            attempt = job.analysis_attempts + (
                job.analysis_attempt_fence != lease.fencing_token
            )
            changed = await session.scalar(
                update(ReviewJob)
                .where(
                    ReviewJob.id == lease.review_job_id,
                    ReviewJob.finished_at.is_(None),
                    live_fence(lease),
                    (
                        ReviewJob.analysis_deadline_at.is_(None)
                        | (ReviewJob.analysis_deadline_at > func.clock_timestamp())
                    ),
                )
                .values(
                    started_at=started_at,
                    analysis_deadline_at=deadline,
                    analysis_attempts=attempt,
                    analysis_attempt_fence=lease.fencing_token,
                )
                .returning(ReviewJob.id)
            )
            if changed is None:
                raise LeaseLostError("Analysis begin authority expired")
            return BeginAnalysisDecision(True, attempt, started_at, deadline)
