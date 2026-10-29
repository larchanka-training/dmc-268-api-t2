"""Bounded HTTP characterization; ASGI durations include BackgroundTasks."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
import platform
import time
import uuid
from collections import Counter
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Any

import httpx
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_postgres_integration import PostgresHarness, _create_repository

from app.api.github_webhook import get_github_client, get_webhook_inbox
from app.core.config import Settings, Webhook, get_settings
from app.db import models
from app.db.webhook_inbox import SqlAlchemyWebhookInbox
from app.main import create_app
from app.vcs.github import GithubClient

pytest_plugins = ("test_postgres_integration",)

_UNIQUE_REQUESTS = 32
_UNIQUE_READS = 128
_CAPTURE_READS = 4
_SECRET = b"qa-03a-test-only-secret"
_BASE_SHA = "b" * 40
_HEAD_SHA = "a" * 40
_MERGE_SHA = "c" * 40
_RAW_DIFF = (
    "diff --git a/app.py b/app.py\nindex 1234567..7654321 100644\n"
    "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-before\n+after\n"
)


@dataclass
class LoadScenario:
    postgres: PostgresHarness
    repository_id: uuid.UUID
    external_id: int
    repository: dict[str, Any]
    pull: dict[str, Any]
    client: httpx.AsyncClient
    manifest: set[str] = field(default_factory=set)
    reads: list[tuple[str, str]] = field(default_factory=list)

    def delivery(self, delivery_id: str, action: str) -> tuple[bytes, dict[str, str]]:
        self.manifest.add(delivery_id)
        body = json.dumps(
            {
                "action": action,
                "number": 7,
                "repository": self.repository,
                "pull_request": self.pull,
            }
        ).encode()
        return body, {
            "Content-Type": "application/json",
            "X-GitHub-Delivery": delivery_id,
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": "sha256="
            + hmac.new(_SECRET, body, hashlib.sha256).hexdigest(),
        }

    async def receipts(self) -> list[models.WebhookReceipt]:
        # Also find unexpected IDs saved for this unique repository. Registration
        # runs again in fixture teardown even if a request or assertion failed.
        async with async_sessionmaker(self.postgres.engine)() as session:
            rows = list(
                await session.scalars(
                    select(models.WebhookReceipt).where(
                        models.WebhookReceipt.provider_name == "github",
                        or_(
                            models.WebhookReceipt.delivery_id.in_(self.manifest),
                            models.WebhookReceipt.request_data["request"][
                                "repository_external_id"
                            ].as_string()
                            == str(self.external_id),
                        ),
                    )
                )
            )
        for row in rows:
            self.postgres.track("webhook_receipt", row.id)
        return rows


@pytest_asyncio.fixture
async def load_scenario(postgres: PostgresHarness) -> AsyncIterator[LoadScenario]:
    external_id = uuid.uuid4().int
    async with postgres.engine.begin() as connection:
        repository_id = await _create_repository(
            postgres, connection, external_id=str(external_id)
        )
    repository: dict[str, Any] = {
        "id": external_id,
        "name": f"repository-{repository_id.hex}",
        "owner": {"login": "postgres-integration-test"},
    }
    pull = {
        "number": 7,
        "state": "open",
        "title": "QA-03A bounded capture",
        "body": "Test-only diff",
        "base": {"sha": _BASE_SHA, "ref": "main", "repo": repository},
        "head": {"sha": _HEAD_SHA, "ref": "feature", "repo": repository},
    }
    prefix = f"/repos/{repository['owner']['login']}/{repository['name']}"
    reads: list[tuple[str, str]] = []

    def github_http(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.host == "api.github.com"
        key = (request.url.path, request.headers["accept"])
        reads.append(key)
        if key == (f"{prefix}/pulls/7", "application/vnd.github+json"):
            return httpx.Response(200, json=pull)
        if key == (
            f"{prefix}/compare/{_BASE_SHA}...{_HEAD_SHA}",
            "application/vnd.github+json",
        ):
            return httpx.Response(200, json={"merge_base_commit": {"sha": _MERGE_SHA}})
        assert key == (
            f"{prefix}/compare/{_MERGE_SHA}...{_HEAD_SHA}",
            "application/vnd.github.diff",
        ), key
        return httpx.Response(200, text=_RAW_DIFF)

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        webhook=Webhook(secret=SecretStr(_SECRET.decode()))
    )
    inbox = SqlAlchemyWebhookInbox(async_sessionmaker(postgres.engine))
    app.dependency_overrides[get_webhook_inbox] = lambda: inbox
    async with httpx.AsyncClient(transport=httpx.MockTransport(github_http)) as github:
        app.dependency_overrides[get_github_client] = lambda: GithubClient(github)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://qa03a.test"
        ) as client:
            scenario = LoadScenario(
                postgres,
                repository_id,
                external_id,
                repository,
                pull,
                client,
                reads=reads,
            )
            try:
                yield scenario
            finally:
                await scenario.receipts()


@dataclass
class Burst:
    outcomes: list[httpx.Response | BaseException]
    durations: list[float]
    elapsed: float
    concurrency: int


async def _burst(
    scenario: LoadScenario,
    deliveries: list[tuple[bytes, dict[str, str]]],
    concurrency: int,
) -> Burst:
    semaphore = asyncio.Semaphore(concurrency)
    durations: list[float] = []

    async def send(body: bytes, headers: dict[str, str]) -> httpx.Response:
        async with semaphore:
            started = time.perf_counter()
            try:
                # ASGITransport returns after the real BackgroundTasks complete.
                return await scenario.client.post(
                    "/api/v1/webhooks/github", content=body, headers=headers
                )
            finally:
                durations.append(time.perf_counter() - started)

    started = time.perf_counter()
    tasks = [asyncio.create_task(send(*delivery)) for delivery in deliveries]
    try:
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        # Drain every task before fixture teardown, including on cancellation.
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return Burst(outcomes, durations, time.perf_counter() - started, concurrency)


def _metrics(
    burst: Burst, receipts: list[models.WebhookReceipt], scenario: LoadScenario
) -> None:
    durations = sorted(burst.durations)
    statuses = Counter(
        outcome.status_code
        for outcome in burst.outcomes
        if isinstance(outcome, httpx.Response)
    )
    print(
        "QA03A "
        + json.dumps(
            {
                "requests": len(burst.outcomes),
                "concurrency": burst.concurrency,
                "unique_deliveries": len(scenario.manifest),
                "github_reads": len(scenario.reads),
                "elapsed_s": burst.elapsed,
                "completed_requests_per_second": sum(statuses.values()) / burst.elapsed,
                "p50_ms": durations[math.ceil(len(durations) * 0.50) - 1] * 1000,
                "p95_ms": durations[math.ceil(len(durations) * 0.95) - 1] * 1000,
                "http_statuses": dict(statuses),
                "exceptions": sum(
                    isinstance(outcome, BaseException) for outcome in burst.outcomes
                ),
                "receipts": len(receipts),
                "ready": sum(row.processing_status == "READY" for row in receipts),
                "python": platform.python_version(),
                "platform": platform.system(),
                "transport": "ASGITransport (includes BackgroundTasks)",
                "db_pool": type(scenario.postgres.engine.pool).__name__,
            },
            sort_keys=True,
        )
    )


def _assert_capture(row: models.WebhookReceipt, scenario: LoadScenario) -> None:
    assert row.processing_status == "READY"
    assert row.attempts == 1
    assert row.reason_code is None
    assert row.raw_diff == _RAW_DIFF
    snapshot = row.snapshot_data
    assert snapshot is not None
    captured_at = snapshot["snapshot"]["captured_at"]
    assert datetime.fromisoformat(captured_at).tzinfo == UTC
    assert snapshot == {
        "schema_version": "1.0",
        "context_level": 1,
        "snapshot": {
            "provider": "github",
            "base_repository_id": str(scenario.external_id),
            "head_repository_id": str(scenario.external_id),
            "base_sha": _BASE_SHA,
            "merge_base_sha": _MERGE_SHA,
            "head_sha": _HEAD_SHA,
            "captured_at": captured_at,
            "provider_metadata": {
                "pull_number": 7,
                "diff_version": f"{_BASE_SHA}:{_MERGE_SHA}:{_HEAD_SHA}",
            },
        },
        "metadata": {
            "title": "QA-03A bounded capture",
            "body": "Test-only diff",
            "base_ref": "main",
            "head_ref": "feature",
            "commit_messages": [],
            "languages": [],
            "discussions": [],
        },
        "files": [
            {
                "diff": {
                    "file_id": "file_1",
                    "old_path": "app.py",
                    "new_path": "app.py",
                    "status": "modified",
                    "old_blob_sha": None,
                    "new_blob_sha": None,
                    "language": None,
                    "hunks": [
                        {
                            "hunk_id": "file_1_hunk_1",
                            "old_start": 1,
                            "old_count": 1,
                            "new_start": 1,
                            "new_count": 1,
                            "lines": [
                                {
                                    "kind": "deleted",
                                    "text": "before",
                                    "old_line": 1,
                                    "new_line": None,
                                },
                                {
                                    "kind": "added",
                                    "text": "after",
                                    "old_line": None,
                                    "new_line": 1,
                                },
                            ],
                        }
                    ],
                },
                "source_windows": [],
                "whole_file": None,
            }
        ],
        "coverage": {
            "included": [
                {"path": "app.py", "ranges": [{"start_line": 1, "end_line": 1}]}
            ],
            "skipped": [],
            "context_quality": "diff_only",
        },
    }


async def _assert_no_review_work(scenario: LoadScenario) -> None:
    async with async_sessionmaker(scenario.postgres.engine)() as session:
        assert (
            list(
                await session.scalars(
                    select(models.ReviewJob.id).where(
                        models.ReviewJob.repository_id == scenario.repository_id
                    )
                )
            )
            == []
        )
        assert (
            list(
                await session.scalars(
                    select(models.OutboxEvent.id)
                    .join(models.ReviewJob)
                    .where(models.ReviewJob.repository_id == scenario.repository_id)
                )
            )
            == []
        )


async def test_concurrent_unique_deliveries(load_scenario: LoadScenario) -> None:
    scenario = load_scenario
    expected_ids = {str(uuid.uuid4()) for _ in range(_UNIQUE_REQUESTS)}
    deliveries = [
        scenario.delivery(delivery_id, "opened" if index % 2 == 0 else "synchronize")
        for index, delivery_id in enumerate(sorted(expected_ids))
    ]
    burst = await _burst(scenario, deliveries, concurrency=4)
    receipts = await scenario.receipts()
    _metrics(burst, receipts, scenario)
    assert all(isinstance(outcome, httpx.Response) for outcome in burst.outcomes), (
        burst.outcomes
    )
    assert [
        outcome.status_code
        for outcome in burst.outcomes
        if isinstance(outcome, httpx.Response)
    ] == [HTTPStatus.ACCEPTED] * _UNIQUE_REQUESTS
    assert len(receipts) == _UNIQUE_REQUESTS
    assert {row.delivery_id for row in receipts} == expected_ids
    for row in receipts:
        _assert_capture(row, scenario)
    assert len(scenario.reads) == _UNIQUE_READS
    await _assert_no_review_work(scenario)


async def test_concurrent_duplicate_deliveries(load_scenario: LoadScenario) -> None:
    scenario = load_scenario
    delivery_id = str(uuid.uuid4())
    delivery = scenario.delivery(delivery_id, "opened")
    burst = await _burst(scenario, [delivery] * 16, concurrency=8)
    receipts = await scenario.receipts()
    _metrics(burst, receipts, scenario)
    assert all(isinstance(outcome, httpx.Response) for outcome in burst.outcomes), (
        burst.outcomes
    )
    assert [
        outcome.status_code
        for outcome in burst.outcomes
        if isinstance(outcome, httpx.Response)
    ] == [HTTPStatus.ACCEPTED] * 16
    assert len(receipts) == 1
    assert {row.delivery_id for row in receipts} == {delivery_id}
    _assert_capture(receipts[0], scenario)
    assert len(scenario.reads) == _CAPTURE_READS
    await _assert_no_review_work(scenario)
