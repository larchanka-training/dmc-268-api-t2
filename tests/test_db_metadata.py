from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.orm import configure_mappers

from app.db import models
from app.db.base import Base


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


def test_relationship_mappers_are_configurable() -> None:
    configure_mappers()
    assert tuple(
        (remote.name, local.name)
        for remote, local in (
            models.Repository.current_settings.property.synchronize_pairs
        )
    ) == (("id", "current_settings_id"),)
    assert tuple(
        (remote.name, local.name)
        for remote, local in models.ReviewJob.change_request.property.synchronize_pairs
    ) == (("id", "change_request_id"),)
    assert tuple(
        (remote.name, local.name)
        for remote, local in (
            models.ReviewJob.repository_settings.property.synchronize_pairs
        )
    ) == (("id", "repository_settings_id"),)


def test_repository_identity_and_current_settings_constraints() -> None:
    repository = Base.metadata.tables["repository"]
    settings = Base.metadata.tables["repository_settings"]

    assert repository.c.current_settings_id.nullable is True
    assert (
        "uq_repository_provider_external_id",
        ("provider_name", "external_id"),
    ) in {
        (constraint.name, tuple(column.name for column in constraint.columns))
        for constraint in repository.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert (
        "uq_repository_settings_repository_id_id",
        ("repository_id", "id"),
    ) in {
        (constraint.name, tuple(column.name for column in constraint.columns))
        for constraint in settings.constraints
        if isinstance(constraint, UniqueConstraint)
    }

    current_settings_fk = next(
        constraint
        for constraint in repository.constraints
        if isinstance(constraint, ForeignKeyConstraint)
        and constraint.name == "fk_repository_current_settings_same_repository"
    )
    assert tuple(column.name for column in current_settings_fk.columns) == (
        "id",
        "current_settings_id",
    )
    assert tuple(
        element.target_fullname for element in current_settings_fk.elements
    ) == (
        "repository_settings.repository_id",
        "repository_settings.id",
    )
    assert current_settings_fk.deferrable is None


def test_review_job_same_repository_and_active_run_constraints() -> None:
    change_request = Base.metadata.tables["change_request"]
    review_job = Base.metadata.tables["review_job"]

    assert review_job.c.repository_id.nullable is False
    assert review_job.c.config_digest.nullable is False
    assert (
        "uq_change_request_repository_id_id",
        ("repository_id", "id"),
    ) in {
        (constraint.name, tuple(column.name for column in constraint.columns))
        for constraint in change_request.constraints
        if isinstance(constraint, UniqueConstraint)
    }

    foreign_keys = {
        constraint.name: (
            tuple(column.name for column in constraint.columns),
            tuple(element.target_fullname for element in constraint.elements),
        )
        for constraint in review_job.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }
    assert foreign_keys["fk_review_job_change_request_same_repository"] == (
        ("repository_id", "change_request_id"),
        ("change_request.repository_id", "change_request.id"),
    )
    assert foreign_keys["fk_review_job_settings_same_repository"] == (
        ("repository_id", "repository_settings_id"),
        ("repository_settings.repository_id", "repository_settings.id"),
    )
    assert ("change_request_id",) not in {
        columns for columns, _targets in foreign_keys.values()
    }
    assert ("repository_settings_id",) not in {
        columns for columns, _targets in foreign_keys.values()
    }

    status_finished_check = next(
        constraint
        for constraint in review_job.constraints
        if isinstance(constraint, CheckConstraint)
        and constraint.name == "ck_review_job_status_finished_at"
    )
    check_sql = str(status_finished_check.sqltext)
    assert "status IN ('QUEUED','RUNNING') AND finished_at IS NULL" in check_sql
    assert "finished_at IS NOT NULL" in check_sql

    active_index = next(
        index
        for index in review_job.indexes
        if index.name == "uq_review_job_active_equivalent"
    )
    assert active_index.unique is True
    assert tuple(column.name for column in active_index.columns) == (
        "change_request_id",
        "requested_head_sha",
        "config_digest",
    )
    assert str(active_index.dialect_options["postgresql"]["where"]) == (
        "finished_at IS NULL"
    )


def test_one_chunk_result_per_context_payload() -> None:
    table = Base.metadata.tables["chunk_result"]
    assert table.c.context_payload_id.nullable is True
    assert table.c.review_job_id.nullable is False
    assert (
        "uq_chunk_result_context_payload",
        ("context_payload_id",),
    ) in {
        (constraint.name, tuple(column.name for column in constraint.columns))
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }

    context_fk = next(
        constraint
        for constraint in table.constraints
        if isinstance(constraint, ForeignKeyConstraint)
        and constraint.name == "fk_chunk_result_context_payload"
    )
    assert context_fk.ondelete == "SET NULL"


def test_one_publication_and_one_live_lease_per_review_job() -> None:
    assert Base.metadata.tables["publication"].c.review_job_id.unique is True
    assert Base.metadata.tables["task_lease"].c.review_job_id.unique is True
