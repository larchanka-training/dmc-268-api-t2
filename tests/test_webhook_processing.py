from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from http import HTTPStatus

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_postgres_integration import PostgresHarness, _create_repository

from app.api.github_webhook import get_github_client, get_webhook_inbox
from app.application.github_ports import GithubRequest
from app.application.use_cases.process_webhook import process_webhook
from app.core.config import Settings, Webhook, get_settings
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
