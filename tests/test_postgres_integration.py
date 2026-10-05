from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.db import models
from app.db.base import Base

_POSTGRES_URL_ENV = os.environ.get("DMC268_TEST_DATABASE_URL")
if not _POSTGRES_URL_ENV:
    pytest.skip(
        "set DMC268_TEST_DATABASE_URL to run PostgreSQL integration tests",
        allow_module_level=True,
    )
_POSTGRES_URL = os.environ["DMC268_TEST_DATABASE_URL"]


@dataclass
class PostgresHarness:
    engine: AsyncEngine
    ids: dict[str, set[uuid.UUID]] = field(default_factory=dict)

    def track(self, table_name: str, row_id: uuid.UUID) -> uuid.UUID:
        self.ids.setdefault(table_name, set()).add(row_id)
        return row_id

    async def cleanup(self) -> None:
        async with self.engine.begin() as connection:
            repository_ids = self.ids.get("repository", set())
            if repository_ids:
                repository = Base.metadata.tables["repository"]
                await connection.execute(
                    update(repository)
                    .where(repository.c.id.in_(repository_ids))
                    .values(current_settings_id=None)
                )

            for table_name in (
                "idempotency_record",
                "outbox_event",
                "publication",
                "finding",
                "chunk_result",
                "context_payload",
                "review_job",
                "change_request",
                "repository_settings",
                "repository",
            ):
                row_ids = self.ids.get(table_name, set())
                if row_ids:
                    table = Base.metadata.tables[table_name]
                    await connection.execute(
                        delete(table).where(table.c.id.in_(row_ids))
                    )


@pytest_asyncio.fixture
async def postgres() -> AsyncIterator[PostgresHarness]:
    engine = create_async_engine(_POSTGRES_URL, poolclass=NullPool)
    harness = PostgresHarness(engine)
    async with engine.connect() as connection:
        assert (await connection.execute(text("SELECT 1"))).scalar_one() == 1

    try:
        yield harness
    finally:
        await harness.cleanup()
        await engine.dispose()


async def _create_repository(
    postgres: PostgresHarness,
    connection: AsyncConnection,
    *,
    provider_name: str = "github",
    external_id: str | None = None,
) -> uuid.UUID:
    repository_id = postgres.track("repository", uuid.uuid4())
    external_id = external_id or f"repo-{repository_id.hex}"
    await connection.execute(
        insert(models.Repository).values(
            id=repository_id,
            external_id=external_id,
            provider_name=provider_name,
            repo_name=f"repository-{repository_id.hex}",
            owner="postgres-integration-test",
            current_settings_id=None,
        )
    )
    return repository_id


async def _create_settings(
    postgres: PostgresHarness,
    connection: AsyncConnection,
    repository_id: uuid.UUID,
    *,
    rules_digest: str | None = None,
) -> uuid.UUID:
    settings_id = postgres.track("repository_settings", uuid.uuid4())
    await connection.execute(
        insert(models.RepositorySettings).values(
            id=settings_id,
            repository_id=repository_id,
            rules={},
            ignores={},
            max_starts_per_hour=10,
            max_active_jobs=4,
            output_language="en",
            surrounding_lines=5,
            created_at=datetime.now(UTC),
            rules_digest=rules_digest or f"rules-{settings_id.hex}",
        )
    )
    return settings_id


async def _create_change_request(
    postgres: PostgresHarness,
    connection: AsyncConnection,
    repository_id: uuid.UUID,
) -> uuid.UUID:
    change_request_id = postgres.track("change_request", uuid.uuid4())
    await connection.execute(
        insert(models.ChangeRequest).values(
            id=change_request_id,
            repository_id=repository_id,
            external_number=change_request_id.hex,
            source_repository_external_id=None,
            source_ref="feature/postgres-validation",
            base_ref="main",
            head_sha="head-sha",
            base_sha="base-sha",
            title="PostgreSQL validation",
            description="Integration-test ChangeRequest",
            current_status="open",
        )
    )
    return change_request_id


@dataclass(frozen=True)
class ReviewScope:
    repository_id: uuid.UUID
    change_request_id: uuid.UUID
    settings_id: uuid.UUID


def _review_values(
    review_job_id: uuid.UUID,
    scope: ReviewScope,
    *,
    requested_head_sha: str = "head-sha",
    config_digest: str = "config-digest",
) -> dict[str, object]:
    now = datetime.now(UTC)
    return {
        "id": review_job_id,
        "repository_id": scope.repository_id,
        "change_request_id": scope.change_request_id,
        "repository_settings_id": scope.settings_id,
        "config_digest": config_digest,
        "rules_digest": "rules-digest",
        "trigger_type": "manual",
        "initiator_user_id": None,
        "requested_head_sha": requested_head_sha,
        "model_ref": "model:postgres-test",
        "model_digest": None,
        "model_settings": {"temperature": 0},
        "prompt_version": "prompt-v1",
        "prompt_digest": "prompt-digest",
        "created_at": now,
        "finished_at": None,
        "status": "QUEUED",
        "stage": None,
        "queue_deadline_at": now + timedelta(minutes=15),
        "coverage": {},
    }


async def _create_review_job(
    postgres: PostgresHarness,
    connection: AsyncConnection,
    scope: ReviewScope,
    *,
    requested_head_sha: str = "head-sha",
    config_digest: str = "config-digest",
) -> uuid.UUID:
    review_job_id = postgres.track("review_job", uuid.uuid4())
    await connection.execute(
        insert(models.ReviewJob).values(
            **_review_values(
                review_job_id,
                scope,
                requested_head_sha=requested_head_sha,
                config_digest=config_digest,
            )
        )
    )
    return review_job_id


async def _create_review_fixture(
    postgres: PostgresHarness,
    connection: AsyncConnection,
) -> tuple[ReviewScope, uuid.UUID]:
    repository_id = await _create_repository(postgres, connection)
    settings_id = await _create_settings(postgres, connection, repository_id)
    change_request_id = await _create_change_request(
        postgres, connection, repository_id
    )
    review_job_id = await _create_review_job(
        postgres,
        connection,
        ReviewScope(repository_id, change_request_id, settings_id),
    )
    return ReviewScope(repository_id, change_request_id, settings_id), review_job_id


async def test_repository_settings_isolation_and_frozen_history(
    postgres: PostgresHarness,
) -> None:
    async with postgres.engine.begin() as connection:
        repository_a = await _create_repository(postgres, connection)
        repository_b = await _create_repository(postgres, connection)

        current_settings = await connection.scalar(
            select(models.Repository.current_settings_id).where(
                models.Repository.id == repository_a
            )
        )
        assert current_settings is None

        settings_a_v1 = await _create_settings(postgres, connection, repository_a)
        settings_a_v2 = await _create_settings(postgres, connection, repository_a)
        settings_b = await _create_settings(postgres, connection, repository_b)

    session_factory = async_sessionmaker(postgres.engine, expire_on_commit=False)
    async with session_factory() as session:
        repository = await session.get(models.Repository, repository_a)
        settings = await session.get(models.RepositorySettings, settings_a_v1)
        assert repository is not None
        assert settings is not None
        repository.current_settings = settings
        await session.commit()
        assert repository.id == repository_a
        assert repository.current_settings_id == settings_a_v1

    async with postgres.engine.begin() as connection:
        cross_repository_update = (
            update(models.Repository)
            .where(models.Repository.id == repository_a)
            .values(current_settings_id=settings_b)
        )
        with pytest.raises(IntegrityError) as cross_settings_error:
            async with connection.begin_nested():
                await connection.execute(cross_repository_update)
        assert "fk_repository_current_settings_same_repository" in str(
            cross_settings_error.value.orig
        )

        change_request_a = await _create_change_request(
            postgres, connection, repository_a
        )
        review_scope_a = ReviewScope(repository_a, change_request_a, settings_a_v1)
        review_job_id = await _create_review_job(
            postgres,
            connection,
            review_scope_a,
        )

        rejected_review_id = postgres.track("review_job", uuid.uuid4())
        with pytest.raises(IntegrityError) as cross_review_error:
            async with connection.begin_nested():
                await connection.execute(
                    insert(models.ReviewJob).values(
                        **_review_values(
                            rejected_review_id,
                            ReviewScope(repository_a, change_request_a, settings_b),
                            config_digest="cross-repository-config",
                        )
                    )
                )
        assert "fk_review_job_settings_same_repository" in str(
            cross_review_error.value.orig
        )

        await connection.execute(
            update(models.Repository)
            .where(models.Repository.id == repository_a)
            .values(current_settings_id=settings_a_v2)
        )
        frozen_settings = await connection.scalar(
            select(models.ReviewJob.repository_settings_id).where(
                models.ReviewJob.id == review_job_id
            )
        )
        assert frozen_settings == settings_a_v1


async def test_postgres_catalog_contains_exact_constraints_and_index(
    postgres: PostgresHarness,
) -> None:
    expected_constraints = {
        "uq_repository_provider_external_id": "u",
        "uq_repository_settings_repository_id_id": "u",
        "fk_repository_current_settings_same_repository": "f",
        "uq_change_request_repository_id_id": "u",
        "fk_review_job_change_request_same_repository": "f",
        "fk_review_job_settings_same_repository": "f",
        "ck_review_job_status_finished_at": "c",
        "uq_context_payload_review_job_id_id": "u",
        "fk_chunk_result_context_payload_same_review": "f",
        "uq_chunk_result_context_payload": "u",
    }
    async with postgres.engine.connect() as connection:
        assert (
            await connection.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one() == "0003"

        result = await connection.execute(
            text(
                """
                SELECT
                    constraint_row.conname,
                    CAST(constraint_row.contype AS text) AS constraint_type,
                    constraint_row.condeferrable,
                    constraint_row.condeferred,
                    CAST(constraint_row.confdeltype AS text) AS delete_action,
                    pg_get_constraintdef(constraint_row.oid) AS definition
                FROM pg_constraint AS constraint_row
                JOIN pg_class AS table_row
                  ON table_row.oid = constraint_row.conrelid
                JOIN pg_namespace AS namespace_row
                  ON namespace_row.oid = table_row.relnamespace
                WHERE namespace_row.nspname = current_schema()
                  AND constraint_row.conname = ANY(:constraint_names)
                """
            ),
            {"constraint_names": list(expected_constraints)},
        )
        constraints = {row[0]: row for row in result.all()}

        assert set(constraints) == set(expected_constraints)
        assert {
            name: row[1] for name, row in constraints.items()
        } == expected_constraints
        assert constraints["uq_repository_settings_repository_id_id"][5] == (
            "UNIQUE (repository_id, id)"
        )
        assert constraints["uq_change_request_repository_id_id"][5] == (
            "UNIQUE (repository_id, id)"
        )
        assert constraints["fk_repository_current_settings_same_repository"][5] == (
            "FOREIGN KEY (id, current_settings_id) "
            "REFERENCES repository_settings(repository_id, id)"
        )
        assert constraints["fk_review_job_change_request_same_repository"][5] == (
            "FOREIGN KEY (repository_id, change_request_id) "
            "REFERENCES change_request(repository_id, id)"
        )
        assert constraints["fk_review_job_settings_same_repository"][5] == (
            "FOREIGN KEY (repository_id, repository_settings_id) "
            "REFERENCES repository_settings(repository_id, id)"
        )
        assert all(not row[2] and not row[3] for row in constraints.values())
        assert constraints["uq_context_payload_review_job_id_id"][5] == (
            "UNIQUE (review_job_id, id)"
        )
        assert constraints["fk_chunk_result_context_payload_same_review"][4] == "n"
        assert constraints["fk_chunk_result_context_payload_same_review"][5] == (
            "FOREIGN KEY (review_job_id, context_payload_id) "
            "REFERENCES context_payload(review_job_id, id) "
            "ON DELETE SET NULL (context_payload_id)"
        )

        status_check = constraints["ck_review_job_status_finished_at"][5]
        for status in (
            "QUEUED",
            "RUNNING",
            "COMPLETED",
            "PARTIAL",
            "FAILED",
            "SKIPPED",
        ):
            assert status in status_check
        assert "finished_at IS NULL" in status_check
        assert "finished_at IS NOT NULL" in status_check

        index_row = (
            await connection.execute(
                text(
                    """
                    SELECT
                        index_row.indisunique,
                        pg_get_indexdef(index_row.indexrelid) AS definition,
                        pg_get_expr(index_row.indpred, index_row.indrelid) AS predicate
                    FROM pg_index AS index_row
                    JOIN pg_class AS index_class
                      ON index_class.oid = index_row.indexrelid
                    JOIN pg_class AS table_class
                      ON table_class.oid = index_row.indrelid
                    JOIN pg_namespace AS namespace_row
                      ON namespace_row.oid = table_class.relnamespace
                    WHERE namespace_row.nspname = current_schema()
                      AND index_class.relname = 'uq_review_job_active_equivalent'
                    """
                )
            )
        ).one()
        assert index_row[0] is True
        assert "(change_request_id, requested_head_sha, config_digest)" in index_row[1]
        assert index_row[2] == "(finished_at IS NULL)"


async def _create_retained_result(
    postgres: PostgresHarness,
    connection: AsyncConnection,
    review_job_id: uuid.UUID,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    context_payload_id = postgres.track("context_payload", uuid.uuid4())
    chunk_result_id = postgres.track("chunk_result", uuid.uuid4())
    finding_id = postgres.track("finding", uuid.uuid4())
    now = datetime.now(UTC)
    await connection.execute(
        insert(models.ContextPayload).values(
            id=context_payload_id,
            review_job_id=review_job_id,
            schema_version=1,
            payload_body={"context": context_payload_id.hex},
            created_at=now,
        )
    )
    await connection.execute(
        insert(models.ChunkResult).values(
            id=chunk_result_id,
            review_job_id=review_job_id,
            context_payload_id=context_payload_id,
            schema_version=1,
            summary="Retained result",
            limitations=[],
            usage={"tokens": 10},
            latency_ms=5,
            created_at=now,
        )
    )
    await connection.execute(
        insert(models.Finding).values(
            id=finding_id,
            chunk_result_id=chunk_result_id,
            fingerprint=finding_id.hex,
            path="src/example.py",
            side="NEW",
            start_line=1,
            end_line=1,
            category="correctness",
            severity="medium",
            title="Retained finding",
            explanation="Finding remains after context cleanup",
            evidence="line 1",
            recommendation="Keep the durable result",
            created_at=now,
        )
    )
    return context_payload_id, chunk_result_id, finding_id


async def test_context_cleanup_retains_results_and_findings(
    postgres: PostgresHarness,
) -> None:
    async with postgres.engine.begin() as connection:
        _, review_job_id = await _create_review_fixture(postgres, connection)
        (
            deleted_context,
            retained_chunk,
            retained_finding,
        ) = await _create_retained_result(postgres, connection, review_job_id)
        other_context, other_chunk, other_finding = await _create_retained_result(
            postgres, connection, review_job_id
        )

    async with postgres.engine.begin() as connection:
        await connection.execute(
            delete(models.ContextPayload).where(
                models.ContextPayload.id == deleted_context
            )
        )

    async with postgres.engine.connect() as connection:
        assert (
            await connection.scalar(
                select(func.count())
                .select_from(models.ContextPayload)
                .where(models.ContextPayload.id == deleted_context)
            )
        ) == 0

        retained_row = (
            await connection.execute(
                select(
                    models.ChunkResult.context_payload_id,
                    models.ChunkResult.review_job_id,
                ).where(models.ChunkResult.id == retained_chunk)
            )
        ).one()
        assert retained_row == (None, review_job_id)

        retained_findings = set(
            (
                await connection.scalars(
                    select(models.Finding.id)
                    .join(
                        models.ChunkResult,
                        models.Finding.chunk_result_id == models.ChunkResult.id,
                    )
                    .where(models.ChunkResult.review_job_id == review_job_id)
                )
            ).all()
        )
        assert retained_findings == {retained_finding, other_finding}

        other_row = (
            await connection.execute(
                select(
                    models.ChunkResult.context_payload_id,
                    models.ChunkResult.review_job_id,
                ).where(models.ChunkResult.id == other_chunk)
            )
        ).one()
        assert other_row == (other_context, review_job_id)


@pytest.mark.parametrize("operation", ["insert", "result_owner", "context_owner"])
async def test_chunk_result_context_must_share_review_owner(
    postgres: PostgresHarness,
    operation: str,
) -> None:
    async with postgres.engine.begin() as connection:
        _, review_a = await _create_review_fixture(postgres, connection)
        _, review_b = await _create_review_fixture(postgres, connection)
        context_id, result_id, _ = await _create_retained_result(
            postgres, connection, review_a
        )

        with pytest.raises(IntegrityError) as ownership_error:
            async with connection.begin_nested():
                if operation == "insert":
                    other_context = postgres.track("context_payload", uuid.uuid4())
                    await connection.execute(
                        insert(models.ContextPayload).values(
                            id=other_context,
                            review_job_id=review_a,
                            schema_version=1,
                            payload_body={},
                        )
                    )
                    await connection.execute(
                        insert(models.ChunkResult).values(
                            id=postgres.track("chunk_result", uuid.uuid4()),
                            review_job_id=review_b,
                            context_payload_id=other_context,
                            schema_version=1,
                            summary="Wrong owner",
                            limitations={},
                            usage={},
                            latency_ms=0,
                        )
                    )
                elif operation == "result_owner":
                    await connection.execute(
                        update(models.ChunkResult)
                        .where(models.ChunkResult.id == result_id)
                        .values(review_job_id=review_b)
                    )
                else:
                    await connection.execute(
                        update(models.ContextPayload)
                        .where(models.ContextPayload.id == context_id)
                        .values(review_job_id=review_b)
                    )

        assert "fk_chunk_result_context_payload_same_review" in str(
            ownership_error.value
        )


@dataclass(frozen=True)
class AggregateIds:
    review_job_id: uuid.UUID
    publication_id: uuid.UUID
    outbox_event_id: uuid.UUID
    idempotency_record_id: uuid.UUID


def _new_aggregate_ids(postgres: PostgresHarness) -> AggregateIds:
    return AggregateIds(
        review_job_id=postgres.track("review_job", uuid.uuid4()),
        publication_id=postgres.track("publication", uuid.uuid4()),
        outbox_event_id=postgres.track("outbox_event", uuid.uuid4()),
        idempotency_record_id=postgres.track("idempotency_record", uuid.uuid4()),
    )


async def _insert_initial_aggregate(
    connection: AsyncConnection,
    ids: AggregateIds,
    scope: ReviewScope,
    *,
    idempotency_key: str,
) -> None:
    now = datetime.now(UTC)
    await connection.execute(
        insert(models.ReviewJob).values(
            **_review_values(
                ids.review_job_id,
                scope,
                requested_head_sha="concurrent-head",
                config_digest="concurrent-config",
            )
        )
    )
    await connection.execute(
        insert(models.Publication).values(
            id=ids.publication_id,
            review_job_id=ids.review_job_id,
            status="NOT_READY",
            provider_metadata=None,
            created_at=now,
        )
    )
    await connection.execute(
        insert(models.OutboxEvent).values(
            id=ids.outbox_event_id,
            review_job_id=ids.review_job_id,
            schema_version=1,
            task_kind="analyze",
            attempt=1,
            trace_id=f"trace-{ids.review_job_id.hex}",
            created_at=now,
            broker_published_at=None,
        )
    )
    await connection.execute(
        insert(models.IdempotencyRecord).values(
            id=ids.idempotency_record_id,
            scope="start-review",
            idempotency_key=idempotency_key,
            request_hash=ids.review_job_id.hex,
            review_job_id=ids.review_job_id,
            created_at=now,
            expires_at=now + timedelta(hours=1),
        )
    )


async def _wait_for_postgres_lock(engine: AsyncEngine, backend_pid: int) -> None:
    async with engine.connect() as observer:
        for _ in range(200):
            wait_event_type = await observer.scalar(
                text(
                    """
                    SELECT wait_event_type
                    FROM pg_stat_activity
                    WHERE pid = :backend_pid
                    """
                ),
                {"backend_pid": backend_pid},
            )
            if wait_event_type == "Lock":
                return
            await asyncio.sleep(0.01)
    raise AssertionError("second PostgreSQL session did not wait on the unique index")


async def test_concurrent_equivalent_start_keeps_one_atomic_aggregate(
    postgres: PostgresHarness,
) -> None:
    async with postgres.engine.begin() as setup_connection:
        repository_id = await _create_repository(postgres, setup_connection)
        settings_id = await _create_settings(postgres, setup_connection, repository_id)
        change_request_id = await _create_change_request(
            postgres, setup_connection, repository_id
        )
        review_scope = ReviewScope(repository_id, change_request_id, settings_id)

    winner_ids = _new_aggregate_ids(postgres)
    loser_ids = _new_aggregate_ids(postgres)
    loser_task: asyncio.Task[None] | None = None

    async with (
        postgres.engine.connect() as winner_connection,
        postgres.engine.connect() as loser_connection,
    ):
        winner_transaction = await winner_connection.begin()
        loser_transaction = await loser_connection.begin()
        try:
            winner_pid = int(
                (
                    await winner_connection.execute(text("SELECT pg_backend_pid()"))
                ).scalar_one()
            )
            loser_pid = int(
                (
                    await loser_connection.execute(text("SELECT pg_backend_pid()"))
                ).scalar_one()
            )
            assert winner_pid != loser_pid

            await _insert_initial_aggregate(
                winner_connection,
                winner_ids,
                review_scope,
                idempotency_key="concurrent-key-winner",
            )
            loser_task = asyncio.create_task(
                _insert_initial_aggregate(
                    loser_connection,
                    loser_ids,
                    review_scope,
                    idempotency_key="concurrent-key-loser",
                )
            )
            await _wait_for_postgres_lock(postgres.engine, loser_pid)
            await winner_transaction.commit()

            with pytest.raises(IntegrityError) as duplicate_error:
                await loser_task
            assert "uq_review_job_active_equivalent" in str(duplicate_error.value.orig)
            await loser_transaction.rollback()
        finally:
            if winner_transaction.is_active:
                await winner_transaction.rollback()
            if loser_task is not None and not loser_task.done():
                loser_task.cancel()
                with suppress(asyncio.CancelledError):
                    await loser_task
            if loser_transaction.is_active:
                await loser_transaction.rollback()

    async with postgres.engine.connect() as connection:
        active_count = await connection.scalar(
            select(func.count())
            .select_from(models.ReviewJob)
            .where(
                models.ReviewJob.change_request_id == change_request_id,
                models.ReviewJob.requested_head_sha == "concurrent-head",
                models.ReviewJob.config_digest == "concurrent-config",
                models.ReviewJob.finished_at.is_(None),
            )
        )
        assert active_count == 1

        for model, winner_id, loser_id in (
            (models.ReviewJob, winner_ids.review_job_id, loser_ids.review_job_id),
            (models.Publication, winner_ids.publication_id, loser_ids.publication_id),
            (models.OutboxEvent, winner_ids.outbox_event_id, loser_ids.outbox_event_id),
            (
                models.IdempotencyRecord,
                winner_ids.idempotency_record_id,
                loser_ids.idempotency_record_id,
            ),
        ):
            surviving_ids = set(
                (
                    await connection.scalars(
                        select(model.id).where(model.id.in_([winner_id, loser_id]))
                    )
                ).all()
            )
            assert surviving_ids == {winner_id}


async def test_active_run_variations_and_status_finished_check(
    postgres: PostgresHarness,
) -> None:
    async with postgres.engine.begin() as connection:
        repository_id = await _create_repository(postgres, connection)
        settings_id = await _create_settings(postgres, connection, repository_id)
        change_request_id = await _create_change_request(
            postgres, connection, repository_id
        )
        review_scope = ReviewScope(repository_id, change_request_id, settings_id)
        original_review = await _create_review_job(
            postgres,
            connection,
            review_scope,
            requested_head_sha="protected-head",
            config_digest="protected-config",
        )

        await connection.execute(
            update(models.ReviewJob)
            .where(models.ReviewJob.id == original_review)
            .values(status="RUNNING")
        )
        protected_duplicate = postgres.track("review_job", uuid.uuid4())
        with pytest.raises(IntegrityError) as active_duplicate_error:
            async with connection.begin_nested():
                await connection.execute(
                    insert(models.ReviewJob).values(
                        **_review_values(
                            protected_duplicate,
                            review_scope,
                            requested_head_sha="protected-head",
                            config_digest="protected-config",
                        )
                    )
                )
        assert "uq_review_job_active_equivalent" in str(
            active_duplicate_error.value.orig
        )

        different_head = await _create_review_job(
            postgres,
            connection,
            review_scope,
            requested_head_sha="different-head",
            config_digest="protected-config",
        )
        different_config = await _create_review_job(
            postgres,
            connection,
            review_scope,
            requested_head_sha="protected-head",
            config_digest="different-config",
        )
        assert different_head != different_config

        with pytest.raises(IntegrityError) as active_finished_error:
            async with connection.begin_nested():
                await connection.execute(
                    update(models.ReviewJob)
                    .where(models.ReviewJob.id == original_review)
                    .values(finished_at=datetime.now(UTC))
                )
        assert "ck_review_job_status_finished_at" in str(
            active_finished_error.value.orig
        )

        with pytest.raises(IntegrityError) as terminal_unfinished_error:
            async with connection.begin_nested():
                await connection.execute(
                    update(models.ReviewJob)
                    .where(models.ReviewJob.id == original_review)
                    .values(status="COMPLETED", finished_at=None)
                )
        assert "ck_review_job_status_finished_at" in str(
            terminal_unfinished_error.value.orig
        )

        await connection.execute(
            update(models.ReviewJob)
            .where(models.ReviewJob.id == original_review)
            .values(status="COMPLETED", finished_at=datetime.now(UTC))
        )
        rerun_id = await _create_review_job(
            postgres,
            connection,
            review_scope,
            requested_head_sha="protected-head",
            config_digest="protected-config",
        )

        equivalent_active_count = await connection.scalar(
            select(func.count())
            .select_from(models.ReviewJob)
            .where(
                models.ReviewJob.change_request_id == change_request_id,
                models.ReviewJob.requested_head_sha == "protected-head",
                models.ReviewJob.config_digest == "protected-config",
                models.ReviewJob.finished_at.is_(None),
            )
        )
        assert equivalent_active_count == 1
        assert rerun_id != original_review


async def test_repository_external_identity_is_provider_scoped(
    postgres: PostgresHarness,
) -> None:
    async with postgres.engine.begin() as connection:
        external_id = f"external-{uuid.uuid4().hex}"
        await _create_repository(
            postgres,
            connection,
            provider_name="github",
            external_id=external_id,
        )

        duplicate_id = postgres.track("repository", uuid.uuid4())
        with pytest.raises(IntegrityError) as duplicate_error:
            async with connection.begin_nested():
                await connection.execute(
                    insert(models.Repository).values(
                        id=duplicate_id,
                        external_id=external_id,
                        provider_name="github",
                        repo_name="duplicate",
                        owner="postgres-integration-test",
                        current_settings_id=None,
                    )
                )
        assert "uq_repository_provider_external_id" in str(duplicate_error.value.orig)

        same_external_other_provider = await _create_repository(
            postgres,
            connection,
            provider_name="gitlab",
            external_id=external_id,
        )
        different_external_same_provider = await _create_repository(
            postgres,
            connection,
            provider_name="github",
            external_id=f"{external_id}-other",
        )
        assert same_external_other_provider != different_external_same_provider
