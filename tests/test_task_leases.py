from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection, async_sessionmaker
from test_postgres_integration import PostgresHarness, _create_review_fixture

from app.application.ports import LeaseLostError
from app.db.models import Publication, ReviewJob, TaskLease
from app.db.task_leases import PostgresTaskLeaseStore
from app.db.uow import SqlAlchemyUnitOfWork
from app.domain.enums import PublicationStatus, ReviewStatus

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
        review_id, owner="worker-a", task_kind="analyze"
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
        *(
            store.acquire(review_id, owner=f"worker-{index}", task_kind="analyze")
            for index in range(8)
        )
    )
    winners = [lease for lease in leases if lease is not None]
    assert len(winners) == 1
    assert (
        await store.acquire(review_id, owner=winners[0].owner, task_kind="analyze")
        is None
    )
    assert (
        await store.acquire(review_id, owner="worker-other", task_kind="analyze")
        is None
    )


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
    first = await store.acquire(review_id, owner="worker-a", task_kind="analyze")
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
                task = asyncio.create_task(
                    store.acquire(review_id, owner=owner, task_kind="analyze")
                )
                await _wait_for_blocked_operation(postgres, blocker_pid)
                await blocker.execute(text("SELECT pg_sleep(0.4)"))
            lease = await task
        else:
            lease = await store.acquire(review_id, owner=owner, task_kind="analyze")
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
    lease = await store.acquire(review_id, owner="worker-a", task_kind="analyze")
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
    replacement = await store.acquire(review_id, owner="worker-b", task_kind="analyze")
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
    first = await store.acquire(review_id, owner="worker-a", task_kind="analyze")
    assert first is not None
    assert not await store.release(replace(first, owner="worker-b"))
    assert not await store.release(replace(first, fencing_token=2))
    assert await store.release(first)
    assert not await store.release(first)
    assert await store.renew(first) is None

    second = await store.acquire(review_id, owner="worker-b", task_kind="analyze")
    assert second is not None
    assert second.fencing_token == 2  # noqa: PLR2004 — literal AC expectation
    assert not await store.release(first)
    assert await store.acquire(review_id, owner="worker-a", task_kind="analyze") is None
    assert await store.release(second)
    third = await store.acquire(review_id, owner="worker-a", task_kind="analyze")
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
    first = await store.acquire(review_id, owner="worker-a", task_kind="analyze")
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
    second = await store.acquire(review_id, owner="worker-a", task_kind="analyze")
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
        lease = await store.acquire(review_id, owner="worker-a", task_kind="analyze")
        assert lease is not None
        assert await store.release(lease)
    async with postgres.engine.begin() as connection:
        await connection.execute(
            update(ReviewJob)
            .where(ReviewJob.id == review_id)
            .values(status=status, finished_at=func.clock_timestamp())
        )
        blocker_pid = await connection.scalar(select(func.pg_backend_pid()))
        task = asyncio.create_task(
            store.acquire(review_id, owner="worker-b", task_kind="analyze")
        )
        await _wait_for_blocked_operation(postgres, blocker_pid)
    assert await task is None
    assert await store.acquire(review_id, owner="worker-b", task_kind="analyze") is None


async def test_fencing_rechecks_expiry_at_write_after_waiting_for_update_lock(
    postgres: PostgresHarness,
    review_id: uuid.UUID,
) -> None:
    factory = async_sessionmaker(postgres.engine, expire_on_commit=False)
    store = PostgresTaskLeaseStore(factory)
    lease = await store.acquire(review_id, owner="worker-a", task_kind="analyze")
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
    lease = await store.acquire(review_id, owner="worker-a", task_kind="analyze")
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
    assert await store.acquire(review_id, owner="worker-b", task_kind="analyze") is None


async def test_heartbeat_cannot_resurrect_lease_expired_while_update_waits(
    postgres: PostgresHarness,
    review_id: uuid.UUID,
) -> None:
    store = PostgresTaskLeaseStore(async_sessionmaker(postgres.engine))
    lease = await store.acquire(review_id, owner="worker-a", task_kind="analyze")
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
    next_lease = await store.acquire(review_id, owner="worker-b", task_kind="analyze")
    assert next_lease is not None
    assert next_lease.fencing_token == 2  # noqa: PLR2004 — literal AC expectation


async def test_release_refuses_lease_expired_while_update_waits(
    postgres: PostgresHarness,
    review_id: uuid.UUID,
) -> None:
    store = PostgresTaskLeaseStore(async_sessionmaker(postgres.engine))
    lease = await store.acquire(review_id, owner="worker-a", task_kind="analyze")
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
    next_lease = await store.acquire(review_id, owner="worker-b", task_kind="analyze")
    assert next_lease is not None
    assert next_lease.fencing_token == 2  # noqa: PLR2004 — literal AC expectation


@pytest.mark.parametrize("status", [ReviewStatus.COMPLETED, ReviewStatus.PARTIAL])
async def test_completed_analysis_refuses_heartbeat_but_allows_release(
    postgres: PostgresHarness,
    review_id: uuid.UUID,
    status: ReviewStatus,
) -> None:
    factory = async_sessionmaker(postgres.engine)
    store = PostgresTaskLeaseStore(factory)
    lease = await store.acquire(review_id, owner="analyzer", task_kind="analyze")
    assert lease is not None
    # Heartbeat waits behind the terminal write and must see its committed state.
    async with SqlAlchemyUnitOfWork(factory) as uow:
        await uow.review_jobs.update_status(lease, status=status)
        heartbeat = asyncio.create_task(store.renew(lease))
        await _wait_for_blocked_operation(postgres)
        await uow.commit()

    assert await heartbeat is None
    assert await store.release(lease)
    async with SqlAlchemyUnitOfWork(factory) as uow:
        saved = await uow.review_jobs.get(review_id)
        assert saved is not None
        assert saved.status == status.value
        assert saved.finished_at is not None


async def test_analysis_grant_has_bound_task_kind(
    postgres: PostgresHarness, review_id: uuid.UUID
) -> None:
    store = PostgresTaskLeaseStore(async_sessionmaker(postgres.engine))
    lease = await store.acquire(review_id, owner="analyzer", task_kind="analyze")
    assert lease is not None
    assert lease.task_kind == "analyze"
    renewed = await store.renew(lease)
    assert renewed is not None
    assert renewed.task_kind == "analyze"
    assert await store.release(renewed)


async def test_forged_kind_cannot_renew_release_or_write_analysis(
    postgres: PostgresHarness, review_id: uuid.UUID
) -> None:
    factory = async_sessionmaker(postgres.engine)
    store = PostgresTaskLeaseStore(factory)
    lease = await store.acquire(review_id, owner="worker", task_kind="analyze")
    assert lease is not None
    forged = replace(lease, task_kind="publish")
    assert await store.renew(forged) is None
    assert not await store.release(forged)
    async with SqlAlchemyUnitOfWork(factory) as uow:
        with pytest.raises(LeaseLostError):
            await uow.review_jobs.update_status(
                forged, status=ReviewStatus.FETCHING_DIFF
            )
        await uow.commit()
    assert await store.release(lease)


async def _create_publication(
    postgres: PostgresHarness,
    review_id: uuid.UUID,
    *,
    review_status: str = "COMPLETED",
    publication_status: str = "PENDING",
    deadline: timedelta | None = timedelta(minutes=5),
) -> None:
    async with postgres.engine.begin() as connection:
        await connection.execute(
            update(ReviewJob)
            .where(ReviewJob.id == review_id)
            .values(
                status=review_status,
                finished_at=func.clock_timestamp()
                if review_status in {"COMPLETED", "PARTIAL", "FAILED", "SKIPPED"}
                else None,
            )
        )
        await connection.execute(
            insert(Publication).values(
                id=postgres.track("publication", uuid.uuid4()),
                review_job_id=review_id,
                status=publication_status,
                deadline_at=func.clock_timestamp() + deadline
                if deadline is not None
                else None,
            )
        )


@pytest.mark.parametrize(
    ("review_status", "publication_status", "deadline", "eligible"),
    [
        ("COMPLETED", "PENDING", timedelta(minutes=5), True),
        ("PARTIAL", "PENDING", timedelta(minutes=5), True),
        *(
            (state, "PENDING", timedelta(minutes=5), False)
            for state in (
                "QUEUED",
                "FETCHING_DIFF",
                "PARSING_CONTEXT",
                "LLM_PROCESSING",
                "FAILED",
                "SKIPPED",
            )
        ),
        *(
            ("COMPLETED", state, timedelta(minutes=5), False)
            for state in (
                "NOT_READY",
                "PUBLISHED",
                "PARTIAL",
                "FAILED",
                "UNKNOWN",
                "SKIPPED",
            )
        ),
        ("COMPLETED", "PENDING", None, False),
        ("COMPLETED", "PENDING", timedelta(seconds=-1), False),
    ],
)
async def test_publish_acquisition_requires_eligible_review_and_publication(  # noqa: PLR0913, PLR0917 — fixtures and eligibility matrix
    postgres: PostgresHarness,
    review_id: uuid.UUID,
    review_status: str,
    publication_status: str,
    deadline: timedelta | None,
    eligible: bool,
) -> None:
    await _create_publication(
        postgres,
        review_id,
        review_status=review_status,
        publication_status=publication_status,
        deadline=deadline,
    )
    store = PostgresTaskLeaseStore(async_sessionmaker(postgres.engine))
    lease = await store.acquire(review_id, owner="publisher", task_kind="publish")
    assert (lease is not None) == eligible
    if lease is not None:
        assert lease.task_kind == "publish"
        assert lease.fencing_token == 1
        assert await store.release(lease)


@pytest.mark.parametrize("previous_analysis", [False, True])
@pytest.mark.parametrize("deadline_expires", [False, True])
async def test_publish_acquire_rechecks_clock_after_sql_lock_wait(
    postgres: PostgresHarness,
    review_id: uuid.UUID,
    previous_analysis: bool,
    deadline_expires: bool,
) -> None:
    factory = async_sessionmaker(postgres.engine)
    store = PostgresTaskLeaseStore(factory)
    analysis = None
    if previous_analysis:
        analysis = await store.acquire(
            review_id, owner="same-worker", task_kind="analyze"
        )
        assert analysis is not None
    await _create_publication(postgres, review_id)
    if analysis is not None:
        assert (
            await store.acquire(review_id, owner="same-worker", task_kind="publish")
            is None
        )
        assert await store.release(analysis)
    if deadline_expires:
        async with postgres.engine.begin() as connection:
            await connection.execute(
                update(Publication)
                .where(Publication.review_job_id == review_id)
                .values(deadline_at=func.clock_timestamp() + timedelta(seconds=0.3))
            )
    async with postgres.engine.begin() as blocker:
        await blocker.execute(text("LOCK TABLE task_lease IN SHARE MODE"))
        blocker_pid = await blocker.scalar(select(func.pg_backend_pid()))
        task = asyncio.create_task(
            store.acquire(review_id, owner="same-worker", task_kind="publish")
        )
        await _wait_for_blocked_operation(postgres, blocker_pid)
        await blocker.execute(text("SELECT pg_sleep(0.4)"))
        unlock_time = (
            await blocker.execute(select(func.clock_timestamp()))
        ).scalar_one()
    acquired = await task
    if deadline_expires:
        assert acquired is None
        async with postgres.engine.begin() as connection:
            await connection.execute(
                update(Publication)
                .where(Publication.review_job_id == review_id)
                .values(deadline_at=func.clock_timestamp() + timedelta(minutes=5))
            )
    else:
        assert acquired is not None
        assert acquired.expires_at >= unlock_time + timedelta(seconds=90)
        assert await store.release(acquired)
    # Mixed-kind contention still grants one publisher; completed analysis stays closed.
    claims = await asyncio.gather(
        *(
            store.acquire(review_id, owner="same-worker", task_kind=kind)
            for kind in ("analyze", "publish", "publish", "publish")
        )
    )
    winners = [claim for claim in claims if claim is not None]
    assert len(winners) == 1
    winner = winners[0]
    expected = (2 if previous_analysis else 1) + (0 if deadline_expires else 1)
    assert winner.fencing_token == expected
    assert winner.task_kind == "publish"
    if analysis is not None:
        assert await store.renew(analysis) is None
        assert not await store.release(analysis)
        assert await store.renew(replace(analysis, task_kind="publish")) is None
    assert await store.release(winner)


@pytest.mark.parametrize(
    "status",
    ["PUBLISHED", "PARTIAL", "FAILED", "UNKNOWN", "SKIPPED", "NOT_READY", "PENDING"],
)
async def test_publish_heartbeat_requires_current_eligibility_but_release_does_not(
    postgres: PostgresHarness, review_id: uuid.UUID, status: str
) -> None:
    await _create_publication(postgres, review_id)
    factory = async_sessionmaker(postgres.engine)
    store = PostgresTaskLeaseStore(factory)
    lease = await store.acquire(review_id, owner="publisher", task_kind="publish")
    assert lease is not None
    renewed = await store.renew(lease)
    assert renewed is not None
    assert renewed.task_kind == "publish"
    assert renewed.fencing_token == lease.fencing_token
    assert renewed.expires_at > lease.expires_at
    assert await store.renew(replace(lease, task_kind="analyze")) is None
    assert not await store.release(replace(lease, task_kind="analyze"))
    async with postgres.engine.begin() as connection:
        await connection.execute(
            update(Publication)
            .where(Publication.review_job_id == review_id)
            .values(
                status=status,
                deadline_at=func.clock_timestamp() - timedelta(seconds=1)
                if status == "PENDING"
                else func.clock_timestamp() + timedelta(minutes=5),
            )
        )
        blocker_pid = await connection.scalar(select(func.pg_backend_pid()))
        task = asyncio.create_task(store.renew(lease))
        await _wait_for_blocked_operation(postgres, blocker_pid)
    assert await task is None
    assert await store.acquire(review_id, owner="other", task_kind="publish") is None
    assert await store.release(lease)


@pytest.mark.parametrize("boundary", ["lease", "deadline"])
async def test_publish_heartbeat_rechecks_expiry_at_blocked_sql_write(
    postgres: PostgresHarness, review_id: uuid.UUID, boundary: str
) -> None:
    await _create_publication(postgres, review_id)
    store = PostgresTaskLeaseStore(async_sessionmaker(postgres.engine))
    lease = await store.acquire(review_id, owner="publisher", task_kind="publish")
    assert lease is not None
    async with postgres.engine.begin() as connection:
        if boundary == "lease":
            await connection.execute(
                update(TaskLease)
                .where(TaskLease.review_job_id == review_id)
                .values(expires_at=func.clock_timestamp() + timedelta(seconds=0.3))
            )
        else:
            await connection.execute(
                update(Publication)
                .where(Publication.review_job_id == review_id)
                .values(deadline_at=func.clock_timestamp() + timedelta(seconds=0.3))
            )
    async with postgres.engine.begin() as blocker:
        await blocker.execute(text("LOCK TABLE task_lease IN SHARE MODE"))
        blocker_pid = await blocker.scalar(select(func.pg_backend_pid()))
        task = asyncio.create_task(store.renew(lease))
        await _wait_for_blocked_operation(postgres, blocker_pid)
        await blocker.execute(text("SELECT pg_sleep(0.4)"))
    assert await task is None


async def test_analysis_write_requires_authoritative_analyze_kind(
    postgres: PostgresHarness, review_id: uuid.UUID
) -> None:
    factory = async_sessionmaker(postgres.engine)
    lease = await PostgresTaskLeaseStore(factory).acquire(
        review_id, owner="worker", task_kind="analyze"
    )
    assert lease is not None
    # A database-valid publish row must not authorize an analysis write even
    # when its snapshot matches and the review is active.
    async with postgres.engine.begin() as connection:
        await connection.execute(
            update(TaskLease)
            .where(TaskLease.review_job_id == review_id)
            .values(task_kind="publish")
        )
    async with SqlAlchemyUnitOfWork(factory) as uow:
        with pytest.raises(LeaseLostError):
            await uow.review_jobs.update_status(
                replace(lease, task_kind="publish"), status=ReviewStatus.FETCHING_DIFF
            )
        await uow.commit()
        saved = await uow.review_jobs.get(review_id)
        assert saved is not None
        assert saved.status == "QUEUED"


async def test_analysis_prepares_publication_in_completion_uow(
    postgres: PostgresHarness, review_id: uuid.UUID
) -> None:
    await _create_publication(
        postgres,
        review_id,
        review_status="LLM_PROCESSING",
        publication_status="NOT_READY",
        deadline=None,
    )
    factory = async_sessionmaker(postgres.engine)
    store = PostgresTaskLeaseStore(factory)
    lease = await store.acquire(review_id, owner="analyzer", task_kind="analyze")
    assert lease is not None
    async with SqlAlchemyUnitOfWork(factory) as uow:
        await uow.review_jobs.update_status(lease, status=ReviewStatus.COMPLETED)
        await uow.publications.prepare(lease)
        await uow.rollback()
    async with SqlAlchemyUnitOfWork(factory) as uow:
        rolled_back = await uow.publications.get(review_id)
        assert rolled_back is not None
        assert rolled_back.status == "NOT_READY"
        assert rolled_back.deadline_at is None
        await uow.review_jobs.update_status(lease, status=ReviewStatus.COMPLETED)
        await uow.publications.prepare(lease)
        await uow.commit()
    assert await store.renew(lease) is None
    assert await store.release(lease)
    async with SqlAlchemyUnitOfWork(factory) as uow:
        publication = await uow.publications.get(review_id)
        assert publication is not None
        assert publication.status == "PENDING"
        assert publication.started_at is not None
        assert publication.deadline_at == publication.started_at + timedelta(minutes=5)
        assert publication.finished_at is None
    publisher = await store.acquire(review_id, owner="publisher", task_kind="publish")
    assert publisher is not None
    assert publisher.fencing_token == 2  # noqa: PLR2004


@pytest.mark.parametrize(
    "case",
    [
        "active",
        "failed",
        "pending",
        "unknown",
        "published",
        "missing",
        "publish_kind",
        "expiry_at_write",
    ],
)
async def test_publication_prepare_requires_analysis_handoff_authority(
    postgres: PostgresHarness, review_id: uuid.UUID, case: str
) -> None:
    factory = async_sessionmaker(postgres.engine)
    store = PostgresTaskLeaseStore(factory)
    lease = await store.acquire(review_id, owner="worker", task_kind="analyze")
    assert lease is not None
    if case == "missing":
        async with SqlAlchemyUnitOfWork(factory) as uow:
            await uow.review_jobs.update_status(lease, status=ReviewStatus.COMPLETED)
            await uow.commit()
    else:
        await _create_publication(
            postgres,
            review_id,
            review_status={"active": "QUEUED", "failed": "FAILED"}.get(
                case, "COMPLETED"
            ),
            publication_status={
                "pending": "PENDING",
                "unknown": "UNKNOWN",
                "published": "PUBLISHED",
                "publish_kind": "PENDING",
            }.get(case, "NOT_READY"),
            deadline=timedelta(minutes=5) if case == "publish_kind" else None,
        )
    if case == "publish_kind":
        assert await store.release(lease)
        publisher = await store.acquire(review_id, owner="worker", task_kind="publish")
        assert publisher is not None
        lease = publisher
    async with SqlAlchemyUnitOfWork(factory) as uow:
        before = await uow.publications.get(review_id)
        if case == "expiry_at_write":
            async with postgres.engine.begin() as connection:
                await connection.execute(
                    update(TaskLease)
                    .where(TaskLease.review_job_id == review_id)
                    .values(expires_at=func.clock_timestamp() + timedelta(seconds=0.3))
                )
            async with postgres.engine.begin() as blocker:
                await blocker.execute(text("LOCK TABLE publication IN SHARE MODE"))
                blocker_pid = await blocker.scalar(select(func.pg_backend_pid()))
                task = asyncio.create_task(uow.publications.prepare(lease))
                await _wait_for_blocked_operation(postgres, blocker_pid)
                await blocker.execute(text("SELECT pg_sleep(0.4)"))
            with pytest.raises(LeaseLostError):
                await task
        else:
            with pytest.raises(LeaseLostError):
                await uow.publications.prepare(lease)
        await uow.commit()
    async with SqlAlchemyUnitOfWork(factory) as uow:
        assert await uow.publications.get(review_id) == before


@pytest.mark.parametrize(
    "status",
    [
        PublicationStatus.PENDING,
        PublicationStatus.PUBLISHED,
        PublicationStatus.PARTIAL,
        PublicationStatus.FAILED,
        PublicationStatus.UNKNOWN,
        PublicationStatus.SKIPPED,
    ],
)
async def test_publisher_persistence_is_atomic_and_preserves_completed_review(
    postgres: PostgresHarness, review_id: uuid.UUID, status: PublicationStatus
) -> None:
    await _create_publication(postgres, review_id)
    factory = async_sessionmaker(postgres.engine)
    store = PostgresTaskLeaseStore(factory)
    lease = await store.acquire(review_id, owner="publisher", task_kind="publish")
    assert lease is not None
    async with SqlAlchemyUnitOfWork(factory) as uow:
        review_before = await uow.review_jobs.get(review_id)
        before = await uow.publications.get(review_id)
        assert before is not None
        await uow.publications.update(
            lease, status=status, provider_metadata={"review_id": "opaque-id"}
        )
        release = asyncio.create_task(store.release(lease))
        await _wait_for_blocked_operation(postgres)
        await uow.rollback()
    assert await release
    async with SqlAlchemyUnitOfWork(factory) as uow:
        assert await uow.publications.get(review_id) == before
    replacement = await store.acquire(review_id, owner="publisher", task_kind="publish")
    assert replacement is not None
    async with SqlAlchemyUnitOfWork(factory) as uow:
        with pytest.raises(LeaseLostError):
            await uow.publications.update(lease, status=status, provider_metadata=None)
        await uow.publications.update(
            replacement, status=status, provider_metadata={"review_id": "opaque-id"}
        )
        await uow.commit()
    async with SqlAlchemyUnitOfWork(factory) as uow:
        saved = await uow.publications.get(review_id)
        assert saved is not None
        assert saved.status == status.value
        assert saved.provider_metadata == {"review_id": "opaque-id"}
        assert saved.deadline_at == before.deadline_at
        assert saved.started_at == before.started_at
        assert (saved.finished_at is None) == (status == PublicationStatus.PENDING)
        assert await uow.review_jobs.get(review_id) == review_before
    assert await store.release(replacement)


@pytest.mark.parametrize(
    "case",
    [
        "analysis_kind",
        "forged_kind",
        "active",
        "failed_review",
        "expired_deadline",
        "invalid_target",
        "missing",
        "UNKNOWN",
        "PUBLISHED",
        "SKIPPED",
        "FAILED",
        "PARTIAL",
        "NOT_READY",
    ],
)
async def test_publication_write_refuses_wrong_authority_or_state(
    postgres: PostgresHarness, review_id: uuid.UUID, case: str
) -> None:
    factory = async_sessionmaker(postgres.engine)
    store = PostgresTaskLeaseStore(factory)
    lease = None
    if case == "analysis_kind":
        lease = await store.acquire(review_id, owner="worker", task_kind="analyze")
    await _create_publication(postgres, review_id)
    if lease is None:
        lease = await store.acquire(review_id, owner="worker", task_kind="publish")
    assert lease is not None
    if case == "forged_kind":
        lease = replace(lease, task_kind="analyze")
    async with postgres.engine.begin() as connection:
        if case in {"active", "failed_review"}:
            await connection.execute(
                update(ReviewJob)
                .where(ReviewJob.id == review_id)
                .values(
                    status="QUEUED" if case == "active" else "FAILED",
                    finished_at=None if case == "active" else func.clock_timestamp(),
                )
            )
        elif case == "expired_deadline":
            await connection.execute(
                update(Publication)
                .where(Publication.review_job_id == review_id)
                .values(deadline_at=func.clock_timestamp() - timedelta(seconds=1))
            )
        elif case == "missing":
            await connection.execute(
                delete(Publication).where(Publication.review_job_id == review_id)
            )
        elif case.isupper():
            await connection.execute(
                update(Publication)
                .where(Publication.review_job_id == review_id)
                .values(status=case)
            )
    async with SqlAlchemyUnitOfWork(factory) as uow:
        before = await uow.publications.get(review_id)
        with pytest.raises(LeaseLostError):
            await uow.publications.update(
                lease,
                status=PublicationStatus.NOT_READY
                if case == "invalid_target"
                else PublicationStatus.PENDING,
                provider_metadata={"review_id": "forbidden"},
            )
        await uow.commit()
    async with SqlAlchemyUnitOfWork(factory) as uow:
        assert await uow.publications.get(review_id) == before


async def _wait_for_database_clock(
    connection: AsyncConnection, cutoff: datetime
) -> None:
    # clock_timestamp is wall time: elapsed sleep alone cannot prove expiry if
    # the host clock moves backwards. Keep the blocker until DB time crosses it.
    async with asyncio.timeout(15):
        while True:
            now = (
                await connection.execute(select(func.clock_timestamp()))
            ).scalar_one()
            if now > cutoff:
                return
            await asyncio.sleep(0.05)


@pytest.mark.parametrize("boundary", ["lease", "deadline"])
@pytest.mark.parametrize("lock_kind", ["row", "table"])
@pytest.mark.parametrize(
    "status",
    [
        PublicationStatus.PENDING,
        PublicationStatus.PUBLISHED,
        PublicationStatus.PARTIAL,
        PublicationStatus.FAILED,
        PublicationStatus.UNKNOWN,
        PublicationStatus.SKIPPED,
    ],
)
async def test_publication_write_rechecks_clock_after_lock_wait(
    postgres: PostgresHarness,
    review_id: uuid.UUID,
    boundary: str,
    lock_kind: str,
    status: PublicationStatus,
) -> None:
    await _create_publication(postgres, review_id)
    factory = async_sessionmaker(postgres.engine)
    lease = await PostgresTaskLeaseStore(factory).acquire(
        review_id, owner="publisher", task_kind="publish"
    )
    assert lease is not None
    async with postgres.engine.begin() as connection:
        if boundary == "lease":
            await connection.execute(
                update(TaskLease)
                .where(TaskLease.review_job_id == review_id)
                .values(expires_at=func.clock_timestamp() + timedelta(seconds=0.3))
            )
        else:
            await connection.execute(
                update(Publication)
                .where(Publication.review_job_id == review_id)
                .values(deadline_at=func.clock_timestamp() + timedelta(seconds=0.3))
            )
        cutoff = (
            await connection.execute(
                select(TaskLease.expires_at).where(TaskLease.review_job_id == review_id)
                if boundary == "lease"
                else select(Publication.deadline_at).where(
                    Publication.review_job_id == review_id
                )
            )
        ).scalar_one()
    async with SqlAlchemyUnitOfWork(factory) as uow:
        before = await uow.publications.get(review_id)
        review_before = await uow.review_jobs.get(review_id)
        async with postgres.engine.begin() as blocker:
            if lock_kind == "table":
                await blocker.execute(text("LOCK TABLE publication IN SHARE MODE"))
            else:
                await blocker.execute(
                    select(Publication)
                    .where(Publication.review_job_id == review_id)
                    .with_for_update()
                )
            blocker_pid = await blocker.scalar(select(func.pg_backend_pid()))
            task = asyncio.create_task(
                uow.publications.update(
                    lease,
                    status=status,
                    provider_metadata={"review_id": "operation-outcome"},
                )
            )
            await _wait_for_blocked_operation(postgres, blocker_pid)
            await _wait_for_database_clock(blocker, cutoff)
        allowed = boundary == "deadline" and status != PublicationStatus.PENDING
        if allowed:
            await task
        else:
            with pytest.raises(LeaseLostError):
                await task
        await uow.commit()
    async with SqlAlchemyUnitOfWork(factory) as uow:
        saved = await uow.publications.get(review_id)
        if allowed:
            assert before is not None and saved is not None
            assert saved.status == status.value
            assert saved.provider_metadata == {"review_id": "operation-outcome"}
            assert saved.deadline_at == before.deadline_at
            assert saved.started_at == before.started_at
            assert saved.finished_at is not None
            assert saved.deadline_at is not None
            assert saved.finished_at > saved.deadline_at
        else:
            assert saved == before
        assert await uow.review_jobs.get(review_id) == review_before


@pytest.mark.parametrize(
    "status",
    [
        PublicationStatus.PUBLISHED,
        PublicationStatus.PARTIAL,
        PublicationStatus.FAILED,
        PublicationStatus.UNKNOWN,
        PublicationStatus.SKIPPED,
    ],
)
async def test_predeadline_operation_can_record_late_outcome_with_live_lease(
    postgres: PostgresHarness, review_id: uuid.UUID, status: PublicationStatus
) -> None:
    await _create_publication(postgres, review_id)
    factory = async_sessionmaker(postgres.engine)
    store = PostgresTaskLeaseStore(factory)
    lease = await store.acquire(review_id, owner="publisher", task_kind="publish")
    assert lease is not None
    async with postgres.engine.begin() as connection:
        await connection.execute(
            update(Publication)
            .where(Publication.review_job_id == review_id)
            .values(deadline_at=func.clock_timestamp() + timedelta(seconds=0.3))
        )
    # Durable progress/preflight succeeds before the simulated external call.
    async with SqlAlchemyUnitOfWork(factory) as uow:
        await uow.publications.update(
            lease,
            status=PublicationStatus.PENDING,
            provider_metadata={"attempt": "in-flight"},
        )
        await uow.commit()
        before = await uow.publications.get(review_id)
        review_before = await uow.review_jobs.get(review_id)
        assert before is not None and before.deadline_at is not None
    # Simulate response/timeout latency without provider I/O or an open UoW.
    async with postgres.engine.connect() as observer:
        await _wait_for_database_clock(observer, before.deadline_at)
    assert await store.acquire(review_id, owner="new", task_kind="publish") is None
    assert await store.renew(lease) is None
    async with SqlAlchemyUnitOfWork(factory) as uow:
        with pytest.raises(LeaseLostError):
            await uow.publications.update(
                lease,
                status=PublicationStatus.PENDING,
                provider_metadata={"attempt": "new"},
            )
        await uow.publications.update(
            lease, status=status, provider_metadata={"attempt": "finished"}
        )
        await uow.commit()
    async with SqlAlchemyUnitOfWork(factory) as uow:
        saved = await uow.publications.get(review_id)
        assert saved is not None
        assert saved.status == status.value
        assert saved.provider_metadata == {"attempt": "finished"}
        assert saved.finished_at is not None and saved.finished_at > before.deadline_at
        assert saved.deadline_at == before.deadline_at
        assert saved.started_at == before.started_at
        assert await uow.review_jobs.get(review_id) == review_before
    assert await store.acquire(review_id, owner="new", task_kind="publish") is None
    assert await store.renew(lease) is None
    assert await store.release(lease)


@pytest.mark.parametrize(
    "case",
    [
        "owner",
        "token",
        "old_token",
        "expired_lease",
        "forged_kind",
        "stored_kind",
        "null_deadline",
        "active_review",
        "missing",
        "UNKNOWN",
        "PUBLISHED",
        "PARTIAL",
        "FAILED",
        "SKIPPED",
        "NOT_READY",
    ],
)
async def test_late_outcome_preserves_ownership_and_state_fencing(
    postgres: PostgresHarness, review_id: uuid.UUID, case: str
) -> None:
    await _create_publication(postgres, review_id)
    factory = async_sessionmaker(postgres.engine)
    store = PostgresTaskLeaseStore(factory)
    lease = await store.acquire(review_id, owner="publisher", task_kind="publish")
    assert lease is not None
    if case == "owner":
        lease = replace(lease, owner="other")
    elif case == "token":
        lease = replace(lease, fencing_token=lease.fencing_token + 1)
    elif case == "old_token":
        assert await store.release(lease)
        assert (
            await store.acquire(review_id, owner="publisher", task_kind="publish")
            is not None
        )
    elif case == "forged_kind":
        lease = replace(lease, task_kind="analyze")
    async with postgres.engine.begin() as connection:
        await connection.execute(
            update(Publication)
            .where(Publication.review_job_id == review_id)
            .values(
                deadline_at=None
                if case == "null_deadline"
                else func.clock_timestamp() - timedelta(seconds=1)
            )
        )
        if case == "expired_lease":
            await connection.execute(
                update(TaskLease)
                .where(TaskLease.review_job_id == review_id)
                .values(expires_at=func.clock_timestamp() - timedelta(seconds=1))
            )
        elif case == "stored_kind":
            await connection.execute(
                update(TaskLease)
                .where(TaskLease.review_job_id == review_id)
                .values(task_kind="analyze")
            )
            lease = replace(lease, task_kind="analyze")
        elif case == "active_review":
            await connection.execute(
                update(ReviewJob)
                .where(ReviewJob.id == review_id)
                .values(status="QUEUED", finished_at=None)
            )
        elif case == "missing":
            await connection.execute(
                delete(Publication).where(Publication.review_job_id == review_id)
            )
        elif case.isupper():
            await connection.execute(
                update(Publication)
                .where(Publication.review_job_id == review_id)
                .values(status=case)
            )
    async with SqlAlchemyUnitOfWork(factory) as uow:
        before = await uow.publications.get(review_id)
        review_before = await uow.review_jobs.get(review_id)
        with pytest.raises(LeaseLostError):
            await uow.publications.update(
                lease,
                status=PublicationStatus.UNKNOWN,
                provider_metadata={"attempt": "forbidden"},
            )
        await uow.commit()
    async with SqlAlchemyUnitOfWork(factory) as uow:
        assert await uow.publications.get(review_id) == before
        assert await uow.review_jobs.get(review_id) == review_before
