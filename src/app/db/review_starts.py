"""Atomic persistence for authenticated review admission."""

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.github_ports import GithubPullRequest
from app.application.ports import StartReviewCommand
from app.application.review_start_ports import (
    AdmissionResourceNotFoundError,
    ChangeRequestIdentity,
    IdempotencyConflictError,
    RepositoryIdentity,
    ReviewAlreadyActiveError,
    ReviewHeadMismatchError,
    ReviewQuotaExceededError,
    SettingsVersionChangedError,
)
from app.application.use_cases.start_review import compute_config_digest
from app.db.models import (
    ChangeRequest,
    IdempotencyRecord,
    QuotaUsage,
    Repository,
    RepositoryAccess,
    RepositorySettings,
    ReviewJob,
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
        if saved.head_sha != saved_change.head_sha:
            raise ReviewHeadMismatchError
        raise AdmissionResourceNotFoundError
    saved.head_sha = live.head_sha
    saved.base_sha = live.base_sha
    saved.source_repository_external_id = live.source_repository_external_id
    saved.source_ref = live.source_ref
    saved.base_ref = live.base_ref
    saved.title = live.title
    saved.description = live.description


async def persist_start(  # noqa: PLR0912, PLR0913 -- atomic admission with optional materialization
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
) -> tuple[UUID, str, bool]:
    repository_id = expected_repository.id
    async with SqlAlchemyUnitOfWork(factory) as uow:
        repository = await uow.session.scalar(
            select(Repository).where(Repository.id == repository_id).with_for_update()
        )
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
        if repository.current_settings_id != command.repository_settings_id:
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
        change = await uow.session.scalar(
            select(ChangeRequest)
            .where(
                ChangeRequest.id == command.change_request_id,
                ChangeRequest.repository_id == repository_id,
            )
            .with_for_update(read=True)
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
                RepositorySettings.id == command.repository_settings_id,
                RepositorySettings.repository_id == repository_id,
            )
            .with_for_update(read=True)
        )
        if settings is None or settings_identity(settings) != expected_settings:
            raise SettingsVersionChangedError
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
            raise ReviewAlreadyActiveError
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
                index_where=QuotaUsage.type == "starts",
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
        await uow.commit()
        return review_id, "QUEUED", True
