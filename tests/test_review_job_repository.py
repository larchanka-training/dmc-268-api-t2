from __future__ import annotations

import uuid
from dataclasses import FrozenInstanceError, is_dataclass

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_postgres_integration import PostgresHarness, _create_review_fixture

from app.db.repositories import SqlAlchemyReviewJobRepository

pytest_plugins = ("test_postgres_integration",)


async def test_get_returns_detached_application_snapshot(
    postgres: PostgresHarness,
) -> None:
    async with postgres.engine.begin() as connection:
        scope, review_job_id = await _create_review_fixture(postgres, connection)

    session_factory = async_sessionmaker(postgres.engine)
    async with session_factory() as session:
        result = await SqlAlchemyReviewJobRepository(session).get(review_job_id)

    assert result is not None
    assert is_dataclass(result)
    assert type(result).__module__ == "app.application.ports"
    assert not hasattr(result, "__dict__")
    assert result.id == review_job_id
    assert result.repository_id == scope.repository_id
    assert result.change_request_id == scope.change_request_id
    assert result.repository_settings_id == scope.settings_id
    assert result.requested_head_sha == "head-sha"
    assert result.config_digest == "config-digest"
    assert result.status == "QUEUED"
    assert not hasattr(result, "stage")
    assert result.created_at.tzinfo is not None
    assert result.finished_at is None
    status_field = "status"
    with pytest.raises(FrozenInstanceError):
        setattr(result, status_field, "RUNNING")


async def test_get_returns_none_for_missing_review_job(
    postgres: PostgresHarness,
) -> None:
    session_factory = async_sessionmaker(postgres.engine)
    async with session_factory() as session:
        result = await SqlAlchemyReviewJobRepository(session).get(uuid.uuid4())

    assert result is None
