"""Old-source golden jobs seeded before the nullable-policy migration."""

import hashlib
import json
from collections.abc import AsyncIterator
from copy import deepcopy
from http import HTTPStatus
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from test_frozen_configuration import PROMPTS, runtime_profiles
from test_migration_upgrade import MigrationDatabase

from app.api.auth import VerifiedMutationIdentity, get_verified_mutation_identity
from app.application.github_publication import prepare_github_review
from app.db.publications import PostgresPublicationStore
from app.db.session import get_session
from app.main import create_app
from app.review.configuration import FrozenConfigurationReader

pytest_plugins = ("test_migration_upgrade",)


async def test_old_v4_policy_one_and_two_jobs_resolve_and_replay_after_migration(  # noqa: PLR0915 -- historical seed, migration roundtrip and public execution readers
    migration_database: MigrationDatabase,
    payload_data: dict[str, Any],
) -> None:
    database = migration_database
    fixture = json.loads(
        (
            Path(__file__).parent
            / "fixtures/repository-policy/legacy-v4-policy-1-2.json"
        ).read_text()
    )
    database.migrate("upgrade", "0019")
    actor, repository, change = (
        UUID(fixture["actor_id"]),
        UUID(fixture["source_settings"]["repository_id"]),
        UUID(fixture["change_id"]),
    )
    await database.connection.execute('INSERT INTO "user" (id) VALUES ($1)', actor)
    await database.connection.execute(
        "INSERT INTO repository(id,external_id,provider_name,owner,repo_name) "
        "VALUES($1,'123456','github','team','legacy')",
        repository,
    )
    for settings in (fixture["source_settings"], fixture["effective_settings"]):
        await database.connection.execute(
            """INSERT INTO repository_settings
            (id,repository_id,rules,ignores,max_starts_per_hour,max_active_jobs,
            output_language,surrounding_lines,created_at,rules_digest,rules_source)
            VALUES($1,$2,$3::jsonb,$4::jsonb,$5,$6,$7,$8,now(),$9,$10::jsonb)""",
            UUID(settings["id"]),
            repository,
            json.dumps(settings["rules"]),
            json.dumps(settings["ignores"]),
            settings["max_starts_per_hour"],
            settings["max_active_jobs"],
            settings["output_language"],
            settings["surrounding_lines"],
            settings["rules_digest"],
            json.dumps(settings["rules_source"])
            if settings.get("rules_source")
            else None,
        )
    await database.connection.execute(
        "UPDATE repository SET current_settings_id=$2 WHERE id=$1",
        repository,
        UUID(fixture["source_settings"]["id"]),
    )
    await database.connection.execute(
        "INSERT INTO repository_access(id,repository_id,user_id,role) "
        "VALUES('80000000-0000-0000-0000-000000000020',$1,$2,'admin')",
        repository,
        actor,
    )
    await database.connection.execute(
        """INSERT INTO change_request
        (id,repository_id,external_number,source_ref,base_ref,head_sha,base_sha,title,description,current_status)
        VALUES($1,$2,'13','feature/legacy','main',$3,$4,'Legacy review','','open')""",
        change,
        repository,
        fixture["head_sha"],
        fixture["base_sha"],
    )
    request_hash = hashlib.sha256(
        (
            '{"number":13,"requested_head_sha":"'
            + fixture["head_sha"]
            + '","rerun_of":null}'
        ).encode()
    ).hexdigest()
    for number, record in enumerate(fixture["records"], 1):
        identifier = UUID(record["id"])
        await database.connection.execute(
            """INSERT INTO review_job
            (id,repository_id,change_request_id,repository_settings_id,config_digest,
            rules_digest,trigger_type,initiator_user_id,requested_head_sha,model_ref,
            model_digest,model_settings,frozen_configuration_version,prompt_version,
            prompt_digest,created_at,status,queue_deadline_at,coverage)
            VALUES($1,$2,$3,$4,$5,$6,'manual',$7,$8,$9,NULL,$10::jsonb,1,'v4',$11,
            now(),'QUEUED',now()+interval '15 minutes','{}')""",
            identifier,
            repository,
            change,
            UUID(fixture["effective_settings"]["id"]),
            record["config_digest"],
            fixture["effective_settings"]["rules_digest"],
            actor,
            fixture["head_sha"],
            fixture["model_ref"],
            json.dumps(record["model_settings"]),
            fixture["prompt_digest"],
        )
        await database.connection.execute(
            "INSERT INTO publication(id,review_job_id,status,created_at) "
            "VALUES($1,$2,'NOT_READY',now())",
            UUID(f"60000000-0000-0000-0000-{number:012d}"),
            identifier,
        )
        await database.connection.execute(
            "INSERT INTO outbox_event(id,review_job_id,schema_version,task_kind,"
            "attempt,trace_id,created_at) "
            "VALUES($1,$2,1,'analyze',1,'legacy-fixture',now())",
            UUID(f"70000000-0000-0000-0000-{number:012d}"),
            identifier,
        )
        await database.connection.execute(
            """INSERT INTO idempotency_record
            (id,scope,idempotency_key,request_hash,review_job_id,created_at,expires_at)
            VALUES($1,$2,$3,$4,$5,now(),now()+interval '1 day')""",
            UUID(f"90000000-0000-0000-0000-{number:012d}"),
            f"startReview:{actor}:{repository}",
            hashlib.sha256(record["idempotency_key"].encode()).hexdigest(),
            request_hash,
            identifier,
        )
    old_receipt = UUID("b0000000-0000-0000-0000-000000000001")
    await database.connection.execute(
        "INSERT INTO webhook_receipt(id,provider_name,delivery_id,received_at) "
        "VALUES($1,'github','historical-ignored',now())",
        old_receipt,
    )
    original_settings = await database.connection.fetch(
        "SELECT id,rules,ignores,rules_digest,rules_source,created_at "
        "FROM repository_settings ORDER BY id"
    )
    # Every literal identity existed before the two additive nullable columns.
    database.migrate("upgrade", "head")
    assert (
        await database.connection.fetchval(
            "SELECT count(*) FROM repository_settings WHERE review_policy IS NOT NULL"
        )
        == 0
    )
    assert (
        await database.connection.fetchval(
            "SELECT review_job_id FROM webhook_receipt WHERE id=$1", old_receipt
        )
        is None
    )
    database.migrate("downgrade", "0019")
    assert (
        await database.connection.fetch(
            "SELECT id,rules,ignores,rules_digest,rules_source,created_at "
            "FROM repository_settings ORDER BY id"
        )
        == original_settings
    )
    database.migrate("upgrade", "head")
    assert (
        await database.connection.fetchval(
            "SELECT review_job_id FROM webhook_receipt WHERE id=$1", old_receipt
        )
        is None
    )
    for number, record in enumerate(fixture["records"], 1):
        context = deepcopy(payload_data)
        context["snapshot"].update(
            provider="github",
            base_repository_id="123456",
            head_repository_id="123456",
            head_sha=fixture["head_sha"],
            base_sha=fixture["base_sha"],
            merge_base_sha=fixture["base_sha"],
        )
        await database.connection.execute(
            """UPDATE review_job SET snapshot_provider='github',base_sha=$2,
            merge_base_sha=$2,source_repository_external_id='123456',
            base_repository_external_id='123456',captured_at=now(),coverage=$3::jsonb
            WHERE id=$1""",
            UUID(record["id"]),
            fixture["base_sha"],
            json.dumps(context["coverage"]),
        )
        await database.connection.execute(
            "INSERT INTO context_payload(id,review_job_id,schema_version,"
            "payload_body,created_at) VALUES($1,$2,1,$3::jsonb,now())",
            UUID(f"a0000000-0000-0000-0000-{number:012d}"),
            UUID(record["id"]),
            json.dumps(context),
        )
    engine = create_async_engine(database.url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    reader = FrozenConfigurationReader(factory, PROMPTS, runtime_profiles())
    app = create_app()

    async def sessions() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = sessions
    app.dependency_overrides[get_verified_mutation_identity] = lambda: (
        VerifiedMutationIdentity(actor, True, True, True)
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            for record in fixture["records"]:
                frozen = await reader.resolve(UUID(record["id"]))
                assert frozen.policy.model_dump(mode="json") == record["model_settings"]
                assert (
                    str(frozen.repository_settings_id)
                    == fixture["effective_settings"]["id"]
                )
                assert frozen.prompt_digest == fixture["prompt_digest"]
                assert frozen.minimum_severity == "low"
                publication = await PostgresPublicationStore(factory).load(
                    UUID(record["id"])
                )
                assert publication is not None and publication.minimum_severity == "low"
                assert "Обзор AI-ревью" in prepare_github_review(publication).body
                response = await client.post(
                    f"/api/v1/repositories/{repository}/pull-requests/13/reviews",
                    json={"requested_head_sha": fixture["head_sha"]},
                    headers={
                        "Origin": "https://trusted.example",
                        "X-CSRF-Token": "trusted",
                        "Idempotency-Key": record["idempotency_key"],
                    },
                )
                assert response.status_code == HTTPStatus.OK
                assert response.json()["review_id"] == record["id"]
    finally:
        await engine.dispose()
