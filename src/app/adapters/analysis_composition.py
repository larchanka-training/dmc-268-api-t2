"""Build the existing bounded worker; this module starts no background service."""

from collections.abc import Mapping
from pathlib import Path

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.analysis_handler import PostgresAnalysisHandler, TerminalFollowUp
from app.adapters.follow_up import SystemFollowUp
from app.adapters.redis_queue import RedisQueue
from app.adapters.review_worker import PostgresReviewWorker
from app.application.github_ports import GithubPort, GithubPullRequestReader
from app.application.review_start_ports import ReviewStartAuthorizer
from app.review.cache import ReviewResultCache
from app.review.configuration import (
    FrozenConfigurationError,
    RuntimeProfile,
    TrustedRuntimeConfiguration,
)
from app.review.execution import FrozenAnalysisExecutionFactory
from app.review.incremental import IncrementalPolicy
from app.vcs.input_guards import InputGuardPolicy


async def prepare_postgres_analysis_worker(  # noqa: PLR0913 -- explicit preparation boundary
    factory: async_sessionmaker[AsyncSession],
    queue: RedisQueue,
    *,
    runtime: TrustedRuntimeConfiguration,
    github: GithubPort,
    http: httpx.AsyncClient,
    prompt_root: Path,
    schemas_dir: Path,
    owner: str,
    environ: Mapping[str, str] | None = None,
    scan_limit: int = 100,
    input_guards: InputGuardPolicy | None = None,
    incremental: IncrementalPolicy | None = None,
    pulls: GithubPullRequestReader | None = None,
    authorizer: ReviewStartAuthorizer | None = None,
    cache: ReviewResultCache | None = None,
) -> PostgresReviewWorker:
    """Future CLI must await this before receive/claim. Starts no loop or I/O task.

    With incremental review, a live PR reader and the initiator's start
    authorizer, a terminal analysis admits the system follow-up start through
    the same frozen admission resolver."""
    report = await runtime.offline_preflight(
        factory, prompt_root, scan_limit=scan_limit
    )
    if not report.ready:
        raise FrozenConfigurationError("runtime_preflight_failed")
    profiles = runtime.execution_profiles(report.required_profiles, environ=environ)
    follow_up = (
        SystemFollowUp(
            factory,
            pulls,
            runtime.admission_resolver(factory, prompt_root),
            authorizer=authorizer,
            prompt_root=prompt_root,
        )
        if incremental is not None and pulls is not None and authorizer is not None
        else None
    )
    return build_postgres_analysis_worker(
        factory,
        queue,
        github=github,
        http=http,
        profiles=profiles,
        prompt_root=prompt_root,
        schemas_dir=schemas_dir,
        owner=owner,
        input_guards=input_guards,
        incremental=incremental,
        follow_up=follow_up,
        cache=cache,
    )


def build_postgres_analysis_worker(  # noqa: PLR0913 -- explicit runtime dependencies
    factory: async_sessionmaker[AsyncSession],
    queue: RedisQueue,
    *,
    github: GithubPort,
    http: httpx.AsyncClient,
    profiles: Mapping[str, RuntimeProfile],
    prompt_root: Path,
    schemas_dir: Path,
    owner: str,
    input_guards: InputGuardPolicy | None = None,
    incremental: IncrementalPolicy | None = None,
    follow_up: TerminalFollowUp | None = None,
    cache: ReviewResultCache | None = None,
) -> PostgresReviewWorker:
    return PostgresReviewWorker(
        factory,
        queue,
        PostgresAnalysisHandler(
            factory,
            github,
            None,
            schemas_dir=schemas_dir,
            execution_factory=FrozenAnalysisExecutionFactory(
                factory, prompt_root, profiles, http
            ),
            input_guards=input_guards,
            incremental=incremental,
            follow_up=follow_up,
            cache=cache,
        ),
        owner=owner,
        task_kind="analyze",
    )
