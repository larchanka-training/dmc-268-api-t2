# ruff: noqa: PLR2004 -- fixed protocol and bounded retry contract vectors
"""Automatic admission through signed webhook, inbox, frozen config and PostgreSQL."""

import asyncio
import hashlib
import hmac
import inspect
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy import delete, event, func, insert, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from test_repository_settings_api import SettingsScope, replace_settings
from test_runtime_manifest import PROMPTS, manifest_values, write_manifest

from app.adapters.automatic_review_admission import AutomaticReviewAdmission
from app.api.github_webhook import get_webhook_inbox
from app.application.github_ports import GithubRequest
from app.application.webhook_ports import AutomaticReceiptContext
from app.core.config import get_settings
from app.db import models
from app.db.webhook_inbox import SqlAlchemyWebhookInbox
from app.review.prompt import PromptBuilder, ReflectionBundle
from app.vcs.github import GithubClient
from app.webhook_worker import run_once

pytest_plugins = ("test_repositories_api", "test_repository_settings_api")
HEAD = "a" * 40
BASE = "b" * 40
RAW_DIFF = (
    "diff --git a/app.py b/app.py\nindex 1234567..7654321 100644\n"
    "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-before\n+after\n"
)


@dataclass
class AutomaticScope:
    settings: SettingsScope
    inbox: SqlAlchemyWebhookInbox
    pull: dict[str, Any]
    provider: Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]]
    requests: list[httpx.Request]

    async def dispatch(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        value = self.provider(request)
        return await value if inspect.isawaitable(value) else value

    async def send(self, delivery: str | None = None) -> httpx.Response:
        body = json.dumps(
            {
                "action": "opened",
                "number": 7,
                "repository": {
                    "id": self.pull["base"]["repo"]["id"],
                    "name": "settings",
                    "owner": {"login": "team"},
                },
                "pull_request": self.pull,
                "sender": {"id": 999, "login": "untrusted-sender"},
            }
        ).encode()
        return await self.settings.catalog.client.post(
            "/api/v1/webhooks/github",
            content=body,
            headers={
                "X-GitHub-Delivery": delivery or str(uuid4()),
                "X-GitHub-Event": "pull_request",
                "X-Hub-Signature-256": "sha256="
                + hmac.new(b"synthetic-hook-secret", body, hashlib.sha256).hexdigest(),
            },
        )


@pytest_asyncio.fixture
async def automatic_scope(
    settings_scope: SettingsScope, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> AsyncIterator[AutomaticScope]:
    scope = settings_scope
    saved = await replace_settings(
        scope,
        auto_review_enabled=True,
        base_branches=["main"],
        minimum_severity="medium",
    )
    assert saved.status_code == HTTPStatus.OK
    engine = scope.catalog.postgres.engine
    factory = async_sessionmaker(engine, expire_on_commit=False)
    inbox = SqlAlchemyWebhookInbox(factory)
    async with engine.begin() as connection:
        external_id = await connection.scalar(
            select(models.Repository.external_id).where(
                models.Repository.id == scope.repository
            )
        )
        assert external_id is not None
        key = Fernet.generate_key()
        await connection.execute(
            insert(models.GithubIdentity).values(
                user_id=scope.catalog.user,
                external_id=str(scope.catalog.user.int),
                login="authorizing-admin",
                encrypted_token=Fernet(key)
                .encrypt(b"synthetic-personal-token")
                .decode(),
            )
        )
    values = manifest_values()
    admission = values["admission"]
    admission.update(
        prompt_version="v4",
        prompt_digest=PromptBuilder.load(PROMPTS, "v4").prompt_digest,
    )
    admission["policy"].update(
        schema_version=2,
        reflection={
            "max_reflections_per_run": 1,
            "max_reflections_per_chunk": 1,
            "failure_mode": "fail_open",
            "verifier_version": "v1",
            "verifier_digest": ReflectionBundle.load(
                PROMPTS / "reflection", "v1"
            ).digest,
        },
    )
    monkeypatch.setenv("ANALYSIS__CONFIG_PATH", str(write_manifest(tmp_path, values)))
    monkeypatch.setenv("AUTH__CREDENTIAL_KEY", key.decode())
    monkeypatch.setenv("GITHUB__TOKEN", "synthetic-bot-read-token")
    monkeypatch.setenv("WEBHOOK__SECRET", "synthetic-hook-secret")
    get_settings.cache_clear()
    scope.catalog.app.dependency_overrides[get_webhook_inbox] = lambda: inbox
    monkeypatch.setattr("app.api.github_webhook.async_session_factory", factory)
    monkeypatch.setattr(
        "app.api.review_start_dependencies.async_session_factory", factory
    )
    pull = {
        "number": 7,
        "state": "open",
        "title": "Automatic review",
        "body": "Synthetic change",
        "head": {"sha": HEAD, "ref": "feature", "repo": {"id": 501}},
        "base": {"sha": BASE, "ref": "main", "repo": {"id": int(external_id)}},
    }
    requests: list[httpx.Request] = []

    def provider(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.github.com"
        assert request.method == "GET", "Intake must not publish or call a model"
        if request.headers.get("accept") == "application/vnd.github.diff":
            return httpx.Response(200, text=RAW_DIFF)
        if "/pulls/7" in request.url.path:
            return httpx.Response(200, json=deepcopy(pull))
        if "/compare/" in request.url.path:
            return httpx.Response(200, json={"merge_base_commit": {"sha": "c" * 40}})
        if "/contents/" in request.url.path:
            return httpx.Response(404)
        if "/commits/" in request.url.path:
            return httpx.Response(200, json={"sha": BASE})
        return httpx.Response(
            200,
            json={
                "id": int(external_id),
                "name": "settings",
                "owner": {"login": "team"},
                "permissions": {"pull": True, "admin": True},
            },
        )

    state = AutomaticScope(scope, inbox, pull, provider, requests)

    monkeypatch.setattr(
        "httpx._client.AsyncHTTPTransport",
        lambda **kwargs: httpx.MockTransport(state.dispatch),
    )
    try:
        yield state
    finally:
        get_settings.cache_clear()
        async with engine.begin() as connection:
            ids = select(models.ReviewJob.id).where(
                models.ReviewJob.repository_id == scope.repository
            )
            await connection.execute(
                delete(models.WebhookReceipt).where(
                    models.WebhookReceipt.request_data["request"][
                        "repository_external_id"
                    ].astext
                    == external_id
                )
            )
            for table in (
                models.ReviewEvent,
                models.IdempotencyRecord,
                models.OutboxEvent,
                models.Publication,
            ):
                await connection.execute(
                    delete(table).where(table.review_job_id.in_(ids))
                )
            await connection.execute(
                delete(models.QuotaUsage).where(
                    models.QuotaUsage.repository_id == scope.repository
                )
            )
            await connection.execute(
                delete(models.ReviewJob).where(
                    models.ReviewJob.repository_id == scope.repository
                )
            )
            await connection.execute(
                delete(models.ChangeRequest).where(
                    models.ChangeRequest.repository_id == scope.repository
                )
            )
            await connection.execute(
                delete(models.GithubIdentity).where(
                    models.GithubIdentity.user_id == scope.catalog.user
                )
            )


async def test_signed_opt_in_delivery_atomically_admits_existing_review_aggregate(
    automatic_scope: AutomaticScope,
) -> None:
    scope = automatic_scope
    delivery = str(uuid4())
    response = await scope.send(delivery)
    assert response.status_code == HTTPStatus.ACCEPTED
    receipt = await scope.inbox.get_by_delivery(delivery)
    assert receipt is not None and receipt.status == "READY"
    factory = async_sessionmaker(
        scope.settings.catalog.postgres.engine, expire_on_commit=False
    )
    async with factory() as session:
        jobs = list(
            await session.scalars(
                select(models.ReviewJob).where(
                    models.ReviewJob.repository_id == scope.settings.repository
                )
            )
        )
        assert len(jobs) == 1
        job = jobs[0]
        assert job.trigger_type == "webhook"
        assert job.initiator_user_id == scope.settings.catalog.user
        assert job.status == "QUEUED" and job.prompt_version == "v4"
        assert job.model_settings["schema_version"] == 2
        assert receipt.review_job_id == job.id
        assert (
            await session.scalar(
                select(models.Publication.status).where(
                    models.Publication.review_job_id == job.id
                )
            )
            == "NOT_READY"
        )
        assert (
            await session.scalar(
                select(models.OutboxEvent.task_kind).where(
                    models.OutboxEvent.review_job_id == job.id
                )
            )
            == "analyze"
        )
        assert (
            await session.scalar(
                select(models.QuotaUsage.value).where(
                    models.QuotaUsage.repository_id == scope.settings.repository
                )
            )
            == 1
        )
    assert receipt.raw_diff == RAW_DIFF and receipt.snapshot is not None
    assert all(request.method == "GET" for request in scope.requests)


async def test_automatic_branch_filter_uses_verified_base_not_sender_or_head(
    automatic_scope: AutomaticScope,
) -> None:
    scope = automatic_scope
    scope.pull["base"]["ref"] = "release/next"
    scope.pull["head"]["ref"] = "main"
    response = await scope.send()
    assert response.status_code == HTTPStatus.ACCEPTED
    async with scope.settings.catalog.postgres.engine.connect() as connection:
        jobs = list(
            await connection.scalars(
                select(models.ReviewJob.id).where(
                    models.ReviewJob.repository_id == scope.settings.repository
                )
            )
        )
        assert jobs == []
        receipt = (
            (
                await connection.execute(
                    select(models.WebhookReceipt.__table__).where(
                        models.WebhookReceipt.request_data["request"][
                            "repository_external_id"
                        ].astext
                        == str(scope.pull["base"]["repo"]["id"])
                    )
                )
            )
            .mappings()
            .one()
        )
        assert receipt["processing_status"] == "READY"
        assert receipt["reason_code"] == "AUTO_BASE_FILTERED"
        assert receipt["review_job_id"] is None


async def test_automatic_receipt_binds_equivalent_manual_active_run_without_new_quota(
    automatic_scope: AutomaticScope,
) -> None:
    scope = automatic_scope
    external_id = str(scope.pull["base"]["repo"]["id"])
    identifier = await scope.inbox.accept(
        str(uuid4()), GithubRequest("team", "settings", external_id, 7, HEAD, BASE)
    )
    assert identifier is not None
    response = await scope.settings.catalog.client.post(
        f"/api/v1/repositories/{scope.settings.repository}/pull-requests/7/reviews",
        json={"requested_head_sha": HEAD},
        headers={
            "Origin": "https://trusted.example",
            "X-CSRF-Token": "trusted",
            "Idempotency-Key": str(identifier),
        },
    )
    assert response.status_code == HTTPStatus.ACCEPTED, response.json()
    factory = async_sessionmaker(
        scope.settings.catalog.postgres.engine, expire_on_commit=False
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(scope.dispatch)) as http:
        assert (
            await run_once(
                scope.inbox, GithubClient(http), AutomaticReviewAdmission(factory)
            )
            == 1
        )
    receipt = await scope.inbox.get(identifier)
    assert receipt is not None and receipt.status == "READY"
    assert receipt.review_job_id == UUID(response.json()["review_id"])
    async with factory() as session:
        assert (
            len(
                list(
                    await session.scalars(
                        select(models.ReviewJob.id).where(
                            models.ReviewJob.repository_id == scope.settings.repository
                        )
                    )
                )
            )
            == 1
        )
        assert (
            await session.scalar(
                select(models.QuotaUsage.value).where(
                    models.QuotaUsage.repository_id == scope.settings.repository
                )
            )
            == 1
        )


async def test_automatic_admission_revalidates_captured_admin_not_event_sender(
    automatic_scope: AutomaticScope,
) -> None:
    scope = automatic_scope
    original = scope.provider

    async def provider(request: httpx.Request) -> httpx.Response:
        value = original(request)
        response = await value if inspect.isawaitable(value) else value
        if request.headers.get("Authorization") == "Bearer synthetic-personal-token":
            body = response.json()
            body["permissions"]["admin"] = False
            return httpx.Response(200, json=body)
        return response

    scope.provider = provider
    delivery = str(uuid4())
    assert (await scope.send(delivery)).status_code == HTTPStatus.ACCEPTED
    receipt = await scope.inbox.get_by_delivery(delivery)
    assert receipt is not None and receipt.status == "READY"
    assert receipt.reason_code == "AUTO_ACTOR_DENIED" and receipt.review_job_id is None
    async with scope.settings.catalog.postgres.engine.connect() as connection:
        assert (
            await connection.scalar(
                select(models.ReviewJob.id).where(
                    models.ReviewJob.repository_id == scope.settings.repository
                )
            )
            is None
        )
        assert (
            await connection.scalar(
                select(models.Repository.integration_status).where(
                    models.Repository.id == scope.settings.repository
                )
            )
            == "healthy"
        )
        assert (
            await connection.scalar(
                select(models.QuotaUsage.id).where(
                    models.QuotaUsage.repository_id == scope.settings.repository
                )
            )
            is None
        )


async def test_source_policy_mutation_during_personal_provider_read_creates_no_job(
    automatic_scope: AutomaticScope,
) -> None:
    scope = automatic_scope
    original = scope.provider

    async def provider(request: httpx.Request) -> httpx.Response:
        if request.headers.get("Authorization") == "Bearer synthetic-personal-token":
            async with scope.settings.catalog.postgres.engine.begin() as connection:
                version = await connection.scalar(
                    select(models.Repository.current_settings_id).where(
                        models.Repository.id == scope.settings.repository
                    )
                )
                policy = await connection.scalar(
                    select(models.RepositorySettings.review_policy).where(
                        models.RepositorySettings.id == version
                    )
                )
                assert isinstance(policy, dict)
                await connection.execute(
                    update(models.RepositorySettings)
                    .where(models.RepositorySettings.id == version)
                    .values(review_policy={**policy, "minimum_severity": "critical"})
                )
        value = original(request)
        return await value if inspect.isawaitable(value) else value

    scope.provider = provider
    delivery = str(uuid4())
    assert (await scope.send(delivery)).status_code == HTTPStatus.ACCEPTED
    receipt = await scope.inbox.get_by_delivery(delivery)
    assert receipt is not None and receipt.status == "READY"
    assert (
        receipt.reason_code == "AUTO_SETTINGS_CHANGED" and receipt.review_job_id is None
    )
    async with scope.settings.catalog.postgres.engine.connect() as connection:
        assert (
            await connection.scalar(
                select(models.ReviewJob.id).where(
                    models.ReviewJob.repository_id == scope.settings.repository
                )
            )
            is None
        )
        assert (
            await connection.scalar(
                select(models.QuotaUsage.id).where(
                    models.QuotaUsage.repository_id == scope.settings.repository
                )
            )
            is None
        )


async def test_transient_personal_failure_recovers_one_admission(
    automatic_scope: AutomaticScope,
) -> None:
    scope = automatic_scope
    original = scope.provider

    def limited(request: httpx.Request) -> httpx.Response | Awaitable[httpx.Response]:
        if request.headers.get("Authorization") == "Bearer synthetic-personal-token":
            return httpx.Response(
                429,
                headers={"Retry-After": "23"},
                json={"message": "PRIVATE_PROVIDER_BODY"},
            )
        return original(request)

    scope.provider = limited
    delivery = str(uuid4())
    assert (await scope.send(delivery)).status_code == HTTPStatus.ACCEPTED
    receipt = await scope.inbox.get_by_delivery(delivery)
    assert receipt is not None and receipt.status == "PENDING"
    assert receipt.review_job_id is None and receipt.snapshot is None
    factory = async_sessionmaker(
        scope.settings.catalog.postgres.engine, expire_on_commit=False
    )
    async with factory.begin() as session:
        row = await session.get(models.WebhookReceipt, receipt.id)
        assert row is not None and row.retry_at is not None
        remaining = await session.scalar(select(row.retry_at - func.clock_timestamp()))
        assert remaining is not None and remaining.total_seconds() > 15
        assert (
            await session.scalar(
                select(models.ReviewJob.id).where(
                    models.ReviewJob.repository_id == scope.settings.repository
                )
            )
            is None
        )
        await session.execute(
            update(models.WebhookReceipt)
            .where(models.WebhookReceipt.id == receipt.id)
            .values(retry_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    scope.provider = original
    async with httpx.AsyncClient(transport=httpx.MockTransport(scope.dispatch)) as http:
        assert (
            await run_once(
                scope.inbox, GithubClient(http), AutomaticReviewAdmission(factory)
            )
            == 1
        )
        assert (
            await run_once(
                scope.inbox, GithubClient(http), AutomaticReviewAdmission(factory)
            )
            == 0
        )
    ready = await scope.inbox.get(receipt.id)
    assert (
        ready is not None
        and ready.status == "READY"
        and ready.review_job_id is not None
    )
    async with factory() as session:
        assert (
            len(
                list(
                    await session.scalars(
                        select(models.ReviewJob.id).where(
                            models.ReviewJob.repository_id == scope.settings.repository
                        )
                    )
                )
            )
            == 1
        )
        assert (
            await session.scalar(
                select(models.QuotaUsage.value).where(
                    models.QuotaUsage.repository_id == scope.settings.repository
                )
            )
            == 1
        )


async def test_permanent_admission_provider_failure_is_not_retried(
    automatic_scope: AutomaticScope,
) -> None:
    scope = automatic_scope
    original = scope.provider
    diffs = 0

    def invalid_revalidation(
        request: httpx.Request,
    ) -> httpx.Response | Awaitable[httpx.Response]:
        nonlocal diffs
        if request.headers.get("accept") == "application/vnd.github.diff":
            diffs += 1
            if diffs == 2:
                return httpx.Response(200, text="not a trustworthy diff")
        return original(request)

    scope.provider = invalid_revalidation
    delivery = str(uuid4())
    assert (await scope.send(delivery)).status_code == HTTPStatus.ACCEPTED
    receipt = await scope.inbox.get_by_delivery(delivery)
    assert receipt is not None and receipt.status == "READY"
    assert receipt.reason_code == "AUTO_ADMISSION_REFUSED"
    assert receipt.review_job_id is None
    assert await scope.inbox.pending() == []


async def test_invalid_materialized_snapshot_is_a_safe_admission_retry(
    automatic_scope: AutomaticScope,
) -> None:
    scope = automatic_scope
    engine = scope.settings.catalog.postgres.engine.sync_engine

    def corrupt_saved_snapshot(
        connection: Any, cursor: Any, statement: str, parameters: Any, *args: Any
    ) -> None:
        if statement.startswith("INSERT INTO repository_settings"):
            connection.execute(
                update(models.RepositorySettings)
                .where(
                    models.RepositorySettings.repository_id
                    == scope.settings.repository,
                    models.RepositorySettings.rules_source.is_not(None),
                )
                .values(rules_source={})
            )

    event.listen(engine, "after_cursor_execute", corrupt_saved_snapshot)
    delivery = str(uuid4())
    try:
        response = await scope.send(delivery)
    finally:
        event.remove(engine, "after_cursor_execute", corrupt_saved_snapshot)
    assert response.status_code == HTTPStatus.ACCEPTED
    receipt = await scope.inbox.get_by_delivery(delivery)
    assert receipt is not None and receipt.status == "PENDING"
    assert receipt.reason_code == "AUTO_ADMISSION_UNAVAILABLE"
    assert receipt.review_job_id is None


async def test_inbox_write_failure_does_not_abort_the_recovery_batch(
    automatic_scope: AutomaticScope,
) -> None:
    scope = automatic_scope
    request = GithubRequest(
        "team", "settings", str(scope.pull["base"]["repo"]["id"]), 7, HEAD, BASE
    )
    receipts = [await scope.inbox.accept(str(uuid4()), request) for _ in range(2)]
    assert all(receipt is not None for receipt in receipts)
    original = scope.provider

    def denied(request: httpx.Request) -> httpx.Response | Awaitable[httpx.Response]:
        if request.headers.get("Authorization") == "Bearer synthetic-personal-token":
            return httpx.Response(403)
        return original(request)

    scope.provider = denied
    engine = scope.settings.catalog.postgres.engine
    failed = False

    def reject_first_completion(
        connection: Any, cursor: Any, statement: str, parameters: Any, *args: Any
    ) -> None:
        nonlocal failed
        if (
            not failed
            and statement.startswith("UPDATE webhook_receipt SET")
            and "snapshot_data" in statement.split(" WHERE ", 1)[0]
        ):
            failed = True
            raise OSError("synthetic inbox write unavailable")

    event.listen(engine.sync_engine, "before_cursor_execute", reject_first_completion)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(scope.dispatch)
        ) as http:
            assert (
                await run_once(
                    scope.inbox, GithubClient(http), AutomaticReviewAdmission(factory)
                )
                == 2
            )
    finally:
        event.remove(
            engine.sync_engine, "before_cursor_execute", reject_first_completion
        )
    assert failed
    first, second = [await scope.inbox.get(receipt) for receipt in receipts if receipt]
    assert first is not None and first.status == "PROCESSING"
    assert first.review_job_id is None
    assert second is not None and second.status == "READY"
    assert second.reason_code == "AUTO_ACTOR_DENIED"
    assert second.review_job_id is None


async def test_live_automatic_admission_renews_its_receipt_lease(
    automatic_scope: AutomaticScope,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = automatic_scope
    monkeypatch.setattr(
        "app.adapters.automatic_review_admission._RENEW_INTERVAL_SECONDS",
        0.02,
        raising=False,
    )
    original = scope.provider
    renewed = asyncio.Event()
    watching = False
    engine = scope.settings.catalog.postgres.engine

    def observe_renewal(
        connection: Any, cursor: Any, statement: str, parameters: Any, *args: Any
    ) -> None:
        if watching and statement.startswith("UPDATE webhook_receipt SET lease_until="):
            renewed.set()

    async def slow_admin(request: httpx.Request) -> httpx.Response:
        nonlocal watching
        if request.headers.get("Authorization") == "Bearer synthetic-personal-token":
            statement = select(models.WebhookReceipt.lease_until).where(
                models.WebhookReceipt.request_data["request"][
                    "repository_external_id"
                ].astext
                == str(scope.pull["base"]["repo"]["id"])
            )
            async with engine.connect() as connection:
                before = await connection.scalar(statement)
            watching = True
            await asyncio.wait_for(renewed.wait(), timeout=5)
            async with engine.begin() as connection:
                after = await connection.scalar(statement.with_for_update())
                assert before is not None and after is not None and after > before
        value = original(request)
        return await value if inspect.isawaitable(value) else value

    scope.provider = slow_admin
    delivery = str(uuid4())
    event.listen(engine.sync_engine, "after_cursor_execute", observe_renewal)
    try:
        assert (await scope.send(delivery)).status_code == HTTPStatus.ACCEPTED
    finally:
        event.remove(engine.sync_engine, "after_cursor_execute", observe_renewal)
    receipt = await scope.inbox.get_by_delivery(delivery)
    assert receipt is not None and receipt.status == "READY"
    assert receipt.review_job_id is not None and receipt.attempts == 1


async def test_receipt_heartbeat_cancels_provider_work_after_fence_loss(
    automatic_scope: AutomaticScope,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = automatic_scope
    monkeypatch.setattr(
        "app.adapters.automatic_review_admission._RENEW_INTERVAL_SECONDS", 0.02
    )
    original = scope.provider
    cancelled = asyncio.Event()

    async def lose_claim(request: httpx.Request) -> httpx.Response:
        if request.headers.get("Authorization") == "Bearer synthetic-personal-token":
            async with scope.settings.catalog.postgres.engine.begin() as connection:
                await connection.execute(
                    update(models.WebhookReceipt)
                    .where(
                        models.WebhookReceipt.request_data["request"][
                            "repository_external_id"
                        ].astext
                        == str(scope.pull["base"]["repo"]["id"])
                    )
                    .values(attempts=models.WebhookReceipt.attempts + 1)
                )
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
        value = original(request)
        return await value if inspect.isawaitable(value) else value

    scope.provider = lose_claim
    delivery = str(uuid4())
    response = await asyncio.wait_for(scope.send(delivery), timeout=5)
    assert response.status_code == HTTPStatus.ACCEPTED
    assert cancelled.is_set()
    receipt = await scope.inbox.get_by_delivery(delivery)
    assert receipt is not None and receipt.status == "PROCESSING"
    assert receipt.attempts == 2 and receipt.review_job_id is None


async def test_durable_receipt_replays_terminal_job_after_key_expiry_and_revoked_actor(
    automatic_scope: AutomaticScope,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = automatic_scope
    delivery = str(uuid4())
    assert (await scope.send(delivery)).status_code == HTTPStatus.ACCEPTED
    receipt = await scope.inbox.get_by_delivery(delivery)
    assert receipt is not None and receipt.review_job_id is not None
    assert receipt.snapshot is not None and receipt.raw_diff is not None
    original_job = receipt.review_job_id
    factory = async_sessionmaker(
        scope.settings.catalog.postgres.engine, expire_on_commit=False
    )
    async with factory.begin() as session:
        await session.execute(
            update(models.ReviewJob)
            .where(models.ReviewJob.id == original_job)
            .values(status="FAILED", finished_at=func.clock_timestamp())
        )
        await session.execute(
            delete(models.IdempotencyRecord).where(
                models.IdempotencyRecord.review_job_id == original_job
            )
        )
        await session.execute(
            delete(models.RepositoryAccess).where(
                models.RepositoryAccess.repository_id == scope.settings.repository
            )
        )
        await session.execute(
            update(models.Repository)
            .where(models.Repository.id == scope.settings.repository)
            .values(current_settings_id=scope.settings.version)
        )
    monkeypatch.setenv("ANALYSIS__CONFIG_PATH", "/missing-runtime")
    before = len(scope.requests)
    await AutomaticReviewAdmission(factory).admit(
        AutomaticReceiptContext(receipt, receipt.snapshot, receipt.raw_diff)
    )
    assert len(scope.requests) == before
    replay = await scope.inbox.get(receipt.id)
    assert (
        replay is not None
        and replay.status == "READY"
        and replay.review_job_id == original_job
    )
    async with factory() as session:
        assert (
            len(
                list(
                    await session.scalars(
                        select(models.ReviewJob.id).where(
                            models.ReviewJob.repository_id == scope.settings.repository
                        )
                    )
                )
            )
            == 1
        )
        assert (
            await session.scalar(
                select(models.QuotaUsage.value).where(
                    models.QuotaUsage.repository_id == scope.settings.repository
                )
            )
            == 1
        )
        assert (
            len(
                list(
                    await session.scalars(
                        select(models.OutboxEvent.id).where(
                            models.OutboxEvent.review_job_id == original_job
                        )
                    )
                )
            )
            == 1
        )


async def test_lost_attempt_fence_during_provider_read_admits_nothing(
    automatic_scope: AutomaticScope,
) -> None:
    scope = automatic_scope
    original = scope.provider
    claim_id: UUID | None = None

    async def provider(request: httpx.Request) -> httpx.Response:
        nonlocal claim_id
        if request.headers.get("Authorization") == "Bearer synthetic-personal-token":
            async with scope.settings.catalog.postgres.engine.begin() as connection:
                claim_id = await connection.scalar(
                    select(models.WebhookReceipt.id).where(
                        models.WebhookReceipt.request_data["request"][
                            "repository_external_id"
                        ].astext
                        == str(scope.pull["base"]["repo"]["id"])
                    )
                )
                await connection.execute(
                    update(models.WebhookReceipt)
                    .where(models.WebhookReceipt.id == claim_id)
                    .values(attempts=models.WebhookReceipt.attempts + 1)
                )
        value = original(request)
        return await value if inspect.isawaitable(value) else value

    scope.provider = provider
    assert (await scope.send()).status_code == HTTPStatus.ACCEPTED
    assert claim_id is not None
    receipt = await scope.inbox.get(claim_id)
    assert receipt is not None and receipt.status == "PROCESSING"
    assert receipt.review_job_id is None and receipt.snapshot is None
    async with scope.settings.catalog.postgres.engine.connect() as connection:
        assert (
            await connection.scalar(
                select(models.ReviewJob.id).where(
                    models.ReviewJob.repository_id == scope.settings.repository
                )
            )
            is None
        )
        assert (
            await connection.scalar(
                select(models.QuotaUsage.id).where(
                    models.QuotaUsage.repository_id == scope.settings.repository
                )
            )
            is None
        )


async def test_repository_lock_wait_crossing_claim_expiry_cannot_admit(
    automatic_scope: AutomaticScope,
) -> None:
    scope = automatic_scope
    entered, release = asyncio.Event(), asyncio.Event()
    original = scope.provider

    async def provider(request: httpx.Request) -> httpx.Response:
        if request.headers.get("Authorization") == "Bearer synthetic-personal-token":
            entered.set()
            await release.wait()
        value = original(request)
        return await value if inspect.isawaitable(value) else value

    scope.provider = provider
    identifier = await scope.inbox.accept(
        str(uuid4()),
        GithubRequest(
            "team", "settings", str(scope.pull["base"]["repo"]["id"]), 7, HEAD, BASE
        ),
    )
    assert identifier is not None
    application = f"auto-lock-{uuid4().hex}"
    engine = create_async_engine(
        os.environ["DMC268_TEST_DATABASE_URL"],
        poolclass=NullPool,
        connect_args={"server_settings": {"application_name": application}},
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    task = None
    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(scope.dispatch)
        ) as http:
            task = asyncio.create_task(
                run_once(
                    scope.inbox, GithubClient(http), AutomaticReviewAdmission(factory)
                )
            )
            await asyncio.wait_for(entered.wait(), 10)
            async with scope.settings.catalog.postgres.engine.begin() as locker:
                await locker.execute(
                    select(models.Repository.id)
                    .where(models.Repository.id == scope.settings.repository)
                    .with_for_update()
                )
                release.set()
                async with scope.settings.catalog.postgres.engine.begin() as observer:
                    for _ in range(200):
                        waiting = await observer.scalar(
                            text(
                                "SELECT count(*) FROM pg_stat_activity "
                                "WHERE application_name=:name "
                                "AND wait_event_type='Lock'"
                            ),
                            {"name": application},
                        )
                        if waiting:
                            break
                        await asyncio.sleep(0.01)
                    else:
                        raise AssertionError(
                            "admission did not wait for the repository lock"
                        )
                    await observer.execute(
                        update(models.WebhookReceipt)
                        .where(models.WebhookReceipt.id == identifier)
                        .values(
                            lease_until=func.clock_timestamp()
                            - text("interval '1 second'")
                        )
                    )
            assert await asyncio.wait_for(task, 10) == 1
        receipt = await scope.inbox.get(identifier)
        assert receipt is not None and receipt.status == "PROCESSING"
        assert receipt.review_job_id is None and receipt.snapshot is None
        async with scope.settings.catalog.postgres.engine.connect() as connection:
            assert (
                await connection.scalar(
                    select(models.ReviewJob.id).where(
                        models.ReviewJob.repository_id == scope.settings.repository
                    )
                )
                is None
            )
            assert (
                await connection.scalar(
                    select(models.QuotaUsage.id).where(
                        models.QuotaUsage.repository_id == scope.settings.repository
                    )
                )
                is None
            )
    finally:
        release.set()
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await engine.dispose()


async def test_lost_commit_ack_retains_atomic_receipt_link_and_cannot_charge_again(
    automatic_scope: AutomaticScope,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = automatic_scope
    engine = scope.settings.catalog.postgres.engine.sync_engine
    dialect = engine.dialect
    original_commit = dialect.do_commit
    armed, fired = False, False

    def after_write(
        connection: Any, cursor: Any, statement: str, parameters: Any, *args: Any
    ) -> None:
        nonlocal armed
        # Claim RETURNING also names review_job_id; only arm the binding SET.
        assigned = statement.split(" WHERE ", 1)[0]
        if (
            statement.startswith("UPDATE webhook_receipt SET ")
            and "review_job_id=" in assigned
        ):
            armed = True

    def commit(connection: Any) -> None:
        nonlocal armed, fired
        lose_ack = armed and not fired
        armed = False
        original_commit(connection)
        if lose_ack:
            fired = True
            raise OSError("private-lost-commit-ack")

    delivery = str(uuid4())
    with monkeypatch.context() as patch:
        patch.setattr(dialect, "do_commit", commit)
        event.listen(engine, "after_cursor_execute", after_write)
        try:
            response = await scope.send(delivery)
        finally:
            event.remove(engine, "after_cursor_execute", after_write)
    assert fired, "The fault must follow the actual aggregate+receipt commit"
    assert response.status_code == HTTPStatus.ACCEPTED
    receipt = await scope.inbox.get_by_delivery(delivery)
    assert (
        receipt is not None
        and receipt.status == "READY"
        and receipt.review_job_id is not None
    )
    before = len(scope.requests)
    assert (await scope.send(delivery)).status_code == HTTPStatus.ACCEPTED
    factory = async_sessionmaker(
        scope.settings.catalog.postgres.engine, expire_on_commit=False
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(scope.dispatch)) as http:
        assert (
            await run_once(
                scope.inbox, GithubClient(http), AutomaticReviewAdmission(factory)
            )
            == 0
        )
    assert len(scope.requests) == before
    async with factory() as session:
        assert (
            len(
                list(
                    await session.scalars(
                        select(models.ReviewJob.id).where(
                            models.ReviewJob.repository_id == scope.settings.repository
                        )
                    )
                )
            )
            == 1
        )
        assert (
            await session.scalar(
                select(models.QuotaUsage.value).where(
                    models.QuotaUsage.repository_id == scope.settings.repository
                )
            )
            == 1
        )
        assert (
            len(
                list(
                    await session.scalars(
                        select(models.OutboxEvent.id).where(
                            models.OutboxEvent.review_job_id == receipt.review_job_id
                        )
                    )
                )
            )
            == 1
        )


async def test_commit_rejection_rolls_back_job_quota_outbox_and_receipt_link(
    automatic_scope: AutomaticScope,
) -> None:
    scope = automatic_scope
    engine = scope.settings.catalog.postgres.engine
    async with engine.begin() as connection:
        await connection.execute(
            text("""CREATE FUNCTION settings20_auto_commit_fault()
        RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
        RAISE EXCEPTION 'private-automatic-commit-fault'; END $$""")
        )
        await connection.execute(
            text("""CREATE CONSTRAINT TRIGGER settings20_auto_commit_fault
        AFTER INSERT ON review_job DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
        EXECUTE FUNCTION settings20_auto_commit_fault()""")
        )
    try:
        delivery = str(uuid4())
        response = await scope.send(delivery)
        assert response.status_code == HTTPStatus.ACCEPTED
        receipt = await scope.inbox.get_by_delivery(delivery)
        assert receipt is not None and receipt.status == "PENDING"
        assert receipt.review_job_id is None and receipt.snapshot is None
        async with engine.connect() as connection:
            assert (
                await connection.scalar(
                    select(models.ReviewJob.id).where(
                        models.ReviewJob.repository_id == scope.settings.repository
                    )
                )
                is None
            )
            assert (
                await connection.scalar(
                    select(models.QuotaUsage.id).where(
                        models.QuotaUsage.repository_id == scope.settings.repository
                    )
                )
                is None
            )
            assert (
                await connection.scalar(
                    select(models.OutboxEvent.id)
                    .join(models.ReviewJob)
                    .where(models.ReviewJob.repository_id == scope.settings.repository)
                )
                is None
            )
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DROP TRIGGER settings20_auto_commit_fault ON review_job")
            )
            await connection.execute(
                text("DROP FUNCTION settings20_auto_commit_fault()")
            )


async def test_expiry_while_quota_reservation_waits_rolls_back_entire_aggregate(  # noqa: PLR0915 -- complete PostgreSQL fault/lock/rollback integration
    automatic_scope: AutomaticScope,
) -> None:
    scope = automatic_scope
    source_engine = scope.settings.catalog.postgres.engine
    identifier = await scope.inbox.accept(
        str(uuid4()),
        GithubRequest(
            "team", "settings", str(scope.pull["base"]["repo"]["id"]), 7, HEAD, BASE
        ),
    )
    assert identifier is not None
    async with source_engine.begin() as connection:
        clock = await connection.scalar(select(func.clock_timestamp()))
        assert clock is not None
        quota = uuid4()
        await connection.execute(
            insert(models.QuotaUsage).values(
                id=quota,
                repository_id=scope.settings.repository,
                user_id=scope.settings.catalog.user,
                type="starts",
                value=0,
                window_start=clock.astimezone(UTC).replace(
                    minute=0, second=0, microsecond=0
                ),
            )
        )
    original = scope.provider
    expires: datetime | None = None

    async def provider(request: httpx.Request) -> httpx.Response:
        nonlocal expires
        if request.headers.get("Authorization") == "Bearer synthetic-personal-token":
            async with source_engine.begin() as connection:
                expires = await connection.scalar(
                    update(models.WebhookReceipt)
                    .where(models.WebhookReceipt.id == identifier)
                    .values(
                        lease_until=func.clock_timestamp()
                        + text("interval '5 seconds'")
                    )
                    .returning(models.WebhookReceipt.lease_until)
                )
        value = original(request)
        return await value if inspect.isawaitable(value) else value

    scope.provider = provider
    application = f"auto-final-fence-{uuid4().hex}"
    engine = create_async_engine(
        os.environ["DMC268_TEST_DATABASE_URL"],
        poolclass=NullPool,
        connect_args={"server_settings": {"application_name": application}},
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    task = None
    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(scope.dispatch)
        ) as http:
            async with source_engine.begin() as locker:
                await locker.execute(
                    select(models.QuotaUsage.id)
                    .where(models.QuotaUsage.id == quota)
                    .with_for_update()
                )
                task = asyncio.create_task(
                    run_once(
                        scope.inbox,
                        GithubClient(http),
                        AutomaticReviewAdmission(factory),
                    )
                )
                async with source_engine.connect() as observer:
                    async with asyncio.timeout(10):
                        while True:
                            await observer.execute(
                                text("SELECT pg_stat_clear_snapshot()")
                            )
                            waiting = await observer.scalar(
                                text(
                                    "SELECT count(*) FROM pg_stat_activity "
                                    "WHERE application_name=:name "
                                    "AND wait_event_type='Lock' "
                                    "AND query LIKE '%INSERT INTO quota_usage%'"
                                ),
                                {"name": application},
                            )
                            if waiting:
                                break
                            if task.done():
                                await task
                                raise AssertionError(
                                    "Admission did not reach locked quota reservation"
                                )
                            await asyncio.sleep(0.01)
                assert expires is not None
                await locker.execute(
                    text(
                        "SELECT pg_sleep(GREATEST(0, EXTRACT(EPOCH FROM "
                        "CAST(:expires AS timestamptz) - clock_timestamp())) + 0.1)"
                    ),
                    {"expires": expires},
                )
            assert await asyncio.wait_for(task, 10) == 1
        receipt = await scope.inbox.get(identifier)
        assert receipt is not None and receipt.status == "PROCESSING"
        assert receipt.review_job_id is None and receipt.snapshot is None
        async with source_engine.connect() as connection:
            assert (
                await connection.scalar(
                    select(models.ReviewJob.id).where(
                        models.ReviewJob.repository_id == scope.settings.repository
                    )
                )
                is None
            )
            assert (
                await connection.scalar(
                    select(models.QuotaUsage.value).where(models.QuotaUsage.id == quota)
                )
                == 0
            )
            assert (
                await connection.scalar(
                    select(models.OutboxEvent.id)
                    .join(models.ReviewJob)
                    .where(models.ReviewJob.repository_id == scope.settings.repository)
                )
                is None
            )
    finally:
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await engine.dispose()


async def test_local_disconnect_during_automatic_provider_read_refuses_new_work(
    automatic_scope: AutomaticScope,
) -> None:
    scope = automatic_scope
    original = scope.provider

    async def provider(request: httpx.Request) -> httpx.Response:
        if request.headers.get("Authorization") == "Bearer synthetic-personal-token":
            async with scope.settings.catalog.postgres.engine.begin() as connection:
                await connection.execute(
                    delete(models.RepositoryAccess).where(
                        models.RepositoryAccess.repository_id
                        == scope.settings.repository
                    )
                )
        value = original(request)
        return await value if inspect.isawaitable(value) else value

    scope.provider = provider
    delivery = str(uuid4())
    assert (await scope.send(delivery)).status_code == HTTPStatus.ACCEPTED
    receipt = await scope.inbox.get_by_delivery(delivery)
    assert receipt is not None and receipt.status == "READY"
    assert (
        receipt.reason_code == "AUTO_RESOURCE_UNAVAILABLE"
        and receipt.review_job_id is None
    )
    async with scope.settings.catalog.postgres.engine.connect() as connection:
        assert (
            await connection.scalar(
                select(models.ReviewJob.id).where(
                    models.ReviewJob.repository_id == scope.settings.repository
                )
            )
            is None
        )
        assert (
            await connection.scalar(
                select(models.QuotaUsage.id).where(
                    models.QuotaUsage.repository_id == scope.settings.repository
                )
            )
            is None
        )


async def test_manual_start_is_independent_of_disabled_auto_and_excluded_base(
    automatic_scope: AutomaticScope,
) -> None:
    scope = automatic_scope
    saved = await replace_settings(
        scope.settings, auto_review_enabled=False, base_branches=["release/next"]
    )
    assert saved.status_code == HTTPStatus.OK
    response = await scope.settings.catalog.client.post(
        f"/api/v1/repositories/{scope.settings.repository}/pull-requests/7/reviews",
        json={"requested_head_sha": HEAD},
        headers={
            "Origin": "https://trusted.example",
            "X-CSRF-Token": "trusted",
            "Idempotency-Key": str(uuid4()),
        },
    )
    assert response.status_code == HTTPStatus.ACCEPTED
    async with scope.settings.catalog.postgres.engine.connect() as connection:
        assert (
            await connection.scalar(
                select(models.ReviewJob.trigger_type).where(
                    models.ReviewJob.id == UUID(response.json()["review_id"])
                )
            )
            == "manual"
        )


@pytest.mark.parametrize("same_delivery", [True, False])
async def test_concurrent_deliveries_bind_one_equivalent_active_run(
    automatic_scope: AutomaticScope,
    same_delivery: bool,
) -> None:
    scope = automatic_scope
    first, second = str(uuid4()), str(uuid4())
    responses = await asyncio.gather(
        scope.send(first), scope.send(first if same_delivery else second)
    )
    assert all(response.status_code == HTTPStatus.ACCEPTED for response in responses)
    primary = await scope.inbox.get_by_delivery(first)
    assert (
        primary is not None
        and primary.status == "READY"
        and primary.review_job_id is not None
    )
    if not same_delivery:
        other = await scope.inbox.get_by_delivery(second)
        assert (
            other is not None
            and other.status == "READY"
            and other.review_job_id == primary.review_job_id
        )
    factory = async_sessionmaker(
        scope.settings.catalog.postgres.engine, expire_on_commit=False
    )
    async with factory() as session:
        assert (
            len(
                list(
                    await session.scalars(
                        select(models.ReviewJob.id).where(
                            models.ReviewJob.repository_id == scope.settings.repository
                        )
                    )
                )
            )
            == 1
        )
        assert (
            await session.scalar(
                select(models.QuotaUsage.value).where(
                    models.QuotaUsage.repository_id == scope.settings.repository
                )
            )
            == 1
        )
        assert (
            len(
                list(
                    await session.scalars(
                        select(models.OutboxEvent.id).where(
                            models.OutboxEvent.review_job_id == primary.review_job_id
                        )
                    )
                )
            )
            == 1
        )
