from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.application.publication_ports import (
    PublicationAuthority,
    PublicationAuthorityError,
)
from app.db.base import Base
from app.db.models import (
    ChangeRequest,
    ChunkResult,
    ContextPayload,
    Finding,
    Publication,
    Repository,
    RepositoryAccess,
    RepositorySettings,
    ReviewJob,
    TaskLease,
    User,
)
from app.db.publication_models import PublicationOperation  # noqa: F401
from app.db.publications import PostgresPublicationStore
from app.db.review_events import SqlAlchemyReviewEvents

_DATABASE_URL = os.environ.get("DMC268_TEST_DATABASE_URL")
if not _DATABASE_URL:
    pytest.skip("set DMC268_TEST_DATABASE_URL", allow_module_level=True)

HEAD = "a" * 40
BASE = "b" * 40
MERGE_BASE = "c" * 40
PR_NUMBER = 42
FINDING_LINE = 39


def lease_values(**values: Any) -> dict[str, Any]:
    """Support the accepted main and PR #29's additive task-kind column."""
    if "task_kind" in TaskLease.__table__.columns:
        values.setdefault("task_kind", "publish")
    return values


@pytest_asyncio.fixture
async def publication_engine() -> AsyncIterator[AsyncEngine]:
    assert _DATABASE_URL is not None
    schema = f"publication_test_{uuid.uuid4().hex}"
    admin = create_async_engine(_DATABASE_URL, poolclass=NullPool)
    async with admin.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        _DATABASE_URL,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": schema}},
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield engine
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


async def seed_review(
    engine: AsyncEngine,
    payload: dict[str, Any],
    *,
    change_id: uuid.UUID | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    repository_id, settings_id, review_id = (uuid.uuid4() for _ in range(3))
    context_id, chunk_id = uuid.uuid4(), uuid.uuid4()
    now = datetime.now(UTC)
    payload = deepcopy(payload)
    payload["snapshot"].update(
        head_sha=HEAD,
        base_sha=BASE,
        merge_base_sha=MERGE_BASE,
        base_repository_id="100",
        head_repository_id="101",
    )
    payload["review_id"] = str(review_id)
    payload["chunk_id"] = str(chunk_id)
    async with engine.begin() as connection:
        if change_id is None:
            change_id = uuid.uuid4()
            await connection.execute(
                insert(Repository).values(
                    id=repository_id,
                    provider_name="github",
                    external_id="100",
                    owner="larchanka-training",
                    repo_name="demo",
                )
            )
            await connection.execute(
                insert(RepositorySettings).values(
                    id=settings_id,
                    repository_id=repository_id,
                    rules={"instructions": []},
                    ignores={"globs": []},
                    rules_digest="rules-v1",
                    max_starts_per_hour=10,
                    max_active_jobs=2,
                    output_language="ru",
                    surrounding_lines=5,
                )
            )
            await connection.execute(
                insert(ChangeRequest).values(
                    id=change_id,
                    repository_id=repository_id,
                    external_number="42",
                    source_repository_external_id="101",
                    source_ref="feature",
                    base_ref="main",
                    head_sha=HEAD,
                    base_sha=BASE,
                    title="Review me",
                    description="",
                    current_status="open",
                )
            )
        else:
            row = (
                await connection.execute(
                    text(
                        "SELECT repository_id, repository_settings_id FROM review_job "
                        "WHERE change_request_id = :change_id LIMIT 1"
                    ),
                    {"change_id": change_id},
                )
            ).one()
            repository_id, settings_id = row
        await connection.execute(
            insert(ReviewJob).values(
                id=review_id,
                repository_id=repository_id,
                change_request_id=change_id,
                repository_settings_id=settings_id,
                config_digest="config-v1",
                rules_digest="rules-v1",
                trigger_type="manual",
                requested_head_sha=HEAD,
                snapshot_provider="github",
                base_sha=BASE,
                merge_base_sha=MERGE_BASE,
                source_repository_external_id="101",
                base_repository_external_id="100",
                provider_diff_version={"pull_number": 42},
                captured_at=now,
                model_ref="test-model",
                prompt_version="v1",
                prompt_digest="prompt-v1",
                status="COMPLETED",
                started_at=now,
                finished_at=now,
                queue_deadline_at=now + timedelta(minutes=15),
                final_summary="One security risk",
                coverage=payload["coverage"],
            )
        )
        await connection.execute(
            insert(Publication).values(
                id=uuid.uuid4(),
                review_job_id=review_id,
                status="PENDING",
                started_at=now,
                deadline_at=now + timedelta(minutes=5),
            )
        )
        await connection.execute(
            insert(ContextPayload).values(
                id=context_id,
                review_job_id=review_id,
                schema_version=1,
                payload_body=payload,
            )
        )
        await connection.execute(
            insert(ChunkResult).values(
                id=chunk_id,
                review_job_id=review_id,
                context_payload_id=context_id,
                schema_version=1,
                summary="One security risk",
                limitations=[],
                latency_ms=1,
            )
        )
        await connection.execute(
            insert(Finding).values(
                id=uuid.uuid4(),
                chunk_result_id=chunk_id,
                fingerprint="finding-v1",
                path="src/review.py",
                side="NEW",
                start_line=39,
                end_line=39,
                category="security",
                severity="high",
                title="Validate input",
                explanation="Untrusted input is returned",
                evidence="return data",
                recommendation="Validate the input",
                proposed_diff_fix=None,
            )
        )
    return review_id, change_id


async def test_publisher_loads_frozen_analysis_and_retained_findings(
    publication_engine: AsyncEngine, payload_data: dict[str, Any]
) -> None:
    review_id, change_id = await seed_review(publication_engine, payload_data)
    async with publication_engine.begin() as connection:
        await connection.execute(
            update(ChangeRequest)
            .where(ChangeRequest.id == change_id)
            .values(head_sha="d" * 40)
        )

    store = PostgresPublicationStore(
        async_sessionmaker(publication_engine, expire_on_commit=False)
    )
    loaded = await store.load(review_id)

    assert loaded is not None
    assert loaded.review_id == review_id
    assert loaded.head_sha == HEAD
    assert loaded.base_sha == BASE
    assert loaded.merge_base_sha == MERGE_BASE
    assert loaded.config_digest == "config-v1"
    assert loaded.number == PR_NUMBER
    assert loaded.owner == "larchanka-training"
    assert loaded.repository_external_id == "100"
    assert loaded.summary == "One security risk"
    assert loaded.contexts[0]["files"] == payload_data["files"]
    assert loaded.coverage == payload_data["coverage"]
    assert loaded.findings[0].fingerprint == "finding-v1"
    assert loaded.findings[0].start_line == FINDING_LINE


async def test_publisher_refuses_incomplete_frozen_snapshot(
    publication_engine: AsyncEngine, payload_data: dict[str, Any]
) -> None:
    review_id, _ = await seed_review(publication_engine, payload_data)
    async with publication_engine.begin() as connection:
        await connection.execute(
            update(ReviewJob).where(ReviewJob.id == review_id).values(base_sha=None)
        )

    store = PostgresPublicationStore(
        async_sessionmaker(publication_engine, expire_on_commit=False)
    )
    assert await store.load(review_id) is None


async def test_publication_begin_records_durable_operation_with_live_lease(
    publication_engine: AsyncEngine, payload_data: dict[str, Any]
) -> None:
    review_id, change_id = await seed_review(publication_engine, payload_data)
    async with publication_engine.begin() as connection:
        await connection.execute(
            insert(TaskLease).values(
                **lease_values(
                    id=uuid.uuid4(),
                    review_job_id=review_id,
                    owner="publisher-a",
                    fencing_token=1,
                    expires_at=datetime.now(UTC) + timedelta(seconds=90),
                )
            )
        )

    store = PostgresPublicationStore(
        async_sessionmaker(publication_engine, expire_on_commit=False)
    )
    claim = await store.begin(
        review_id,
        authority=PublicationAuthority(review_id, "publisher-a", 1),
        marker="<!-- review:one -->",
        payload_hash="a" * 64,
    )
    assert claim is not None
    assert claim.review_id == review_id
    assert claim.change_request_id == change_id
    assert claim.marker == "<!-- review:one -->"
    assert claim.payload_hash == "a" * 64
    assert await store.check_authority(claim) is True


async def test_publication_begin_rejects_expired_or_replaced_authority(
    publication_engine: AsyncEngine, payload_data: dict[str, Any]
) -> None:
    review_id, _ = await seed_review(publication_engine, payload_data)
    async with publication_engine.begin() as connection:
        await connection.execute(
            insert(TaskLease).values(
                **lease_values(
                    id=uuid.uuid4(),
                    review_job_id=review_id,
                    owner="publisher-b",
                    fencing_token=2,
                    expires_at=datetime.now(UTC) - timedelta(seconds=1),
                )
            )
        )

    store = PostgresPublicationStore(
        async_sessionmaker(publication_engine, expire_on_commit=False)
    )
    assert (
        await store.begin(
            review_id,
            authority=PublicationAuthority(review_id, "publisher-a", 1),
            marker="<!-- review:one -->",
            payload_hash="a" * 64,
        )
        is None
    )


async def test_analysis_authority_cannot_begin_publication(
    publication_engine: AsyncEngine, payload_data: dict[str, Any]
) -> None:
    review_id, _ = await seed_review(publication_engine, payload_data)
    async with publication_engine.begin() as connection:
        await connection.execute(
            insert(TaskLease).values(
                **lease_values(
                    id=uuid.uuid4(),
                    review_job_id=review_id,
                    owner="analyzer",
                    fencing_token=1,
                    expires_at=datetime.now(UTC) + timedelta(seconds=90),
                )
            )
        )

    store = PostgresPublicationStore(
        async_sessionmaker(publication_engine, expire_on_commit=False)
    )
    assert (
        await store.begin(
            review_id,
            authority=PublicationAuthority(review_id, "analyzer", 1, "analyze"),
            marker="<!-- review:one -->",
            payload_hash="a" * 64,
        )
        is None
    )


async def test_pr29_persisted_task_kind_is_authoritative(
    publication_engine: AsyncEngine, payload_data: dict[str, Any]
) -> None:
    review_id, _ = await seed_review(publication_engine, payload_data)
    async with publication_engine.begin() as connection:
        await connection.execute(
            text("ALTER TABLE task_lease ADD COLUMN IF NOT EXISTS task_kind text")
        )
        await connection.execute(
            insert(TaskLease).values(
                **lease_values(
                    id=uuid.uuid4(),
                    review_job_id=review_id,
                    owner="analyzer",
                    fencing_token=1,
                    expires_at=datetime.now(UTC) + timedelta(seconds=90),
                )
            )
        )
        await connection.execute(text("UPDATE task_lease SET task_kind = 'analyze'"))

    store = PostgresPublicationStore(
        async_sessionmaker(publication_engine, expire_on_commit=False)
    )
    assert (
        await store.begin(
            review_id,
            authority=PublicationAuthority(review_id, "analyzer", 1),
            marker="<!-- review:one -->",
            payload_hash="a" * 64,
        )
        is None
    )


async def test_one_unresolved_operation_serializes_all_reviews_of_same_pr(
    publication_engine: AsyncEngine, payload_data: dict[str, Any]
) -> None:
    first, change_id = await seed_review(publication_engine, payload_data)
    second, _ = await seed_review(publication_engine, payload_data, change_id=change_id)
    async with publication_engine.begin() as connection:
        for review_id in (first, second):
            await connection.execute(
                insert(TaskLease).values(
                    **lease_values(
                        id=uuid.uuid4(),
                        review_job_id=review_id,
                        owner=str(review_id),
                        fencing_token=1,
                        expires_at=datetime.now(UTC) + timedelta(seconds=90),
                    )
                )
            )

    store = PostgresPublicationStore(
        async_sessionmaker(publication_engine, expire_on_commit=False)
    )
    claims = await asyncio.gather(
        *(
            store.begin(
                review_id,
                authority=PublicationAuthority(review_id, str(review_id), 1),
                marker=f"<!-- review:{review_id} -->",
                payload_hash="a" * 64,
            )
            for review_id in (first, second)
        )
    )
    assert sum(claim is not None for claim in claims) == 1
    winner = next(claim for claim in claims if claim is not None)
    loser = second if winner.review_id == first else first
    async with publication_engine.begin() as connection:
        await connection.execute(
            update(TaskLease)
            .where(TaskLease.review_job_id == winner.review_id)
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    assert (
        await store.begin(
            loser,
            authority=PublicationAuthority(loser, str(loser), 1),
            marker="<!-- waiting -->",
            payload_hash="a" * 64,
        )
        is None
    )


async def test_publication_attempt_requires_unexpired_pending_cycle(
    publication_engine: AsyncEngine, payload_data: dict[str, Any]
) -> None:
    review_id, _ = await seed_review(publication_engine, payload_data)
    async with publication_engine.begin() as connection:
        await connection.execute(
            insert(TaskLease).values(
                **lease_values(
                    id=uuid.uuid4(),
                    review_job_id=review_id,
                    owner="publisher-a",
                    fencing_token=1,
                    expires_at=datetime.now(UTC) + timedelta(seconds=90),
                )
            )
        )
        await connection.execute(
            update(Publication)
            .where(Publication.review_job_id == review_id)
            .values(deadline_at=datetime.now(UTC) - timedelta(seconds=1))
        )

    store = PostgresPublicationStore(
        async_sessionmaker(publication_engine, expire_on_commit=False)
    )
    assert (
        await store.begin(
            review_id,
            authority=PublicationAuthority(review_id, "publisher-a", 1),
            marker="<!-- review:one -->",
            payload_hash="a" * 64,
        )
        is None
    )


@pytest.mark.parametrize(
    "status", ["PUBLISHED", "PARTIAL", "FAILED", "UNKNOWN", "SKIPPED"]
)
async def test_publication_outcomes_are_durable_after_attempt_deadline(
    publication_engine: AsyncEngine, payload_data: dict[str, Any], status: str
) -> None:
    review_id, _ = await seed_review(publication_engine, payload_data)
    async with publication_engine.begin() as connection:
        await connection.execute(
            insert(TaskLease).values(
                **lease_values(
                    id=uuid.uuid4(),
                    review_job_id=review_id,
                    owner="publisher-a",
                    fencing_token=1,
                    expires_at=datetime.now(UTC) + timedelta(seconds=90),
                )
            )
        )

    store = PostgresPublicationStore(
        async_sessionmaker(publication_engine, expire_on_commit=False)
    )
    claim = await store.begin(
        review_id,
        authority=PublicationAuthority(review_id, "publisher-a", 1),
        marker="<!-- review:one -->",
        payload_hash="a" * 64,
    )
    assert claim is not None
    async with publication_engine.begin() as connection:
        await connection.execute(
            update(Publication)
            .where(Publication.review_job_id == review_id)
            .values(deadline_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    await store.save(claim, status=status, provider_metadata={"review_id": 123})
    stored = await PostgresPublicationStore(
        async_sessionmaker(publication_engine, expire_on_commit=False)
    ).get(review_id)
    assert stored is not None
    assert stored.status == status
    assert stored.provider_metadata == {"review_id": 123}
    assert stored.finished_at is not None
    assert stored.deadline_at is not None
    assert stored.finished_at > stored.deadline_at
    loaded = await store.load(review_id)
    assert loaded is not None
    assert loaded.summary == "One security risk"


async def test_publication_progress_and_outcome_append_replayable_events(
    publication_engine: AsyncEngine, payload_data: dict[str, Any]
) -> None:
    review_id, _ = await seed_review(publication_engine, payload_data)
    user_id = uuid.uuid4()
    async with publication_engine.begin() as connection:
        repository_id = await connection.scalar(
            select(ReviewJob.repository_id).where(ReviewJob.id == review_id)
        )
        await connection.execute(insert(User).values(id=user_id))
        await connection.execute(
            insert(RepositoryAccess).values(
                id=uuid.uuid4(),
                repository_id=repository_id,
                user_id=user_id,
                role="reviewer",
            )
        )
        await connection.execute(
            insert(TaskLease).values(
                **lease_values(
                    id=uuid.uuid4(),
                    review_job_id=review_id,
                    owner="publisher-a",
                    fencing_token=1,
                    expires_at=datetime.now(UTC) + timedelta(seconds=90),
                )
            )
        )

    sessions = async_sessionmaker(publication_engine, expire_on_commit=False)
    store = PostgresPublicationStore(sessions)
    claim = await store.begin(
        review_id,
        authority=PublicationAuthority(review_id, "publisher-a", 1),
        marker="<!-- review:one -->",
        payload_hash="a" * 64,
    )
    assert claim is not None
    await store.save(claim, status="PUBLISHED", provider_metadata={"review_id": 123})
    page = await SqlAlchemyReviewEvents(sessions).read(review_id, user_id, after=0)
    assert [event.publication_status for event in page.events] == [
        "PENDING",
        "PUBLISHED",
    ]
    assert [event.sequence for event in page.events] == [1, 2]
    assert [event.status for event in page.events] == ["COMPLETED", "COMPLETED"]


async def test_unknown_reconciliation_requires_fresh_fence_and_confirmed_absence(
    publication_engine: AsyncEngine, payload_data: dict[str, Any]
) -> None:
    review_id, _ = await seed_review(publication_engine, payload_data)
    async with publication_engine.begin() as connection:
        await connection.execute(
            insert(TaskLease).values(
                **lease_values(
                    id=uuid.uuid4(),
                    review_job_id=review_id,
                    owner="publisher-a",
                    fencing_token=1,
                    expires_at=datetime.now(UTC) + timedelta(seconds=90),
                )
            )
        )

    store = PostgresPublicationStore(
        async_sessionmaker(publication_engine, expire_on_commit=False)
    )
    original_authority = PublicationAuthority(review_id, "publisher-a", 1)
    claim = await store.begin(
        review_id,
        authority=original_authority,
        marker="<!-- review:one -->",
        payload_hash="a" * 64,
    )
    assert claim is not None
    await store.save(claim, status="UNKNOWN", provider_metadata=None)
    with pytest.raises(PublicationAuthorityError):
        await store.reconcile(
            review_id,
            authority=original_authority,
            status="FAILED",
            confirmed_absent=True,
            provider_metadata=None,
        )
    async with publication_engine.begin() as connection:
        await connection.execute(
            update(TaskLease)
            .where(TaskLease.review_job_id == review_id)
            .values(owner="reconciler", fencing_token=2)
        )
    authority = PublicationAuthority(review_id, "reconciler", 2)
    with pytest.raises(ValueError, match="absence"):
        await store.reconcile(
            review_id,
            authority=authority,
            status="FAILED",
            confirmed_absent=False,
            provider_metadata=None,
        )
    await store.reconcile(
        review_id,
        authority=authority,
        status="FAILED",
        confirmed_absent=True,
        provider_metadata=None,
    )
    state = await store.get(review_id)
    assert state is not None
    assert state.status == "FAILED"
    assert (
        await store.begin(
            review_id,
            authority=authority,
            marker="<!-- review:one -->",
            payload_hash="a" * 64,
        )
        is None
    )


@pytest.mark.parametrize("status", ["PUBLISHED", "PARTIAL"])
async def test_reconciliation_preserves_confirmed_remote_effect(
    publication_engine: AsyncEngine, payload_data: dict[str, Any], status: str
) -> None:
    review_id, _ = await seed_review(publication_engine, payload_data)
    async with publication_engine.begin() as connection:
        await connection.execute(
            insert(TaskLease).values(
                **lease_values(
                    id=uuid.uuid4(),
                    review_job_id=review_id,
                    owner="publisher-a",
                    fencing_token=1,
                    expires_at=datetime.now(UTC) + timedelta(seconds=90),
                )
            )
        )

    store = PostgresPublicationStore(
        async_sessionmaker(publication_engine, expire_on_commit=False)
    )
    claim = await store.begin(
        review_id,
        authority=PublicationAuthority(review_id, "publisher-a", 1),
        marker="<!-- review:one -->",
        payload_hash="a" * 64,
    )
    assert claim is not None
    await store.save(claim, status="UNKNOWN", provider_metadata=None)
    async with publication_engine.begin() as connection:
        await connection.execute(
            update(TaskLease)
            .where(TaskLease.review_job_id == review_id)
            .values(owner="reconciler", fencing_token=2)
        )
    await store.reconcile(
        review_id,
        authority=PublicationAuthority(review_id, "reconciler", 2),
        status=status,
        confirmed_absent=False,
        provider_metadata={"review_id": "123"},
    )
    state = await store.get(review_id)
    assert state is not None
    assert state.status == status
    assert state.provider_metadata == {"review_id": "123"}


async def test_resolved_publication_allows_a_different_review_of_same_pr(
    publication_engine: AsyncEngine, payload_data: dict[str, Any]
) -> None:
    first, change_id = await seed_review(publication_engine, payload_data)
    second, _ = await seed_review(publication_engine, payload_data, change_id=change_id)
    async with publication_engine.begin() as connection:
        for review_id in (first, second):
            await connection.execute(
                insert(TaskLease).values(
                    **lease_values(
                        id=uuid.uuid4(),
                        review_job_id=review_id,
                        owner=str(review_id),
                        fencing_token=1,
                        expires_at=datetime.now(UTC) + timedelta(seconds=90),
                    )
                )
            )

    store = PostgresPublicationStore(
        async_sessionmaker(publication_engine, expire_on_commit=False)
    )
    original = await store.begin(
        first,
        authority=PublicationAuthority(first, str(first), 1),
        marker="<!-- first -->",
        payload_hash="a" * 64,
    )
    assert original is not None
    await store.save(
        original, status="PUBLISHED", provider_metadata={"review_id": "123"}
    )
    following = await store.begin(
        second,
        authority=PublicationAuthority(second, str(second), 1),
        marker="<!-- second -->",
        payload_hash="b" * 64,
    )
    assert following is not None
    assert following.operation_id != original.operation_id
    assert await store.check_authority(original) is False
    state = await store.get(first)
    assert state is not None
    assert state.status == "PUBLISHED"


async def test_publisher_refuses_context_from_different_frozen_snapshot(
    publication_engine: AsyncEngine, payload_data: dict[str, Any]
) -> None:
    review_id, _ = await seed_review(publication_engine, payload_data)
    bad_context = deepcopy(payload_data)
    bad_context["snapshot"]["head_sha"] = "e" * 40
    async with publication_engine.begin() as connection:
        await connection.execute(
            update(ContextPayload)
            .where(ContextPayload.review_job_id == review_id)
            .values(payload_body=bad_context)
        )

    store = PostgresPublicationStore(
        async_sessionmaker(publication_engine, expire_on_commit=False)
    )
    assert await store.load(review_id) is None


async def test_reconciliation_reads_original_operation_identity_after_restart(
    publication_engine: AsyncEngine, payload_data: dict[str, Any]
) -> None:
    review_id, _ = await seed_review(publication_engine, payload_data)
    async with publication_engine.begin() as connection:
        await connection.execute(
            insert(TaskLease).values(
                **lease_values(
                    id=uuid.uuid4(),
                    review_job_id=review_id,
                    owner="publisher-a",
                    fencing_token=1,
                    expires_at=datetime.now(UTC) + timedelta(seconds=90),
                )
            )
        )

    sessions = async_sessionmaker(publication_engine, expire_on_commit=False)
    claim = await PostgresPublicationStore(sessions).begin(
        review_id,
        authority=PublicationAuthority(review_id, "publisher-a", 1),
        marker="<!-- original:one -->",
        payload_hash="c" * 64,
    )
    assert claim is not None
    operation = await PostgresPublicationStore(sessions).get_operation(review_id)
    assert operation is not None
    assert operation.operation_id == claim.operation_id
    assert operation.review_id == review_id
    assert operation.marker == "<!-- original:one -->"
    assert operation.payload_hash == "c" * 64
    assert operation.state == "IN_FLIGHT"


async def test_abandoned_inflight_operation_can_reconcile_confirmed_remote_review(
    publication_engine: AsyncEngine, payload_data: dict[str, Any]
) -> None:
    review_id, _ = await seed_review(publication_engine, payload_data)
    async with publication_engine.begin() as connection:
        await connection.execute(
            insert(TaskLease).values(
                **lease_values(
                    id=uuid.uuid4(),
                    review_job_id=review_id,
                    owner="publisher-a",
                    fencing_token=1,
                    expires_at=datetime.now(UTC) + timedelta(seconds=90),
                )
            )
        )

    sessions = async_sessionmaker(publication_engine, expire_on_commit=False)
    claim = await PostgresPublicationStore(sessions).begin(
        review_id,
        authority=PublicationAuthority(review_id, "publisher-a", 1),
        marker="<!-- original:one -->",
        payload_hash="c" * 64,
    )
    assert claim is not None
    async with publication_engine.begin() as connection:
        await connection.execute(
            update(TaskLease)
            .where(TaskLease.review_job_id == review_id)
            .values(owner="reconciler", fencing_token=2)
        )
    store = PostgresPublicationStore(sessions)
    await store.reconcile(
        review_id,
        authority=PublicationAuthority(review_id, "reconciler", 2),
        status="PUBLISHED",
        confirmed_absent=False,
        provider_metadata={"review_id": "123"},
    )
    state = await store.get(review_id)
    operation = await store.get_operation(review_id)
    assert state is not None and operation is not None
    assert state.status == "PUBLISHED"
    assert operation.state == "RESOLVED"


async def test_call_start_check_refuses_mislabelled_authority(
    publication_engine: AsyncEngine, payload_data: dict[str, Any]
) -> None:
    review_id, _ = await seed_review(publication_engine, payload_data)
    async with publication_engine.begin() as connection:
        await connection.execute(
            insert(TaskLease).values(
                **lease_values(
                    id=uuid.uuid4(),
                    review_job_id=review_id,
                    owner="publisher-a",
                    fencing_token=1,
                    expires_at=datetime.now(UTC) + timedelta(seconds=90),
                )
            )
        )
    store = PostgresPublicationStore(
        async_sessionmaker(publication_engine, expire_on_commit=False)
    )
    claim = await store.begin(
        review_id,
        authority=PublicationAuthority(review_id, "publisher-a", 1),
        marker="<!-- review:one -->",
        payload_hash="a" * 64,
    )
    assert claim is not None
    mislabelled = replace(
        claim, authority=replace(claim.authority, task_kind="analyze")
    )
    assert await store.check_authority(mislabelled) is False


def test_alembic_registers_publication_metadata_in_a_fresh_process() -> None:
    root = Path(__file__).parents[1]
    code = """
from alembic.config import Config
from alembic.environment import EnvironmentContext
from alembic.script import ScriptDirectory

config = Config('alembic.ini')
scripts = ScriptDirectory.from_config(config)

def inspect_metadata(revision, context):
    assert 'publication_operation' in context.opts['target_metadata'].tables, \\
        'PublicationOperation missing from Alembic target_metadata'
    return iter(())

with EnvironmentContext(config, scripts, fn=inspect_metadata, as_sql=True):
    scripts.run_env()
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=root,
        env=dict(os.environ) | {"PYTHONPATH": str(root / "src")},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "status,stored_kind,token,seconds,allowed",
    [
        ("UNKNOWN", "publish", 2, 90, True),
        ("PENDING", "publish", 2, 90, True),
        ("UNKNOWN", "analyze", 2, 90, False),
        ("UNKNOWN", "publish", 1, 90, False),
        ("UNKNOWN", "publish", 2, -1, False),
    ],
)
async def test_reconciliation_reads_require_fresh_live_publish_authority(  # noqa: PLR0913 - explicit authority case matrix
    *,
    publication_engine: AsyncEngine,
    payload_data: dict[str, Any],
    status: str,
    stored_kind: str,
    token: int,
    seconds: int,
    allowed: bool,
) -> None:
    review_id, _ = await seed_review(publication_engine, payload_data)
    async with publication_engine.begin() as connection:
        await connection.execute(
            insert(TaskLease).values(
                **lease_values(
                    id=uuid.uuid4(),
                    review_job_id=review_id,
                    owner="publisher-a",
                    fencing_token=1,
                    expires_at=datetime.now(UTC) + timedelta(seconds=90),
                )
            )
        )
    store = PostgresPublicationStore(
        async_sessionmaker(publication_engine, expire_on_commit=False)
    )
    claim = await store.begin(
        review_id,
        authority=PublicationAuthority(review_id, "publisher-a", 1),
        marker="<!-- review:one -->",
        payload_hash="a" * 64,
    )
    assert claim is not None
    if status == "UNKNOWN":
        await store.save(claim, status="UNKNOWN", provider_metadata=None)
    async with publication_engine.begin() as connection:
        await connection.execute(
            update(TaskLease)
            .where(
                TaskLease.review_job_id == review_id,
            )
            .values(
                owner="reconciler",
                fencing_token=2,
                expires_at=datetime.now(UTC) + timedelta(seconds=seconds),
            )
        )
        await connection.execute(
            text("ALTER TABLE task_lease ADD COLUMN IF NOT EXISTS task_kind text")
        )
        await connection.execute(
            text("UPDATE task_lease SET task_kind = :kind"), {"kind": stored_kind}
        )
        await connection.execute(
            update(Publication)
            .where(
                Publication.review_job_id == review_id,
            )
            .values(deadline_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    authority = PublicationAuthority(review_id, "reconciler", token)
    assert await store.check_reconciliation_authority(review_id, authority) is allowed
