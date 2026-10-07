from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from pathlib import Path

import asyncpg  # type: ignore[import-untyped]
import pytest
import pytest_asyncio
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from test_start_review import make_command

from app.application.ports import StartReviewCommand
from app.application.use_cases.start_review import compute_config_digest, start_review
from app.db.uow import SqlAlchemyUnitOfWork
from app.domain.settings import compute_rules_digest


@dataclass
class MigrationDatabase:
    connection: asyncpg.Connection
    url: str
    environment: dict[str, str]

    def migrate(self, *arguments: str, success: bool = True) -> str:
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *arguments],
            cwd=Path(__file__).parents[1],
            env=self.environment,
            capture_output=True,
            text=True,
            check=False,
        )
        output = result.stdout + result.stderr
        assert (result.returncode == 0) == success, output
        return output


@pytest_asyncio.fixture
async def migration_database() -> AsyncIterator[MigrationDatabase]:
    configured_url = os.environ.get("DMC268_TEST_DATABASE_URL")
    if not configured_url:
        pytest.skip("set DMC268_TEST_DATABASE_URL to test seeded migrations")
    url = make_url(configured_url)
    admin = await asyncpg.connect(
        url.set(drivername="postgresql").render_as_string(hide_password=False)
    )
    name = f"dmc268_migration_{uuid.uuid4().hex}"
    connection = None
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        target = url.set(database=name)
        environment = dict(os.environ) | {
            "POSTGRES__HOST": url.host or "localhost",
            "POSTGRES__PORT": str(url.port or 5432),
            "POSTGRES__USER": url.username or "postgres",
            "POSTGRES__PASSWORD": url.password or "",
            "POSTGRES__DB": name,
        }
        connection = await asyncpg.connect(
            target.set(drivername="postgresql").render_as_string(hide_password=False)
        )
        database = MigrationDatabase(
            connection, target.render_as_string(hide_password=False), environment
        )
        database.migrate("upgrade", "0003")
        yield database
    finally:
        if connection is not None:
            await connection.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
        await admin.close()


async def _seed_legacy(
    database: MigrationDatabase,
) -> tuple[StartReviewCommand, uuid.UUID]:
    connection = database.connection
    repository_id, settings_id, change_request_id, job_id = [
        uuid.uuid4() for _ in range(4)
    ]
    command = replace(
        make_command(),
        change_request_id=change_request_id,
        repository_settings_id=settings_id,
        initiator_user_id=None,
        rules_digest=compute_rules_digest({}),
    )
    await connection.execute(
        """INSERT INTO repository
        (id, external_id, provider_name, repo_name, owner)
        VALUES ($1,$2,'github','migration-test','team')""",
        repository_id,
        str(repository_id),
    )
    await connection.execute(
        """INSERT INTO repository_settings
        (id,repository_id,rules,ignores,max_starts_per_hour,max_active_jobs,
         output_language,surrounding_lines,created_at,rules_digest)
        VALUES ($1,$2,'{}','{}',10,20,'en',30,now(),$3)""",
        settings_id,
        repository_id,
        command.rules_digest,
    )
    await connection.execute(
        "UPDATE repository SET current_settings_id=$1 WHERE id=$2",
        settings_id,
        repository_id,
    )
    await connection.execute(
        """INSERT INTO change_request
        (id,repository_id,external_number,source_ref,base_ref,head_sha,
         base_sha,title,description,current_status)
        VALUES ($1,$2,'1','feature','main','head-sha','base-sha','Test','','open')""",
        change_request_id,
        repository_id,
    )
    await connection.execute(
        """INSERT INTO review_job
        (id,repository_id,change_request_id,repository_settings_id,
         config_digest,rules_digest,trigger_type,requested_head_sha,
         model_ref,model_digest,model_settings,prompt_version,prompt_digest,
         created_at,status,queue_deadline_at,coverage)
        VALUES ($1,$2,$3,$4,$5,$6,'manual',$7,$8,$9,$10,$11,$12,
                now(),'QUEUED',now()+interval '15 minutes','{}')""",
        job_id,
        repository_id,
        change_request_id,
        settings_id,
        compute_config_digest(command),
        command.rules_digest,
        command.requested_head_sha,
        command.model_ref,
        command.model_digest,
        json.dumps(command.model_settings),
        command.prompt_version,
        command.prompt_digest,
    )
    return command, job_id


async def test_upgrade_preserves_frozen_settings_and_active_review_identity(
    migration_database: MigrationDatabase,
) -> None:
    command, job_id = await _seed_legacy(migration_database)
    migration_database.migrate("upgrade", "head")
    settings = await migration_database.connection.fetchrow(
        "SELECT rules, ignores, rules_digest, output_language "
        "FROM repository_settings WHERE id=$1",
        command.repository_settings_id,
    )
    assert settings is not None
    assert settings["rules_digest"] == command.rules_digest
    assert json.loads(settings["rules"]) == {}
    assert json.loads(settings["ignores"]) == {}
    assert settings["output_language"] == "en"
    engine = create_async_engine(migration_database.url, poolclass=NullPool)
    try:
        with pytest.raises(IntegrityError):
            await start_review(
                command, unit_of_work=SqlAlchemyUnitOfWork(async_sessionmaker(engine))
            )
        assert (
            await migration_database.connection.fetchval(
                "SELECT count(*) FROM review_job "
                "WHERE change_request_id=$1 AND finished_at IS NULL",
                command.change_request_id,
            )
            == 1
        )
        assert await migration_database.connection.fetchval(
            "SELECT config_digest FROM review_job WHERE id=$1",
            job_id,
        ) == compute_config_digest(command)
    finally:
        await engine.dispose()


async def test_upgrade_preserves_historical_reason_codes(
    migration_database: MigrationDatabase,
) -> None:
    _, job_id = await _seed_legacy(migration_database)
    event_id = uuid.uuid4()
    await migration_database.connection.execute(
        """INSERT INTO review_event
        (id, review_job_id, occurred_at, event_type, status, stage, reason_code)
        VALUES ($1,$2,now(),'legacy-error','RUNNING','inference','MODEL_TIMEOUT')""",
        event_id,
        job_id,
    )
    migration_database.migrate("upgrade", "head")
    event = await migration_database.connection.fetchrow(
        "SELECT status, phase, reason_code FROM review_event WHERE id=$1",
        event_id,
    )
    assert event is not None
    assert tuple(event.values()) == (
        "LLM_PROCESSING",
        "LLM_PROCESSING",
        "MODEL_TIMEOUT",
    )


async def test_upgrade_normalizes_known_legacy_result_shapes_without_losing_warnings(
    migration_database: MigrationDatabase,
) -> None:
    _, job_id = await _seed_legacy(migration_database)
    ids = []
    for limitations in ({}, {"warnings": ["legacy warning"]}):
        result_id = uuid.uuid4()
        ids.append(result_id)
        await migration_database.connection.execute(
            """INSERT INTO chunk_result
            (id,review_job_id,schema_version,summary,limitations,usage,latency_ms,created_at)
            VALUES ($1,$2,1,'Legacy result',$3,'{}',10,now())""",
            result_id,
            job_id,
            json.dumps(limitations),
        )
    migration_database.migrate("upgrade", "head")
    for result_id, expected in zip(ids, ([], ["legacy warning"]), strict=True):
        row = await migration_database.connection.fetchrow(
            "SELECT limitations, usage FROM chunk_result WHERE id=$1",
            result_id,
        )
        assert row is not None
        assert json.loads(row["limitations"]) == expected
        assert row["usage"] is None


@pytest.mark.parametrize(
    ("limitations", "usage"),
    [
        ({"other": ["legacy warning"]}, {}),
        ([], {"tokens": 10}),
        ([42], {}),
    ],
)
async def test_unknown_legacy_result_aborts_upgrade_without_mutating_data(
    migration_database: MigrationDatabase,
    limitations: object,
    usage: object,
) -> None:
    _, job_id = await _seed_legacy(migration_database)
    result_id = uuid.uuid4()
    await migration_database.connection.execute(
        """INSERT INTO chunk_result
        (id,review_job_id,schema_version,summary,limitations,usage,latency_ms,created_at)
        VALUES ($1,$2,1,'Legacy result',$3,$4,10,now())""",
        result_id,
        job_id,
        json.dumps(limitations),
        json.dumps(usage),
    )
    output = migration_database.migrate("upgrade", "head", success=False)
    assert "0004 preflight" in output
    assert str(result_id) in output
    assert (
        await migration_database.connection.fetchval(
            "SELECT version_num FROM alembic_version"
        )
        == "0003"
    )
    row = await migration_database.connection.fetchrow(
        "SELECT limitations, usage FROM chunk_result WHERE id=$1", result_id
    )
    assert row is not None
    assert json.loads(row["limitations"]) == limitations
    assert json.loads(row["usage"]) == usage


async def test_seeded_downgrade_restores_old_result_shape_without_losing_warnings(
    migration_database: MigrationDatabase,
) -> None:
    _, job_id = await _seed_legacy(migration_database)
    result_id = uuid.uuid4()
    await migration_database.connection.execute(
        """INSERT INTO chunk_result
        (id,review_job_id,schema_version,summary,limitations,usage,latency_ms,created_at)
        VALUES ($1,$2,1,'Legacy result','{"warnings":["keep this"]}','{}',10,now())""",
        result_id,
        job_id,
    )
    migration_database.migrate("upgrade", "head")
    migration_database.migrate("downgrade", "0003")
    row = await migration_database.connection.fetchrow(
        "SELECT limitations, usage FROM chunk_result WHERE id=$1",
        result_id,
    )
    assert row is not None
    assert json.loads(row["limitations"]) == {"warnings": ["keep this"]}
    assert json.loads(row["usage"]) == {}
    migration_database.migrate("upgrade", "head")
    migration_database.migrate("check")


async def test_upgrade_preflight_sees_old_writer_committing_during_migration(
    migration_database: MigrationDatabase,
) -> None:
    _, job_id = await _seed_legacy(migration_database)
    connection = migration_database.connection
    transaction = connection.transaction()
    await transaction.start()
    await connection.execute("UPDATE review_job SET coverage='{}' WHERE id=$1", job_id)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "alembic",
        "upgrade",
        "head",
        cwd=Path(__file__).parents[1],
        env=migration_database.environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    committed = False
    try:
        async with asyncio.timeout(15):
            while not await connection.fetchval(
                "SELECT EXISTS(SELECT 1 FROM pg_locks "
                "WHERE relation='review_job'::regclass AND NOT granted)"
            ):
                await asyncio.sleep(0.05)
        await connection.execute(
            """INSERT INTO chunk_result
            (id,review_job_id,schema_version,summary,limitations,usage,latency_ms,created_at)
            VALUES ($1,$2,1,'Legacy writer','{"unknown":true}','{}',10,now())""",
            uuid.uuid4(),
            job_id,
        )
        await transaction.commit()
        committed = True
        output, _ = await asyncio.wait_for(process.communicate(), 15)
        assert process.returncode != 0, output.decode()
        assert "0004 preflight" in output.decode()
        assert (
            await connection.fetchval("SELECT version_num FROM alembic_version")
            == "0003"
        )
    finally:
        if not committed:
            await transaction.rollback()
        if process.returncode is None:
            process.terminate()
            await process.communicate()
