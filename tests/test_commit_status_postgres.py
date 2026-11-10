"""Commit status selection and retry state through real PostgreSQL."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from test_publications_postgres import HEAD, seed_review

from app.db import commit_status_models  # noqa: F401 -- table for create_all
from app.db.commit_status_models import CommitStatus
from app.db.commit_statuses import PostgresCommitStatusStore
from app.db.models import ChunkResult, Finding, Repository, ReviewJob

pytest_plugins = ("test_publications_postgres",)


def _store(engine: AsyncEngine) -> PostgresCommitStatusStore:
    return PostgresCommitStatusStore(async_sessionmaker(engine, expire_on_commit=False))


async def _add_critical(engine: AsyncEngine, review_id: uuid.UUID) -> None:
    async with engine.begin() as connection:
        chunk_id = await connection.scalar(
            select(ChunkResult.id).where(ChunkResult.review_job_id == review_id)
        )
        await connection.execute(
            insert(Finding).values(
                id=uuid.uuid4(),
                chunk_result_id=chunk_id,
                fingerprint="critical-v1",
                path="src/review.py",
                side="NEW",
                start_line=1,
                end_line=1,
                category="security",
                severity="critical",
                title="SQL injection",
                explanation="Raw input in SQL",
                evidence="execute(raw)",
                recommendation="Bind parameters",
                proposed_diff_fix=None,
            )
        )


async def _set_job(engine: AsyncEngine, review_id: uuid.UUID, **values: Any) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            update(ReviewJob).where(ReviewJob.id == review_id).values(**values)
        )


async def test_due_selects_unposted_cycle_with_critical_count(
    publication_engine: AsyncEngine, payload_data: dict[str, Any]
) -> None:
    review_id, _ = await seed_review(publication_engine, payload_data)
    await _add_critical(publication_engine, review_id)

    [target] = await _store(publication_engine).due(limit=10)

    assert target.review_id == review_id
    assert (target.owner, target.name, target.head_sha) == (
        "larchanka-training",
        "demo",
        HEAD,
    )
    assert target.review_status == "COMPLETED"
    assert target.critical_findings == 1
    assert (target.posted_state, target.failed_attempts) == (None, 0)


async def test_final_posted_state_is_not_selected_again(
    publication_engine: AsyncEngine, payload_data: dict[str, Any]
) -> None:
    review_id, _ = await seed_review(publication_engine, payload_data)
    store = _store(publication_engine)

    await store.record_posted(review_id, "success")

    assert await store.due(limit=10) == []


async def test_pending_is_reselected_only_after_the_review_finishes(
    publication_engine: AsyncEngine, payload_data: dict[str, Any]
) -> None:
    review_id, _ = await seed_review(publication_engine, payload_data)
    await _set_job(publication_engine, review_id, status="QUEUED", finished_at=None)
    store = _store(publication_engine)
    [running] = await store.due(limit=10)
    assert running.review_status == "QUEUED"

    await store.record_posted(review_id, "pending")
    assert await store.due(limit=10) == []

    await _set_job(
        publication_engine,
        review_id,
        status="COMPLETED",
        finished_at=datetime.now(UTC),
    )
    [finished] = await store.due(limit=10)
    assert (finished.review_status, finished.posted_state) == ("COMPLETED", "pending")


async def test_newer_cycle_on_the_same_head_supersedes_the_old_one(
    publication_engine: AsyncEngine, payload_data: dict[str, Any]
) -> None:
    old_id, change_id = await seed_review(publication_engine, payload_data)
    new_id, _ = await seed_review(publication_engine, payload_data, change_id=change_id)
    await _set_job(
        publication_engine, old_id, created_at=datetime.now(UTC) - timedelta(minutes=5)
    )

    assert [
        item.review_id for item in await _store(publication_engine).due(limit=10)
    ] == [new_id]


async def test_failure_backs_off_and_counts_attempts(
    publication_engine: AsyncEngine, payload_data: dict[str, Any]
) -> None:
    review_id, _ = await seed_review(publication_engine, payload_data)
    store = _store(publication_engine)

    await store.record_failure(review_id, retry_after_s=60)
    assert await store.due(limit=10) == []

    async with publication_engine.begin() as connection:
        await connection.execute(
            update(CommitStatus)
            .where(CommitStatus.review_job_id == review_id)
            .values(retry_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    await store.record_failure(review_id, retry_after_s=0)
    async with publication_engine.begin() as connection:
        await connection.execute(
            update(CommitStatus)
            .where(CommitStatus.review_job_id == review_id)
            .values(retry_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    [target] = await store.due(limit=10)
    assert target.failed_attempts == 2  # noqa: PLR2004

    await store.record_posted(review_id, "success")
    async with publication_engine.connect() as connection:
        row = (
            await connection.execute(
                select(CommitStatus.failed_attempts, CommitStatus.retry_at).where(
                    CommitStatus.review_job_id == review_id
                )
            )
        ).one()
    assert tuple(row) == (0, None)


async def test_cycles_older_than_the_window_are_not_backfilled(
    publication_engine: AsyncEngine, payload_data: dict[str, Any]
) -> None:
    review_id, _ = await seed_review(publication_engine, payload_data)
    await _set_job(
        publication_engine, review_id, created_at=datetime.now(UTC) - timedelta(days=2)
    )

    assert await _store(publication_engine).due(limit=10) == []


async def test_bot_denied_integration_is_not_selected(
    publication_engine: AsyncEngine, payload_data: dict[str, Any]
) -> None:
    review_id, _ = await seed_review(publication_engine, payload_data)
    async with publication_engine.begin() as connection:
        repository_id = await connection.scalar(
            select(ReviewJob.repository_id).where(ReviewJob.id == review_id)
        )
        await connection.execute(
            update(Repository)
            .where(Repository.id == repository_id)
            .values(integration_status="bot_denied")
        )

    assert await _store(publication_engine).due(limit=10) == []
