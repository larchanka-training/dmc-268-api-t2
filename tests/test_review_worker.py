from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from redis.exceptions import NoPermissionError
from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_postgres_integration import PostgresHarness, _create_outbox_event

from app.adapters.outbox_dispatcher import PostgresOutboxDispatcher
from app.adapters.redis_queue import RedisQueue
from app.adapters.review_worker import PostgresReviewWorker
from app.application.ports import (
    DurableTaskOutcome,
    LeaseLostError,
    QueueTaskEnvelope,
    TaskLeaseSnapshot,
)
from app.db.models import OutboxEvent, Publication, ReviewJob, TaskLease
from app.db.repositories import (
    SqlAlchemyPublicationRepository,
    SqlAlchemyReviewJobRepository,
)
from app.db.task_leases import PostgresTaskLeaseStore
from app.db.uow import SqlAlchemyUnitOfWork
from app.domain.enums import PublicationStatus, ReviewStatus

pytest_plugins = ("test_postgres_integration", "test_redis_queue")


@pytest_asyncio.fixture
async def task(postgres: PostgresHarness) -> AsyncIterator[QueueTaskEnvelope]:
    event_id, review_id = await _create_outbox_event(postgres)
    async with postgres.engine.begin() as connection:
        await connection.execute(
            insert(Publication).values(
                id=postgres.track("publication", uuid.uuid4()),
                review_job_id=review_id,
                status="NOT_READY",
            )
        )
    try:
        yield QueueTaskEnvelope(
            1, str(event_id), str(review_id), "analyze", 2, "trace-outbox-test"
        )
    finally:
        async with postgres.engine.begin() as connection:
            await connection.execute(
                delete(TaskLease).where(TaskLease.review_job_id == review_id)
            )


class SkipAnalysis:
    def __init__(
        self,
        postgres: PostgresHarness,
        *,
        status: ReviewStatus = ReviewStatus.SKIPPED,
        commit: bool = True,
    ) -> None:
        self.factory = async_sessionmaker(postgres.engine)
        self.claims: list[TaskLeaseSnapshot] = []
        self.status = status
        self.commit = commit

    async def handle(
        self, task: QueueTaskEnvelope, lease: TaskLeaseSnapshot
    ) -> DurableTaskOutcome:
        self.claims.append(lease)
        async with SqlAlchemyUnitOfWork(self.factory) as uow:
            await uow.review_jobs.update_status(lease, status=self.status)
            if self.commit:
                await uow.commit()
        return DurableTaskOutcome.COMPLETED


async def test_committed_analysis_decision_is_acked_even_before_outbox_marker(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    task: QueueTaskEnvelope,
) -> None:
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    await queue.enqueue(task)
    handler = SkipAnalysis(postgres)
    worker = PostgresReviewWorker(
        async_sessionmaker(postgres.engine),
        queue,
        handler,
        owner="worker-a",
        task_kind="analyze",
    )

    await worker.run_once()

    async with SqlAlchemyUnitOfWork(handler.factory) as uow:
        review = await uow.review_jobs.get(uuid.UUID(task.review_id))
    assert (
        review is not None
        and review.status == "SKIPPED"
        and review.finished_at is not None
    )
    assert len(handler.claims) == 1
    assert handler.claims[0].owner == "worker-a"
    assert handler.claims[0].task_kind == "analyze"
    assert handler.claims[0].fencing_token == 1
    assert (await redis_client.xpending(f"{stream_prefix}:analyze", "review-workers"))[
        "pending"
    ] == 0


class FinishPublication(SkipAnalysis):
    def __init__(
        self,
        postgres: PostgresHarness,
        *,
        publication_status: PublicationStatus = PublicationStatus.PUBLISHED,
    ) -> None:
        super().__init__(postgres)
        self.publication_status = publication_status

    async def handle(
        self, task: QueueTaskEnvelope, lease: TaskLeaseSnapshot
    ) -> DurableTaskOutcome:
        self.claims.append(lease)
        async with SqlAlchemyUnitOfWork(self.factory) as uow:
            await uow.publications.update(
                lease,
                status=self.publication_status,
                provider_metadata={"test": "opaque-reference"},
            )
            await uow.commit()
        return DurableTaskOutcome.COMPLETED


async def prepare_publish(
    postgres: PostgresHarness, task: QueueTaskEnvelope
) -> QueueTaskEnvelope:
    async with postgres.engine.begin() as connection:
        await connection.execute(
            update(ReviewJob)
            .where(ReviewJob.id == uuid.UUID(task.review_id))
            .values(status="COMPLETED", finished_at=func.clock_timestamp())
        )
        await connection.execute(
            update(Publication)
            .where(Publication.review_job_id == uuid.UUID(task.review_id))
            .values(
                status="PENDING",
                started_at=func.clock_timestamp(),
                deadline_at=func.clock_timestamp() + timedelta(minutes=5),
            )
        )
        await connection.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == uuid.UUID(task.event_id))
            .values(task_kind="publish")
        )
    return replace(task, task_kind="publish")


async def test_eligible_publish_runs_under_publish_lease_and_commits_before_ack(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    task: QueueTaskEnvelope,
) -> None:
    task = await prepare_publish(postgres, task)
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    await queue.enqueue(task)
    handler = FinishPublication(postgres)
    worker = PostgresReviewWorker(
        handler.factory, queue, handler, owner="publisher", task_kind="publish"
    )

    assert await worker.run_once() is True

    async with SqlAlchemyUnitOfWork(handler.factory) as uow:
        publication = await uow.publications.get(uuid.UUID(task.review_id))
        review = await uow.review_jobs.get(uuid.UUID(task.review_id))
    assert (
        publication is not None
        and publication.status == "PUBLISHED"
        and publication.finished_at is not None
    )
    assert review is not None and review.status == "COMPLETED"
    assert [claim.task_kind for claim in handler.claims] == ["publish"]
    assert (await redis_client.xpending(f"{stream_prefix}:publish", "review-workers"))[
        "pending"
    ] == 0


@pytest.mark.parametrize(
    "decision",
    [(ReviewStatus.FETCHING_DIFF, True), (ReviewStatus.SKIPPED, False)],
)
async def test_completion_assertion_without_durable_decision_stays_pending(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    task: QueueTaskEnvelope,
    decision: tuple[ReviewStatus, bool],
) -> None:
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    await queue.enqueue(task)
    handler = SkipAnalysis(postgres, status=decision[0], commit=decision[1])
    worker = PostgresReviewWorker(
        async_sessionmaker(postgres.engine),
        queue,
        handler,
        owner="worker-a",
        task_kind="analyze",
    )

    assert await worker.run_once() is False

    assert (await redis_client.xpending(f"{stream_prefix}:analyze", "review-workers"))[
        "pending"
    ] == 1


@pytest.mark.parametrize(
    "field", ["event_id", "review_id", "task_kind", "attempt", "trace_id", "non_uuid"]
)
async def test_identity_mismatch_is_not_json_dlq_and_never_executes_handler(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    task: QueueTaskEnvelope,
    field: str,
) -> None:
    changes = (
        {"event_id": str(uuid.uuid4())}
        if field == "event_id"
        else (
            {"review_id": str(uuid.uuid4())}
            if field == "review_id"
            else (
                {"task_kind": "publish"}
                if field == "task_kind"
                else (
                    {"attempt": 99}
                    if field == "attempt"
                    else (
                        {"trace_id": "forged"}
                        if field == "trace_id"
                        else {"event_id": "not-a-uuid"}
                    )
                )
            )
        )
    )
    # Valid canonical wire shape, but not the authoritative PostgreSQL command.
    forged = replace(task, **changes)
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    await queue.enqueue(forged)
    handler = SkipAnalysis(postgres)
    worker = PostgresReviewWorker(
        async_sessionmaker(postgres.engine),
        queue,
        handler,
        owner="worker-a",
        task_kind=forged.task_kind,
    )

    with pytest.raises(LookupError, match="authoritative"):
        await worker.run_once()

    assert handler.claims == []
    assert (
        await redis_client.xpending(
            f"{stream_prefix}:{forged.task_kind}", "review-workers"
        )
    )["pending"] == 1
    assert await redis_client.xlen(f"{stream_prefix}:dead") == 0


async def test_invalid_envelope_is_quarantined_by_existing_safe_dlq(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
) -> None:
    await redis_client.xadd(
        f"{stream_prefix}:analyze", {"envelope": "private:not-json"}
    )
    handler = SkipAnalysis(postgres)
    worker = PostgresReviewWorker(
        async_sessionmaker(postgres.engine),
        RedisQueue(redis_client, stream_prefix=stream_prefix),
        handler,
        owner="worker-a",
        task_kind="analyze",
    )

    assert await worker.run_once() is True

    assert handler.claims == []
    assert (await redis_client.xpending(f"{stream_prefix}:analyze", "review-workers"))[
        "pending"
    ] == 0
    dead = await redis_client.xrange(f"{stream_prefix}:dead")
    assert dead is not None and len(dead) == 1
    assert "private" not in str(dead)


@pytest.mark.parametrize("status", [ReviewStatus.COMPLETED, ReviewStatus.PARTIAL])
async def test_useful_terminal_analysis_without_publish_handoff_remains_recoverable(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    task: QueueTaskEnvelope,
    status: ReviewStatus,
) -> None:
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    await queue.enqueue(task)
    handler = SkipAnalysis(postgres, status=status)
    worker = PostgresReviewWorker(
        async_sessionmaker(postgres.engine),
        queue,
        handler,
        owner="worker-a",
        task_kind="analyze",
    )

    assert await worker.run_once() is False

    assert (await redis_client.xpending(f"{stream_prefix}:analyze", "review-workers"))[
        "pending"
    ] == 1


@pytest.mark.parametrize("status", [ReviewStatus.FAILED, ReviewStatus.SKIPPED])
async def test_duplicate_event_on_terminal_analysis_is_acked_without_reopening(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    task: QueueTaskEnvelope,
    status: ReviewStatus,
) -> None:
    async with postgres.engine.begin() as connection:
        await connection.execute(
            update(ReviewJob)
            .where(ReviewJob.id == uuid.UUID(task.review_id))
            .values(status=status.value, finished_at=func.clock_timestamp())
        )
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    await queue.enqueue(task)
    await queue.enqueue(task)
    handler = SkipAnalysis(postgres)
    worker = PostgresReviewWorker(
        async_sessionmaker(postgres.engine),
        queue,
        handler,
        owner="worker-a",
        task_kind="analyze",
    )

    assert await worker.run_once() is True
    assert await worker.run_once() is True

    assert handler.claims == []
    assert (await redis_client.xpending(f"{stream_prefix}:analyze", "review-workers"))[
        "pending"
    ] == 0


class LoseAuthorityAfterCommit(SkipAnalysis):
    def __init__(self, postgres: PostgresHarness, *, replace_owner: bool) -> None:
        super().__init__(postgres)
        self.replace_owner = replace_owner

    async def handle(
        self, task: QueueTaskEnvelope, lease: TaskLeaseSnapshot
    ) -> DurableTaskOutcome:
        outcome = await super().handle(task, lease)
        async with self.factory.begin() as session:
            await session.execute(
                update(TaskLease)
                .where(TaskLease.review_job_id == lease.review_job_id)
                .values(
                    owner="replacement" if self.replace_owner else lease.owner,
                    fencing_token=lease.fencing_token + 1
                    if self.replace_owner
                    else lease.fencing_token,
                    expires_at=func.clock_timestamp()
                    + timedelta(seconds=90 if self.replace_owner else -1),
                )
            )
        return outcome


@pytest.mark.parametrize("replace_owner", [False, True])
async def test_lost_authority_after_commit_cannot_ack_as_current_processing_success(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    task: QueueTaskEnvelope,
    replace_owner: bool,
) -> None:
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    await queue.enqueue(task)
    handler = LoseAuthorityAfterCommit(postgres, replace_owner=replace_owner)
    worker = PostgresReviewWorker(
        handler.factory, queue, handler, owner="worker-a", task_kind="analyze"
    )

    assert await worker.run_once() is False

    assert (await redis_client.xpending(f"{stream_prefix}:analyze", "review-workers"))[
        "pending"
    ] == 1


class CallbackHandler:
    def __init__(
        self,
        callback: Callable[
            [QueueTaskEnvelope, TaskLeaseSnapshot], Awaitable[DurableTaskOutcome | None]
        ],
    ) -> None:
        self.callback = callback

    async def handle(
        self, task: QueueTaskEnvelope, lease: TaskLeaseSnapshot
    ) -> DurableTaskOutcome | None:
        return await self.callback(task, lease)


async def test_handler_error_rolls_back_leaves_pending_and_releases_owned_lease(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    task: QueueTaskEnvelope,
) -> None:
    factory = async_sessionmaker(postgres.engine)

    async def fail(
        task: QueueTaskEnvelope, lease: TaskLeaseSnapshot
    ) -> DurableTaskOutcome | None:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            await uow.review_jobs.update_status(lease, status=ReviewStatus.SKIPPED)
            raise RuntimeError("test failure before commit")

    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    await queue.enqueue(task)
    worker = PostgresReviewWorker(
        factory, queue, CallbackHandler(fail), owner="worker-a", task_kind="analyze"
    )

    with pytest.raises(RuntimeError, match="before commit"):
        await worker.run_once()

    async with SqlAlchemyUnitOfWork(factory) as uow:
        review = await uow.review_jobs.get(uuid.UUID(task.review_id))
    assert review is not None and review.status == "QUEUED"
    assert (await redis_client.xpending(f"{stream_prefix}:analyze", "review-workers"))[
        "pending"
    ] == 1
    replacement = await PostgresTaskLeaseStore(factory).acquire(
        uuid.UUID(task.review_id), owner="worker-b", task_kind="analyze"
    )
    assert replacement is not None and replacement.fencing_token == 2  # noqa: PLR2004 — literal AC


async def test_outbox_quarantine_racing_copied_enqueue_preserves_redis_delivery(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    task: QueueTaskEnvelope,
) -> None:
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    await queue.enqueue(task)
    async with postgres.engine.begin() as connection:
        await connection.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == uuid.UUID(task.event_id))
            .values(
                quarantined_at=func.clock_timestamp(),
                quarantine_reason_code="INVALID_ENVELOPE",
            )
        )
    handler = SkipAnalysis(postgres)
    worker = PostgresReviewWorker(
        handler.factory, queue, handler, owner="worker-a", task_kind="analyze"
    )

    assert await worker.run_once() is False

    assert handler.claims == []
    assert (await redis_client.xpending(f"{stream_prefix}:analyze", "review-workers"))[
        "pending"
    ] == 1
    assert await redis_client.xlen(f"{stream_prefix}:dead") == 0


async def test_identity_is_rechecked_before_durable_completion_ack(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    task: QueueTaskEnvelope,
) -> None:
    handler = SkipAnalysis(postgres)

    async def change_command(
        task: QueueTaskEnvelope, lease: TaskLeaseSnapshot
    ) -> DurableTaskOutcome | None:
        outcome = await handler.handle(task, lease)
        async with handler.factory.begin() as session:
            await session.execute(
                update(OutboxEvent)
                .where(OutboxEvent.id == uuid.UUID(task.event_id))
                .values(trace_id="changed-authoritative-identity")
            )
        return outcome

    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    await queue.enqueue(task)
    worker = PostgresReviewWorker(
        handler.factory,
        queue,
        CallbackHandler(change_command),
        owner="worker-a",
        task_kind="analyze",
    )

    with pytest.raises(LookupError, match="authoritative"):
        await worker.run_once()

    assert (await redis_client.xpending(f"{stream_prefix}:analyze", "review-workers"))[
        "pending"
    ] == 1


@pytest.mark.parametrize(
    "status", [PublicationStatus.PARTIAL, PublicationStatus.SKIPPED]
)
async def test_publish_no_republish_outcomes_are_durable_and_duplicate_safe(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    task: QueueTaskEnvelope,
    status: PublicationStatus,
) -> None:
    task = await prepare_publish(postgres, task)
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    await queue.enqueue(task)
    await queue.enqueue(task)
    handler = FinishPublication(postgres, publication_status=status)
    worker = PostgresReviewWorker(
        handler.factory, queue, handler, owner="publisher", task_kind="publish"
    )

    assert await worker.run_once() is True
    assert await worker.run_once() is True

    assert len(handler.claims) == 1
    async with SqlAlchemyUnitOfWork(handler.factory) as uow:
        publication = await uow.publications.get(uuid.UUID(task.review_id))
    assert publication is not None and publication.status == status.value
    assert (await redis_client.xpending(f"{stream_prefix}:publish", "review-workers"))[
        "pending"
    ] == 0


async def test_quarantine_while_acquisition_waits_is_rechecked_before_handler(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    task: QueueTaskEnvelope,
) -> None:
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    await queue.enqueue(task)
    handler = SkipAnalysis(postgres)
    worker = PostgresReviewWorker(
        handler.factory, queue, handler, owner="worker-a", task_kind="analyze"
    )
    async with postgres.engine.begin() as blocker:
        await blocker.execute(text("LOCK TABLE task_lease IN SHARE MODE"))
        blocker_pid = await blocker.scalar(select(func.pg_backend_pid()))
        running = asyncio.create_task(worker.run_once())
        async with postgres.engine.connect() as observer:
            for _ in range(200):
                await observer.execute(select(func.pg_stat_clear_snapshot()))
                if await observer.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE "
                        ":blocker = ANY(pg_blocking_pids(pid))"
                    ),
                    {"blocker": blocker_pid},
                ):
                    break
                await asyncio.sleep(0.01)
            else:
                raise AssertionError("worker did not wait for acquisition lock")
        async with postgres.engine.begin() as connection:
            await connection.execute(
                update(OutboxEvent)
                .where(OutboxEvent.id == uuid.UUID(task.event_id))
                .values(
                    quarantined_at=func.clock_timestamp(),
                    quarantine_reason_code="INVALID_ENVELOPE",
                )
            )
    assert await asyncio.wait_for(running, timeout=5) is False

    assert handler.claims == []
    assert (await redis_client.xpending(f"{stream_prefix}:analyze", "review-workers"))[
        "pending"
    ] == 1


async def wait_for_blocked_worker(
    postgres: PostgresHarness, blocker_pid: int | None
) -> None:
    async with postgres.engine.connect() as observer:
        for _ in range(200):
            await observer.execute(select(func.pg_stat_clear_snapshot()))
            if await observer.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE "
                    ":blocker = ANY(pg_blocking_pids(pid))"
                ),
                {"blocker": blocker_pid},
            ):
                return
            await asyncio.sleep(0.01)
    raise AssertionError("worker did not wait for PostgreSQL lock")


@pytest.mark.parametrize("change", ["status", "deadline"])
async def test_publish_eligibility_is_rechecked_after_preflight_lock_wait(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    task: QueueTaskEnvelope,
    change: str,
) -> None:
    task = await prepare_publish(postgres, task)
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    await queue.enqueue(task)
    handler = FinishPublication(postgres)
    worker = PostgresReviewWorker(
        handler.factory, queue, handler, owner="publisher", task_kind="publish"
    )
    async with postgres.engine.begin() as acquire_blocker:
        await acquire_blocker.execute(text("LOCK TABLE task_lease IN SHARE MODE"))
        running = asyncio.create_task(worker.run_once())
        await wait_for_blocked_worker(
            postgres, await acquire_blocker.scalar(select(func.pg_backend_pid()))
        )
        async with postgres.engine.begin() as preflight_blocker:
            await preflight_blocker.execute(
                select(OutboxEvent)
                .where(OutboxEvent.id == uuid.UUID(task.event_id))
                .with_for_update()
            )
            await acquire_blocker.commit()
            await wait_for_blocked_worker(
                postgres, await preflight_blocker.scalar(select(func.pg_backend_pid()))
            )
            async with postgres.engine.begin() as connection:
                await connection.execute(
                    update(Publication)
                    .where(Publication.review_job_id == uuid.UUID(task.review_id))
                    .values(
                        **(
                            {"status": "UNKNOWN"}
                            if change == "status"
                            else {
                                "deadline_at": func.clock_timestamp()
                                - timedelta(seconds=1)
                            }
                        )
                    )
                )

    assert await asyncio.wait_for(running, timeout=5) is False
    assert handler.claims == []
    assert (await redis_client.xpending(f"{stream_prefix}:publish", "review-workers"))[
        "pending"
    ] == 1


@asynccontextmanager
async def redis_without_command(
    admin: Redis, prefix: str, denied_command: str
) -> AsyncIterator[Redis]:
    username, password = uuid.uuid4().hex, uuid.uuid4().hex
    await admin.acl_setuser(
        username,
        enabled=True,
        passwords=[f"+{password}"],
        commands=["+@all", f"-{denied_command}"],
        keys=[f"{prefix}:*"],
    )
    try:
        async with Redis.from_url(
            os.environ["DMC268_TEST_REDIS_URL"],
            username=username,
            password=password,
            decode_responses=True,
        ) as client:
            yield client
    finally:
        await admin.acl_deluser(username)


async def test_commit_then_failed_ack_redelivers_original_without_second_effect(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    task: QueueTaskEnvelope,
) -> None:
    handler = SkipAnalysis(postgres)
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    message_id = await queue.enqueue(task)
    async with redis_without_command(redis_client, stream_prefix, "xack") as client:
        worker = PostgresReviewWorker(
            handler.factory,
            RedisQueue(client, stream_prefix=stream_prefix),
            handler,
            owner="worker-a",
            task_kind="analyze",
        )
        with pytest.raises(NoPermissionError, match="xack"):
            await worker.run_once()
    pending = await redis_client.xpending_range(
        f"{stream_prefix}:analyze", "review-workers", "-", "+", 10
    )
    assert [entry["message_id"] for entry in pending] == [message_id]
    # Test-only broker cursor reset redelivers the original Redis ID. Production
    # pending recovery (XAUTOCLAIM) is deliberately deferred to 5B.
    await redis_client.xgroup_setid(f"{stream_prefix}:analyze", "review-workers", "0")
    retry = PostgresReviewWorker(
        handler.factory, queue, handler, owner="worker-b", task_kind="analyze"
    )
    assert await retry.run_once() is True
    assert len(handler.claims) == 1
    assert (await redis_client.xpending(f"{stream_prefix}:analyze", "review-workers"))[
        "pending"
    ] == 0


async def test_failed_dlq_write_preserves_invalid_delivery(
    postgres: PostgresHarness, redis_client: Redis, stream_prefix: str
) -> None:
    await redis_client.xadd(f"{stream_prefix}:analyze", {"envelope": "not-json"})
    handler = SkipAnalysis(postgres)
    async with redis_without_command(redis_client, stream_prefix, "xadd") as client:
        worker = PostgresReviewWorker(
            handler.factory,
            RedisQueue(client, stream_prefix=stream_prefix),
            handler,
            owner="worker-a",
            task_kind="analyze",
        )
        with pytest.raises(NoPermissionError, match="xadd"):
            await worker.run_once()
    assert handler.claims == []
    assert await redis_client.xlen(f"{stream_prefix}:dead") == 0
    assert (await redis_client.xpending(f"{stream_prefix}:analyze", "review-workers"))[
        "pending"
    ] == 1


@pytest.mark.parametrize("replace_owner", [False, True])
async def test_fencing_loss_before_commit_refuses_write_and_preserves_new_owner(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    task: QueueTaskEnvelope,
    replace_owner: bool,
) -> None:
    handler = SkipAnalysis(postgres)
    replacement: TaskLeaseSnapshot | None = None

    async def lose_then_write(
        task: QueueTaskEnvelope, lease: TaskLeaseSnapshot
    ) -> DurableTaskOutcome | None:
        nonlocal replacement
        async with handler.factory.begin() as session:
            await session.execute(
                update(TaskLease)
                .where(TaskLease.review_job_id == lease.review_job_id)
                .values(expires_at=func.clock_timestamp() - timedelta(seconds=1))
            )
        if replace_owner:
            replacement = await PostgresTaskLeaseStore(handler.factory).acquire(
                lease.review_job_id, owner="new-owner", task_kind="analyze"
            )
        return await handler.handle(task, lease)

    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    await queue.enqueue(task)
    worker = PostgresReviewWorker(
        handler.factory,
        queue,
        CallbackHandler(lose_then_write),
        owner="worker-a",
        task_kind="analyze",
    )
    with pytest.raises(LeaseLostError):
        await worker.run_once()
    async with SqlAlchemyUnitOfWork(handler.factory) as uow:
        review = await uow.review_jobs.get(uuid.UUID(task.review_id))
    assert review is not None and review.status == "QUEUED"
    if replace_owner:
        assert replacement is not None
        assert (
            await PostgresTaskLeaseStore(handler.factory).renew(replacement) is not None
        )
    assert (await redis_client.xpending(f"{stream_prefix}:analyze", "review-workers"))[
        "pending"
    ] == 1


async def test_concurrent_workers_execute_duplicate_event_only_once(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    task: QueueTaskEnvelope,
) -> None:
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    for _ in range(8):
        await queue.enqueue(task)
    handler = SkipAnalysis(postgres)
    entered, finish = asyncio.Event(), asyncio.Event()

    async def hold_then_commit(
        task: QueueTaskEnvelope, lease: TaskLeaseSnapshot
    ) -> DurableTaskOutcome | None:
        entered.set()
        await finish.wait()
        return await handler.handle(task, lease)

    workers = [
        PostgresReviewWorker(
            handler.factory,
            queue,
            CallbackHandler(hold_then_commit),
            owner=f"worker-{index}",
            task_kind="analyze",
        )
        for index in range(8)
    ]
    running = [asyncio.create_task(worker.run_once()) for worker in workers]
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        for _ in range(300):
            if sum(item.done() for item in running) == len(workers) - 1:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("competing deliveries did not remain recoverable")
    finally:
        finish.set()
    results = await asyncio.wait_for(asyncio.gather(*running), timeout=5)
    assert results.count(True) == 1
    assert len(handler.claims) == 1
    pending = await redis_client.xpending_range(
        f"{stream_prefix}:analyze", "review-workers", "-", "+", 10
    )
    assert len(pending) == 7  # noqa: PLR2004 — seven recoverable contenders
    assert len({entry["consumer"] for entry in pending}) == len(pending)
    await queue.enqueue(task)
    assert await workers[0].run_once() is True
    assert len(handler.claims) == 1


@pytest.mark.parametrize(
    "condition",
    [
        "not-ready",
        "failed",
        "unknown",
        "expired",
        "no-deadline",
        "no-publication",
        "active-analysis",
        "failed-analysis",
    ],
)
async def test_ineligible_publish_never_runs_handler_or_discards_recovery_work(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    task: QueueTaskEnvelope,
    condition: str,
) -> None:
    task = await prepare_publish(postgres, task)
    async with postgres.engine.begin() as connection:
        predicate = Publication.review_job_id == uuid.UUID(task.review_id)
        if condition == "no-publication":
            await connection.execute(delete(Publication).where(predicate))
        elif condition in {"active-analysis", "failed-analysis"}:
            await connection.execute(
                update(ReviewJob)
                .where(ReviewJob.id == uuid.UUID(task.review_id))
                .values(
                    status="QUEUED" if condition == "active-analysis" else "FAILED",
                    finished_at=None
                    if condition == "active-analysis"
                    else func.clock_timestamp(),
                )
            )
        else:
            values: dict[str, dict[str, object]] = {
                "not-ready": {"status": "NOT_READY"},
                "failed": {"status": "FAILED"},
                "unknown": {"status": "UNKNOWN"},
                "expired": {
                    "deadline_at": func.clock_timestamp() - timedelta(seconds=1)
                },
                "no-deadline": {"deadline_at": None},
            }
            await connection.execute(
                update(Publication).where(predicate).values(**values[condition])
            )
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    await queue.enqueue(task)
    handler = FinishPublication(postgres)
    worker = PostgresReviewWorker(
        handler.factory, queue, handler, owner="publisher", task_kind="publish"
    )
    assert await worker.run_once() is False
    assert handler.claims == []
    assert (await redis_client.xpending(f"{stream_prefix}:publish", "review-workers"))[
        "pending"
    ] == 1


class CompleteAnalysis(SkipAnalysis):
    def __init__(self, postgres: PostgresHarness, status: ReviewStatus) -> None:
        super().__init__(postgres, status=status)
        self.publish_event_id = postgres.track("outbox_event", uuid.uuid4())
        self.publish_trace_id: str | None = None

    async def handle(
        self, task: QueueTaskEnvelope, lease: TaskLeaseSnapshot
    ) -> DurableTaskOutcome:
        self.claims.append(lease)
        async with self.factory.begin() as session:
            await SqlAlchemyReviewJobRepository(session).update_status(
                lease, status=self.status
            )
            await SqlAlchemyPublicationRepository(session).prepare(lease)
            session.add(
                OutboxEvent(
                    id=self.publish_event_id,
                    review_job_id=lease.review_job_id,
                    schema_version=1,
                    task_kind="publish",
                    attempt=1,
                    trace_id=task.trace_id
                    if self.publish_trace_id is None
                    else self.publish_trace_id,
                )
            )
        return DurableTaskOutcome.COMPLETED


@pytest.mark.parametrize("status", [ReviewStatus.COMPLETED, ReviewStatus.PARTIAL])
async def test_atomic_analysis_handoff_is_dispatchable_and_publishable(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    task: QueueTaskEnvelope,
    status: ReviewStatus,
) -> None:
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    await queue.enqueue(task)
    analyzer = CompleteAnalysis(postgres, status)
    worker = PostgresReviewWorker(
        analyzer.factory, queue, analyzer, owner="analyzer", task_kind="analyze"
    )
    assert await worker.run_once() is True
    # Original event marker is still NULL, so dispatcher sends its duplicate
    # alongside the atomically committed publication command.
    dispatched = await PostgresOutboxDispatcher(
        analyzer.factory, queue
    ).dispatch_batch()
    assert dispatched == 2  # noqa: PLR2004 — original and publish commands
    assert await worker.run_once() is True
    assert len(analyzer.claims) == 1
    publisher = FinishPublication(postgres)
    publish_worker = PostgresReviewWorker(
        publisher.factory, queue, publisher, owner="publisher", task_kind="publish"
    )
    assert await publish_worker.run_once() is True
    assert publisher.claims[0].fencing_token == 2  # noqa: PLR2004 — second grant
    async with SqlAlchemyUnitOfWork(publisher.factory) as uow:
        publication = await uow.publications.get(uuid.UUID(task.review_id))
        review = await uow.review_jobs.get(uuid.UUID(task.review_id))
    assert publication is not None and publication.status == "PUBLISHED"
    assert review is not None and review.status == status.value


async def test_successful_callback_without_explicit_outcome_keeps_delivery_pending(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    task: QueueTaskEnvelope,
) -> None:
    handler = SkipAnalysis(postgres)

    async def no_confirmation(
        task: QueueTaskEnvelope, lease: TaskLeaseSnapshot
    ) -> DurableTaskOutcome | None:
        await handler.handle(task, lease)
        return None

    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    await queue.enqueue(task)
    worker = PostgresReviewWorker(
        handler.factory,
        queue,
        CallbackHandler(no_confirmation),
        owner="worker-a",
        task_kind="analyze",
    )
    assert await worker.run_once() is False
    assert (await redis_client.xpending(f"{stream_prefix}:analyze", "review-workers"))[
        "pending"
    ] == 1


@pytest.mark.parametrize(
    "trace_id",
    ["x" * 17000, "я" * 9000, "\\" * 9000],
    ids=["ascii", "utf8", "json-escaping"],
)
async def test_undeliverable_publish_handoff_cannot_authorize_analysis_ack(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    task: QueueTaskEnvelope,
    trace_id: str,
) -> None:
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    await queue.enqueue(task)
    handler = CompleteAnalysis(postgres, ReviewStatus.COMPLETED)
    handler.publish_trace_id = trace_id
    worker = PostgresReviewWorker(
        handler.factory, queue, handler, owner="analyzer", task_kind="analyze"
    )
    assert await worker.run_once() is False
    assert (await redis_client.xpending(f"{stream_prefix}:analyze", "review-workers"))[
        "pending"
    ] == 1


@pytest.mark.parametrize("kind", ["analyze", "publish"])
async def test_handler_keeps_lease_live_through_automatic_heartbeat(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    task: QueueTaskEnvelope,
    kind: str,
) -> None:
    if kind == "publish":
        task = await prepare_publish(postgres, task)
    async with postgres.engine.begin() as connection:
        await connection.execute(
            update(ReviewJob)
            .where(ReviewJob.id == uuid.UUID(task.review_id))
            .values(
                analysis_deadline_at=func.clock_timestamp() + timedelta(minutes=10),
                retry_at=func.clock_timestamp() + timedelta(minutes=1),
            )
        )
    entered, finish = asyncio.Event(), asyncio.Event()
    handler = (
        FinishPublication(postgres) if kind == "publish" else SkipAnalysis(postgres)
    )
    state = (
        select(
            ReviewJob.status,
            ReviewJob.retry_at,
            ReviewJob.queue_deadline_at,
            ReviewJob.analysis_deadline_at,
            OutboxEvent.attempt,
            Publication.deadline_at,
        )
        .join(OutboxEvent, OutboxEvent.review_job_id == ReviewJob.id)
        .join(Publication, Publication.review_job_id == ReviewJob.id)
        .where(OutboxEvent.id == uuid.UUID(task.event_id))
    )
    async with postgres.engine.connect() as connection:
        before = (await connection.execute(state)).one()

    async def hold(
        task: QueueTaskEnvelope, lease: TaskLeaseSnapshot
    ) -> DurableTaskOutcome | None:
        entered.set()
        await finish.wait()
        return await handler.handle(task, lease)

    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    await queue.enqueue(task)
    worker = PostgresReviewWorker(
        handler.factory,
        queue,
        CallbackHandler(hold),
        owner="worker-a",
        task_kind=task.task_kind,
        heartbeat_interval=0.01,
    )
    baseline = asyncio.all_tasks()
    running = asyncio.create_task(worker.run_once())
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        # Shorten a real live DB lease; no 90-second wall-clock wait or fake renew.
        async with postgres.engine.begin() as connection:
            cutoff = await connection.scalar(
                update(TaskLease)
                .where(TaskLease.review_job_id == uuid.UUID(task.review_id))
                .values(expires_at=func.clock_timestamp() + timedelta(seconds=30))
                .returning(TaskLease.expires_at)
            )
        assert cutoff is not None
        async with asyncio.timeout(3):
            while True:
                async with postgres.engine.connect() as connection:
                    expiry = await connection.scalar(
                        select(TaskLease.expires_at).where(
                            TaskLease.review_job_id == uuid.UUID(task.review_id)
                        )
                    )
                if expiry is not None and expiry > cutoff + timedelta(seconds=30):
                    break
                await asyncio.sleep(0.01)
        assert (
            await PostgresTaskLeaseStore(handler.factory).acquire(
                uuid.UUID(task.review_id), owner="worker-b", task_kind=task.task_kind
            )
            is None
        )
        async with postgres.engine.connect() as connection:
            assert (await connection.execute(state)).one() == before
        assert (
            await redis_client.xpending(
                f"{stream_prefix}:{task.task_kind}", "review-workers"
            )
        )["pending"] == 1
        finish.set()
        assert await asyncio.wait_for(running, timeout=5) is True
        assert asyncio.all_tasks() <= baseline
        expiry_query = select(TaskLease.expires_at).where(
            TaskLease.review_job_id == uuid.UUID(task.review_id)
        )
        async with postgres.engine.connect() as connection:
            released_at = await connection.scalar(expiry_query)
        await asyncio.sleep(0.05)
        async with postgres.engine.connect() as connection:
            assert await connection.scalar(expiry_query) == released_at
    finally:
        finish.set()
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)


@pytest.mark.parametrize(
    "lost_field", ["owner", "fencing_token", "task_kind", "expiry"]
)
async def test_heartbeat_loss_cancels_handler_and_fences_late_persistence(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    task: QueueTaskEnvelope,
    lost_field: str,
) -> None:
    entered, cancelled, fenced = asyncio.Event(), asyncio.Event(), asyncio.Event()
    handler = SkipAnalysis(postgres)

    async def external_call(
        task: QueueTaskEnvelope, lease: TaskLeaseSnapshot
    ) -> DurableTaskOutcome | None:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            # Model a provider returning a late result despite cancellation.
            try:
                await handler.handle(task, lease)
            except LeaseLostError:
                fenced.set()
            return DurableTaskOutcome.COMPLETED
        return None

    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    await queue.enqueue(task)
    worker = PostgresReviewWorker(
        handler.factory,
        queue,
        CallbackHandler(external_call),
        owner="worker-a",
        task_kind="analyze",
        heartbeat_interval=0.01,
    )
    running = asyncio.create_task(worker.run_once())
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        changes: dict[str, object] = {
            "owner": "replacement",
            "fencing_token": 2,
            "task_kind": "publish",
            "expiry": func.clock_timestamp() - timedelta(minutes=5),
        }
        field = "expires_at" if lost_field == "expiry" else lost_field
        async with postgres.engine.begin() as connection:
            await connection.execute(
                update(TaskLease)
                .where(TaskLease.review_job_id == uuid.UUID(task.review_id))
                .values(**{field: changes[lost_field]})
            )
        with pytest.raises(LeaseLostError):
            await asyncio.wait_for(asyncio.shield(running), timeout=3)
        assert cancelled.is_set() and fenced.is_set()
        async with postgres.engine.connect() as connection:
            token = await connection.scalar(
                select(TaskLease.fencing_token).where(
                    TaskLease.review_job_id == uuid.UUID(task.review_id)
                )
            )
        assert token == (2 if lost_field == "fencing_token" else 1)
        async with SqlAlchemyUnitOfWork(handler.factory) as uow:
            review = await uow.review_jobs.get(uuid.UUID(task.review_id))
        assert review is not None and review.status == "QUEUED"
        assert (
            await redis_client.xpending(f"{stream_prefix}:analyze", "review-workers")
        )["pending"] == 1
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)


async def test_terminal_commit_racing_heartbeat_still_allows_durable_ack(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    task: QueueTaskEnvelope,
) -> None:
    committed, finish = asyncio.Event(), asyncio.Event()
    handler = SkipAnalysis(postgres)

    async def finish_after_commit(
        task: QueueTaskEnvelope, lease: TaskLeaseSnapshot
    ) -> DurableTaskOutcome | None:
        outcome = await handler.handle(task, lease)
        committed.set()
        await finish.wait()
        return outcome

    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    await queue.enqueue(task)
    worker = PostgresReviewWorker(
        handler.factory,
        queue,
        CallbackHandler(finish_after_commit),
        owner="worker-a",
        task_kind="analyze",
        heartbeat_interval=0.01,
    )
    running = asyncio.create_task(worker.run_once())
    try:
        await asyncio.wait_for(committed.wait(), timeout=5)
        # Leave the completed handler alive across multiple heartbeat ticks.
        await asyncio.wait((running,), timeout=0.1)
        finish.set()
        assert await asyncio.wait_for(running, timeout=5) is True
        assert (
            await redis_client.xpending(f"{stream_prefix}:analyze", "review-workers")
        )["pending"] == 0
    finally:
        finish.set()
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)


async def test_postgres_renewal_error_cancels_handler_and_never_acks(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    task: QueueTaskEnvelope,
) -> None:
    entered, cancelled = asyncio.Event(), asyncio.Event()
    factory = async_sessionmaker(postgres.engine)

    async def external_call(
        task: QueueTaskEnvelope, lease: TaskLeaseSnapshot
    ) -> DurableTaskOutcome | None:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            # Even a provider that returns COMPLETED cannot override renewal error.
            return DurableTaskOutcome.COMPLETED
        return None

    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    await queue.enqueue(task)
    worker = PostgresReviewWorker(
        factory,
        queue,
        CallbackHandler(external_call),
        owner="worker-a",
        task_kind="analyze",
        heartbeat_interval=0.01,
    )
    # Real PostgreSQL error at the UPDATE, scoped to this fixture row. The release
    # sets expiry to now, so it remains usable for cleanup after the renewal fails.
    trigger = f"renewal_failure_{uuid.uuid4().hex}"
    async with postgres.engine.begin() as connection:
        await connection.execute(
            text(
                f"CREATE FUNCTION {trigger}() RETURNS trigger LANGUAGE plpgsql AS $$ "
                f"BEGIN IF NEW.review_job_id = '{task.review_id}'::uuid AND "
                "NEW.owner = OLD.owner AND NEW.fencing_token = OLD.fencing_token AND "
                "NEW.expires_at > clock_timestamp() + interval '60 seconds' THEN "
                "RAISE EXCEPTION 'test PostgreSQL renewal failure'; END IF; "
                "RETURN NEW; END $$"
            )
        )
        await connection.execute(
            text(
                f"CREATE TRIGGER {trigger} BEFORE UPDATE ON task_lease "
                f"FOR EACH ROW EXECUTE FUNCTION {trigger}()"
            )
        )
    baseline = asyncio.all_tasks()
    running = asyncio.create_task(worker.run_once())
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        with pytest.raises(DBAPIError, match="test PostgreSQL renewal failure"):
            await asyncio.wait_for(asyncio.shield(running), timeout=5)
        assert cancelled.is_set()
        assert asyncio.all_tasks() <= baseline
        assert (
            await redis_client.xpending(f"{stream_prefix}:analyze", "review-workers")
        )["pending"] == 1
        replacement = await PostgresTaskLeaseStore(factory).acquire(
            uuid.UUID(task.review_id), owner="worker-b", task_kind="analyze"
        )
        assert replacement is not None and replacement.fencing_token == 2  # noqa: PLR2004
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        async with postgres.engine.begin() as connection:
            await connection.execute(text(f"DROP TRIGGER {trigger} ON task_lease"))
            await connection.execute(text(f"DROP FUNCTION {trigger}()"))


async def test_worker_cancellation_joins_blocked_heartbeat_before_release(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    task: QueueTaskEnvelope,
) -> None:
    entered, cancelled = asyncio.Event(), asyncio.Event()
    factory = async_sessionmaker(postgres.engine)

    async def external_call(
        task: QueueTaskEnvelope, lease: TaskLeaseSnapshot
    ) -> DurableTaskOutcome | None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return None

    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    await queue.enqueue(task)
    worker = PostgresReviewWorker(
        factory,
        queue,
        CallbackHandler(external_call),
        owner="worker-a",
        task_kind="analyze",
        heartbeat_interval=0.05,
    )
    baseline = asyncio.all_tasks()
    running = asyncio.create_task(worker.run_once())
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        async with postgres.engine.begin() as blocker:
            await blocker.execute(text("LOCK TABLE task_lease IN SHARE MODE"))
            pid = await blocker.scalar(select(func.pg_backend_pid()))
            await wait_for_blocked_worker(postgres, pid)
            running.cancel()
            await asyncio.wait_for(cancelled.wait(), timeout=5)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(running, timeout=5)
        assert asyncio.all_tasks() <= baseline
        assert (
            await redis_client.xpending(f"{stream_prefix}:analyze", "review-workers")
        )["pending"] == 1
        replacement = await PostgresTaskLeaseStore(factory).acquire(
            uuid.UUID(task.review_id), owner="worker-b", task_kind="analyze"
        )
        assert replacement is not None and replacement.fencing_token == 2  # noqa: PLR2004
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
