import uuid
from datetime import UTC, datetime
from typing import cast
from unittest.mock import AsyncMock

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ReviewJob
from app.db.repositories import SqlAlchemyReviewJobRepository


async def test_review_snapshot_uses_status_without_removed_stage() -> None:
    job = ReviewJob(
        id=uuid.uuid4(),
        repository_id=uuid.uuid4(),
        change_request_id=uuid.uuid4(),
        repository_settings_id=uuid.uuid4(),
        requested_head_sha="head",
        config_digest="frozen-config",
        status="PARSING_CONTEXT",
        created_at=datetime.now(UTC),
        finished_at=None,
    )
    session = AsyncMock(spec=AsyncSession)
    session.get.return_value = job

    snapshot = await SqlAlchemyReviewJobRepository(cast(AsyncSession, session)).get(
        job.id
    )

    assert snapshot is not None
    assert snapshot.status == "PARSING_CONTEXT"
    assert not hasattr(snapshot, "stage")
