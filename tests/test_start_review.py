from __future__ import annotations

import inspect
import uuid
from dataclasses import replace
from types import TracebackType
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.application.use_cases.start_review as start_review_module
from app.application.ports import ReviewJobRepositoryPort, StartReviewCommand
from app.application.use_cases.start_review import (
    compute_config_digest,
    start_review,
)
from app.db.models import OutboxEvent, Publication, ReviewJob
from app.db.repositories import SqlAlchemyReviewJobRepository
from app.db.uow import SqlAlchemyUnitOfWork


def make_command() -> StartReviewCommand:
    return StartReviewCommand(
        change_request_id=uuid.UUID("00000000-0000-0000-0000-000000000011"),
        repository_settings_id=uuid.UUID("00000000-0000-0000-0000-000000000022"),
        requested_head_sha="head-sha",
        rules_digest="rules-v1",
        trigger_type="manual",
        initiator_user_id=uuid.UUID("00000000-0000-0000-0000-000000000033"),
        model_ref="model:latest",
        model_digest=None,
        model_settings={"temperature": 0, "options": {"top_p": 1, "seed": 7}},
        prompt_version="prompt-v1",
        prompt_digest="prompt-digest-v1",
        trace_id="trace-1",
    )


def test_config_digest_is_deterministic() -> None:
    command = make_command()
    expected = "296f1454240dcedf50b8cfb624df1d8c116b1e7d0037ef65109cea2b54c51fea"
    assert compute_config_digest(command) == expected


def test_config_digest_ignores_model_settings_key_order() -> None:
    command = make_command()
    reordered = replace(
        command,
        model_settings={"options": {"seed": 7, "top_p": 1}, "temperature": 0},
    )
    assert compute_config_digest(command) == compute_config_digest(reordered)


@pytest.mark.parametrize(
    "changed_command",
    [
        replace(
            make_command(),
            repository_settings_id=uuid.UUID("00000000-0000-0000-0000-000000000023"),
        ),
        replace(make_command(), rules_digest="rules-v2"),
        replace(make_command(), model_ref="model:other"),
        replace(make_command(), model_digest="sha256:model"),
        replace(make_command(), model_settings={"temperature": 1}),
        replace(make_command(), prompt_version="prompt-v2"),
        replace(make_command(), prompt_digest="prompt-digest-v2"),
    ],
    ids=[
        "repository-settings",
        "rules",
        "model-ref",
        "model-digest",
        "model-settings",
        "prompt-version",
        "prompt-digest",
    ],
)
def test_each_frozen_configuration_field_changes_digest(
    changed_command: StartReviewCommand,
) -> None:
    command = make_command()
    assert compute_config_digest(command) != compute_config_digest(changed_command)


def test_missing_model_digest_is_an_explicit_digest_input() -> None:
    command = make_command()
    assert compute_config_digest(command) != compute_config_digest(
        replace(command, model_digest="null")
    )


@pytest.mark.parametrize(
    "changed_command",
    [
        replace(
            make_command(),
            change_request_id=uuid.UUID("00000000-0000-0000-0000-000000000012"),
        ),
        replace(make_command(), requested_head_sha="other-head"),
        replace(make_command(), trigger_type="webhook"),
        replace(make_command(), initiator_user_id=None),
        replace(make_command(), trace_id="trace-2"),
    ],
    ids=["change-request", "head", "trigger", "initiator", "trace"],
)
def test_run_identity_fields_are_excluded_from_config_digest(
    changed_command: StartReviewCommand,
) -> None:
    command = make_command()
    assert compute_config_digest(command) == compute_config_digest(changed_command)


class FakeReviewJobRepository:
    def __init__(self, review_id: uuid.UUID) -> None:
        self.review_id = review_id
        self.calls: list[tuple[StartReviewCommand, str]] = []

    async def get(self, review_job_id: uuid.UUID) -> object | None:
        return None

    async def add_start_review(
        self, command: StartReviewCommand, *, config_digest: str
    ) -> uuid.UUID:
        self.calls.append((command, config_digest))
        return self.review_id


class FakeUnitOfWork:
    review_jobs: ReviewJobRepositoryPort

    def __init__(self, repository: FakeReviewJobRepository) -> None:
        self.review_jobs = repository
        self.commits = 0
        self.rollbacks = 0

    async def __aenter__(self) -> FakeUnitOfWork:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if exc_type is not None:
            await self.rollback()

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


@pytest.mark.asyncio
async def test_start_review_uses_storage_port_and_commits_once() -> None:
    expected_review_id = uuid.UUID("00000000-0000-0000-0000-000000000044")
    repository = FakeReviewJobRepository(expected_review_id)
    unit_of_work = FakeUnitOfWork(repository)
    command = make_command()

    review_id = await start_review(command, unit_of_work=unit_of_work)

    assert review_id == expected_review_id
    assert repository.calls == [(command, compute_config_digest(command))]
    assert unit_of_work.commits == 1
    assert unit_of_work.rollbacks == 0


class FailingReviewJobRepository(FakeReviewJobRepository):
    async def add_start_review(
        self, command: StartReviewCommand, *, config_digest: str
    ) -> uuid.UUID:
        raise RuntimeError("constraint failure")


@pytest.mark.asyncio
async def test_start_review_rolls_back_when_storage_fails() -> None:
    repository = FailingReviewJobRepository(uuid.uuid4())
    unit_of_work = FakeUnitOfWork(repository)

    with pytest.raises(RuntimeError, match="constraint failure"):
        await start_review(make_command(), unit_of_work=unit_of_work)

    assert unit_of_work.commits == 0
    assert unit_of_work.rollbacks == 1


def test_start_review_module_has_no_db_dependency() -> None:
    source = inspect.getsource(start_review_module)
    assert "app.db" not in source


@pytest.mark.asyncio
async def test_sqlalchemy_adapter_maps_initial_aggregate_without_commit() -> None:
    repository_id = uuid.UUID("00000000-0000-0000-0000-000000000055")
    session = Mock()
    session.scalar = AsyncMock(return_value=repository_id)
    session.add_all = Mock()
    repository = SqlAlchemyReviewJobRepository(cast(AsyncSession, session))
    command = make_command()
    config_digest = compute_config_digest(command)

    review_id = await repository.add_start_review(
        command,
        config_digest=config_digest,
    )

    records = session.add_all.call_args.args[0]
    expected_record_count = 3
    assert len(records) == expected_record_count
    review_job = next(record for record in records if isinstance(record, ReviewJob))
    publication = next(record for record in records if isinstance(record, Publication))
    outbox_event = next(record for record in records if isinstance(record, OutboxEvent))
    assert review_job.id == review_id
    assert review_job.repository_id == repository_id
    assert review_job.config_digest == config_digest
    assert review_job.finished_at is None
    assert publication.review_job_id == review_id
    assert outbox_event.review_job_id == review_id
    session.commit.assert_not_called()


@pytest.mark.asyncio
async def test_sqlalchemy_uow_commits_once_and_closes_session() -> None:
    repository_id = uuid.UUID("00000000-0000-0000-0000-000000000055")
    session = Mock()
    session.scalar = AsyncMock(return_value=repository_id)
    session.add_all = Mock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.close = AsyncMock()
    session_factory = cast(async_sessionmaker[AsyncSession], Mock(return_value=session))
    unit_of_work = SqlAlchemyUnitOfWork(session_factory)

    await start_review(make_command(), unit_of_work=unit_of_work)

    session.commit.assert_awaited_once_with()
    session.rollback.assert_not_awaited()
    session.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_sqlalchemy_uow_rolls_back_failed_commit() -> None:
    repository_id = uuid.UUID("00000000-0000-0000-0000-000000000055")
    session = Mock()
    session.scalar = AsyncMock(return_value=repository_id)
    session.add_all = Mock()
    session.commit = AsyncMock(side_effect=RuntimeError("constraint failure"))
    session.rollback = AsyncMock()
    session.close = AsyncMock()
    session_factory = cast(async_sessionmaker[AsyncSession], Mock(return_value=session))
    unit_of_work = SqlAlchemyUnitOfWork(session_factory)

    with pytest.raises(RuntimeError, match="constraint failure"):
        await start_review(make_command(), unit_of_work=unit_of_work)

    session.rollback.assert_awaited_once_with()
    session.close.assert_awaited_once_with()
