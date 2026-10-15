"""Build the existing bounded worker; this module starts no background service."""

from collections.abc import Mapping
from pathlib import Path

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.analysis_handler import PostgresAnalysisHandler
from app.adapters.redis_queue import RedisQueue
from app.adapters.review_worker import PostgresReviewWorker
from app.application.github_ports import GithubPort
from app.review.configuration import RuntimeProfile
from app.review.execution import FrozenAnalysisExecutionFactory


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
