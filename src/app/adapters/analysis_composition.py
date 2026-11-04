"""Build the existing bounded worker; this module starts no background service."""

from collections.abc import Mapping
from pathlib import Path

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.analysis_handler import PostgresAnalysisHandler
from app.adapters.redis_queue import RedisQueue
from app.adapters.review_worker import PostgresReviewWorker
from app.application.github_ports import GithubPort
from app.review.configuration import (
    FrozenConfigurationError,
    RuntimeProfile,
    TrustedRuntimeConfiguration,
)
from app.review.execution import FrozenAnalysisExecutionFactory


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
) -> PostgresReviewWorker:
    """Future CLI must await this before receive/claim. Starts no loop or I/O task."""
    report = await runtime.offline_preflight(
        factory, prompt_root, scan_limit=scan_limit
    )
    if not report.ready:
        raise FrozenConfigurationError("runtime_preflight_failed")
    profiles = runtime.execution_profiles(report.required_profiles, environ=environ)
    return build_postgres_analysis_worker(
        factory,
        queue,
        github=github,
        http=http,
        profiles=profiles,
        prompt_root=prompt_root,
        schemas_dir=schemas_dir,
        owner=owner,
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
        ),
        owner=owner,
        task_kind="analyze",
    )
