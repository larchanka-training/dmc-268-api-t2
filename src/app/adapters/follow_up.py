"""System follow-up start (spec dmc-268-ui-t2 PR #42 §3.2.1).

When an analysis turns terminal and the live PR head has moved on, exactly one
new review is admitted for the latest head, coalescing intermediate pushes. It
goes through the same admission as ``startReview``: initiator access, quotas,
frozen configuration and one active review per PR. The idempotency key
``systemFollowUp:{J}`` makes repeated terminal deliveries replay, not restart.
"""

import hashlib
import json
from copy import deepcopy
from enum import StrEnum
from pathlib import Path
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.github_ports import GithubFailure, GithubPullRequestReader
from app.application.ports import StartReviewCommand
from app.application.review_start_ports import (
    AdmissionResourceNotFoundError,
    ChangeRequestIdentity,
    IdempotencyConflictError,
    PullRequestReviewActiveError,
    RepositoryIdentity,
    ReviewAlreadyActiveError,
    ReviewConfigurationResolver,
    ReviewHeadMismatchError,
    ReviewQuotaExceededError,
    ReviewStartAuthorizer,
    SettingsVersionChangedError,
)
from app.core.metrics import count
from app.db.models import ChangeRequest, Repository, RepositorySettings, ReviewJob
from app.db.review_starts import change_identity, persist_start, settings_identity
from app.domain.enums import ReviewStatus
from app.domain.frozen_configuration import FrozenConfigurationError
from app.domain.settings import (
    compute_repository_settings_digest,
    project_historical_empty_settings,
)
from app.review.configuration import validate_admission_configuration

TRIGGER_TYPE = "system_follow_up"


class FollowUpOutcome(StrEnum):
    STARTED = "started"
    REPLAYED = "replayed"
    NOT_TERMINAL = "not_terminal"
    SAME_HEAD = "same_head"
    CLOSED = "closed"
    NO_INITIATOR = "no_initiator"
    VCS_UNAVAILABLE = "vcs_unavailable"
    ACCESS_DENIED = "access_denied"
    CONFIGURATION_UNAVAILABLE = "configuration_unavailable"
    BUSY = "busy"
    QUOTA_EXCEEDED = "quota_exceeded"
    NOT_ADMISSIBLE = "not_admissible"


_ADMISSION_OUTCOMES: dict[type[Exception], FollowUpOutcome] = {
    PullRequestReviewActiveError: FollowUpOutcome.BUSY,
    ReviewAlreadyActiveError: FollowUpOutcome.BUSY,
    ReviewQuotaExceededError: FollowUpOutcome.QUOTA_EXCEEDED,
    AdmissionResourceNotFoundError: FollowUpOutcome.NOT_ADMISSIBLE,
    SettingsVersionChangedError: FollowUpOutcome.NOT_ADMISSIBLE,
    ReviewHeadMismatchError: FollowUpOutcome.NOT_ADMISSIBLE,
    IdempotencyConflictError: FollowUpOutcome.NOT_ADMISSIBLE,
}


class SystemFollowUp:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        pulls: GithubPullRequestReader,
        resolver: ReviewConfigurationResolver,
        *,
        authorizer: ReviewStartAuthorizer,
        prompt_root: Path,
    ) -> None:
        self._factory = factory
        self._pulls = pulls
        self._resolver = resolver
        self._authorizer = authorizer
        self._prompt_root = prompt_root

    async def after_terminal(self, review_id: UUID) -> FollowUpOutcome:
        outcome = await self._follow_up(review_id)
        count("follow_up", outcome=outcome.value)
        structlog.get_logger().info(
            "review_follow_up", review_id=str(review_id), outcome=outcome.value
        )
        return outcome

    async def _follow_up(  # noqa: PLR0911 -- one outcome per admission stop
        self, review_id: UUID
    ) -> FollowUpOutcome:
        async with self._factory() as session:
            job = await session.get(ReviewJob, review_id)
            if job is None or job.finished_at is None:
                return FollowUpOutcome.NOT_TERMINAL
            if job.initiator_user_id is None:
                return FollowUpOutcome.NO_INITIATOR
            repository = await session.get(Repository, job.repository_id)
            change = await session.get(ChangeRequest, job.change_request_id)
            assert repository is not None and change is not None
            settings = (
                await session.get(RepositorySettings, repository.current_settings_id)
                if repository.current_settings_id
                else None
            )
            if settings is None:
                return FollowUpOutcome.CONFIGURATION_UNAVAILABLE
            prior = await _elected_prior(session, job.change_request_id)
            identity = RepositoryIdentity(
                repository.id,
                repository.provider_name,
                repository.external_id,
                repository.owner,
                repository.repo_name,
            )
            saved_change = change_identity(change)
            expected_settings = settings_identity(settings)
            settings_digest = compute_repository_settings_digest(
                id=settings.id,
                repository_id=settings.repository_id,
                rules=settings.rules,
                ignores=settings.ignores,
                output_language=settings.output_language,
                surrounding_lines=settings.surrounding_lines,
            )
            instructions = tuple(
                project_historical_empty_settings(settings.rules, "instructions")[
                    "instructions"
                ]
            )
            settings_id, rules_digest = settings.id, settings.rules_digest
            requested_head, number = job.requested_head_sha, change.external_number
            initiator, change_request_id = job.initiator_user_id, change.id
        try:
            live = await self._pulls.get_pull_request(
                identity.owner, identity.name, identity.external_id, int(number)
            )
        except GithubFailure as failure:
            if failure.reason_code == "PR_STALE_OR_CLOSED":
                return FollowUpOutcome.CLOSED
            return FollowUpOutcome.VCS_UNAVAILABLE
        if live.head_sha == requested_head:
            return FollowUpOutcome.SAME_HEAD
        # Like startReview: the initiator's current provider permission, with
        # their own credential, not only the stored access row.
        try:
            await self._authorizer.authorize_start(
                user_id=initiator, repository=identity
            )
        except GithubFailure as failure:
            if failure.reason_code == "VCS_ACCESS_DENIED":
                return FollowUpOutcome.ACCESS_DENIED
            return FollowUpOutcome.VCS_UNAVAILABLE
        try:
            config = await self._resolver.resolve(
                repository=identity, settings_id=settings_id, rules_digest=rules_digest
            )
            policy = validate_admission_configuration(
                config,
                settings_digest=settings_digest,
                instructions=instructions,
                prompt_root=self._prompt_root,
            )
        except FrozenConfigurationError, ValueError:
            return FollowUpOutcome.CONFIGURATION_UNAVAILABLE
        command = StartReviewCommand(
            change_request_id=change_request_id,
            repository_settings_id=settings_id,
            requested_head_sha=live.head_sha,
            rules_digest=rules_digest,
            trigger_type=TRIGGER_TYPE,
            initiator_user_id=initiator,
            model_ref=config.model_ref,
            model_digest=config.model_digest,
            model_settings=deepcopy(policy.model_dump(mode="json")),
            frozen_configuration_version=1,
            prompt_version=config.prompt_version,
            prompt_digest=config.prompt_digest,
            trace_id=f"follow-up-{review_id}",
            rerun_of=prior,
        )
        try:
            _, _, created = await persist_start(
                command,
                factory=self._factory,
                expected_repository=identity,
                scope=f"systemFollowUp:{review_id}",
                key=review_id.hex,
                request_hash=hashlib.sha256(
                    json.dumps({"follow_up_of": str(review_id)}).encode()
                ).hexdigest(),
                expected_change=ChangeRequestIdentity(
                    number,
                    live.head_sha,
                    live.base_sha,
                    live.source_repository_external_id,
                    live.source_ref,
                    live.base_ref,
                    "open",
                ),
                expected_settings=expected_settings,
                new_change=live,
                saved_change=saved_change,
                one_active_per_pr=True,
            )
        except tuple(_ADMISSION_OUTCOMES) as error:
            return _ADMISSION_OUTCOMES[type(error)]
        return FollowUpOutcome.STARTED if created else FollowUpOutcome.REPLAYED


async def _elected_prior(session: AsyncSession, change_request_id: UUID) -> UUID | None:
    """Spec §3.1: the latest finished COMPLETED/PARTIAL review of the PR."""
    prior: UUID | None = await session.scalar(
        select(ReviewJob.id)
        .where(
            ReviewJob.change_request_id == change_request_id,
            ReviewJob.status.in_([ReviewStatus.COMPLETED, ReviewStatus.PARTIAL]),
            ReviewJob.finished_at.is_not(None),
        )
        .order_by(
            ReviewJob.finished_at.desc(), ReviewJob.created_at.desc(), ReviewJob.id
        )
        .limit(1)
    )
    return prior
