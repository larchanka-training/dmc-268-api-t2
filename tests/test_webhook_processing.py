from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid
from collections.abc import AsyncIterator, Sequence
from http import HTTPStatus

import httpx
import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import RowMapping, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from test_postgres_integration import PostgresHarness, _create_repository

from app.api.github_webhook import get_github_client, get_webhook_inbox
from app.application.github_ports import GithubRequest
from app.application.use_cases.process_webhook import process_webhook
from app.core.config import Settings, Webhook, get_settings
from app.db.base import Base
from app.db.models import OutboxEvent, ReviewJob, WebhookReceipt
from app.db.webhook_inbox import SqlAlchemyWebhookInbox
from app.main import create_app
from app.vcs.github import GithubClient
from app.webhook_worker import run_once

pytest_plugins = ("test_postgres_integration",)

_EXPECTED_GITHUB_READS = 4
RAW_DIFF = (
    "diff --git a/app.py b/app.py\nindex 1234567..7654321 100644\n"
    "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-before\n+after\n"
)
PULL = {
    "number": 7,
    "state": "open",
    "title": "Small change",
    "body": "Description",
    "head": {"sha": "a" * 40, "ref": "feature", "repo": {"id": 456}},
    "base": {"sha": "b" * 40, "ref": "main", "repo": {"id": 123}},
}


def github_transport(request: httpx.Request) -> httpx.Response:
    if "/pulls/" in request.url.path:
        return httpx.Response(200, json=PULL)
    if request.headers["accept"] == "application/vnd.github.diff":
        return httpx.Response(200, text=RAW_DIFF)
    return httpx.Response(200, json={"merge_base_commit": {"sha": "c" * 40}})


@pytest_asyncio.fixture
async def backlog_postgres() -> AsyncIterator[PostgresHarness]:
    database_url = os.environ["DMC268_TEST_DATABASE_URL"]
    # The identifier is generated here, never supplied by a caller or environment.
    schema = f"webhook_backlog_test_{uuid.uuid4().hex}"
    admin = create_async_engine(database_url, poolclass=NullPool)
    engine = None
    schema_created = False
    try:
        async with admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        schema_created = True
        engine = create_async_engine(
            database_url,
            poolclass=NullPool,
            connect_args={"server_settings": {"search_path": schema}},
        )
        async with engine.begin() as connection:
            assert (
                await connection.execute(text("SHOW search_path"))
            ).scalar_one() == schema
            assert (
                await connection.execute(text("SELECT current_schema()"))
            ).scalar_one() == schema
            await connection.run_sync(Base.metadata.create_all)
        yield PostgresHarness(engine)
    finally:
        try:
            if engine is not None:
                await engine.dispose()
            if schema_created:
                async with admin.begin() as connection:
                    await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        finally:
            await admin.dispose()


@pytest.mark.asyncio
async def test_recovery_runner_drains_101_pending_receipts(  # noqa: PLR0915 - explicit three-batch characterization
    backlog_postgres: PostgresHarness,
) -> None:
    backlog_size, batch_size = 101, 100
    first_batch_reads, total_reads = 400, 404
    async with backlog_postgres.engine.begin() as connection:
        await _create_repository(backlog_postgres, connection, external_id="123")
    inbox = SqlAlchemyWebhookInbox(async_sessionmaker(backlog_postgres.engine))
    expected_deliveries: dict[uuid.UUID, str] = {}
    for _ in range(backlog_size):
        delivery_id = str(uuid.uuid4())
        receipt_id = await inbox.accept(
            delivery_id,
            GithubRequest("owner", "repo", "123", 7, "a" * 40, "b" * 40),
        )
        assert receipt_id is not None
        backlog_postgres.track("webhook_receipt", receipt_id)
        expected_deliveries[receipt_id] = delivery_id
    assert len(expected_deliveries) == backlog_size
    assert len(set(expected_deliveries.values())) == backlog_size

    async def receipts() -> Sequence[RowMapping]:
        async with backlog_postgres.engine.connect() as connection:
            return (
                (
                    await connection.execute(
                        select(WebhookReceipt.__table__).order_by(
                            WebhookReceipt.received_at, WebhookReceipt.id
                        )
                    )
                )
                .mappings()
                .all()
            )

    before = await receipts()
    assert {row["id"]: row["delivery_id"] for row in before} == expected_deliveries
    assert all(row["processing_status"] == "PENDING" for row in before)
    assert all(row["attempts"] == 0 for row in before)
    assert all(
        row["snapshot_data"] is None and row["raw_diff"] is None for row in before
    )
    first_batch = {row["id"] for row in before[:batch_size]}
    remaining = {before[batch_size]["id"]}

    reads: list[str] = []

    def capture(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        reads.append(request.url.path)
        return github_transport(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(capture)) as github:
        assert await run_once(inbox, GithubClient(github)) == batch_size
        after_first = await receipts()
        assert {
            row["id"]: row["delivery_id"] for row in after_first
        } == expected_deliveries
        assert {
            row["id"] for row in after_first if row["processing_status"] == "READY"
        } == first_batch
        assert {
            row["id"] for row in after_first if row["processing_status"] == "PENDING"
        } == remaining
        assert all(
            row["attempts"] == (1 if row["id"] in first_batch else 0)
            for row in after_first
        )
        assert len(reads) == first_batch_reads

        assert await run_once(inbox, GithubClient(github)) == 1
        after_second = await receipts()
        assert {
            row["id"]: row["delivery_id"] for row in after_second
        } == expected_deliveries
        for row in after_second:
            assert row["processing_status"] == "READY"
            assert row["attempts"] == 1
            assert row["raw_diff"] == RAW_DIFF
            assert row["reason_code"] is None
            assert row["lease_until"] is None
            assert row["retry_at"] is None
            snapshot = row["snapshot_data"]
            assert snapshot is not None
            assert snapshot["schema_version"] == "1.0"
            assert snapshot["context_level"] == 1
            assert snapshot["snapshot"]["base_sha"] == "b" * 40
            assert snapshot["snapshot"]["head_sha"] == "a" * 40
            assert snapshot["snapshot"]["merge_base_sha"] == "c" * 40
            assert snapshot["metadata"]["title"] == PULL["title"]
            assert len(snapshot["files"]) == 1
            assert snapshot["files"][0]["diff"]["hunks"][0]["lines"] == [
                {"kind": "deleted", "text": "before", "old_line": 1, "new_line": None},
                {"kind": "added", "text": "after", "old_line": None, "new_line": 1},
            ]
            assert snapshot["coverage"]["context_quality"] == "diff_only"
        assert len(reads) == total_reads

        assert await run_once(inbox, GithubClient(github)) == 0
        assert await receipts() == after_second
        assert len(reads) == total_reads

    async with backlog_postgres.engine.connect() as connection:
        assert (await connection.execute(select(ReviewJob.id))).all() == []
        assert (await connection.execute(select(OutboxEvent.id))).all() == []


@pytest.mark.asyncio
async def test_processor_captures_and_persists_level1(
    postgres: PostgresHarness,
) -> None:

    async with postgres.engine.begin() as connection:
        await _create_repository(postgres, connection, external_id="123")
    inbox = SqlAlchemyWebhookInbox(async_sessionmaker(postgres.engine))
    receipt_id = await inbox.accept(
        str(uuid.uuid4()), GithubRequest("owner", "repo", "123", 7, "a" * 40, "b" * 40)
    )
    assert receipt_id is not None
    postgres.track("webhook_receipt", receipt_id)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(github_transport)
    ) as client:
        await process_webhook(receipt_id, inbox, GithubClient(client))
    receipt = await inbox.get(receipt_id)
    assert receipt is not None and receipt.status == "READY"
    assert receipt.raw_diff == RAW_DIFF
    assert receipt.snapshot is not None
    assert receipt.snapshot["snapshot"]["merge_base_sha"] == "c" * 40
    assert receipt.snapshot["metadata"]["title"] == "Small change"
    assert receipt.snapshot["files"][0]["diff"]["hunks"][0]["lines"] == [
        {"kind": "deleted", "text": "before", "old_line": 1, "new_line": None},
        {"kind": "added", "text": "after", "old_line": None, "new_line": 1},
    ]
    assert receipt.snapshot["coverage"]["context_quality"] == "diff_only"


@pytest.mark.asyncio
async def test_http_does_not_acknowledge_unpersisted_delivery() -> None:

    engine = create_async_engine(
        "postgresql+asyncpg://unused:unused@127.0.0.1:1/unused",
        connect_args={"timeout": 0.2},
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        webhook=Webhook(secret=SecretStr("test-secret"))
    )
    app.dependency_overrides[get_webhook_inbox] = lambda: SqlAlchemyWebhookInbox(
        async_sessionmaker(engine)
    )
    body = json.dumps(
        {
            "action": "opened",
            "number": 7,
            "repository": {"id": 123, "name": "repo", "owner": {"login": "owner"}},
            "pull_request": PULL,
        }
    ).encode()
    headers = {
        "X-GitHub-Delivery": str(uuid.uuid4()),
        "X-GitHub-Event": "pull_request",
        "X-Hub-Signature-256": "sha256="
        + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest(),
    }
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/v1/webhooks/github", content=body, headers=headers
            )
        assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
        assert response.json() == {
            "code": "SERVICE_UNAVAILABLE",
            "message": "Service is temporarily unavailable.",
            "request_id": response.headers["X-Request-Id"],
            "retryable": True,
            "retry_after": 30,
        }
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_processor_stores_safe_vcs_failure(postgres: PostgresHarness) -> None:

    async with postgres.engine.begin() as connection:
        await _create_repository(postgres, connection, external_id="123")
    inbox = SqlAlchemyWebhookInbox(async_sessionmaker(postgres.engine))
    receipt_id = await inbox.accept(
        str(uuid.uuid4()), GithubRequest("owner", "repo", "123", 7, "a" * 40, "b" * 40)
    )
    assert receipt_id is not None
    postgres.track("webhook_receipt", receipt_id)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(403, text="sensitive response")
        )
    ) as client:
        await process_webhook(receipt_id, inbox, GithubClient(client))
    receipt = await inbox.get(receipt_id)
    assert receipt is not None and receipt.status == "FAILED"
    assert receipt.reason_code == "VCS_ACCESS_DENIED"
    assert receipt.snapshot is None and receipt.raw_diff is None


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["opened", "synchronize"])
async def test_signed_http_delivery_captures_once(
    postgres: PostgresHarness, action: str
) -> None:

    async with postgres.engine.begin() as connection:
        await _create_repository(postgres, connection, external_id="123")
    inbox = SqlAlchemyWebhookInbox(async_sessionmaker(postgres.engine))
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        webhook=Webhook(secret=SecretStr("test-secret"))
    )
    # Replace external persistence/HTTP edges, keeping endpoint and processor real.

    reads: list[str] = []

    def capture(request: httpx.Request) -> httpx.Response:
        reads.append(request.url.path)
        return github_transport(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(capture)) as github:
        app.dependency_overrides[get_webhook_inbox] = lambda: inbox
        app.dependency_overrides[get_github_client] = lambda: GithubClient(github)
        body = json.dumps(
            {
                "action": action,
                "number": 7,
                "repository": {"id": 123, "name": "repo", "owner": {"login": "owner"}},
                "pull_request": PULL,
            }
        ).encode()
        delivery_id = str(uuid.uuid4())
        headers = {
            "X-GitHub-Delivery": delivery_id,
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": "sha256="
            + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest(),
        }
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/v1/webhooks/github", content=body, headers=headers
            )
            assert response.status_code == HTTPStatus.ACCEPTED
            receipt = await inbox.get_by_delivery(delivery_id)
            assert receipt is not None and receipt.status == "READY"
            postgres.track("webhook_receipt", receipt.id)
            assert receipt.raw_diff == RAW_DIFF
            assert len(reads) == _EXPECTED_GITHUB_READS
            duplicate = await client.post(
                "/api/v1/webhooks/github", content=body, headers=headers
            )
            assert duplicate.status_code == HTTPStatus.ACCEPTED
            assert len(reads) == _EXPECTED_GITHUB_READS


@pytest.mark.asyncio
async def test_recovery_runner_processes_durable_pending_work(
    postgres: PostgresHarness,
) -> None:

    async with postgres.engine.begin() as connection:
        await _create_repository(postgres, connection, external_id="123")
    inbox = SqlAlchemyWebhookInbox(async_sessionmaker(postgres.engine))
    receipt_id = await inbox.accept(
        str(uuid.uuid4()), GithubRequest("owner", "repo", "123", 7, "a" * 40, "b" * 40)
    )
    assert receipt_id is not None
    postgres.track("webhook_receipt", receipt_id)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(github_transport)
    ) as github:
        assert await run_once(inbox, GithubClient(github)) >= 1
    receipt = await inbox.get(receipt_id)
    assert receipt is not None and receipt.status == "READY"


@pytest.mark.asyncio
async def test_processor_refuses_truncated_diff(postgres: PostgresHarness) -> None:

    async with postgres.engine.begin() as connection:
        await _create_repository(postgres, connection, external_id="123")
    inbox = SqlAlchemyWebhookInbox(async_sessionmaker(postgres.engine))
    receipt_id = await inbox.accept(
        str(uuid.uuid4()), GithubRequest("owner", "repo", "123", 7, "a" * 40, "b" * 40)
    )
    assert receipt_id is not None
    postgres.track("webhook_receipt", receipt_id)

    def malformed(request: httpx.Request) -> httpx.Response:
        if request.headers["accept"] == "application/vnd.github.diff":
            return httpx.Response(
                200, text=RAW_DIFF.replace("@@ -1 +1 @@", "@@ -1,8 +1,8 @@")
            )
        return github_transport(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(malformed)) as client:
        await process_webhook(receipt_id, inbox, GithubClient(client))
    receipt = await inbox.get(receipt_id)
    assert receipt is not None and receipt.status == "FAILED"
    assert receipt.reason_code == "DIFF_UNTRUSTWORTHY"
    assert receipt.snapshot is None and receipt.raw_diff is None
