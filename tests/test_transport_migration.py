"""Additive transport receipts preserve historical authority through migration."""

import asyncio
import json
import uuid
from dataclasses import replace

import asyncpg  # type: ignore[import-untyped]
import pytest
from sqlalchemy.engine import make_url
from test_migration_upgrade import MigrationDatabase, _seed_legacy

from app.application.use_cases.start_review import compute_config_digest
from app.domain.frozen_configuration import admission_policy

pytest_plugins = ("test_migration_upgrade",)


async def test_transport_receipts_upgrade_legacy_and_roundtrip(
    migration_database: MigrationDatabase,
) -> None:
    db = migration_database
    command, job_id = await _seed_legacy(db)
    db.migrate("upgrade", "0010")
    policy = admission_policy("0" * 64).model_dump(mode="json")
    frozen = replace(command, model_settings=policy, frozen_configuration_version=1)
    await db.connection.execute(
        "UPDATE review_job SET frozen_configuration_version=1,model_settings=$2,"
        "config_digest=$3,started_at=now()-interval '2 minutes',"
        "analysis_deadline_at=now()+interval '3 minutes',analysis_attempts=2,"
        "analysis_attempt_fence=17,retry_at=now()+interval '1 minute',"
        "format_repair_id=$4,format_repair_claimed_at=now(),format_repair_fence=17,"
        "format_repair_chunk_id=$5 WHERE id=$1",
        job_id,
        json.dumps(policy),
        compute_config_digest(frozen),
        uuid.uuid4(),
        uuid.uuid4(),
    )
    ids = [uuid.uuid4() for _ in range(3)]
    for index, event_id in enumerate(ids):
        await db.connection.execute(
            "INSERT INTO outbox_event "
            "(id,review_job_id,schema_version,task_kind,attempt,trace_id,created_at,"
            "broker_published_at,quarantined_at,quarantine_reason_code) "
            "VALUES ($1,$2,1,'analyze',1,'legacy',now(),"
            "CASE WHEN $3=1 THEN now() END,CASE WHEN $3=2 THEN now() END,"
            "CASE WHEN $3=2 THEN 'INVALID_ENVELOPE' END)",
            event_id,
            job_id,
            index,
        )
    await db.connection.execute(
        "INSERT INTO task_lease (id,review_job_id,task_kind,owner,expires_at,"
        "fencing_token) VALUES ($1,$2,'analyze','old',now(),17)",
        uuid.uuid4(),
        job_id,
    )
    before = await db.connection.fetch("SELECT * FROM outbox_event ORDER BY id")
    job = await db.connection.fetchrow("SELECT * FROM review_job WHERE id=$1", job_id)
    lease = await db.connection.fetchrow("SELECT * FROM task_lease")
    db.migrate("upgrade", "head")
    metadata = await db.connection.fetch(
        "SELECT broker_message_id,dispatch_generation,dispatch_not_before,"
        "recovery_window_started_at,recovery_send_count FROM outbox_event"
    )
    assert [tuple(row) for row in metadata] == [(None, 0, None, None, 0)] * 3
    for assignment in (
        "dispatch_generation=-1",
        "recovery_send_count=-1",
        "recovery_send_count=1",
        "recovery_window_started_at=now()",
    ):
        with pytest.raises(asyncpg.CheckViolationError):
            await db.connection.execute(
                f"UPDATE outbox_event SET {assignment} WHERE id=$1", ids[0]
            )
    # A reserved generation without receipt/marker is a valid crash state.
    await db.connection.execute(
        "UPDATE outbox_event SET dispatch_generation=1,"
        "dispatch_not_before=now()+interval '60 seconds' WHERE id=$1",
        ids[0],
    )
    db.migrate("check")
    db.migrate("downgrade", "0010")
    assert await db.connection.fetch("SELECT * FROM outbox_event ORDER BY id") == before
    assert (
        await db.connection.fetchrow("SELECT * FROM review_job WHERE id=$1", job_id)
        == job
    )
    assert await db.connection.fetchrow("SELECT * FROM task_lease") == lease
    db.migrate("upgrade", "head")
    db.migrate("check")


async def _seed_transport_event(db: MigrationDatabase) -> tuple[uuid.UUID, uuid.UUID]:
    _, job_id = await _seed_legacy(db)
    db.migrate("upgrade", "head")
    event_id = uuid.uuid4()
    await db.connection.execute(
        "INSERT INTO outbox_event "
        "(id,review_job_id,schema_version,task_kind,attempt,trace_id,created_at) "
        "VALUES ($1,$2,1,'analyze',1,'transport-migration',clock_timestamp())",
        event_id,
        job_id,
    )
    return job_id, event_id


async def test_transport_check_failure_rolls_back_entire_reservation(
    migration_database: MigrationDatabase,
) -> None:
    db = migration_database
    job_id, event_id = await _seed_transport_event(db)
    before = await db.connection.fetchrow(
        "SELECT * FROM outbox_event WHERE id=$1", event_id
    )
    job_before = await db.connection.fetchrow(
        "SELECT * FROM review_job WHERE id=$1", job_id
    )
    with pytest.raises(asyncpg.CheckViolationError):
        async with db.connection.transaction():
            await db.connection.execute(
                "UPDATE outbox_event SET dispatch_generation=1,"
                "dispatch_not_before=clock_timestamp()+interval '60 seconds',"
                "recovery_window_started_at=clock_timestamp(),"
                "recovery_send_count=1 WHERE id=$1",
                event_id,
            )
            await db.connection.execute(
                "UPDATE outbox_event SET recovery_send_count=-1 WHERE id=$1",
                event_id,
            )
    assert (
        await db.connection.fetchrow("SELECT * FROM outbox_event WHERE id=$1", event_id)
        == before
    )
    assert (
        await db.connection.fetchrow("SELECT * FROM review_job WHERE id=$1", job_id)
        == job_before
    )


async def test_transport_metadata_cas_rechecks_after_concurrent_commit(
    migration_database: MigrationDatabase,
) -> None:
    db = migration_database
    _, event_id = await _seed_transport_event(db)
    contender = await asyncpg.connect(
        make_url(db.url)
        .set(drivername="postgresql")
        .render_as_string(hide_password=False)
    )
    entered = asyncio.Event()

    async def stale_finalize() -> uuid.UUID | None:
        async with contender.transaction():
            entered.set()
            result = await contender.fetchval(
                "UPDATE outbox_event SET broker_message_id='old-0',"
                "broker_published_at=clock_timestamp() "
                "WHERE id=$1 AND dispatch_generation=0 RETURNING id",
                event_id,
            )
            assert result is None or isinstance(result, uuid.UUID)
            return result

    pending = None
    try:
        async with db.connection.transaction():
            await db.connection.execute(
                "UPDATE outbox_event SET dispatch_generation=1,"
                "broker_message_id='new-0',broker_published_at=clock_timestamp() "
                "WHERE id=$1",
                event_id,
            )
            pending = asyncio.create_task(stale_finalize())
            await entered.wait()
            # PostgreSQL confirms actual contention before the winning commit.
            async with asyncio.timeout(5):
                while not await db.connection.fetchval(
                    "SELECT cardinality(pg_blocking_pids($1)) > 0",
                    contender.get_server_pid(),
                ):
                    pass
            assert not pending.done()
        assert await pending is None
        assert tuple(
            await db.connection.fetchrow(
                "SELECT dispatch_generation,broker_message_id,"
                "broker_published_at IS NOT NULL FROM outbox_event WHERE id=$1",
                event_id,
            )
        ) == (1, "new-0", True)
    finally:
        if pending is not None:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await contender.close()


async def test_transport_migration_lock_timeout_preserves_previous_revision(
    migration_database: MigrationDatabase,
) -> None:
    db = migration_database
    _, job_id = await _seed_legacy(db)
    db.migrate("upgrade", "0010")
    job_before = await db.connection.fetchrow(
        "SELECT * FROM review_job WHERE id=$1", job_id
    )
    name = make_url(db.url).database
    # Only this fixture's uniquely named disposable database is configured.
    await db.connection.execute(f'ALTER DATABASE "{name}" SET lock_timeout="100ms"')
    try:
        async with db.connection.transaction():
            await db.connection.execute("LOCK TABLE outbox_event IN ACCESS SHARE MODE")
            output = await asyncio.to_thread(
                db.migrate, "upgrade", "head", success=False
            )
            assert "lock timeout" in output
    finally:
        await db.connection.execute(f'ALTER DATABASE "{name}" RESET lock_timeout')
    assert (
        await db.connection.fetchval("SELECT version_num FROM alembic_version")
        == "0010"
    )
    assert (
        await db.connection.fetchval(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name='outbox_event' AND column_name IN "
            "('broker_message_id','dispatch_generation','dispatch_not_before',"
            "'recovery_window_started_at','recovery_send_count')"
        )
        == 0
    )
    assert (
        await db.connection.fetchval(
            "SELECT count(*) FROM pg_constraint WHERE conname IN "
            "('ck_outbox_dispatch_generation','ck_outbox_recovery_count',"
            "'ck_outbox_recovery_window')"
        )
        == 0
    )
    assert (
        await db.connection.fetchval("SELECT to_regclass('ix_outbox_analyze_cursor')")
        is None
    )
    assert (
        await db.connection.fetchrow("SELECT * FROM review_job WHERE id=$1", job_id)
        == job_before
    )
    db.migrate("upgrade", "head")
    db.migrate("check")
