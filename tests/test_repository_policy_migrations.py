"""Downgrade refuses to erase retained policy or a permanent receipt binding."""

import asyncio
import json
import sys
from pathlib import Path
from uuid import uuid4

from test_migration_upgrade import MigrationDatabase, _seed_legacy

pytest_plugins = ("test_migration_upgrade",)


async def test_downgrade_refuses_permanent_receipt_job_binding(
    migration_database: MigrationDatabase,
) -> None:
    database = migration_database
    _, job = await _seed_legacy(database)
    database.migrate("upgrade", "head")
    identifier = uuid4()
    await database.connection.execute(
        "INSERT INTO webhook_receipt(id,provider_name,delivery_id,"
        "received_at,review_job_id) "
        "VALUES($1,'github','bound-delivery',now(),$2)",
        identifier,
        job,
    )
    output = database.migrate("downgrade", "0020", success=False)
    assert "Cannot downgrade with retained automatic review bindings" in output
    assert (
        await database.connection.fetchval("SELECT version_num FROM alembic_version")
        == "0022"
    )
    assert (
        await database.connection.fetchval(
            "SELECT review_job_id FROM webhook_receipt WHERE id=$1", identifier
        )
        == job
    )


async def test_downgrade_refuses_retained_nonnull_repository_preferences(
    migration_database: MigrationDatabase,
) -> None:
    database = migration_database
    command, _ = await _seed_legacy(database)
    database.migrate("upgrade", "0020")
    policy = {
        "schema_version": 1,
        "auto_review_enabled": False,
        "base_branches": [],
        "minimum_severity": "critical",
        "authorized_by": None,
    }
    await database.connection.execute(
        "UPDATE repository_settings SET review_policy=$2::jsonb WHERE id=$1",
        command.repository_settings_id,
        json.dumps(policy),
    )
    output = database.migrate("downgrade", "0019", success=False)
    assert "Cannot downgrade with retained repository review policies" in output
    assert (
        await database.connection.fetchval("SELECT version_num FROM alembic_version")
        == "0020"
    )
    actual = await database.connection.fetchval(
        "SELECT review_policy FROM repository_settings WHERE id=$1",
        command.repository_settings_id,
    )
    assert json.loads(actual) == policy


async def test_binding_downgrade_cannot_race_past_a_committing_writer(
    migration_database: MigrationDatabase,
) -> None:
    database = migration_database
    _, job = await _seed_legacy(database)
    # Race only the 0021 binding guard, not later revisions' own locks.
    database.migrate("upgrade", "0021")
    identifier = uuid4()
    await database.connection.execute(
        "INSERT INTO webhook_receipt(id,provider_name,delivery_id,received_at) "
        "VALUES($1,'github','racing-delivery',now())",
        identifier,
    )
    process = None
    try:
        async with database.connection.transaction():
            await database.connection.execute(
                "LOCK TABLE webhook_receipt IN ROW EXCLUSIVE MODE"
            )
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "alembic",
                "downgrade",
                "0020",
                cwd=Path(__file__).parents[1],
                env=database.environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            async with asyncio.timeout(10):
                while not await database.connection.fetchval(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname=current_database() "
                    "AND pid<>pg_backend_pid() AND wait_event_type='Lock'"
                ):
                    if process.returncode is not None:
                        raise AssertionError("Downgrade did not wait for the writer")
                    await database.connection.execute("SELECT pg_stat_clear_snapshot()")
                    await asyncio.sleep(0.01)
            await database.connection.execute(
                "UPDATE webhook_receipt SET review_job_id=$2 WHERE id=$1",
                identifier,
                job,
            )
        stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
        assert process.returncode != 0, (stdout + stderr).decode()
        assert (
            "Cannot downgrade with retained automatic review bindings"
            in (stdout + stderr).decode()
        )
        assert (
            await database.connection.fetchval(
                "SELECT review_job_id FROM webhook_receipt WHERE id=$1", identifier
            )
            == job
        )
    finally:
        if process is not None and process.returncode is None:
            process.terminate()
            await process.communicate()


async def test_policy_downgrade_cannot_race_past_a_committing_writer(
    migration_database: MigrationDatabase,
) -> None:
    database = migration_database
    command, _ = await _seed_legacy(database)
    database.migrate("upgrade", "0020")
    policy = {
        "schema_version": 1,
        "auto_review_enabled": False,
        "base_branches": [],
        "minimum_severity": "critical",
        "authorized_by": None,
    }
    process = None
    try:
        async with database.connection.transaction():
            await database.connection.execute(
                "LOCK TABLE repository_settings IN ROW EXCLUSIVE MODE"
            )
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "alembic",
                "downgrade",
                "0019",
                cwd=Path(__file__).parents[1],
                env=database.environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            async with asyncio.timeout(10):
                while not await database.connection.fetchval(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname=current_database() "
                    "AND pid<>pg_backend_pid() AND wait_event_type='Lock'"
                ):
                    if process.returncode is not None:
                        raise AssertionError("Downgrade did not wait for the writer")
                    await database.connection.execute("SELECT pg_stat_clear_snapshot()")
                    await asyncio.sleep(0.01)
            await database.connection.execute(
                "UPDATE repository_settings SET review_policy=$2::jsonb WHERE id=$1",
                command.repository_settings_id,
                json.dumps(policy),
            )
        stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
        assert process.returncode != 0, (stdout + stderr).decode()
        assert (
            "Cannot downgrade with retained repository review policies"
            in (stdout + stderr).decode()
        )
        actual = await database.connection.fetchval(
            "SELECT review_policy FROM repository_settings WHERE id=$1",
            command.repository_settings_id,
        )
        assert json.loads(actual) == policy
    finally:
        if process is not None and process.returncode is None:
            process.terminate()
            await process.communicate()
