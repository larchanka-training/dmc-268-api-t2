"""Bounded PostgreSQL analysis handler; providers are explicitly injected."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, cast

import sentry_sdk
import structlog
from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from referencing import Registry, Resource
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.analysis_authority import current_analysis_authority
from app.application.github_ports import (
    CompareReader,
    GitattributesReader,
    GithubFailure,
    GithubPort,
    GithubRequest,
    SourceFileReader,
)
from app.application.ports import (
    BuiltContextPayload,
    DurableTaskOutcome,
    LeaseLostError,
    LlmGatewayPort,
    QueueTaskEnvelope,
    ReviewChunkResult,
    ReviewFinding,
    TaskLeaseSnapshot,
    TokenUsage,
)
from app.context.enrichment import enrich_context
from app.core.metric_events import count, record_skipped_files
from app.db.analysis_attempts import PostgresAnalysisAttemptStore
from app.db.models import (
    ChangeRequest,
    ChunkResult,
    ContextPayload,
    Finding,
    OutboxEvent,
    Publication,
    Repository,
    RepositorySettings,
    ReviewJob,
    utcnow,
)
from app.db.repositories import (
    SqlAlchemyPublicationRepository,
    SqlAlchemyReviewJobRepository,
)
from app.db.review_events import append_review_event
from app.db.task_leases import lock_owned_lease, lock_review_job
from app.db.worker_tasks import PostgresWorkerTasks
from app.domain.contract_validation import validate_hunks, validate_ranges
from app.domain.enums import ReviewReasonCode, ReviewStatus
from app.domain.frozen_configuration import FrozenConfigurationError
from app.domain.settings import project_historical_empty_settings
from app.llm.tokens import ConservativeTokenCounter
from app.review.cache import ReviewResultCache
from app.review.chunking import ChunkLimits, plan_chunks
from app.review.execution import FrozenAnalysisExecutionFactory
from app.review.incremental import (
    IncrementalPolicy,
    omit_reviewed,
    reviewed_fragments,
)
from app.review.port_adapter import ChunkReviewFailedError, LlmGatewayAdapter
from app.review.schemas.context import FileDiff
from app.vcs.diff import InvalidDiff, ParsedDiff, parse_diff
from app.vcs.input_guards import InputGuardPolicy

# Coverage reasons that mean the run reviewed less than the admitted diff.
# Chunk diff budget is shrunk by 1/4 while a chunk prompt overflows, down to this.
_MIN_CHUNK_TOKENS = 256
_INCOMPLETE_REASONS = frozenset({"token_budget", "file_limit", "line_limit"})


class _AnalysisFailureError(Exception):
    def __init__(self, reason: ReviewReasonCode) -> None:
        self.reason = reason
        super().__init__(reason.value)


class TerminalFollowUp(Protocol):
    """Runs once an analysis is terminal, e.g. the system follow-up start."""

    async def after_terminal(self, review_id: uuid.UUID) -> object: ...


class PostgresAnalysisHandler:
    """Inject a real LlmGatewayPort at composition time; no default AI provider.

    The gateway must resolve frozen rules/model/prompt from authoritative review
    identity. Context v1 carries the immutable settings key as rules_version,
    rather than embedding instructions outside the canonical wire contract.
    schemas_dir points to the deployed, checked-in canonical schema bundle.
    """

    def __init__(  # noqa: PLR0913 — explicit provider and fixed budget injection
        self,
        session_factory: async_sessionmaker[AsyncSession],
        github: GithubPort,
        llm: LlmGatewayPort | None,
        *,
        schemas_dir: Path,
        limit_input_tokens: int = 100_000,
        reserved_output_tokens: int = 4_000,
        max_chunks: int = 5,
        execution_factory: FrozenAnalysisExecutionFactory | None = None,
        input_guards: InputGuardPolicy | None = None,
        incremental: IncrementalPolicy | None = None,
        follow_up: TerminalFollowUp | None = None,
        cache: ReviewResultCache | None = None,
    ) -> None:
        self._factory = session_factory
        self._input_guards = input_guards
        self._incremental = incremental
        self._follow_up = follow_up
        self._cache = cache
        self._github = github
        self._llm = llm
        self._execution_factory = execution_factory
        self._limit = limit_input_tokens
        self._reserved = reserved_output_tokens
        self._max_chunks = max_chunks
        context_schema = json.loads(
            (schemas_dir / "context/v1.json").read_text(encoding="utf-8")
        )
        result_schemas = {
            version: json.loads(
                (schemas_dir / f"review-result/v{version}.json").read_text(
                    encoding="utf-8"
                )
            )
            for version in (1, 2)
        }
        registry: Registry[Any] = Registry().with_resources(
            (doc["$id"], Resource.from_contents(doc))
            for doc in (context_schema, *result_schemas.values())
        )
        self._context_validator = Draft202012Validator(
            context_schema, registry=registry, format_checker=FormatChecker()
        )
        self._result_validators = {
            version: Draft202012Validator(
                doc, registry=registry, format_checker=FormatChecker()
            )
            for version, doc in result_schemas.items()
        }

    async def _transition(  # noqa: PLR0913 — fenced transition with safe diagnostics
        self,
        session: AsyncSession,
        task: QueueTaskEnvelope,
        lease: TaskLeaseSnapshot,
        status: ReviewStatus,
        reason: ReviewReasonCode | None = None,
        *,
        error_code: str | None = None,
    ) -> None:
        saved: list[ChunkResult] = []
        if status == ReviewStatus.FAILED:
            locked = await lock_owned_lease(session, lease)
            if locked is None:
                raise LeaseLostError("Analysis lease is no longer current")
            saved = await self._usable_saved_results(session, lease.review_job_id)
            if saved:
                status = ReviewStatus.PARTIAL
                locked[0].final_summary = "\n\n".join(row.summary for row in saved)
                limitation = error_code or (
                    reason.value.lower() if reason else "analysis_incomplete"
                )
                for row in saved:
                    row.limitations = [*row.limitations, limitation]
        await SqlAlchemyReviewJobRepository(session).update_status(lease, status=status)
        if status == ReviewStatus.PARTIAL and saved:
            await self._prepare_publication(session, task, lease)
        job = await session.get(ReviewJob, lease.review_job_id)
        assert job is not None
        details: dict[str, object] = {"error_code": error_code} if error_code else {}
        if job.analysis_attempts == 0:
            details["pre_execution"] = True
        await append_review_event(
            session,
            lease.review_job_id,
            event_type="TRANSITION",
            publication_status=(
                await session.scalar(
                    select(Publication.status).where(
                        Publication.review_job_id == lease.review_job_id,
                    )
                )
            )
            or "NOT_READY",
            phase=status.value
            if status
            in {
                ReviewStatus.FETCHING_DIFF,
                ReviewStatus.PARSING_CONTEXT,
                ReviewStatus.LLM_PROCESSING,
            }
            else None,
            attempt=max(1, job.analysis_attempts),
            reason_code=reason,
            safe_details=details,
        )

    async def handle(
        self,
        task: QueueTaskEnvelope,
        lease: TaskLeaseSnapshot,
    ) -> DurableTaskOutcome | None:
        outcome = await self._handle(task, lease)
        if outcome is DurableTaskOutcome.COMPLETED and self._follow_up is not None:
            try:
                await self._follow_up.after_terminal(lease.review_job_id)
            except Exception as error:  # the follow-up never blocks the ACK
                structlog.get_logger().warning(
                    "review_follow_up_failed",
                    review_id=str(lease.review_job_id),
                    error_type=type(error).__name__,
                )
        return outcome

    async def _handle(  # noqa: PLR0911 -- explicit expected terminal/retry outcomes
        self,
        task: QueueTaskEnvelope,
        lease: TaskLeaseSnapshot,
    ) -> DurableTaskOutcome | None:
        try:
            return await self._execute(task, lease)
        except FrozenConfigurationError as failure:
            async with self._factory.begin() as session:
                if await self._locked_finished(session, lease.review_job_id):
                    return DurableTaskOutcome.COMPLETED
                await self._transition(
                    session,
                    task,
                    lease,
                    ReviewStatus.FAILED,
                    ReviewReasonCode.LLM_UNAVAILABLE,
                    error_code=failure.reason,
                )
            return DurableTaskOutcome.COMPLETED
        except ChunkReviewFailedError as failure:
            error_code = failure.outcome.error_code or "llm_error"
            reason = {
                "invalid_llm_output": ReviewReasonCode.LLM_OUTPUT_INVALID,
                "analysis_deadline_exceeded": (
                    ReviewReasonCode.ANALYSIS_DEADLINE_EXCEEDED
                ),
            }.get(error_code, ReviewReasonCode.LLM_UNAVAILABLE)
            if failure.outcome.retryable and await PostgresAnalysisAttemptStore(
                self._factory
            ).defer_retry(
                task=task,
                lease=lease,
                reason=reason,
                error_code=failure.outcome.root_error_code or error_code,
                delay_s=2,
            ):
                return None
            async with self._factory.begin() as session:
                if await self._locked_finished(session, lease.review_job_id):
                    return DurableTaskOutcome.COMPLETED
                await self._transition(
                    session,
                    task,
                    lease,
                    ReviewStatus.FAILED,
                    reason,
                    error_code=failure.outcome.root_error_code or error_code,
                )
            return DurableTaskOutcome.COMPLETED
        except GithubFailure as failure:
            if await PostgresAnalysisAttemptStore(self._factory).defer_retry(
                task=task,
                lease=lease,
                reason=ReviewReasonCode(failure.reason_code),
                error_code=failure.reason_code.lower(),
                delay_s=2,
            ):
                return None
            async with self._factory.begin() as session:
                if await self._locked_finished(session, lease.review_job_id):
                    return DurableTaskOutcome.COMPLETED
                await self._transition(
                    session,
                    task,
                    lease,
                    ReviewStatus.FAILED,
                    ReviewReasonCode(failure.reason_code),
                )
            return DurableTaskOutcome.COMPLETED
        except _AnalysisFailureError as failure:
            async with self._factory.begin() as session:
                if await self._locked_finished(session, lease.review_job_id):
                    return DurableTaskOutcome.COMPLETED
                status = (
                    ReviewStatus.SKIPPED
                    if failure.reason == ReviewReasonCode.PR_STALE_OR_CLOSED
                    else ReviewStatus.FAILED
                )
                await self._transition(
                    session,
                    task,
                    lease,
                    status,
                    failure.reason,
                    error_code="analysis_deadline_exceeded"
                    if failure.reason == ReviewReasonCode.ANALYSIS_DEADLINE_EXCEEDED
                    else None,
                )
            return DurableTaskOutcome.COMPLETED

    async def _already_finished(self, review_id: uuid.UUID) -> bool:
        """True once Stop or another finish committed finished_at."""
        async with self._factory.begin() as session:
            finished_at = await session.scalar(
                select(ReviewJob.finished_at).where(ReviewJob.id == review_id)
            )
        return finished_at is not None

    async def _locked_finished(
        self, session: AsyncSession, review_id: uuid.UUID
    ) -> bool:
        current = await lock_review_job(session, review_id)
        return current is None or current.finished_at is not None

    async def _execute(  # noqa: PLR0911, PLR0912, PLR0915 — one bounded Level 1 capture
        self,
        task: QueueTaskEnvelope,
        lease: TaskLeaseSnapshot,
    ) -> DurableTaskOutcome | None:
        # Stop may have committed before this delivery reached call authority.
        if await self._already_finished(lease.review_job_id):
            return DurableTaskOutcome.COMPLETED
        decision = await PostgresAnalysisAttemptStore(self._factory).begin(
            task=task, lease=lease
        )
        if decision.error_code == "retry_deferred":
            return None
        if not decision.accepted:
            reason = {
                "queue_deadline_exceeded": ReviewReasonCode.QUEUE_DEADLINE_EXCEEDED,
                "analysis_deadline_exceeded": (
                    ReviewReasonCode.ANALYSIS_DEADLINE_EXCEEDED
                ),
            }.get(decision.error_code or "", ReviewReasonCode.LLM_UNAVAILABLE)
            async with self._factory.begin() as session:
                await self._transition(
                    session,
                    task,
                    lease,
                    ReviewStatus.FAILED,
                    reason,
                    error_code=decision.error_code,
                )
            return DurableTaskOutcome.COMPLETED
        llm = self._llm
        limit, reserved = self._limit, self._reserved
        max_chunks = self._max_chunks
        if self._execution_factory is not None:
            authority = current_analysis_authority.get()
            if authority is None:
                raise FrozenConfigurationError("execution_authority_required")
            execution = await self._execution_factory.create(
                task=task,
                lease=lease,
                analysis_attempt=decision.analysis_attempt,
                authority=authority,
            )
            llm = execution.llm
            limit = execution.configuration.policy.limits.max_input_tokens
            reserved = execution.configuration.policy.limits.max_output_tokens
            max_chunks = execution.configuration.policy.limits.max_chunks
        assert llm is not None
        if await self._already_finished(lease.review_job_id):
            return DurableTaskOutcome.COMPLETED
        await self._ensure_call_authority(task, lease)
        async with self._factory.begin() as session:
            if await self._locked_finished(session, lease.review_job_id):
                return DurableTaskOutcome.COMPLETED
            stored = (
                await session.scalars(
                    select(ContextPayload)
                    .where(ContextPayload.review_job_id == lease.review_job_id)
                    .order_by(ContextPayload.created_at, ContextPayload.id)
                )
            ).all()
            completed = set(
                (
                    await session.scalars(
                        select(ChunkResult.context_payload_id).where(
                            ChunkResult.review_job_id == lease.review_job_id,
                        )
                    )
                ).all()
            )
            # Resume only the chunks whose results are not durable yet.
            saved = [
                BuiltContextPayload(
                    row.schema_version, row.review_job_id, row.id, row.payload_body
                )
                for row in stored
                if row.id not in completed
            ]
            await self._transition(
                session,
                task,
                lease,
                ReviewStatus.LLM_PROCESSING if stored else ReviewStatus.FETCHING_DIFF,
            )
            review = await session.get(ReviewJob, lease.review_job_id)
            assert review is not None
            repo = await session.get(Repository, review.repository_id)
            change = await session.get(ChangeRequest, review.change_request_id)
            settings = await session.get(
                RepositorySettings, review.repository_settings_id
            )
            assert repo is not None and change is not None and settings is not None
            if (
                repo.provider_name != "github"
                or settings.rules_digest != review.rules_digest
            ):
                raise _AnalysisFailureError(ReviewReasonCode.DIFF_UNTRUSTWORTHY)
            if (
                change.head_sha != review.requested_head_sha
                or change.current_status != "open"
            ):
                raise _AnalysisFailureError(ReviewReasonCode.PR_STALE_OR_CLOSED)
            request = GithubRequest(
                repo.owner,
                repo.repo_name,
                repo.external_id,
                int(change.external_number),
                review.requested_head_sha,
                review.base_sha or change.base_sha,
            )
            settings_id = settings.id
            context_enabled = review.prompt_version in {"v4", "v5"}
            language = settings.output_language
            ignore_settings = project_historical_empty_settings(
                settings.ignores, "globs"
            )
            ignores = list(ignore_settings["globs"])
            try:
                guards = (
                    self._input_guards.for_repository(ignore_settings)
                    if self._input_guards is not None
                    else None
                )
            except ValueError:
                raise _AnalysisFailureError(
                    ReviewReasonCode.DIFF_UNTRUSTWORTHY
                ) from None
            expected_source = change.source_repository_external_id
            cycle = (review.id, review.change_request_id, review.config_digest)

        if stored:
            for context in saved:
                self._check_snapshot(
                    cast(dict[str, Any], context.payload_body["snapshot"]),
                    request,
                    expected_source,
                )
                self._validate_context(context)
            return await self._infer(task, lease, saved, llm, cycle[2])
        if await self._already_finished(lease.review_job_id):
            return DurableTaskOutcome.COMPLETED
        await self._ensure_call_authority(task, lease)
        with sentry_sdk.start_span(op="review.diff", name="Diff"):
            try:
                captured = await self._github.fetch(request)
            except GithubFailure as failure:
                if failure.retryable:
                    raise
                raise _AnalysisFailureError(
                    ReviewReasonCode(failure.reason_code)
                ) from None
            # Fetch is not aborted. A cancel that committed during it inserts nothing.
            if await self._already_finished(lease.review_job_id):
                return DurableTaskOutcome.COMPLETED
            self._check_snapshot(
                captured.metadata["snapshot"], request, expected_source
            )
            if guards is not None and isinstance(self._github, GitattributesReader):
                guards = guards.with_gitattributes(
                    await self._read_gitattributes(self._github, request)
                )
            try:
                parsed = parse_diff(
                    captured.raw_diff, ignore_globs=ignores, guards=guards
                )
            except InvalidDiff:
                raise _AnalysisFailureError(
                    ReviewReasonCode.DIFF_UNTRUSTWORTHY
                ) from None
        if self._incremental is not None:
            reviewed, prior_head = await self._prior_reviews(self._incremental, *cycle)
            count("delta_mode", mode=await self._delta_mode(request, prior_head))
            parsed = omit_reviewed(parsed, reviewed)
        record_skipped_files(lease.review_job_id, parsed.skipped)
        if not parsed.files:
            async with self._factory.begin() as session:
                if await self._locked_finished(session, lease.review_job_id):
                    return DurableTaskOutcome.COMPLETED
                await self._transition(session, task, lease, ReviewStatus.SKIPPED)
                await self._save_snapshot(
                    session,
                    lease,
                    captured.metadata["snapshot"],
                    parsed.review_coverage,
                )
            return DurableTaskOutcome.COMPLETED
        with sentry_sdk.start_span(op="review.context", name="Context"):
            body: dict[str, Any] = {
                **captured.metadata,
                "metadata": {
                    **captured.metadata["metadata"],
                    "rules_version": str(settings_id),
                    "output_language": language,
                },
                "files": parsed.files,
                "related_symbols": [],
                "coverage": parsed.coverage,
                "budget": {
                    "estimated_input_tokens": 0,
                    "reserved_output_tokens": reserved,
                    "limit_input_tokens": limit,
                },
            }
            context = BuiltContextPayload(1, lease.review_job_id, uuid.uuid4(), body)
            if context_enabled:
                await enrich_context(body, cast(SourceFileReader, self._github))
            if _estimate(context) > limit and context_enabled:
                # Optional source context has the same bounded fallback as before.
                for file in body["files"]:
                    file["source_windows"] = []
                    file["whole_file"] = None
                body["related_symbols"] = []
                body["coverage"]["context_quality"] = "degraded"
                body["budget"]["estimated_input_tokens"] = 0
            # Wire coverage strips guard ``detail``; the stored run keeps it.
            contexts = [context]
            coverage = {**body["coverage"], "skipped": parsed.skipped}
            # The real gateway also knows the rendered prompt: system prompt and
            # few-shots add to the diff, so the wire estimate alone is not enough.
            prompt_fits = (
                llm.prompt_fits if isinstance(llm, LlmGatewayAdapter) else None
            )
            if _estimate(context) > limit or (
                prompt_fits is not None and not prompt_fits(context)
            ):
                contexts, coverage = _chunk_contexts(
                    lease.review_job_id,
                    body,
                    parsed,
                    limit=limit,
                    max_chunks=max_chunks,
                    prompt_fits=prompt_fits,
                )
                if not contexts and _estimate(context) <= limit:
                    # The diff fits; only the fixed prompt does not. That is a
                    # configuration failure for the gateway (prompt_too_large).
                    contexts = [context]
                    coverage = {**body["coverage"], "skipped": parsed.skipped}
            if not contexts:
                coverage = {
                    "included": [],
                    "skipped": [
                        *parsed.skipped,
                        *[
                            {**entry, "reason": "token_budget"}
                            for entry in parsed.coverage["included"]
                        ],
                    ],
                    "context_quality": "diff_only",
                }
                async with self._factory.begin() as session:
                    if await self._locked_finished(session, lease.review_job_id):
                        return DurableTaskOutcome.COMPLETED
                    await self._transition(session, task, lease, ReviewStatus.SKIPPED)
                    await self._save_snapshot(
                        session, lease, body["snapshot"], coverage
                    )
                return DurableTaskOutcome.COMPLETED
            for context in contexts:
                self._validate_context(context)
        created_at = utcnow()
        with sentry_sdk.start_span(op="review.db", name="Context commit"):
            async with self._factory.begin() as session:
                if await self._locked_finished(session, lease.review_job_id):
                    return DurableTaskOutcome.COMPLETED
                await self._transition(
                    session, task, lease, ReviewStatus.PARSING_CONTEXT
                )
                await self._save_snapshot(session, lease, body["snapshot"], coverage)
                for index, context in enumerate(contexts):
                    session.add(
                        ContextPayload(
                            id=context.chunk_id,
                            review_job_id=context.review_id,
                            schema_version=context.schema_version,
                            payload_body=context.payload_body,
                            # /diff reads plan order through (created_at, id).
                            created_at=created_at + timedelta(microseconds=index),
                        )
                    )
                await self._transition(
                    session, task, lease, ReviewStatus.LLM_PROCESSING
                )

        return await self._infer(task, lease, contexts, llm, cycle[2])

    def _validate_context(self, context: BuiltContextPayload) -> None:
        self._context_validator.validate(context.to_wire())
        validate_ranges(context.to_wire())
        for file in cast(list[dict[str, Any]], context.payload_body["files"]):
            validate_hunks(file["diff"]["hunks"])

    async def _prior_reviews(
        self,
        policy: IncrementalPolicy,
        review_id: uuid.UUID,
        change_request_id: uuid.UUID,
        config_digest: str,
    ) -> tuple[frozenset[str], str | None]:
        """Fragments of prior reviews of this PR whose results reached the PR,
        under the same frozen configuration (another model, prompt or rules
        digest reviews again), and the head of the latest of them.

        An unpublished result (e.g. skipped as stale after a mid-flight push)
        never reached the author, so its fragments are reviewed again."""
        async with self._factory() as session:
            rows = (
                await session.execute(
                    select(ContextPayload.payload_body, ReviewJob.requested_head_sha)
                    .join(ReviewJob, ReviewJob.id == ContextPayload.review_job_id)
                    # Contexts are saved up front for every chunk: only a chunk
                    # with a stored result was actually reviewed.
                    .join(
                        ChunkResult,
                        and_(
                            ChunkResult.review_job_id == ContextPayload.review_job_id,
                            ChunkResult.context_payload_id == ContextPayload.id,
                        ),
                    )
                    .join(Publication, Publication.review_job_id == ReviewJob.id)
                    .where(
                        ReviewJob.change_request_id == change_request_id,
                        ReviewJob.id != review_id,
                        ReviewJob.config_digest == config_digest,
                        ReviewJob.status.in_(
                            [ReviewStatus.COMPLETED, ReviewStatus.PARTIAL]
                        ),
                        Publication.status.in_(["PUBLISHED", "PARTIAL"]),
                    )
                    .order_by(ReviewJob.finished_at.desc())
                    .limit(policy.max_prior_reviews)
                )
            ).all()
        reviewed = reviewed_fragments(body for body, _ in rows)
        return reviewed, rows[0][1] if rows else None

    async def _delta_mode(self, request: GithubRequest, prior_head: str | None) -> str:
        """``full`` without a prior review; ``force_push`` when the prior head is
        not an ancestor of the new one (or that cannot be shown)."""
        if prior_head is None:
            return "full"
        if not isinstance(self._github, CompareReader):
            return "incremental"
        try:
            status = await self._github.compare_status(request, prior_head)
        except GithubFailure:
            status = None  # a metric label never retries the analysis
        return "incremental" if status in {"ahead", "identical"} else "force_push"

    @staticmethod
    async def _read_gitattributes(
        github: GitattributesReader, request: GithubRequest
    ) -> str | None:
        """Linguist flags only refine skips: a permanent failure leaves defaults."""
        try:
            return await github.fetch_gitattributes(request)
        except GithubFailure as failure:
            if failure.retryable:
                raise
            structlog.get_logger().warning(
                "gitattributes_unavailable", reason_code=failure.reason_code
            )
            return None

    def _check_snapshot(
        self,
        snapshot: dict[str, Any],
        request: GithubRequest,
        expected_source: str | None,
    ) -> None:
        if (
            snapshot["provider"] != "github"
            or snapshot["head_sha"] != request.head_sha
            or snapshot["base_sha"] != request.base_sha
            or snapshot["base_repository_id"] != request.repository_external_id
            or snapshot["provider_metadata"]["pull_number"] != request.number
            or (
                expected_source is not None
                and snapshot["head_repository_id"] != expected_source
            )
        ):
            raise _AnalysisFailureError(ReviewReasonCode.PR_STALE_OR_CLOSED)

    async def _infer(  # noqa: PLR0911 -- stop checks return before the next write
        self,
        task: QueueTaskEnvelope,
        lease: TaskLeaseSnapshot,
        contexts: list[BuiltContextPayload],
        llm: LlmGatewayPort,
        config_digest: str,
    ) -> DurableTaskOutcome:
        """Review pending chunks in plan order.

        Each earlier chunk result is durable on its own, so a redelivery resumes
        the rest; the last one lands atomically with the terminal status. With
        a result cache, each chunk is looked up by its own content identity.
        """
        if not contexts:
            # Every chunk already has a result but the run never ended.
            async with self._factory.begin() as session:
                if await self._locked_finished(session, lease.review_job_id):
                    return DurableTaskOutcome.COMPLETED
                await self._transition(
                    session,
                    task,
                    lease,
                    ReviewStatus.FAILED,
                    error_code="analysis_incomplete",
                )
            return DurableTaskOutcome.COMPLETED
        for index, context in enumerate(contexts):
            files = cast(list[dict[str, Any]], context.payload_body["files"])
            if await self._already_finished(lease.review_job_id):
                return DurableTaskOutcome.COMPLETED
            await self._ensure_call_authority(task, lease)
            with sentry_sdk.start_span(op="review.llm", name="LLM"):
                # Do not close the provider call. Stop only skips the insert after it.
                result = await self._review_chunk(llm, context, files, config_digest)
            if await self._already_finished(lease.review_job_id):
                return DurableTaskOutcome.COMPLETED
            authority = current_analysis_authority.get()
            if authority is not None:
                authority.check()
            if not self._valid_result(result, files):
                # Raw format repair belongs exclusively to ReviewService. Invalid
                # backend results fail defensively without another provider call.
                async with self._factory.begin() as session:
                    if await self._locked_finished(session, lease.review_job_id):
                        return DurableTaskOutcome.COMPLETED
                    await self._transition(
                        session,
                        task,
                        lease,
                        ReviewStatus.FAILED,
                        ReviewReasonCode.LLM_OUTPUT_INVALID,
                        error_code="backend_result_invalid",
                    )
                return DurableTaskOutcome.COMPLETED
            with sentry_sdk.start_span(op="review.db", name="Result commit"):
                async with self._factory.begin() as session:
                    if await self._locked_finished(session, lease.review_job_id):
                        return DurableTaskOutcome.COMPLETED
                    locked = await lock_owned_lease(session, lease)
                    if locked is None:
                        raise LeaseLostError("Analysis lease is no longer current")
                    if authority is not None:
                        authority.check()
                    await self._save_chunk_result(session, context, result)
                    if index == len(contexts) - 1:
                        deadline = locked[0].analysis_deadline_at
                        await self._finish(
                            session,
                            task,
                            lease,
                            late=deadline is not None and deadline <= locked[2],
                        )
        return DurableTaskOutcome.COMPLETED

    async def _review_chunk(
        self,
        llm: LlmGatewayPort,
        context: BuiltContextPayload,
        files: list[dict[str, Any]],
        config_digest: str,
    ) -> ReviewChunkResult:
        async def call() -> ReviewChunkResult:
            count("llm_calls_total")
            return await llm.review(context)

        if self._cache is None:
            return await call()
        return await self._cache.review(
            context.payload_body,
            config_digest,
            call,
            lambda candidate: self._valid_result(candidate, files),
        )

    async def _save_chunk_result(
        self,
        session: AsyncSession,
        context: BuiltContextPayload,
        result: ReviewChunkResult,
    ) -> None:
        chunk_id = uuid.uuid4()
        session.add(
            ChunkResult(
                id=chunk_id,
                review_job_id=context.review_id,
                context_payload_id=context.chunk_id,
                schema_version=result.schema_version,
                summary=result.summary,
                limitations=result.limitations,
                usage=asdict(result.usage) if result.usage is not None else None,
                latency_ms=result.latency_ms,
            )
        )
        await session.flush()
        # Run-wide dedup: chunks of one review never repeat a finding.
        seen_fingerprints = set(
            (
                await session.scalars(
                    select(Finding.fingerprint)
                    .join(ChunkResult, Finding.chunk_result_id == ChunkResult.id)
                    .where(ChunkResult.review_job_id == context.review_id)
                )
            ).all()
        )
        for finding in result.findings:
            values = asdict(finding)
            # Backend fingerprint v1: canonical location/category/title identity.
            identity = {
                key: values[key]
                for key in (
                    "path",
                    "side",
                    "start_line",
                    "end_line",
                    "category",
                    "title",
                )
            }
            fingerprint = hashlib.sha256(
                json.dumps(
                    identity,
                    sort_keys=True,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            if fingerprint in seen_fingerprints:
                continue
            seen_fingerprints.add(fingerprint)
            session.add(
                Finding(chunk_result_id=chunk_id, fingerprint=fingerprint, **values)
            )

    async def _finish(
        self,
        session: AsyncSession,
        task: QueueTaskEnvelope,
        lease: TaskLeaseSnapshot,
        *,
        late: bool,
    ) -> None:
        review = await session.get(ReviewJob, lease.review_job_id)
        assert review is not None
        rows = (
            await session.scalars(
                select(ChunkResult)
                .where(ChunkResult.review_job_id == lease.review_job_id)
                .order_by(ChunkResult.created_at, ChunkResult.id)
            )
        ).all()
        incomplete = any(
            item.get("reason") in _INCOMPLETE_REASONS
            for item in (review.coverage or {}).get("skipped", [])
        )
        await self._transition(
            session,
            task,
            lease,
            ReviewStatus.PARTIAL
            if late or incomplete or any(row.limitations for row in rows)
            else ReviewStatus.COMPLETED,
            ReviewReasonCode.ANALYSIS_DEADLINE_EXCEEDED if late else None,
        )
        # SD §7: the run summary is assembled without another LLM call.
        review.final_summary = "\n\n".join(row.summary for row in rows)
        await self._prepare_publication(session, task, lease)

    async def _usable_saved_results(
        self, session: AsyncSession, review_id: uuid.UUID
    ) -> list[ChunkResult]:
        usable = []
        rows = (
            await session.scalars(
                select(ChunkResult)
                .where(ChunkResult.review_job_id == review_id)
                .order_by(ChunkResult.created_at, ChunkResult.id)
            )
        ).all()
        for row in rows:
            try:
                usage = TokenUsage(**row.usage) if row.usage is not None else None
            except TypeError:
                continue
            findings = (
                await session.scalars(
                    select(Finding).where(Finding.chunk_result_id == row.id)
                )
            ).all()
            fields = ReviewFinding.__dataclass_fields__
            result = ReviewChunkResult(
                schema_version=row.schema_version,
                summary=row.summary,
                limitations=row.limitations,
                usage=usage,
                latency_ms=row.latency_ms,
                findings=[
                    ReviewFinding(**{name: getattr(f, name) for name in fields})
                    for f in findings
                ],
            )
            # Retained canonical results can outlive their ContextPayload by contract.
            context = (
                await session.get(ContextPayload, row.context_payload_id)
                if row.context_payload_id
                else None
            )
            if context is not None:
                try:
                    wire = BuiltContextPayload(
                        context.schema_version,
                        context.review_job_id,
                        context.id,
                        context.payload_body,
                    ).to_wire()
                    self._context_validator.validate(wire)
                    validate_ranges(wire)
                    for file in context.payload_body["files"]:
                        validate_hunks(file["diff"]["hunks"])
                except ValidationError, ValueError, TypeError:
                    continue
            files = context.payload_body.get("files", []) if context else None
            if self._valid_result(result, files):
                usable.append(row)
        return usable

    async def _prepare_publication(
        self, session: AsyncSession, task: QueueTaskEnvelope, lease: TaskLeaseSnapshot
    ) -> None:
        await SqlAlchemyPublicationRepository(session).prepare(lease)
        successor = QueueTaskEnvelope(
            1, str(uuid.uuid4()), str(lease.review_job_id), "publish", 1, task.trace_id
        )
        successor.to_json()
        session.add(
            OutboxEvent(
                id=uuid.UUID(successor.event_id),
                review_job_id=lease.review_job_id,
                schema_version=1,
                task_kind="publish",
                attempt=1,
                trace_id=task.trace_id,
            )
        )

    async def _ensure_call_authority(
        self,
        task: QueueTaskEnvelope,
        lease: TaskLeaseSnapshot,
    ) -> None:
        if (
            lease.task_kind != "analyze"
            or lease.review_job_id != uuid.UUID(task.review_id)
            or not await PostgresWorkerTasks(self._factory).ready(task, lease)
        ):
            raise LeaseLostError("Analysis lease is no longer current")
        async with self._factory.begin() as session:
            locked = await lock_owned_lease(session, lease)
            if locked is None:
                raise LeaseLostError("Analysis lease is no longer current")
            review, _, now = locked
            if review.started_at is None and review.queue_deadline_at <= now:
                raise _AnalysisFailureError(ReviewReasonCode.QUEUE_DEADLINE_EXCEEDED)
            if (
                review.analysis_deadline_at is not None
                and review.analysis_deadline_at <= now
            ):
                raise _AnalysisFailureError(ReviewReasonCode.ANALYSIS_DEADLINE_EXCEEDED)

    async def _save_snapshot(
        self,
        session: AsyncSession,
        lease: TaskLeaseSnapshot,
        snapshot: dict[str, Any],
        coverage: dict[str, Any],
    ) -> None:
        review = await session.get(ReviewJob, lease.review_job_id)
        assert review is not None
        review.snapshot_provider = snapshot["provider"]
        review.base_sha = snapshot["base_sha"]
        review.merge_base_sha = snapshot["merge_base_sha"]
        review.captured_at = datetime.fromisoformat(snapshot["captured_at"])
        review.source_repository_external_id = snapshot["head_repository_id"]
        review.base_repository_external_id = snapshot["base_repository_id"]
        review.provider_diff_version = snapshot["provider_metadata"]
        review.coverage = coverage

    def _valid_result(
        self,
        result: ReviewChunkResult,
        files: list[dict[str, Any]] | None,
    ) -> bool:
        try:
            wire = result.to_wire()
            self._result_validators[result.schema_version].validate(wire)
            validate_ranges(wire)
            if files is None:
                return True
            allowed: dict[tuple[str, str], set[int]] = {}
            for file in files:
                diff = file["diff"]
                for side in ("old", "new"):
                    path = diff[f"{side}_path"]
                    if path is not None:
                        allowed.setdefault((path, side.upper()), set()).update(
                            line[f"{side}_line"]
                            for hunk in diff["hunks"]
                            for line in hunk["lines"]
                            if line[f"{side}_line"] is not None
                        )
            for finding in result.findings:
                lines = allowed.get((finding.path, finding.side), set())
                if finding.end_line - finding.start_line + 1 > len(lines) or any(
                    number not in lines
                    for number in range(finding.start_line, finding.end_line + 1)
                ):
                    return False
        except ValidationError, ValueError, TypeError, AttributeError:
            return False
        return True


_COUNTER = ConservativeTokenCounter()


def _estimate(context: BuiltContextPayload) -> int:
    """Record the Gateway estimate of the complete wire, including IDs.

    Iterate until the estimate's own decimal width is accounted for.
    """
    budget = cast(dict[str, Any], context.payload_body["budget"])
    while (
        estimated := _COUNTER.count(json.dumps(context.to_wire(), ensure_ascii=False))
    ) > budget["estimated_input_tokens"]:
        budget["estimated_input_tokens"] = estimated
    return estimated


def _wire_file(diff: FileDiff) -> dict[str, Any]:
    return {
        "diff": diff.model_dump(mode="json"),
        "source_windows": [],
        "whole_file": None,
    }


def _chunk_body(
    body: dict[str, Any], files: list[dict[str, Any]], skipped: list[dict[str, Any]]
) -> dict[str, Any]:
    coverage = ParsedDiff(files, skipped, 0).coverage
    if body["coverage"]["context_quality"] != "diff_only":
        coverage["context_quality"] = "degraded"
    return {
        **body,
        "files": files,
        "related_symbols": [],
        "coverage": coverage,
        "budget": {**body["budget"], "estimated_input_tokens": 0},
    }


def _chunk_contexts(  # noqa: PLR0913 -- explicit plan inputs and limits
    review_id: uuid.UUID,
    body: dict[str, Any],
    parsed: ParsedDiff,
    *,
    limit: int,
    max_chunks: int,
    prompt_fits: Callable[[BuiltContextPayload], bool] | None = None,
) -> tuple[list[BuiltContextPayload], dict[str, Any]]:
    """Split an over-budget diff into chunks that each fit ``limit`` (spec §5.2).

    Code goes first, then config, then docs; at most ``max_chunks`` chunks.
    Whatever does not fit is a visible ``token_budget`` skip, never truncated.
    While a chunk prompt overflows (``prompt_fits``), plan smaller chunks.
    """
    # The fixed part of every chunk wire: metadata, policy skips, a full-width
    # budget figure. Per file add its wire, coverage entry and separators.
    shell = BuiltContextPayload(
        1, review_id, uuid.uuid4(), _chunk_body(body, [], parsed.skipped)
    )
    cast(dict[str, Any], shell.payload_body["budget"])["estimated_input_tokens"] = limit
    envelope = _COUNTER.count(json.dumps(shell.to_wire(), ensure_ascii=False))

    def measure(diff: FileDiff) -> int:
        file = _wire_file(diff)
        entry = ParsedDiff([file], [], 0).coverage["included"][0]
        return (
            _COUNTER.count(json.dumps(file, ensure_ascii=False))
            + _COUNTER.count(json.dumps(entry, ensure_ascii=False))
            + 2
        )

    if envelope >= limit:
        return [], {}
    diffs = [FileDiff.model_validate(file["diff"]) for file in parsed.files]
    target = limit - envelope
    while True:
        plan = plan_chunks(
            diffs,
            _COUNTER,
            ChunkLimits(max_chunk_tokens=target, max_chunks=max_chunks),
            measure=measure,
        )
        contexts: list[BuiltContextPayload] = []
        overflow: list[dict[str, Any]] = []
        for chunk in plan.chunks:
            files = [_wire_file(diff) for diff in chunk.files]
            context = BuiltContextPayload(
                1, review_id, uuid.uuid4(), _chunk_body(body, files, parsed.skipped)
            )
            if _estimate(context) > limit or (
                prompt_fits is not None and not prompt_fits(context)
            ):
                overflow.extend(files)
            else:
                contexts.append(context)
        if not overflow or target <= _MIN_CHUNK_TOKENS:
            break
        target = max(_MIN_CHUNK_TOKENS, target * 3 // 4)
    included: dict[str, list[dict[str, int]]] = {}
    for context in contexts:
        coverage = cast(dict[str, Any], context.payload_body["coverage"])
        for entry in coverage["included"]:
            included.setdefault(entry["path"], []).extend(entry["ranges"])
    coverage = {
        "included": [
            {"path": path, "ranges": ranges} for path, ranges in included.items()
        ],
        "skipped": [
            *parsed.skipped,
            *[item.model_dump(mode="json") for item in plan.skipped],
            *[
                {**entry, "reason": "token_budget"}
                for entry in ParsedDiff(overflow, [], 0).coverage["included"]
            ],
        ],
        "context_quality": cast(dict[str, Any], shell.payload_body["coverage"])[
            "context_quality"
        ],
    }
    return contexts, coverage
