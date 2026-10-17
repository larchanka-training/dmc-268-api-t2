"""Pending recovery survives Redis tombstones and consumer group resets."""

import uuid

import httpx
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_analysis_gateway import analysis_task
from test_analysis_handler import github_http
from test_analysis_pending_recovery import snapshot
from test_analysis_recovery import paid_response, production_handler
from test_analysis_terminal import freeze, ledger_cleanup  # noqa: F401 -- fixture
from test_postgres_integration import PostgresHarness
from test_review_worker import redis_without_command

from app.adapters.redis_queue import RedisQueue
from app.adapters.review_worker import PostgresReviewWorker
from app.db.worker_tasks import PostgresWorkerTasks

pytest_plugins = ("test_analysis_handler",)


async def test_worker_recovery_tombstone_never_executes_or_acknowledges(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    review_id: uuid.UUID,
) -> None:
    factory = async_sessionmaker(postgres.engine, expire_on_commit=False)
    await freeze(factory, review_id)
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    message_id = await queue.enqueue(await analysis_task(factory, review_id))
    assert await queue.receive(task_kind="analyze", consumer_name="crashed")
    await redis_client.xdel(f"{stream_prefix}:analyze", message_id)
    assert (await redis_client.xpending(f"{stream_prefix}:analyze", "review-workers"))[
        "pending"
    ] == 1
    before = await snapshot(factory, review_id)

    def forbidden(request: httpx.Request) -> httpx.Response:
        raise AssertionError("deleted Redis entry must never execute HTTP")

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as http,
        redis_without_command(redis_client, stream_prefix, "xack") as denied,
    ):
        worker = PostgresReviewWorker(
            factory,
            RedisQueue(denied, stream_prefix=stream_prefix),
            production_handler(factory, http, http),
            owner="tombstone-recovery",
            task_kind="analyze",
        )
        # Redis removes the tombstone itself; an explicit ACK would fail the ACL.
        assert await worker.recover_once(cursor="0-0", min_idle_time=0) == (
            "0-0",
            False,
        )
    assert await snapshot(factory, review_id) == before
    assert (await redis_client.xpending(f"{stream_prefix}:analyze", "review-workers"))[
        "pending"
    ] == 0
    assert await redis_client.xlen(f"{stream_prefix}:dead") == 0


async def test_group_reset_redelivers_completed_task_without_http_or_new_debit(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    review_id: uuid.UUID,
) -> None:
    factory = async_sessionmaker(postgres.engine, expire_on_commit=False)
    await freeze(factory, review_id)
    task = await analysis_task(factory, review_id)
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    message_id = await queue.enqueue(task)
    calls: list[httpx.Request] = []
    async with (
        httpx.AsyncClient(transport=github_http()) as github,
        httpx.AsyncClient(transport=paid_response(calls)) as http,
    ):
        first = PostgresReviewWorker(
            factory,
            queue,
            production_handler(factory, github, http),
            owner="before-reset",
            task_kind="analyze",
        )
        assert await first.run_once() is True
    assert len(calls) == 1
    assert await PostgresWorkerTasks(factory).completed(task)
    committed = await snapshot(factory, review_id)
    assert len(committed["review_llm_attempt"]) == 1
    assert committed["review_llm_attempt"][0]["state"] == "SUCCEEDED"

    def forbidden(request: httpx.Request) -> httpx.Response:
        raise AssertionError("group reset must not repeat completed task HTTP")

    assert await redis_client.xgroup_destroy(
        f"{stream_prefix}:analyze", "review-workers"
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as http:
        restarted = PostgresReviewWorker(
            factory,
            queue,
            production_handler(factory, http, http),
            owner="after-reset",
            task_kind="analyze",
        )
        # A reset drops the PEL. Recovery recreates the group; its unread history
        # then uses the existing new-delivery path and full completion proof.
        assert await restarted.recover_once(cursor="0-0", min_idle_time=0) == (
            "0-0",
            False,
        )
        assert await restarted.run_once() is True
        assert await restarted.run_once() is False
    assert await snapshot(factory, review_id) == committed
    assert (await redis_client.xpending(f"{stream_prefix}:analyze", "review-workers"))[
        "pending"
    ] == 0
    entries = await redis_client.xrange(f"{stream_prefix}:analyze")
    assert entries is not None and [entry[0] for entry in entries] == [message_id]
    assert await redis_client.xlen(f"{stream_prefix}:dead") == 0
