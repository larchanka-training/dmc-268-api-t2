"""Atomic persistence for authenticated review admission."""

import hashlib
import json
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, func, literal_column, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.github_ports import GithubPullRequest
from app.application.ports import StartReviewCommand
from app.application.review_start_ports import (
    AdmissionResourceNotFoundError,
    AutomaticReceiptClaimLostError,
    ChangeRequestIdentity,
    IdempotencyConflictError,
    PullRequestReviewActiveError,
    RepositoryIdentity,
    ReviewAlreadyActiveError,
    ReviewHeadMismatchError,
    ReviewQuotaExceededError,
    SettingsVersionChangedError,
)
from app.application.use_cases.start_review import compute_config_digest
from app.application.webhook_ports import AutomaticReceiptContext
from app.context.snapshot import valid_snapshot
from app.db.models import (
    ChangeRequest,
    IdempotencyRecord,
    QuotaUsage,
    Repository,
    RepositoryAccess,
    RepositorySettings,
    ReviewJob,
    WebhookReceipt,
)
from app.db.uow import SqlAlchemyUnitOfWork


def change_identity(change: ChangeRequest) -> ChangeRequestIdentity:
    return ChangeRequestIdentity(
        change.external_number,
        change.head_sha,
        change.base_sha,
        change.source_repository_external_id,
        change.source_ref,
        change.base_ref,
        change.current_status,
    )


def settings_identity(settings: RepositorySettings) -> str:
    values = {
        name: getattr(settings, name)
        for name in (
            "rules",
            "ignores",
            "rules_digest",
            "max_starts_per_hour",
            "max_active_jobs",
            "output_language",
            "surrounding_lines",
        )
    }
    if settings.review_policy is not None:
        values["review_policy"] = settings.review_policy
    return hashlib.sha256(
        json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


async def find_replay(
    session: AsyncSession, *, scope: str, key: str, request_hash: str
) -> tuple[UUID, str] | None:
    row = (
        await session.execute(
            select(ReviewJob.id, ReviewJob.status, IdempotencyRecord.request_hash)
            .join(IdempotencyRecord, IdempotencyRecord.review_job_id == ReviewJob.id)
            .where(
                IdempotencyRecord.scope == scope,
                IdempotencyRecord.idempotency_key == key,
                IdempotencyRecord.expires_at > func.clock_timestamp(),
            )
        )
    ).one_or_none()
    if row is not None and row.request_hash != request_hash:
        raise IdempotencyConflictError
    return None if row is None else (row.id, row.status)


async def _check_quotas(
    session: AsyncSession,
    repository_id: UUID,
    user_id: UUID | None,
    settings: RepositorySettings,
    window: datetime,
) -> None:
    if settings.max_starts_per_hour == 0 or settings.max_active_jobs == 0:
        raise ReviewQuotaExceededError
    starts = await session.scalar(
        select(QuotaUsage.value).where(
            QuotaUsage.repository_id == repository_id,
            QuotaUsage.user_id == user_id,
            QuotaUsage.type == "starts",
            QuotaUsage.window_start == window,
        )
    )
    if (starts or 0) >= settings.max_starts_per_hour:
        raise ReviewQuotaExceededError
    active = await session.scalar(
        select(func.count())
        .select_from(ReviewJob)
        .where(
            ReviewJob.repository_id == repository_id,
            ReviewJob.finished_at.is_(None),
        )
    )
    if (active or 0) >= settings.max_active_jobs:
        raise ReviewQuotaExceededError


async def _refresh_saved_change(  # noqa: PLR0913, PLR0917 -- explicit optimistic-lock identities
    session: AsyncSession,
    command: StartReviewCommand,
    repository_id: UUID,
    saved_change: ChangeRequestIdentity,
    expected_change: ChangeRequestIdentity,
    live: GithubPullRequest,
) -> None:
    """Refresh a previously seen PR row to what GitHub just reported.

    The row is a cache of the provider PR: pushes after the last start must be
    admitted, and older in-flight runs must see their head as stale.
    """
    saved = await session.scalar(
        select(ChangeRequest)
        .where(
            ChangeRequest.id == command.change_request_id,
            ChangeRequest.repository_id == repository_id,
        )
        .with_for_update()
    )
    if saved is None:
        raise AdmissionResourceNotFoundError
    # Another start may have moved the row while GitHub was read; never
    # overwrite a newer head with the state this request observed earlier.
    # A concurrent start that already refreshed it to this same state is fine.
    if change_identity(saved) not in {saved_change, expected_change}:
        if saved.head_sha != live.head_sha:
            raise ReviewHeadMismatchError
        raise AdmissionResourceNotFoundError
    saved.head_sha = live.head_sha
    saved.base_sha = live.base_sha
    saved.source_repository_external_id = live.source_repository_external_id
    saved.source_ref = live.source_ref
    saved.base_ref = live.base_ref
    saved.title = live.title
    saved.description = live.description


async def persist_start(  # noqa: PLR0912, PLR0913, PLR0915 -- atomic admission with optional materialization
async def _verify_effective_settings(
    session: AsyncSession,
    command: StartReviewCommand,
    source_id: UUID,
    expected_settings: str,
    repository_id: UUID,
) -> None:
    if source_id != command.repository_settings_id:
        effective = await session.get(
            RepositorySettings, command.repository_settings_id
        )
        if (
            effective is None
            or effective.repository_id != repository_id
            or not valid_snapshot(effective)
            or effective.rules_source is None
            or effective.rules_source.get("source_settings_id") != str(source_id)
            or effective.rules_source.get("source_settings_identity")
            != expected_settings
        ):
            raise SettingsVersionChangedError


async def persist_start(  # noqa: PLR0912, PLR0913, PLR0915 -- one fenced aggregate transaction
    command: StartReviewCommand,
    *,
    factory: async_sessionmaker[AsyncSession],
    expected_repository: RepositoryIdentity,
    scope: str,
    key: str,
    request_hash: str,
    expected_change: ChangeRequestIdentity,
    expected_settings: str,
    new_change: GithubPullRequest | None = None,
    saved_change: ChangeRequestIdentity | None = None,
    one_active_per_pr: bool = False,
    source_settings_id: UUID | None = None,
    automatic: AutomaticReceiptContext | None = None,
) -> tuple[UUID, str, bool]:
    repository_id = expected_repository.id
    async with SqlAlchemyUnitOfWork(factory) as uow:
        repository = await uow.session.scalar(
            select(Repository).where(Repository.id == repository_id).with_for_update()
        )
        if automatic is not None:
            bound = await _lock_automatic_receipt(
                uow.session,
                automatic,
                command,
                repository=expected_repository,
                source_id=source_settings_id or command.repository_settings_id,
                source_identity=expected_settings,
            )
            if bound is not None:
                return (*bound, False)
        access = await uow.session.scalar(
            select(RepositoryAccess.id)
            .where(
                RepositoryAccess.repository_id == repository_id,
                RepositoryAccess.user_id == command.initiator_user_id,
            )
            .with_for_update(read=True)
        )
        if repository is None or access is None:
            raise AdmissionResourceNotFoundError
        replay = await find_replay(
            uow.session, scope=scope, key=key, request_hash=request_hash
        )
        if replay is not None:
            return (*replay, False)
        if (
            RepositoryIdentity(
                repository.id,
                repository.provider_name,
                repository.external_id,
                repository.owner,
                repository.repo_name,
            )
            != expected_repository
        ):
            raise AdmissionResourceNotFoundError
        if repository.current_settings_id != (
            source_id := source_settings_id or command.repository_settings_id
        ):
            raise SettingsVersionChangedError
        if new_change is not None and saved_change is not None:
            await _refresh_saved_change(
                uow.session,
                command,
                repository_id,
                saved_change,
                expected_change,
                new_change,
            )
        elif new_change is not None:
            await uow.session.execute(
                insert(ChangeRequest)
                .values(
                    id=command.change_request_id,
                    repository_id=repository_id,
                    external_number=expected_change.number,
                    head_sha=new_change.head_sha,
                    base_sha=new_change.base_sha,
                    source_repository_external_id=new_change.source_repository_external_id,
                    source_ref=new_change.source_ref,
                    base_ref=new_change.base_ref,
                    current_status="open",
                    title=new_change.title,
                    description=new_change.description,
                )
                .on_conflict_do_nothing(
                    constraint="uq_change_request_repository_number"
                )
            )
            change_id = await uow.session.scalar(
                select(ChangeRequest.id).where(
                    ChangeRequest.repository_id == repository_id,
                    ChangeRequest.external_number == expected_change.number,
                )
            )
            assert change_id is not None
            command = replace(command, change_request_id=change_id)
        if command.rerun_of is not None:
            parent = await uow.session.scalar(
                select(ReviewJob.id)
                .where(
                    ReviewJob.id == command.rerun_of,
                    ReviewJob.repository_id == repository_id,
                    ReviewJob.change_request_id == command.change_request_id,
                )
                .with_for_update(read=True)
            )
            if parent is None:
                raise AdmissionResourceNotFoundError
        # One active analysis per PR needs an exclusive row lock: under a
        # shared lock two starts for different heads both see no active run.
        change = await uow.session.scalar(
            select(ChangeRequest)
            .where(
                ChangeRequest.id == command.change_request_id,
                ChangeRequest.repository_id == repository_id,
            )
            .with_for_update(read=not one_active_per_pr)
        )
        if change is None or change.current_status != "open":
            raise AdmissionResourceNotFoundError
        if change.head_sha != command.requested_head_sha:
            raise ReviewHeadMismatchError
        if change_identity(change) != expected_change:
            raise AdmissionResourceNotFoundError
        await uow.session.execute(
            delete(IdempotencyRecord).where(
                IdempotencyRecord.scope == scope,
                IdempotencyRecord.idempotency_key == key,
                IdempotencyRecord.expires_at <= func.clock_timestamp(),
            )
        )
        now = await uow.session.scalar(select(func.clock_timestamp()))
        assert now is not None
        window = now.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
        settings = await uow.session.scalar(
            select(RepositorySettings)
            .where(
                RepositorySettings.id == source_id,
                RepositorySettings.repository_id == repository_id,
            )
            .with_for_update(read=True)
        )
        if settings is None or settings_identity(settings) != expected_settings:
            raise SettingsVersionChangedError
        await _verify_effective_settings(
            uow.session, command, source_id, expected_settings, repository_id
        )
        config_digest = compute_config_digest(command)
        equivalent = await uow.session.scalar(
            select(ReviewJob.id).where(
                ReviewJob.change_request_id == command.change_request_id,
                ReviewJob.requested_head_sha == command.requested_head_sha,
                ReviewJob.config_digest == config_digest,
                ReviewJob.finished_at.is_(None),
            )
        )
        if equivalent is not None:
            if automatic is not None:
                status = await uow.session.scalar(
                    select(ReviewJob.status).where(ReviewJob.id == equivalent)
                )
                assert status is not None
                await _bind_automatic_review(uow.session, automatic, equivalent)
                await uow.commit()
                return equivalent, status, False
            raise ReviewAlreadyActiveError
        if one_active_per_pr and await uow.session.scalar(
            select(ReviewJob.id).where(
                ReviewJob.change_request_id == command.change_request_id,
                ReviewJob.finished_at.is_(None),
            )
        ):
            raise PullRequestReviewActiveError
        await _check_quotas(
            uow.session, repository_id, command.initiator_user_id, settings, window
        )
        review_id = await uow.review_jobs.add_start_review(
            command, config_digest=config_digest
        )
        quota = insert(QuotaUsage).values(
            repository_id=repository_id,
            user_id=command.initiator_user_id,
            type="starts",
            value=1,
            window_start=window,
        )
        reserved = await uow.session.scalar(
            quota.on_conflict_do_update(
                index_elements=[
                    QuotaUsage.repository_id,
                    QuotaUsage.user_id,
                    QuotaUsage.type,
                    QuotaUsage.window_start,
                ],
                # Match the partial index even when PostgreSQL uses a generic plan.
                index_where=QuotaUsage.type == literal_column("'starts'"),
                set_={"value": QuotaUsage.value + 1},
                where=QuotaUsage.value < settings.max_starts_per_hour,
            ).returning(QuotaUsage.id)
        )
        if reserved is None:
            raise ReviewQuotaExceededError
        uow.session.add(
            IdempotencyRecord(
                scope=scope,
                idempotency_key=key,
                request_hash=request_hash,
                review_job_id=review_id,
                created_at=now,
                expires_at=now + timedelta(hours=24),
            )
        )
        if automatic is not None:
            await _bind_automatic_review(uow.session, automatic, review_id)
        await uow.commit()
        return review_id, "QUEUED", True


def _same_automatic_receipt(
    receipt: WebhookReceipt, context: AutomaticReceiptContext
) -> bool:
    intent = context.claim.automatic
    data = receipt.request_data
    return (
        intent is not None
        and data is not None
        and data.get("request") == asdict(context.claim.request)
        and data.get("automatic") == intent.model_dump(mode="json")
    )


async def find_automatic_replay(
    session: AsyncSession,
    context: AutomaticReceiptContext,
) -> tuple[UUID, str] | None:
    """A durable receipt link precedes expiring keys or current-user settings."""
    receipt = await session.get(
        WebhookReceipt, context.claim.id, populate_existing=True
    )
    if receipt is None or not _same_automatic_receipt(receipt, context):
        raise AutomaticReceiptClaimLostError
    if receipt.review_job_id is None:
        return None
    job = await session.get(ReviewJob, receipt.review_job_id)
    intent = context.claim.automatic
    assert intent is not None
    if (
        job is None
        or job.repository_id != intent.repository_id
        or job.requested_head_sha != context.claim.request.head_sha
    ):
        raise AutomaticReceiptClaimLostError
    return job.id, job.status


async def _lock_automatic_receipt(  # noqa: PLR0913 -- bound delivery and frozen source authority
    session: AsyncSession,
    context: AutomaticReceiptContext,
    command: StartReviewCommand,
    *,
    repository: RepositoryIdentity,
    source_id: UUID,
    source_identity: str,
) -> tuple[UUID, str] | None:
    receipt = await session.scalar(
        select(WebhookReceipt)
        .where(WebhookReceipt.id == context.claim.id)
        .with_for_update()
    )
    if receipt is None or not _same_automatic_receipt(receipt, context):
        raise AutomaticReceiptClaimLostError
    bound = await find_automatic_replay(session, context)
    if bound is not None:
        return bound
    now = await session.scalar(select(func.clock_timestamp()))
    intent = context.claim.automatic
    assert intent is not None
    if (
        now is None
        or receipt.processing_status != "PROCESSING"
        or receipt.attempts != context.claim.attempts
        or receipt.lease_until is None
        or receipt.lease_until <= now
        or intent.repository_id != repository.id
        or context.claim.request.repository_external_id != repository.external_id
        or context.claim.request.head_sha != command.requested_head_sha
        or intent.source_settings_id != source_id
        or intent.source_settings_identity != source_identity
        or not intent.policy.auto_review_enabled
        or intent.actor_user_id != command.initiator_user_id
        or intent.policy.authorized_by != command.initiator_user_id
    ):
        raise AutomaticReceiptClaimLostError
    return None


async def _bind_automatic_review(
    session: AsyncSession,
    context: AutomaticReceiptContext,
    review_id: UUID,
) -> None:
    # Flush every aggregate write before the final lease check/one commit.
    await session.flush()
    bound = await session.scalar(
        update(WebhookReceipt)
        .where(
            WebhookReceipt.id == context.claim.id,
            WebhookReceipt.processing_status == "PROCESSING",
            WebhookReceipt.attempts == context.claim.attempts,
            WebhookReceipt.lease_until > func.clock_timestamp(),
            WebhookReceipt.review_job_id.is_(None),
        )
        .values(
            review_job_id=review_id,
            processing_status="READY",
            snapshot_data=context.snapshot,
            raw_diff=context.raw_diff,
            reason_code=None,
            lease_until=None,
            retry_at=None,
        )
        .returning(WebhookReceipt.id)
    )
    if bound is None:
        raise AutomaticReceiptClaimLostError
