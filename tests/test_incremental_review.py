"""Incremental review through the analysis worker seam (dmc-268-ui-t2#33).

Each scenario runs two review cycles on one PR with a counting fake LLM:
the second cycle must send only fragments the first one did not review.
"""

import uuid
from dataclasses import replace
from typing import Any, cast

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs
from test_analysis_handler import BASE, MERGE, DeterministicLlm
from test_contracts import SCHEMAS
from test_postgres_integration import PostgresHarness

from app.adapters.analysis_handler import PostgresAnalysisHandler
from app.adapters.outbox_dispatcher import PostgresOutboxDispatcher
from app.adapters.redis_queue import RedisQueue
from app.adapters.review_worker import PostgresReviewWorker
from app.application.ports import StartReviewCommand
from app.application.use_cases.start_review import start_review
from app.db.models import (
    ChangeRequest,
    ChunkResult,
    ContextPayload,
    Finding,
    OutboxEvent,
    Publication,
    ReviewEvent,
    ReviewJob,
    TaskLease,
)
from app.db.uow import SqlAlchemyUnitOfWork
from app.review.incremental import IncrementalPolicy
from app.vcs.github import GithubClient

pytest_plugins = ("test_analysis_handler",)
FIRST_HEAD, SECOND_HEAD = "a" * 40, "d" * 40
FUNCTIONS = 10


def _module(changed: int | None = None) -> list[str]:
    lines: list[str] = []
    for n in range(FUNCTIONS):
        body = f"    return x * {n}" if n == changed else f"    return x + {n}"
        lines += [f"def f{n}(x):", body, ""]
    return lines[:-1]


def _added(path: str, lines: list[str]) -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "index 0000000..2222222\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n" + "".join(f"+{line}\n" for line in lines)
    )


def _github(raw_diff: str, head: str, ancestry: str) -> httpx.MockTransport:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(f"/compare/{FIRST_HEAD}...{head}"):
            return httpx.Response(200, json={"status": ancestry})
        if "/pulls/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "title": "Incremental",
                    "body": None,
                    "state": "open",
                    "head": {"repo": {"id": 43}, "sha": head, "ref": "feature"},
                    "base": {"repo": {"id": 42}, "sha": BASE, "ref": "main"},
                },
            )
        if request.headers["accept"] == "application/vnd.github.diff":
            return httpx.Response(200, text=raw_diff)
        return httpx.Response(200, json={"merge_base_commit": {"sha": MERGE}})

    return httpx.MockTransport(respond)


class _Cycles:
    def __init__(self, postgres: PostgresHarness, redis: Redis, prefix: str) -> None:
        self.factory = async_sessionmaker(postgres.engine)
        self.queue = RedisQueue(redis, stream_prefix=prefix)
        self.postgres = postgres
        self.llm = DeterministicLlm()
        self.llm.result = replace(self.llm.result, findings=[])
        self.extra: list[uuid.UUID] = []
        self.delta_modes: list[str] = []
        self.events: list[Any] = []  # structlog events of the last run
        self.follow_up: Any = None
        self.incremental: IncrementalPolicy | None = IncrementalPolicy()
        self.cache: Any = None

    async def run(self, raw_diff: str, head: str, ancestry: str = "ahead") -> None:
        await PostgresOutboxDispatcher(self.factory, self.queue).dispatch_batch()
        transport = _github(raw_diff, head, ancestry)
        with capture_logs() as events:
            await self._run(transport)
        self.events = events
        self.delta_modes += [
            e["mode"] for e in events if e.get("metric") == "delta_mode"
        ]

    async def _run(self, transport: httpx.MockTransport) -> None:
        async with httpx.AsyncClient(transport=transport) as client:
            handler = PostgresAnalysisHandler(
                self.factory,
                GithubClient(client),
                self.llm,
                schemas_dir=SCHEMAS,
                incremental=self.incremental,
                follow_up=self.follow_up,
                cache=self.cache,
            )
            worker = PostgresReviewWorker(
                self.factory, self.queue, handler, owner="analysis", task_kind="analyze"
            )
            assert await worker.run_once() is True

    async def push(
        self,
        first: uuid.UUID,
        head: str,
        *,
        published: str = "PUBLISHED",
        **changes: Any,
    ) -> uuid.UUID:
        """The first cycle's publication outcome, then a new head on the same PR
        and a new review cycle for it."""
        async with self.factory.begin() as session:
            job = await session.get(ReviewJob, first)
            assert job is not None
            await session.execute(
                update(Publication)
                .where(Publication.review_job_id == first)
                .values(status=published, finished_at=func.now())
            )
            await session.execute(
                update(ChangeRequest)
                .where(ChangeRequest.id == job.change_request_id)
                .values(head_sha=head)
            )
            command = StartReviewCommand(
                job.change_request_id,
                job.repository_settings_id,
                head,
                job.rules_digest,
                "manual",
                None,
                job.model_ref,
                job.model_digest,
                job.model_settings,
                job.prompt_version,
                job.prompt_digest,
                "trace-incremental",
                rerun_of=first,
                frozen_configuration_version=1,
            )
        review = await start_review(
            replace(command, **changes), unit_of_work=SqlAlchemyUnitOfWork(self.factory)
        )
        self.postgres.track("review_job", review)
        self.extra.append(review)
        return review

    async def job(self, review: uuid.UUID) -> ReviewJob:
        async with self.factory() as session:
            job = await session.get(ReviewJob, review)
        assert job is not None
        return job

    def sent_lines(self, call: int) -> dict[str, list[str]]:
        files = cast(
            list[dict[str, Any]], self.llm.payloads[call].payload_body["files"]
        )
        return {
            file["diff"]["new_path"]: [
                line["text"]
                for hunk in file["diff"]["hunks"]
                for line in hunk["lines"]
                if line["kind"] != "context"
            ]
            for file in files
        }

    async def purge(self) -> None:
        async with self.factory.begin() as session:
            for review in self.extra:
                await _purge(session, review)


async def _purge(session: AsyncSession, review: uuid.UUID) -> None:
    chunks = select(ChunkResult.id).where(ChunkResult.review_job_id == review)
    await session.execute(delete(Finding).where(Finding.chunk_result_id.in_(chunks)))
    for table in (
        ChunkResult,
        ContextPayload,
        ReviewEvent,
        OutboxEvent,
        Publication,
        TaskLease,
    ):
        await session.execute(delete(table).where(table.review_job_id == review))


async def test_one_changed_function_of_ten_costs_one_call_for_it_alone(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    review_id: uuid.UUID,
) -> None:
    """DoD 1: a new commit changing one of ten reviewed functions."""
    cycles = _Cycles(postgres, redis_client, stream_prefix)
    try:
        await cycles.run(_added("src/m.py", _module()), FIRST_HEAD)
        second = await cycles.push(review_id, SECOND_HEAD)
        await cycles.run(_added("src/m.py", _module(changed=3)), SECOND_HEAD)

        assert len(cycles.llm.payloads) == 2  # noqa: PLR2004 -- one per cycle
        assert cycles.sent_lines(1) == {"src/m.py": ["def f3(x):", "    return x * 3"]}
        assert cycles.delta_modes == ["full", "incremental"]
        job = await cycles.job(second)
        assert job.status == "COMPLETED"
        assert [item["detail"] for item in job.coverage["skipped"]] == [
            "already_reviewed"
        ]
    finally:
        await cycles.purge()


async def test_rebase_without_code_changes_calls_no_llm(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    review_id: uuid.UUID,
) -> None:
    """DoD 5: force-push after a rebase that changed no code."""
    cycles = _Cycles(postgres, redis_client, stream_prefix)
    try:
        await cycles.run(_added("src/m.py", _module()), FIRST_HEAD)
        second = await cycles.push(review_id, SECOND_HEAD)
        await cycles.run(_added("src/m.py", _module()), SECOND_HEAD, "diverged")

        assert len(cycles.llm.payloads) == 1
        assert cycles.delta_modes == ["full", "force_push"]
        job = await cycles.job(second)
        assert job.status == "SKIPPED"
        assert job.coverage["included"] == []
    finally:
        await cycles.purge()


async def test_force_push_with_one_changed_hunk_sends_only_that_hunk(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    review_id: uuid.UUID,
) -> None:
    """DoD 5: force-push where exactly one hunk differs."""
    first = _added("src/a.py", _module()) + _added("src/b.py", ["value = 1"])
    second_diff = _added("src/a.py", _module()) + _added("src/b.py", ["value = 2"])
    cycles = _Cycles(postgres, redis_client, stream_prefix)
    try:
        await cycles.run(first, FIRST_HEAD)
        await cycles.push(review_id, SECOND_HEAD)
        await cycles.run(second_diff, SECOND_HEAD)

        assert len(cycles.llm.payloads) == 2  # noqa: PLR2004 -- one per cycle
        assert cycles.sent_lines(1) == {"src/b.py": ["value = 2"]}
    finally:
        await cycles.purge()


async def test_changed_review_configuration_reviews_everything_again(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    review_id: uuid.UUID,
) -> None:
    """A prior review under another prompt/model/rules digest does not count."""
    cycles = _Cycles(postgres, redis_client, stream_prefix)
    try:
        await cycles.run(_added("src/m.py", _module()), FIRST_HEAD)
        await cycles.push(review_id, SECOND_HEAD, prompt_digest="prompt-digest-v2")
        await cycles.run(_added("src/m.py", _module()), SECOND_HEAD)

        assert len(cycles.llm.payloads) == 2  # noqa: PLR2004 -- one per cycle
        assert cycles.sent_lines(1) == cycles.sent_lines(0)
    finally:
        await cycles.purge()


@pytest.mark.parametrize("published", ["SKIPPED", "PENDING", "FAILED"])
async def test_prior_review_never_published_does_not_count(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    review_id: uuid.UUID,
    published: str,
) -> None:
    """A head moved mid-flight leaves the prior result unpublished (stale): its
    findings never reached the PR, so the next cycle reviews everything."""
    cycles = _Cycles(postgres, redis_client, stream_prefix)
    try:
        await cycles.run(_added("src/m.py", _module()), FIRST_HEAD)
        await cycles.push(review_id, SECOND_HEAD, published=published)
        await cycles.run(_added("src/m.py", _module(changed=3)), SECOND_HEAD)

        assert cycles.sent_lines(1)["src/m.py"] == _module(changed=3)
        assert cycles.delta_modes == ["full", "full"]
    finally:
        await cycles.purge()


class _RecordingFollowUp:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[uuid.UUID] = []
        self.fail = fail

    async def after_terminal(self, review_id: uuid.UUID) -> object:
        self.calls.append(review_id)
        if self.fail:
            raise RuntimeError("follow-up admission crashed")
        return None


@pytest.mark.parametrize("fail", [False, True])
async def test_terminal_analysis_triggers_one_follow_up_without_blocking_ack(
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    review_id: uuid.UUID,
    fail: bool,
) -> None:
    cycles = _Cycles(postgres, redis_client, stream_prefix)
    cycles.follow_up = _RecordingFollowUp(fail=fail)

    await cycles.run(_added("src/m.py", _module()), FIRST_HEAD)

    assert cycles.follow_up.calls == [review_id]
    assert (await cycles.job(review_id)).status == "COMPLETED"
    pending = await redis_client.xpending(f"{stream_prefix}:analyze", "review-workers")
    assert pending["pending"] == 0
