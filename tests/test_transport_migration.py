"""Forward transport simplification preserves receipts and business authority."""

import asyncio
import json
import uuid
from dataclasses import replace

from sqlalchemy.engine import make_url
from test_migration_upgrade import MigrationDatabase, _seed_legacy

from app.application.use_cases.start_review import compute_config_digest
from app.domain.frozen_configuration import admission_policy

pytest_plugins = ("test_migration_upgrade",)

_REMOVED = (
    "dispatch_generation",
    "recovery_window_started_at",
    "recovery_send_count",
)


async def test_lean_transport_upgrade_preserves_historical_data_and_roundtrip(
    migration_database: MigrationDatabase,
) -> None:
    db = migration_database
    command, job_id = await _seed_legacy(db)
    db.migrate("upgrade", "0011")
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
    for index in range(3):
        await db.connection.execute(
            "INSERT INTO outbox_event "
            "(id,review_job_id,schema_version,task_kind,attempt,trace_id,created_at,"
            "broker_published_at,broker_message_id,dispatch_not_before,"
            "dispatch_generation,recovery_window_started_at,recovery_send_count,"
            "quarantined_at,quarantine_reason_code) "
            "VALUES ($1,$2,1,'analyze',1,'legacy',now(),"
            "CASE WHEN $3=1 THEN now() END,CASE WHEN $3>0 THEN '123456789-0' END,"
            "now()+interval '90 seconds',7,now(),4,"
            "CASE WHEN $3=2 THEN now() END,"
            "CASE WHEN $3=2 THEN 'INVALID_ENVELOPE' END)",
            uuid.uuid4(),
            job_id,
            index,
        )
    await db.connection.execute(
        "INSERT INTO task_lease (id,review_job_id,task_kind,owner,expires_at,"
        "fencing_token) VALUES ($1,$2,'analyze','old',now(),17)",
        uuid.uuid4(),
        job_id,
    )
    await db.connection.execute(
        "INSERT INTO chunk_result "
        "(id,review_job_id,schema_version,summary,limitations,usage,"
        "latency_ms,created_at) "
        "VALUES ($1,$2,1,'retained result','[]','{\"input_tokens\":12}',3,now())",
        uuid.uuid4(),
        job_id,
    )
    await db.connection.execute(
        "INSERT INTO review_llm_attempt "
        "(id,review_job_id,chunk_id,operation_id,call_kind,analysis_attempt,"
        "lease_owner,fencing_token,provider_profile_ref,model_ref,"
        "reserved_input_tokens,reserved_output_tokens,state,actual_input_tokens,"
        "actual_output_tokens,reserved_at,settled_at,latency_ms) "
        "VALUES ($1,$2,$3,$4,'initial',2,'old',17,'profile','model',20,10,"
        "'SUCCEEDED',12,4,now()-interval '1 minute',now(),3)",
        uuid.uuid4(),
        job_id,
        uuid.uuid4(),
        uuid.uuid4(),
    )
    tables = ("review_job", "task_lease", "chunk_result", "review_llm_attempt")
    before = {
        table: await db.connection.fetch(f"SELECT * FROM {table} ORDER BY id")
        for table in tables
    }
    events = [
        {key: value for key, value in row.items() if key not in _REMOVED}
        for row in await db.connection.fetch("SELECT * FROM outbox_event ORDER BY id")
    ]

    db.migrate("upgrade", "head")
    assert (
        await db.connection.fetchval(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name='outbox_event' AND column_name=ANY($1::text[])",
            list(_REMOVED),
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
    assert [
        dict(row)
        for row in await db.connection.fetch("SELECT * FROM outbox_event ORDER BY id")
    ] == events
    for table in tables:
        assert (
            await db.connection.fetch(f"SELECT * FROM {table} ORDER BY id")
            == before[table]
        )
    db.migrate("downgrade", "0011")
    assert [
        tuple(row)
        for row in await db.connection.fetch(
            "SELECT dispatch_generation,recovery_window_started_at,recovery_send_count "
            "FROM outbox_event"
        )
    ] == [(0, None, 0)] * 3
    db.migrate("upgrade", "head")
    assert [
        dict(row)
        for row in await db.connection.fetch("SELECT * FROM outbox_event ORDER BY id")
    ] == events
    for table in tables:
        assert (
            await db.connection.fetch(f"SELECT * FROM {table} ORDER BY id")
            == before[table]
        )


async def test_lean_transport_lock_timeout_preserves_previous_revision(
    migration_database: MigrationDatabase,
) -> None:
    db = migration_database
    _, job_id = await _seed_legacy(db)
    db.migrate("upgrade", "0011")
    before = await db.connection.fetchrow(
        "SELECT * FROM review_job WHERE id=$1", job_id
    )
    name = make_url(db.url).database
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
        == "0011"
    )
    assert await db.connection.fetchval(
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_name='outbox_event' AND column_name=ANY($1::text[])",
        list(_REMOVED),
    ) == len(_REMOVED)
    assert await db.connection.fetchval(
        "SELECT count(*) FROM pg_constraint WHERE conname IN "
        "('ck_outbox_dispatch_generation','ck_outbox_recovery_count',"
        "'ck_outbox_recovery_window')"
    ) == len(_REMOVED)
    assert (
        await db.connection.fetchrow("SELECT * FROM review_job WHERE id=$1", job_id)
        == before
    )
    db.migrate("upgrade", "head")
