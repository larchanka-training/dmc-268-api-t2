"""AC27: unknown historical cost never becomes a fresh zero budget."""

import uuid
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_analysis_accounting import Execution
from test_analysis_gateway import analysis_task
from test_analysis_handler import DeterministicLlm, github_http
from test_analysis_terminal import detail
from test_postgres_integration import PostgresHarness

from app.adapters.analysis_handler import PostgresAnalysisHandler
from app.application.ports import LeaseLostError
from app.db.llm_accounting import PostgresReviewRunBudget
from app.db.models import ChunkResult, ReviewEvent, ReviewJob
from app.db.task_leases import PostgresTaskLeaseStore
from app.domain.frozen_configuration import FrozenConfigurationError
from app.vcs.github import GithubClient

pytest_plugins = ("test_analysis_accounting", "test_analysis_handler")


async def test_legacy_unknown_spend_is_not_reported_as_empty_budget(
    execution: Execution,
) -> None:
    async with execution.factory.begin() as session:
        await session.execute(
            update(ReviewJob)
            .where(ReviewJob.id == execution.lease.review_job_id)
            .values(frozen_configuration_version=None)
        )
    with pytest.raises(FrozenConfigurationError, match="historical_budget_unknown"):
        await execution.budget().snapshot()


@pytest.mark.parametrize("saved", ["none", "canonical", "invalid"])
async def test_legacy_job_terminalizes_without_inference_or_identity_rewrite(
    postgres: PostgresHarness, review_id: uuid.UUID, saved: str
) -> None:
    factory = async_sessionmaker(postgres.engine, expire_on_commit=False)
    async with factory.begin() as session:
        job = await session.get(ReviewJob, review_id)
        assert job is not None
        identity = (
            job.config_digest,
            job.prompt_digest,
            job.model_settings,
            job.repository_settings_id,
        )
        job.frozen_configuration_version = None
        if saved != "none":
            session.add(
                ChunkResult(
                    id=uuid.uuid4(),
                    review_job_id=review_id,
                    schema_version=1 if saved == "canonical" else 2,
                    summary="Verified historical result",
                    limitations=[],
                    latency_ms=1,
                )
            )
    task = await analysis_task(factory, review_id)
    lease = await PostgresTaskLeaseStore(factory).acquire(
        review_id, owner="legacy", task_kind="analyze"
    )
    assert lease is not None
    llm = DeterministicLlm()
    async with httpx.AsyncClient(transport=github_http()) as github:
        await PostgresAnalysisHandler(
            factory, GithubClient(github), llm, schemas_dir=Path("schemas")
        ).handle(task, lease)
    assert llm.payloads == []
    view = await detail(factory, review_id)
    assert view["status"] == ("PARTIAL" if saved == "canonical" else "FAILED")
    assert view["error"]["reason_code"] == "LLM_UNAVAILABLE"
    assert view["publication_status"] == (
        "PENDING" if saved == "canonical" else "NOT_READY"
    )
    async with factory() as session:
        job = await session.get(ReviewJob, review_id)
        assert job is not None and job.frozen_configuration_version is None
        assert job.analysis_attempts == 0 and job.started_at is None
        assert (
            job.config_digest,
            job.prompt_digest,
            job.model_settings,
            job.repository_settings_id,
        ) == identity
        event = (
            await session.scalars(
                select(ReviewEvent).where(ReviewEvent.review_job_id == review_id)
            )
        ).one()
        assert event.safe_details == {
            "error_code": "historical_budget_unknown",
            "pre_execution": True,
        }
    with pytest.raises(FrozenConfigurationError, match="historical_budget_unknown"):
        await PostgresReviewRunBudget(
            factory, task=task, lease=lease, analysis_attempt=1
        ).snapshot()


async def test_legacy_job_without_live_fence_cannot_terminalize(
    postgres: PostgresHarness, review_id: uuid.UUID
) -> None:
    factory = async_sessionmaker(postgres.engine)
    async with factory.begin() as session:
        await session.execute(
            update(ReviewJob)
            .where(ReviewJob.id == review_id)
            .values(frozen_configuration_version=None)
        )
    task = await analysis_task(factory, review_id)
    lease = await PostgresTaskLeaseStore(factory).acquire(
        review_id, owner="legacy-stale", task_kind="analyze"
    )
    assert lease is not None
    async with httpx.AsyncClient(transport=github_http()) as github:
        with pytest.raises(LeaseLostError):
            await PostgresAnalysisHandler(
                factory,
                GithubClient(github),
                DeterministicLlm(),
                schemas_dir=Path("schemas"),
            ).handle(task, replace(lease, fencing_token=lease.fencing_token + 1))
    view = await detail(factory, review_id)
    assert view["status"] == "QUEUED" and view["finished_at"] is None


@pytest.mark.parametrize("status", ["COMPLETED", "PARTIAL", "FAILED", "SKIPPED"])
async def test_terminal_legacy_review_remains_readable_with_original_digests(
    postgres: PostgresHarness, review_id: uuid.UUID, status: str
) -> None:
    factory = async_sessionmaker(postgres.engine, expire_on_commit=False)
    async with factory.begin() as session:
        job = await session.get(ReviewJob, review_id)
        assert job is not None
        identity = (
            job.config_digest,
            job.rules_digest,
            job.prompt_digest,
            job.model_settings,
        )
        job.frozen_configuration_version = None
        job.status = status
        job.finished_at = await session.scalar(select(func.clock_timestamp()))
    assert (await detail(factory, review_id))["status"] == status
    async with factory() as session:
        job = await session.get(ReviewJob, review_id)
        assert job is not None
        assert (
            job.config_digest,
            job.rules_digest,
            job.prompt_digest,
            job.model_settings,
        ) == identity
