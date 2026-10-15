"""Read-only release evidence. No queue, lease, HTTP or budget dependency."""

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import ReviewJob
from app.domain.frozen_configuration import FrozenConfigurationError
from app.review.prompt import PromptBuilder, PromptConfigError

if TYPE_CHECKING:
    from app.review.configuration import TrustedRuntimeConfiguration

MAX_PREFLIGHT_JOBS = 1000


@dataclass(frozen=True, slots=True)
class PreflightIssue:
    job_id: UUID | None
    code: str
    blocking: bool = True


@dataclass(frozen=True, slots=True)
class PreflightReport:
    manifest_identity: str
    checked_jobs: int
    scan_complete: bool
    required_profiles: tuple[str, ...]
    issues: tuple[PreflightIssue, ...]

    @property
    def ready(self) -> bool:
        return self.scan_complete and not any(issue.blocking for issue in self.issues)


async def offline_preflight(
    runtime: TrustedRuntimeConfiguration,
    factory: async_sessionmaker[AsyncSession],
    prompt_root: Path,
    *,
    scan_limit: int = 100,
) -> PreflightReport:
    if type(scan_limit) is not int or not 1 <= scan_limit <= MAX_PREFLIGHT_JOBS:
        raise ValueError("scan_limit must be an integer in [1, 1000]")
    issues = []
    required = {
        runtime.admission.primary.profile_ref,
        *(route.profile_ref for route in runtime.admission.policy.routing.fallback),
    }
    try:
        for version in ("v1", "v2"):
            PromptBuilder.load(prompt_root, version).ensure_verified()
        PromptBuilder.load(
            prompt_root,
            runtime.admission.prompt_version,
            expected_digest=runtime.admission.prompt_digest,
        ).ensure_verified()
    except PromptConfigError:
        issues.append(PreflightIssue(None, "unsupported_prompt_bundle"))
    async with factory() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        jobs = (
            await session.execute(
                select(ReviewJob.id, ReviewJob.frozen_configuration_version)
                .where(
                    ReviewJob.status.in_(
                        ("QUEUED", "FETCHING_DIFF", "PARSING_CONTEXT", "LLM_PROCESSING")
                    )
                )
                .order_by(ReviewJob.id)
                .limit(scan_limit + 1)
            )
        ).all()
    complete = len(jobs) <= scan_limit
    if not complete:
        issues.append(PreflightIssue(None, "backlog_scan_incomplete"))
    reader = runtime.configuration_reader(factory, prompt_root)
    for job_id, version in jobs[:scan_limit]:
        if version is None:
            # Existing handler can fail/retain canonical results without inference.
            # Unknown historical spend is NOT inferred to be zero or refunded.
            issues.append(
                PreflightIssue(job_id, "legacy_zero_http_terminalization", False)
            )
            continue
        try:
            config = await reader.resolve(job_id)
            required.add(config.policy.routing.primary_profile_ref)
            required.update(
                route.profile_ref for route in config.policy.routing.fallback
            )
        except FrozenConfigurationError as exc:
            issues.append(PreflightIssue(job_id, exc.reason))
    return PreflightReport(
        runtime.identity,
        min(len(jobs), scan_limit),
        complete,
        tuple(sorted(required)),
        tuple(issues),
    )
