from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_postgres_integration import PostgresHarness, _create_review_fixture

from app.application.ports import LeaseLostError
from app.db.models import ReviewJob, TaskLease
from app.db.task_leases import PostgresTaskLeaseStore
from app.db.uow import SqlAlchemyUnitOfWork
from app.domain.enums import ReviewStatus

pytest_plugins = ("test_postgres_integration",)


@pytest_asyncio.fixture
async def review_id(postgres: PostgresHarness) -> AsyncIterator[uuid.UUID]:
    async with postgres.engine.begin() as connection:
        _, review_job_id = await _create_review_fixture(postgres, connection)
    try:
        yield review_job_id
    finally:
        async with postgres.engine.begin() as connection:
            await connection.execute(
                delete(TaskLease).where(TaskLease.review_job_id == review_job_id)
            )


async def test_worker_acquires_a_90_second_lease(
    postgres: PostgresHarness,
    review_id: uuid.UUID,
) -> None:
    async with postgres.engine.connect() as connection:
        before = (await connection.execute(select(func.clock_timestamp()))).scalar_one()
    lease = await PostgresTaskLeaseStore(async_sessionmaker(postgres.engine)).acquire(
        review_id, owner="worker-a"
    )
    async with postgres.engine.connect() as connection:
        after = (await connection.execute(select(func.clock_timestamp()))).scalar_one()

    assert lease is not None
    assert lease.review_job_id == review_id
    assert lease.owner == "worker-a"
    assert lease.fencing_token == 1
    assert before + timedelta(seconds=90) <= lease.expires_at
    assert lease.expires_at <= after + timedelta(seconds=90)


async def test_concurrent_and_repeated_acquire_has_only_one_owner(
    postgres: PostgresHarness,
    review_id: uuid.UUID,
) -> None:
    store = PostgresTaskLeaseStore(async_sessionmaker(postgres.engine))
    leases = await asyncio.gather(
        *(store.acquire(review_id, owner=f"worker-{index}") for index in range(8))
    )
    winners = [lease for lease in leases if lease is not None]
    assert len(winners) == 1
    assert await store.acquire(review_id, owner=winners[0].owner) is None
    assert await store.acquire(review_id, owner="worker-other") is None


async def _wait_for_blocked_operation(
    postgres: PostgresHarness,
    blocker: int | None = None,
) -> None:
    async with postgres.engine.connect() as observer:
        for _ in range(200):
            waiting = await observer.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE "
                    + (
                        ":blocker = ANY(pg_blocking_pids(pid))"
                        if blocker is not None
                        else "cardinality(pg_blocking_pids(pid)) > 0"
                    )
                ),
                {"blocker": blocker} if blocker is not None else {},
            )
            if waiting:
                return
            await asyncio.sleep(0.01)
    raise AssertionError("lease operation did not wait for the PostgreSQL row lock")


@pytest.mark.parametrize("expire_while_waiting", [False, True])
async def test_expired_lease_is_reacquired_with_increasing_tokens(
    postgres: PostgresHarness,
    review_id: uuid.UUID,
    expire_while_waiting: bool,
) -> None:
    store = PostgresTaskLeaseStore(async_sessionmaker(postgres.engine))
    first = await store.acquire(review_id, owner="worker-a")
    assert first is not None
    for expected_token, owner in ((2, "worker-b"), (3, "worker-a")):
        async with postgres.engine.begin() as connection:
            await connection.execute(
                update(TaskLease)
                .where(TaskLease.review_job_id == review_id)
                .values(
                    expires_at=func.clock_timestamp()
                    + timedelta(seconds=0.3 if expire_while_waiting else -1)
                )
            )
        if expire_while_waiting:
            async with postgres.engine.begin() as blocker:
                await blocker.execute(
                    select(TaskLease)
                    .where(TaskLease.review_job_id == review_id)
                    .with_for_update()
                )
                blocker_pid = await blocker.scalar(select(func.pg_backend_pid()))
                task = asyncio.create_task(store.acquire(review_id, owner=owner))
                await _wait_for_blocked_operation(postgres, blocker_pid)
                await blocker.execute(text("SELECT pg_sleep(0.4)"))
            lease = await task
        else:
            lease = await store.acquire(review_id, owner=owner)
        assert lease is not None
        assert lease.owner == owner
        assert lease.fencing_token == expected_token


@pytest.mark.parametrize("expire_while_waiting", [False, True])
async def test_only_current_owner_and_token_can_renew_a_live_lease(
    postgres: PostgresHarness,
    review_id: uuid.UUID,
    expire_while_waiting: bool,
) -> None:
    store = PostgresTaskLeaseStore(async_sessionmaker(postgres.engine))
    lease = await store.acquire(review_id, owner="worker-a")
    assert lease is not None
    assert await store.renew(replace(lease, owner="worker-b")) is None
    assert await store.renew(replace(lease, fencing_token=2)) is None
    renewed = await store.renew(lease)
    assert renewed is not None
    assert renewed.owner == lease.owner
    assert renewed.fencing_token == lease.fencing_token
    assert renewed.expires_at > lease.expires_at

    async with postgres.engine.begin() as connection:
        await connection.execute(
            update(TaskLease)
            .where(TaskLease.review_job_id == review_id)
            .values(
                expires_at=func.clock_timestamp()
                + timedelta(seconds=0.3 if expire_while_waiting else -1)
            )
        )
    if expire_while_waiting:
        async with postgres.engine.begin() as blocker:
            await blocker.execute(
                select(TaskLease)
                .where(TaskLease.review_job_id == review_id)
                .with_for_update()
            )
            blocker_pid = await blocker.scalar(select(func.pg_backend_pid()))
            task = asyncio.create_task(store.renew(renewed))
            await _wait_for_blocked_operation(postgres, blocker_pid)
            await blocker.execute(text("SELECT pg_sleep(0.4)"))
        assert await task is None
    else:
        assert await store.renew(renewed) is None
    replacement = await store.acquire(review_id, owner="worker-b")
    assert replacement is not None
    assert await store.renew(renewed) is None
    assert await store.renew(replace(replacement, owner="worker-a")) is None

    # The snapshot's expires_at is informational; PostgreSQL is authoritative.
    assert (
        await store.renew(replace(replacement, expires_at=lease.expires_at)) is not None
    )


async def test_only_live_owner_can_release_without_resetting_fencing_token(
    postgres: PostgresHarness,
    review_id: uuid.UUID,
) -> None:
    store = PostgresTaskLeaseStore(async_sessionmaker(postgres.engine))
    first = await store.acquire(review_id, owner="worker-a")
    assert first is not None
    assert not await store.release(replace(first, owner="worker-b"))
    assert not await store.release(replace(first, fencing_token=2))
    assert await store.release(first)
    assert not await store.release(first)
    assert await store.renew(first) is None

    second = await store.acquire(review_id, owner="worker-b")
    assert second is not None
    assert second.fencing_token == 2  # noqa: PLR2004 — literal AC expectation
    assert not await store.release(first)
    assert await store.acquire(review_id, owner="worker-a") is None
    assert await store.release(second)
    third = await store.acquire(review_id, owner="worker-a")
    assert third is not None
    assert third.fencing_token == 3  # noqa: PLR2004 — literal AC expectation
    # Reusing an owner identity must not resurrect its earlier token.
    assert not await store.release(first)
    assert await store.renew(first) is None


async def test_status_write_requires_current_fencing_and_rolls_back_with_uow(
    postgres: PostgresHarness,
    review_id: uuid.UUID,
) -> None:
    factory = async_sessionmaker(postgres.engine, expire_on_commit=False)
    store = PostgresTaskLeaseStore(factory)
    first = await store.acquire(review_id, owner="worker-a")
    assert first is not None
    async with SqlAlchemyUnitOfWork(factory) as uow:
        await uow.review_jobs.update_status(first, status=ReviewStatus.FETCHING_DIFF)
        await uow.rollback()
    async with SqlAlchemyUnitOfWork(factory) as uow:
        original = await uow.review_jobs.get(review_id)
        assert original is not None
        assert original.status == "QUEUED"
        await uow.review_jobs.update_status(first, status=ReviewStatus.FETCHING_DIFF)
        release_task = asyncio.create_task(store.release(first))
        await _wait_for_blocked_operation(postgres)
        assert not release_task.done()
        await uow.commit()

    assert await release_task
    second = await store.acquire(review_id, owner="worker-a")
    assert second is not None
    for claim in (first, replace(second, owner="worker-b")):
        async with SqlAlchemyUnitOfWork(factory) as uow:
            with pytest.raises(LeaseLostError):
                await uow.review_jobs.update_status(
                    claim, status=ReviewStatus.LLM_PROCESSING
                )
            await uow.commit()
    async with SqlAlchemyUnitOfWork(factory) as uow:
        saved = await uow.review_jobs.get(review_id)
        assert saved is not None
        assert saved.status == "FETCHING_DIFF"
        await uow.review_jobs.update_status(second, status=ReviewStatus.LLM_PROCESSING)
        await uow.commit()
    async with SqlAlchemyUnitOfWork(factory) as uow:
        saved = await uow.review_jobs.get(review_id)
        assert saved is not None
        assert saved.status == "LLM_PROCESSING"


@pytest.mark.parametrize("status", ["COMPLETED", "PARTIAL", "FAILED", "SKIPPED"])
@pytest.mark.parametrize("has_expired_lease", [False, True])
async def test_terminal_job_cannot_be_acquired(
    postgres: PostgresHarness,
    review_id: uuid.UUID,
    status: str,
    has_expired_lease: bool,
) -> None:
    store = PostgresTaskLeaseStore(async_sessionmaker(postgres.engine))
    if has_expired_lease:
        lease = await store.acquire(review_id, owner="worker-a")
        assert lease is not None
        assert await store.release(lease)
    async with postgres.engine.begin() as connection:
        await connection.execute(
            update(ReviewJob)
            .where(ReviewJob.id == review_id)
            .values(status=status, finished_at=func.clock_timestamp())
        )
        blocker_pid = await connection.scalar(select(func.pg_backend_pid()))
        task = asyncio.create_task(store.acquire(review_id, owner="worker-b"))
        await _wait_for_blocked_operation(postgres, blocker_pid)
    assert await task is None
    assert await store.acquire(review_id, owner="worker-b") is None


async def test_fencing_rechecks_expiry_at_write_after_waiting_for_update_lock(
    postgres: PostgresHarness,
    review_id: uuid.UUID,
) -> None:
    factory = async_sessionmaker(postgres.engine, expire_on_commit=False)
    store = PostgresTaskLeaseStore(factory)
    lease = await store.acquire(review_id, owner="worker-a")
    assert lease is not None
    async with postgres.engine.begin() as connection:
        await connection.execute(
            update(TaskLease)
            .where(TaskLease.review_job_id == review_id)
            .values(expires_at=func.clock_timestamp() + timedelta(seconds=0.3))
        )
    async with SqlAlchemyUnitOfWork(factory) as uow:
        async with postgres.engine.begin() as blocker:
            # SHARE permits SELECT FOR UPDATE but makes the actual UPDATE wait.
            await blocker.execute(text("LOCK TABLE review_job IN SHARE MODE"))
            blocker_pid = await blocker.scalar(select(func.pg_backend_pid()))
            write_task = asyncio.create_task(
                uow.review_jobs.update_status(lease, status=ReviewStatus.LLM_PROCESSING)
            )
            await _wait_for_blocked_operation(postgres, blocker_pid)
            await blocker.execute(text("SELECT pg_sleep(0.4)"))
        with pytest.raises(LeaseLostError):
            await write_task
        await uow.commit()
    async with SqlAlchemyUnitOfWork(factory) as uow:
        saved = await uow.review_jobs.get(review_id)
        assert saved is not None
        assert saved.status == "QUEUED"


@pytest.mark.parametrize(
    "status",
    [
        ReviewStatus.COMPLETED,
        ReviewStatus.PARTIAL,
        ReviewStatus.FAILED,
        ReviewStatus.SKIPPED,
    ],
)
async def test_fenced_terminal_transition_sets_finished_at_and_never_reopens(
    postgres: PostgresHarness,
    review_id: uuid.UUID,
    status: ReviewStatus,
) -> None:
    factory = async_sessionmaker(postgres.engine, expire_on_commit=False)
    store = PostgresTaskLeaseStore(factory)
    lease = await store.acquire(review_id, owner="worker-a")
    assert lease is not None
    async with SqlAlchemyUnitOfWork(factory) as uow:
        # Prime the identity map to exercise lifecycle refresh under the lock.
        await uow.review_jobs.get(review_id)
        await uow.review_jobs.update_status(lease, status=status)
        await uow.commit()
        terminal = await uow.review_jobs.get(review_id)
        assert terminal is not None
        assert terminal.status == status.value
        assert terminal.finished_at is not None
        with pytest.raises(LeaseLostError):
            await uow.review_jobs.update_status(lease, status=ReviewStatus.QUEUED)
        await uow.commit()
    async with SqlAlchemyUnitOfWork(factory) as uow:
        saved = await uow.review_jobs.get(review_id)
        assert saved == terminal
    assert await store.acquire(review_id, owner="worker-b") is None


async def test_heartbeat_cannot_resurrect_lease_expired_while_update_waits(
    postgres: PostgresHarness,
    review_id: uuid.UUID,
) -> None:
    store = PostgresTaskLeaseStore(async_sessionmaker(postgres.engine))
    lease = await store.acquire(review_id, owner="worker-a")
    assert lease is not None
    async with postgres.engine.begin() as connection:
        await connection.execute(
            update(TaskLease)
            .where(TaskLease.review_job_id == review_id)
            .values(expires_at=func.clock_timestamp() + timedelta(seconds=0.3))
        )
    async with postgres.engine.begin() as blocker:
        await blocker.execute(text("LOCK TABLE task_lease IN SHARE MODE"))
        blocker_pid = await blocker.scalar(select(func.pg_backend_pid()))
        task = asyncio.create_task(store.renew(lease))
        await _wait_for_blocked_operation(postgres, blocker_pid)
        await blocker.execute(text("SELECT pg_sleep(0.4)"))
    assert await task is None
    next_lease = await store.acquire(review_id, owner="worker-b")
    assert next_lease is not None
    assert next_lease.fencing_token == 2  # noqa: PLR2004 — literal AC expectation


async def test_release_refuses_lease_expired_while_update_waits(
    postgres: PostgresHarness,
    review_id: uuid.UUID,
) -> None:
    store = PostgresTaskLeaseStore(async_sessionmaker(postgres.engine))
    lease = await store.acquire(review_id, owner="worker-a")
    assert lease is not None
    async with postgres.engine.begin() as connection:
        await connection.execute(
            update(TaskLease)
            .where(TaskLease.review_job_id == review_id)
            .values(expires_at=func.clock_timestamp() + timedelta(seconds=0.3))
        )
    async with postgres.engine.begin() as blocker:
        await blocker.execute(text("LOCK TABLE task_lease IN SHARE MODE"))
        blocker_pid = await blocker.scalar(select(func.pg_backend_pid()))
        task = asyncio.create_task(store.release(lease))
        await _wait_for_blocked_operation(postgres, blocker_pid)
        await blocker.execute(text("SELECT pg_sleep(0.4)"))
    assert not await task
    next_lease = await store.acquire(review_id, owner="worker-b")
    assert next_lease is not None
    assert next_lease.fencing_token == 2  # noqa: PLR2004 — literal AC expectation
