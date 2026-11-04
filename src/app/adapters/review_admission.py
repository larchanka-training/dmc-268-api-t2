"""Shared review admission orchestration without an HTTP request/session identity."""

import hashlib
import json
from collections import OrderedDict
from copy import deepcopy
from pathlib import Path
from typing import Never
from uuid import UUID, uuid4

from jsonschema import Draft202012Validator
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.github_ports import (
    GithubFailure,
    GithubPullRequest,
    GithubRepositoryMismatch,
    GithubRequest,
    RepositoryFile,
)
from app.application.ports import StartReviewCommand
from app.application.review_start_ports import (
    AdmissionResourceNotFoundError,
    ChangeRequestIdentity,
    FrozenAnalysisConfiguration,
    IdempotencyConflictError,
    RepositoryIdentity,
    ReviewAdmissionError,
    ReviewAdmissionRequest,
    ReviewAlreadyActiveError,
    ReviewConfigurationResolver,
    ReviewHeadMismatchError,
    ReviewQuotaExceededError,
    ReviewStartAuthorizer,
    SettingsVersionChangedError,
)
from app.context.loader import RepoConfigLoader
from app.context.snapshot import capture_settings, materialize_settings
from app.core import github_runtime
from app.core.config import get_settings
from app.db.models import (
    ChangeRequest,
    Repository,
    RepositoryAccess,
    RepositorySettings,
    ReviewJob,
)
from app.db.review_starts import (
    change_identity,
    find_automatic_replay,
    find_replay,
    persist_start,
    settings_identity,
)
from app.domain.frozen_configuration import (
    ModelPolicy,
    ModelRoute,
    ModelSettings,
    RepositoryInstructions,
)
from app.domain.settings import (
    compute_repository_settings_digest,
    compute_rules_digest,
    project_historical_empty_settings,
)
from app.review.configuration import FrozenConfigurationError
from app.review.prompt import PromptBuilder, PromptConfigError, reflection_bundle

_RULES_CACHE: OrderedDict[tuple[str, ...], RepositoryFile] = OrderedDict()
_SETTINGS_VALIDATOR = Draft202012Validator(
    json.loads(Path("schemas/settings/v1.json").read_text(encoding="utf-8"))
)


_ADMISSION_ERRORS: dict[type[Exception], tuple[int, str, str]] = {
    ReviewQuotaExceededError: (429, "QUOTA_EXCEEDED", "Quota is exceeded."),
    IdempotencyConflictError: (
        409,
        "IDEMPOTENCY_CONFLICT",
        "Idempotency key conflicts with the request.",
    ),
    ReviewAlreadyActiveError: (
        409,
        "REVIEW_ALREADY_ACTIVE",
        "An equivalent review is already active.",
    ),
    SettingsVersionChangedError: (
        409,
        "SETTINGS_VERSION_CONFLICT",
        "Repository settings changed.",
    ),
    AdmissionResourceNotFoundError: (
        404,
        "RESOURCE_NOT_FOUND",
        "Resource was not found.",
    ),
    ReviewHeadMismatchError: (
        409,
        "PR_HEAD_MISMATCH",
        "Pull request head does not match.",
    ),
}


async def _check_rerun(
    session: AsyncSession, reference: str | None, change: ChangeRequest
) -> None:
    if reference is None:
        return
    try:
        rerun_id = UUID(reference)
    except ValueError:
        raise ReviewAdmissionError(
            404, "RESOURCE_NOT_FOUND", "Resource was not found."
        ) from None
    parent = await session.scalar(
        select(ReviewJob.id).where(
            ReviewJob.id == rerun_id,
            ReviewJob.repository_id == change.repository_id,
            ReviewJob.change_request_id == change.id,
        )
    )
    if parent is None:
        raise ReviewAdmissionError(404, "RESOURCE_NOT_FOUND", "Resource was not found.")


class _BotDeniedError(Exception):
    """The bot token was denied by GitHub (not the user's token)."""


def _unhealthy_error() -> ReviewAdmissionError:
    return ReviewAdmissionError(
        409, "INTEGRATION_UNHEALTHY", "The repository integration is unhealthy."
    )


async def _revalidate_github(
    authorizer: ReviewStartAuthorizer,
    user_id: UUID,
    repository_identity: RepositoryIdentity,
    number: int,
    requested_head_sha: str,
) -> tuple[ChangeRequestIdentity, GithubPullRequest]:
    bot_stage = False
    try:
        await authorizer.authorize_start(
            user_id=user_id, repository=repository_identity
        )
        bot_stage = True
        # The saved PR row is only a cache: pushes after the first start move the
        # head on GitHub, so every start re-reads the live PR and refreshes the row.
        async with github_runtime.github_pull_request_connection(
            get_settings()
        ) as reader:
            new_change = await reader.get_pull_request(
                repository_identity.owner,
                repository_identity.name,
                repository_identity.external_id,
                number,
            )
        if new_change.head_sha != requested_head_sha:
            raise ReviewAdmissionError(
                409, "PR_HEAD_MISMATCH", "Pull request head does not match."
            )
        expected_change = ChangeRequestIdentity(
            str(number),
            new_change.head_sha,
            new_change.base_sha,
            new_change.source_repository_external_id,
            new_change.source_ref,
            new_change.base_ref,
            "open",
        )
        github_request = GithubRequest(
            repository_identity.owner,
            repository_identity.name,
            repository_identity.external_id,
            number,
            requested_head_sha,
            expected_change.base_sha,
        )
        async with github_runtime.github_connection(get_settings()) as github:
            snapshot = await github.fetch(github_request)
    except GithubFailure as error:
        if error.reason_code == "VCS_ACCESS_DENIED":
            if bot_stage and not isinstance(error, GithubRepositoryMismatch):
                raise _BotDeniedError from None
            raise ReviewAdmissionError(
                403, "ACCESS_DENIED", "Access is denied."
            ) from None
        if error.reason_code == "PR_STALE_OR_CLOSED":
            raise ReviewAdmissionError(
                404, "RESOURCE_NOT_FOUND", "Resource was not found."
            ) from None
        raise ReviewAdmissionError(
            503,
            "SERVICE_UNAVAILABLE",
            "Service is unavailable.",
            retryable=error.retryable,
            retry_after=error.retry_after if error.retryable else None,
        ) from None
    proof = snapshot.metadata.get("snapshot", {})
    if (
        not isinstance(proof, dict)
        or proof.get("provider") != repository_identity.provider
        or any(
            not isinstance(proof.get(field), str) or not proof[field]
            for field in (
                "head_sha",
                "base_sha",
                "base_repository_id",
                "head_repository_id",
            )
        )
    ):
        raise ReviewAdmissionError(
            503, "SERVICE_UNAVAILABLE", "Service is unavailable.", retryable=True
        )
    if proof.get("head_sha") != github_request.head_sha:
        raise ReviewAdmissionError(
            409, "PR_HEAD_MISMATCH", "Pull request head does not match."
        )
    if (
        proof.get("base_sha") != github_request.base_sha
        or proof.get("base_repository_id") != repository_identity.external_id
        or (
            expected_change.source_repository_external_id is not None
            and proof.get("head_repository_id")
            != expected_change.source_repository_external_id
        )
    ):
        raise ReviewAdmissionError(404, "RESOURCE_NOT_FOUND", "Resource was not found.")
    metadata = snapshot.metadata.get("metadata")
    if not isinstance(metadata, dict) or any(
        not isinstance(metadata.get(field), str) or not metadata[field]
        for field in ("head_ref", "base_ref")
    ):
        raise ReviewAdmissionError(
            503, "SERVICE_UNAVAILABLE", "Service is unavailable.", retryable=True
        )
    if (
        metadata["head_ref"] != expected_change.source_ref
        or metadata["base_ref"] != expected_change.base_ref
    ):
        raise ReviewAdmissionError(404, "RESOURCE_NOT_FOUND", "Resource was not found.")
    return expected_change, new_change


async def _refuse_configuration(
    session: AsyncSession,
    *,
    settings_id: UUID,
    expected_settings: str,
) -> Never:
    # Preserve the existing conflict outcome if resolution observed a changed
    # row. Successful admissions still use locked revalidation in persist_start.
    current = await session.get(RepositorySettings, settings_id, populate_existing=True)
    changed = current is None or settings_identity(current) != expected_settings
    await session.rollback()
    if changed:
        raise ReviewAdmissionError(*_ADMISSION_ERRORS[SettingsVersionChangedError])
    raise ReviewAdmissionError(
        503,
        "SERVICE_UNAVAILABLE",
        "Review configuration is unavailable.",
        retryable=True,
    )


def _validate_configuration(
    config: FrozenAnalysisConfiguration,
    *,
    settings_digest: str,
    instructions: tuple[str, ...],
) -> ModelPolicy:
    try:
        if (
            not isinstance(config, FrozenAnalysisConfiguration)
            or type(config.frozen_configuration_version) is not int
            or config.frozen_configuration_version != 1
        ):
            raise ValueError("unsupported admission configuration")
        policy = ModelSettings.from_stored(config.model_settings)
        ModelRoute(
            profile_ref=policy.routing.primary_profile_ref,
            model_ref=config.model_ref,
            model_digest=config.model_digest,
        )
        if (
            policy.repository_settings_digest != settings_digest
            or config.model_digest is not None
            or any(route.model_digest is not None for route in policy.routing.fallback)
        ):
            raise ValueError("unsupported admission identity")
        PromptBuilder.load(
            Path(__file__).resolve().parents[3] / ".agents/reviewer",
            config.prompt_version,
            expected_digest=config.prompt_digest,
            repository_instructions=RepositoryInstructions(instructions=instructions),
        )
        reflection_bundle(
            Path(__file__).resolve().parents[3] / ".agents/reviewer",
            policy,
            config.prompt_version,
        )
        return policy
    except ValidationError, ValueError, TypeError, PromptConfigError:
        raise ReviewAdmissionError(
            503,
            "SERVICE_UNAVAILABLE",
            "Review configuration is unavailable.",
            retryable=True,
        ) from None


async def admit_review(  # noqa: PLR0912, PLR0915 -- captured read/provider/locked-write phases
    admission: ReviewAdmissionRequest,
    *,
    session: AsyncSession,
    authorizer: ReviewStartAuthorizer,
    resolver: ReviewConfigurationResolver,
) -> tuple[UUID, str, bool]:
    if admission.automatic is not None:
        bound = await find_automatic_replay(session, admission.automatic)
        if bound is not None:
            return (*bound, False)
    repository = await session.scalar(
        select(Repository)
        .join(RepositoryAccess)
        .where(
            Repository.id == admission.repository_id,
            RepositoryAccess.user_id == admission.actor_id,
        )
    )
    if repository is None:
        raise ReviewAdmissionError(404, "RESOURCE_NOT_FOUND", "Resource was not found.")
    request_hash = hashlib.sha256(
        json.dumps(
            {
                "number": admission.number,
                "requested_head_sha": admission.requested_head_sha,
                "rerun_of": admission.rerun_of,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    key = hashlib.sha256(admission.idempotency_key.encode()).hexdigest()
    scope = (
        f"autoReviewReceipt:{admission.automatic.claim.id}"
        if admission.automatic is not None
        else f"startReview:{admission.actor_id}:{admission.repository_id}"
    )
    try:
        replay = await find_replay(
            session, scope=scope, key=key, request_hash=request_hash
        )
    except IdempotencyConflictError:
        raise ReviewAdmissionError(
            409, "IDEMPOTENCY_CONFLICT", "Idempotency key conflicts with the request."
        ) from None
    if replay is not None:
        return (*replay, False)
    if repository.integration_status != "healthy":
        raise _unhealthy_error()
    change = await session.scalar(
        select(ChangeRequest).where(
            ChangeRequest.repository_id == repository.id,
            ChangeRequest.external_number == str(admission.number),
        )
    )
    if change is None:
        if admission.rerun_of is not None:
            raise ReviewAdmissionError(
                404, "RESOURCE_NOT_FOUND", "Resource was not found."
            )
    else:
        await _check_rerun(session, admission.rerun_of, change)
        if change.current_status != "open":
            raise ReviewAdmissionError(
                404, "RESOURCE_NOT_FOUND", "Resource was not found."
            )
    settings = await session.get(RepositorySettings, repository.current_settings_id)
    if settings is None:
        raise ReviewAdmissionError(
            503, "SERVICE_UNAVAILABLE", "Service is unavailable."
        )
    if (
        not settings.output_language
        or not _SETTINGS_VALIDATOR.is_valid(
            {
                "rules": project_historical_empty_settings(
                    settings.rules, "instructions"
                ),
                "ignores": project_historical_empty_settings(settings.ignores, "globs"),
            }
        )
        or settings.rules_digest != compute_rules_digest(settings.rules)
    ):
        raise ReviewAdmissionError(
            409,
            "LEGACY_SETTINGS_UNSUPPORTED",
            "Create a new supported settings version before starting a review.",
        )
    repository_identity = RepositoryIdentity(
        repository.id,
        repository.provider_name,
        repository.external_id,
        repository.owner,
        repository.repo_name,
    )
    if admission.automatic is not None:
        intent = admission.automatic.claim.automatic
        captured = admission.automatic.claim.request
        if (
            intent is None
            or intent.repository_id != repository.id
            or intent.actor_user_id != admission.actor_id
            or intent.source_settings_id != settings.id
            or intent.source_settings_identity != settings_identity(settings)
            or not intent.policy.auto_review_enabled
            or intent.policy.authorized_by != admission.actor_id
        ):
            raise ReviewAdmissionError(*_ADMISSION_ERRORS[SettingsVersionChangedError])
        if (
            captured.owner != repository.owner
            or captured.name != repository.repo_name
            or captured.repository_external_id != repository.external_id
            or captured.number != admission.number
            or captured.head_sha != admission.requested_head_sha
        ):
            raise ReviewAdmissionError(
                *_ADMISSION_ERRORS[AdmissionResourceNotFoundError]
            )
    settings_id, rules_digest = settings.id, settings.rules_digest
    source_settings_id = settings.id
    source_values = capture_settings(settings)
    change_id = change.id if change is not None else uuid4()
    saved_change = change_identity(change) if change is not None else None
    expected_settings = settings_identity(settings)
    raw_settings_digest = compute_repository_settings_digest(
        id=settings.id,
        repository_id=settings.repository_id,
        rules=settings.rules,
        ignores=settings.ignores,
        output_language=settings.output_language,
        surrounding_lines=settings.surrounding_lines,
        review_policy=settings.review_policy,
    )
    raw_instructions = tuple(
        project_historical_empty_settings(settings.rules, "instructions")[
            "instructions"
        ]
    )
    await session.rollback()
    try:
        expected_change, new_change = await _revalidate_github(
            authorizer,
            admission.actor_id,
            repository_identity,
            admission.number,
            admission.requested_head_sha,
        )
    except _BotDeniedError:
        # Keep RepositoryAccess rows: a generic denial is not a confirmed removal.
        await session.execute(
            update(Repository)
            .where(Repository.id == repository_identity.id)
            .values(integration_status="bot_denied")
        )
        await session.commit()
        raise _unhealthy_error() from None
    if (
        admission.automatic is not None
        and expected_change.base_sha != admission.automatic.claim.request.base_sha
    ):
        raise ReviewAdmissionError(*_ADMISSION_ERRORS[ReviewHeadMismatchError])
    if admission.automatic is not None:
        policy = admission.automatic.claim.automatic
        assert policy is not None
        if (
            policy.policy.base_branches
            and expected_change.base_ref not in policy.policy.base_branches
        ):
            raise ReviewAdmissionError(
                409, "AUTO_BASE_FILTERED", "Automatic base branch is excluded."
            )
    if getattr(resolver, "context_enabled", False) is True:
        try:
            async with github_runtime.github_file_connection(get_settings()) as files:
                loaded = await RepoConfigLoader(files, _RULES_CACHE).load(
                    tenant=str(repository_identity.id),
                    owner=repository_identity.owner,
                    name=repository_identity.name,
                    external_id=repository_identity.external_id,
                    revision=expected_change.base_sha,
                    instructions=raw_instructions,
                )
            effective = await materialize_settings(
                session, source_values, expected_settings, loaded
            )
        except GithubFailure as failure:
            raise ReviewAdmissionError(
                503,
                "SERVICE_UNAVAILABLE",
                "Repository context is unavailable.",
                retryable=failure.retryable,
                retry_after=failure.retry_after,
            ) from None
        settings_id, rules_digest = effective["id"], effective["rules_digest"]
        raw_instructions = tuple(effective["rules"]["instructions"])
        raw_settings_digest = compute_repository_settings_digest(
            id=settings_id,
            repository_id=effective["repository_id"],
            rules=effective["rules"],
            ignores=effective["ignores"],
            output_language=effective["output_language"],
            surrounding_lines=effective["surrounding_lines"],
            review_policy=effective.get("review_policy"),
        )
    try:
        config = await resolver.resolve(
            repository=repository_identity,
            settings_id=settings_id,
            rules_digest=rules_digest,
        )
    except FrozenConfigurationError:
        await _refuse_configuration(
            session,
            settings_id=source_settings_id,
            expected_settings=expected_settings,
        )
    try:
        model_settings = _validate_configuration(
            config,
            settings_digest=raw_settings_digest,
            instructions=raw_instructions,
        )
    except ReviewAdmissionError:
        await _refuse_configuration(
            session,
            settings_id=source_settings_id,
            expected_settings=expected_settings,
        )
    command = StartReviewCommand(
        change_request_id=change_id,
        repository_settings_id=settings_id,
        requested_head_sha=admission.requested_head_sha,
        rules_digest=rules_digest,
        trigger_type="webhook" if admission.automatic is not None else "manual",
        initiator_user_id=admission.actor_id,
        model_ref=config.model_ref,
        model_digest=config.model_digest,
        model_settings=deepcopy(model_settings.model_dump(mode="json")),
        frozen_configuration_version=1,
        prompt_version=config.prompt_version,
        prompt_digest=config.prompt_digest,
        trace_id=admission.trace_id,
        rerun_of=UUID(admission.rerun_of) if admission.rerun_of is not None else None,
    )
    try:
        review_id, status, created = await persist_start(
            command,
            factory=async_sessionmaker(session.bind, expire_on_commit=False),
            expected_repository=repository_identity,
            scope=scope,
            key=key,
            request_hash=request_hash,
            expected_change=expected_change,
            expected_settings=expected_settings,
            new_change=new_change,
            saved_change=saved_change,
            source_settings_id=source_settings_id,
            automatic=admission.automatic,
        )
    except tuple(_ADMISSION_ERRORS) as error:
        raise ReviewAdmissionError(*_ADMISSION_ERRORS[type(error)]) from None
    return review_id, status, created
