"""Offline bounded backlog validation leaves lifecycle and accounting untouched."""

import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_analysis_terminal import freeze
from test_frozen_configuration import save_policy
from test_postgres_integration import PostgresHarness, _create_review_fixture
from test_runtime_manifest import PROMPTS, manifest_values, write_manifest

from app.db.models import ReviewEvent, ReviewJob, ReviewLlmAttempt, TaskLease
from app.review.configuration import load_runtime_configuration

pytest_plugins = ("test_analysis_handler",)


@pytest.mark.parametrize(
    "defect", ["none", "artifacts", "digest", "pin", "model", "legacy"]
)
async def test_offline_backlog_preflight_reports_compatibility_without_mutation(
    postgres: PostgresHarness,
    review_id: uuid.UUID,
    tmp_path: Path,
    defect: str,
) -> None:
    factory = async_sessionmaker(postgres.engine, expire_on_commit=False)
    await freeze(factory, review_id)
    async with factory() as session:
        job = await session.get(ReviewJob, review_id)
        assert job is not None
        policy = dict(job.model_settings)
    policy["routing"]["primary_profile_ref"] = "ollama/review-v1"
    await save_policy(postgres, review_id, model_settings=policy)
    if defect == "digest":
        await save_policy(postgres, review_id, prompt_digest="0" * 64)
    elif defect == "pin":
        await save_policy(postgres, review_id, model_digest="a" * 64)
    elif defect == "model":
        await save_policy(postgres, review_id, model_ref="unknown-private-model")
    elif defect == "legacy":
        async with factory.begin() as session:
            await session.execute(
                update(ReviewJob)
                .where(ReviewJob.id == review_id)
                .values(frozen_configuration_version=None)
            )
    runtime = load_runtime_configuration(write_manifest(tmp_path, manifest_values()))

    async def state() -> tuple[object, ...]:
        async with factory() as session:
            job = await session.get(ReviewJob, review_id)
            assert job is not None
            counts = [
                await session.scalar(select(func.count()).select_from(table))
                for table in (ReviewEvent, ReviewLlmAttempt, TaskLease)
            ]
            return (
                job.status,
                job.started_at,
                job.analysis_attempts,
                job.model_settings,
                job.config_digest,
                *counts,
            )

    before = await state()
    report = await runtime.offline_preflight(
        factory, tmp_path if defect == "artifacts" else PROMPTS, scan_limit=10
    )
    assert await state() == before
    expected = {
        "digest": "unsupported_prompt_bundle",
        "pin": "model_digest_unattestable",
        "model": "unsupported_model_profile",
        "legacy": "legacy_zero_http_terminalization",
        "artifacts": "unsupported_prompt_bundle",
    }
    assert report.checked_jobs == 1
    # Only a broken runtime blocks; an incompatible job is left to the worker.
    assert report.ready == (defect != "artifacts")
    if defect != "none":
        assert expected[defect] in [issue.code for issue in report.issues]
    if defect not in {"none", "artifacts"}:
        assert report.issues[0].job_id == review_id
        assert not report.issues[0].blocking
    assert "unknown-private-model" not in repr(report)
    assert "source" not in repr(report)


async def test_offline_preflight_blocks_incomplete_scan(
    postgres: PostgresHarness, review_id: uuid.UUID, tmp_path: Path
) -> None:
    runtime = load_runtime_configuration(write_manifest(tmp_path, manifest_values()))
    factory = async_sessionmaker(postgres.engine)
    async with factory.begin() as session:
        await session.execute(
            update(ReviewJob)
            .where(ReviewJob.id == review_id)
            .values(frozen_configuration_version=None)
        )
    with pytest.raises(ValueError):
        await runtime.offline_preflight(factory, PROMPTS, scan_limit=0)
    # A legacy job needs no inference; the scan must still reach the end.
    report = await runtime.offline_preflight(factory, PROMPTS, scan_limit=1)
    assert report.ready
    async with postgres.engine.begin() as connection:
        await _create_review_fixture(postgres, connection)
    report = await runtime.offline_preflight(factory, PROMPTS, scan_limit=1)
    assert not report.ready
    assert report.checked_jobs == 1
    assert "backlog_scan_incomplete" in [issue.code for issue in report.issues]
