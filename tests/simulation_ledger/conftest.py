"""Fixture scope is limited to the new ledger suite."""

# ruff: noqa: F811 -- explicit local fixture imports

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from test_migration_upgrade import MigrationDatabase, migration_database  # noqa: F401


@pytest.fixture
async def ledger_engine(
    migration_database: MigrationDatabase,
) -> AsyncIterator[AsyncEngine]:
    migration_database.migrate("upgrade", "head")
    engine = create_async_engine(migration_database.url, pool_size=1, max_overflow=0)
    try:
        yield engine
    finally:
        await engine.dispose()
