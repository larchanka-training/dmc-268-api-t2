"""Public migration contract, on a fresh disposable database."""

# ruff: noqa: F811 -- explicit local fixture import

import pytest
from test_migration_upgrade import MigrationDatabase, migration_database  # noqa: F401


async def test_ledger_migration_matches_metadata_and_refuses_history_loss(
    migration_database: MigrationDatabase,
) -> None:
    db = migration_database
    db.migrate("upgrade", "head")
    assert (
        await db.connection.fetchval("SELECT version_num FROM alembic_version")
        == "0022"
    )
    columns = await db.connection.fetch(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name='simulation_dispatch_claim' ORDER BY ordinal_position"
    )
    assert [row[0] for row in columns] == [
        "operation_digest",
        "user_id",
        "repository_id",
        "authorized_at",
        "state",
    ]
    db.migrate("check")
    db.migrate("downgrade", "0021")
    db.migrate("upgrade", "head")
    await db.connection.execute(
        "INSERT INTO simulation_dispatch_claim(operation_digest,user_id,repository_id) "
        "VALUES ($1,gen_random_uuid(),gen_random_uuid())",
        "a" * 64,
    )
    with pytest.raises(Exception, match="check constraint"):
        await db.connection.execute(
            "UPDATE simulation_dispatch_claim SET state='RETRY'"
        )
    output = db.migrate("downgrade", "0021", success=False)
    assert "retained simulation dispatch claims" in output
