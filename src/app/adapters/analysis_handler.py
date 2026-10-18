"""Bounded PostgreSQL analysis handler; providers are explicitly injected."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from referencing import Registry, Resource
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.analysis_authority import current_analysis_authority
from app.application.github_ports import GithubFailure, GithubPort, GithubRequest
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
)
from app.db.repositories import (
    SqlAlchemyPublicationRepository,
    SqlAlchemyReviewJobRepository,
)
from app.db.review_events import append_review_event
from app.db.task_leases import lock_owned_lease
from app.db.worker_tasks import PostgresWorkerTasks
from app.domain.contract_validation import validate_hunks, validate_ranges
from app.domain.enums import ReviewReasonCode, ReviewStatus
from app.domain.frozen_configuration import FrozenConfigurationError
from app.domain.settings import project_historical_empty_settings
from app.llm.tokens import ConservativeTokenCounter
from app.review.execution import FrozenAnalysisExecutionFactory
from app.review.port_adapter import ChunkReviewFailedError
from app.vcs.diff import InvalidDiff, parse_diff


class _AnalysisFailureError(Exception):
    def __init__(self, reason: ReviewReasonCode) -> None:
        self.reason = reason
        super().__init__(reason.value)


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
        execution_factory: FrozenAnalysisExecutionFactory | None = None,
    ) -> None:
        self._factory = session_factory
        self._github = github
        self._llm = llm
        self._execution_factory = execution_factory
        self._limit = limit_input_tokens
        self._reserved = reserved_output_tokens
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

    async def handle(  # noqa: PLR0911 -- explicit expected terminal/retry outcomes
        self,
        task: QueueTaskEnvelope,
        lease: TaskLeaseSnapshot,
    ) -> DurableTaskOutcome | None:
        try:
            return await self._execute(task, lease)
        except FrozenConfigurationError as failure:
            async with self._factory.begin() as session:
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

    async def _execute(  # noqa: PLR0912, PLR0915 — one bounded Level 1 capture
        self,
        task: QueueTaskEnvelope,
        lease: TaskLeaseSnapshot,
    ) -> DurableTaskOutcome | None:
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
        assert llm is not None
        await self._ensure_call_authority(task, lease)
        async with self._factory.begin() as session:
            stored = await session.scalar(
                select(ContextPayload).where(
                    ContextPayload.review_job_id == lease.review_job_id,
                )
            )
            saved = (
                BuiltContextPayload(
                    stored.schema_version,
                    stored.review_job_id,
                    stored.id,
                    stored.payload_body,
                )
                if stored is not None
                else None
            )
            await self._transition(
                session,
                task,
                lease,
                ReviewStatus.LLM_PROCESSING if saved else ReviewStatus.FETCHING_DIFF,
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
            language = settings.output_language
            ignores = list(
                project_historical_empty_settings(settings.ignores, "globs")["globs"]
            )
            expected_source = change.source_repository_external_id

        if saved is not None:
            self._check_snapshot(
                cast(dict[str, Any], saved.payload_body["snapshot"]),
                request,
                expected_source,
            )
            self._context_validator.validate(saved.to_wire())
            validate_ranges(saved.to_wire())
            for file in cast(list[dict[str, Any]], saved.payload_body["files"]):
                validate_hunks(file["diff"]["hunks"])
            return await self._infer(task, lease, saved, llm)
        await self._ensure_call_authority(task, lease)
        try:
            captured = await self._github.fetch(request)
        except GithubFailure as failure:
            if failure.retryable:
                raise
            raise _AnalysisFailureError(ReviewReasonCode(failure.reason_code)) from None
        self._check_snapshot(captured.metadata["snapshot"], request, expected_source)
        try:
            parsed = parse_diff(captured.raw_diff, ignore_globs=ignores)
        except InvalidDiff:
            raise _AnalysisFailureError(ReviewReasonCode.DIFF_UNTRUSTWORTHY) from None
        if not parsed.files:
            async with self._factory.begin() as session:
                await self._transition(session, task, lease, ReviewStatus.SKIPPED)
                await self._save_snapshot(
                    session,
                    lease,
                    captured.metadata["snapshot"],
                    parsed.coverage,
                )
            return DurableTaskOutcome.COMPLETED
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
        # Use the Gateway's token estimator for the complete wire, including IDs.
        # Iterate until the estimate's own decimal width is accounted for.
        counter = ConservativeTokenCounter()
        while (
            estimated := counter.count(
                json.dumps(context.to_wire(), ensure_ascii=False)
            )
        ) > body["budget"]["estimated_input_tokens"]:
            body["budget"]["estimated_input_tokens"] = estimated
        if estimated > limit:
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
                await self._transition(session, task, lease, ReviewStatus.SKIPPED)
                await self._save_snapshot(session, lease, body["snapshot"], coverage)
            return DurableTaskOutcome.COMPLETED
        self._context_validator.validate(context.to_wire())
        validate_ranges(context.to_wire())
        for file in parsed.files:
            validate_hunks(file["diff"]["hunks"])
        async with self._factory.begin() as session:
            await self._transition(session, task, lease, ReviewStatus.PARSING_CONTEXT)
            await self._save_snapshot(session, lease, body["snapshot"], parsed.coverage)
            session.add(
                ContextPayload(
                    id=context.chunk_id,
                    review_job_id=context.review_id,
                    schema_version=context.schema_version,
                    payload_body=context.payload_body,
                )
            )
            await self._transition(session, task, lease, ReviewStatus.LLM_PROCESSING)

        return await self._infer(task, lease, context, llm)

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

    async def _infer(
        self,
        task: QueueTaskEnvelope,
        lease: TaskLeaseSnapshot,
        context: BuiltContextPayload,
        llm: LlmGatewayPort,
    ) -> DurableTaskOutcome:
        files = cast(list[dict[str, Any]], context.payload_body["files"])
        await self._ensure_call_authority(task, lease)
        result = await llm.review(context)
        authority = current_analysis_authority.get()
        if authority is not None:
            authority.check()
        if not self._valid_result(result, files):
            # Raw format repair belongs exclusively to ReviewService. Invalid
            # backend results fail defensively without another provider call.
            async with self._factory.begin() as session:
                await self._transition(
                    session,
                    task,
                    lease,
                    ReviewStatus.FAILED,
                    ReviewReasonCode.LLM_OUTPUT_INVALID,
                    error_code="backend_result_invalid",
                )
            return DurableTaskOutcome.COMPLETED
        async with self._factory.begin() as session:
            locked = await lock_owned_lease(session, lease)
            if locked is None:
                raise LeaseLostError("Analysis lease is no longer current")
            if authority is not None:
                authority.check()
            deadline = locked[0].analysis_deadline_at
            late = deadline is not None and deadline <= locked[2]
            await self._transition(
                session,
                task,
                lease,
                ReviewStatus.PARTIAL
                if late or result.limitations
                else ReviewStatus.COMPLETED,
                ReviewReasonCode.ANALYSIS_DEADLINE_EXCEEDED if late else None,
            )
            review = await session.get(ReviewJob, lease.review_job_id)
            assert review is not None
            review.final_summary = result.summary
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
            seen_fingerprints: set[str] = set()
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
            await self._prepare_publication(session, task, lease)
        return DurableTaskOutcome.COMPLETED

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
