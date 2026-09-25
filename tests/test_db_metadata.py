from app.db.base import Base
from app.db import models  # noqa: F401


def test_expected_core_tables_are_registered() -> None:
    expected = {
        "user",
        "repository",
        "repository_access",
        "repository_settings",
        "change_request",
        "review_job",
        "review_event",
        "context_payload",
        "chunk_result",
        "finding",
        "publication",
        "outbox_event",
        "task_lease",
        "quota_usage",
        "webhook_receipt",
        "idempotency_record",
    }
    assert set(Base.metadata.tables) == expected


def test_one_chunk_result_per_context_payload() -> None:
    table = Base.metadata.tables["chunk_result"]
    assert table.c.context_payload_id.unique is True


def test_one_publication_and_one_live_lease_per_review_job() -> None:
    assert Base.metadata.tables["publication"].c.review_job_id.unique is True
    assert Base.metadata.tables["task_lease"].c.review_job_id.unique is True
