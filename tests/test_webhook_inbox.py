from __future__ import annotations

import asyncio
import uuid
from contextlib import suppress
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_postgres_integration import (
    PostgresHarness,
    _create_repository,
    _create_settings,
)

from app.application.github_ports import GithubFailure, GithubRequest
from app.application.webhook_ports import InboxUnavailable
from app.db import models
from app.db.webhook_inbox import SqlAlchemyWebhookInbox

pytest_plugins = ("test_postgres_integration",)

_PULL_NUMBER = 7
_SECOND_ATTEMPT = 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ignores", [[], {"globs": "secret"}, {"patterns": ["secret"]}, {"globs": [7]}]
)
async def test_unsupported_legacy_ignore_shape_is_not_captured(
    postgres: PostgresHarness, ignores: object
) -> None:
    async with postgres.engine.begin() as connection:
        repository_id = await _create_repository(
            postgres, connection, external_id="123"
        )
        settings_id = await _create_settings(postgres, connection, repository_id)
        await connection.execute(
            update(models.RepositorySettings)
            .where(models.RepositorySettings.id == settings_id)
            .values(ignores=ignores)
        )
        await connection.execute(
            update(models.Repository)
            .where(models.Repository.id == repository_id)
            .values(current_settings_id=settings_id)
        )
    inbox = SqlAlchemyWebhookInbox(async_sessionmaker(postgres.engine))
    delivery_id = str(uuid.uuid4())
    with pytest.raises(InboxUnavailable):
        await inbox.accept(
            delivery_id,
            GithubRequest("owner", "repo", "123", 7, "a" * 40, "b" * 40),
        )
    assert await inbox.get_by_delivery(delivery_id) is None


@pytest.mark.asyncio
async def test_delivery_is_durable_and_uses_connected_repository(
    postgres: PostgresHarness,
) -> None:

    async with postgres.engine.begin() as connection:
        await _create_repository(postgres, connection, external_id="123")
    inbox = SqlAlchemyWebhookInbox(async_sessionmaker(postgres.engine))
    receipt_id = await inbox.accept(
        f"delivery-{uuid.uuid4()}",
        GithubRequest(
            "untrusted-owner", "untrusted-name", "123", 7, "a" * 40, "b" * 40
        ),
    )
    assert receipt_id is not None
    postgres.track("webhook_receipt", receipt_id)
    receipt = await inbox.get(receipt_id)
    assert receipt is not None
    assert receipt.status == "PENDING"
    assert receipt.request.owner == "postgres-integration-test"
    assert receipt.request.name.startswith("repository-")
    assert receipt.request.number == _PULL_NUMBER
    assert receipt.snapshot is None


@pytest.mark.asyncio
async def test_delivery_can_be_read_by_github_delivery_id(
    postgres: PostgresHarness,
) -> None:

    async with postgres.engine.begin() as connection:
        await _create_repository(postgres, connection, external_id="123")
    inbox = SqlAlchemyWebhookInbox(async_sessionmaker(postgres.engine))
    delivery_id = str(uuid.uuid4())
    receipt_id = await inbox.accept(
        delivery_id, GithubRequest("owner", "repo", "123", 7, "a" * 40, "b" * 40)
    )
    assert receipt_id is not None
    postgres.track("webhook_receipt", receipt_id)
    receipt = await inbox.get_by_delivery(delivery_id)
    assert receipt is not None and receipt.id == receipt_id
    assert await inbox.get_by_delivery("unknown") is None


@pytest.mark.asyncio
async def test_duplicate_delivery_is_accepted_once(postgres: PostgresHarness) -> None:

    async with postgres.engine.begin() as connection:
        await _create_repository(postgres, connection, external_id="123")
    inbox = SqlAlchemyWebhookInbox(async_sessionmaker(postgres.engine))
    delivery = f"delivery-{uuid.uuid4()}"
    request = GithubRequest("owner", "repo", "123", 7, "a" * 40, "b" * 40)
    results = await asyncio.gather(
        inbox.accept(delivery, request), inbox.accept(delivery, request)
    )
    accepted = [value for value in results if value is not None]
    assert len(accepted) == 1
    postgres.track("webhook_receipt", accepted[0])


@pytest.mark.asyncio
async def test_unconnected_repository_does_not_create_work(
    postgres: PostgresHarness,
) -> None:

    inbox = SqlAlchemyWebhookInbox(async_sessionmaker(postgres.engine))
    assert (
        await inbox.accept(
            str(uuid.uuid4()),
            GithubRequest("owner", "repo", "999", 7, "a" * 40, "b" * 40),
        )
        is None
    )


@pytest.mark.asyncio
async def test_pending_delivery_can_be_claimed_once(postgres: PostgresHarness) -> None:

    async with postgres.engine.begin() as connection:
        await _create_repository(postgres, connection, external_id="123")
    inbox = SqlAlchemyWebhookInbox(async_sessionmaker(postgres.engine))
    receipt_id = await inbox.accept(
        str(uuid.uuid4()), GithubRequest("owner", "repo", "123", 7, "a" * 40, "b" * 40)
    )
    assert receipt_id is not None
    postgres.track("webhook_receipt", receipt_id)
    claims = await asyncio.gather(inbox.claim(receipt_id), inbox.claim(receipt_id))
    assert sum(claim is not None for claim in claims) == 1
    receipt = await inbox.get(receipt_id)
    assert receipt is not None
    assert receipt.status == "PROCESSING"


@pytest.mark.asyncio
async def test_claimed_delivery_stores_immutable_snapshot(
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
    claim = await inbox.claim(receipt_id)
    assert claim is not None
    snapshot = {
        "schema_version": "1.0",
        "snapshot": {"head_sha": "a" * 40},
        "files": [],
    }
    assert await inbox.complete(claim, snapshot, "") is True
    assert (
        await inbox.complete(claim, {"files": [{"evil": "replacement"}]}, "changed")
        is False
    )
    receipt = await inbox.get(receipt_id)
    assert receipt is not None
    assert receipt.status == "READY"
    assert receipt.snapshot == snapshot
    assert receipt.raw_diff == ""
    assert await inbox.claim(receipt_id) is None


@pytest.mark.asyncio
async def test_expired_claim_is_recovered_without_stale_overwrite(
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
    now = datetime.now(UTC)
    old = await inbox.claim(receipt_id, now=now)
    assert old is not None
    assert await inbox.claim(receipt_id, now=now + timedelta(seconds=89)) is None
    new = await inbox.claim(receipt_id, now=now + timedelta(seconds=91))
    assert new is not None
    assert new.attempts == _SECOND_ATTEMPT
    assert await inbox.complete(old, {"stale": True}, "stale") is False
    assert await inbox.complete(new, {"snapshot": "fresh"}, "fresh") is True
    receipt = await inbox.get(receipt_id)
    assert receipt is not None and receipt.snapshot == {"snapshot": "fresh"}


@pytest.mark.asyncio
async def test_expired_lease_cannot_write_even_before_recovery(
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
    claim = await inbox.claim(receipt_id, now=datetime.now(UTC) - timedelta(seconds=91))
    assert claim is not None
    assert await inbox.complete(claim, {"expired": True}, "private source") is False
    await inbox.fail(claim, GithubFailure("VCS_ACCESS_DENIED", False))
    receipt = await inbox.get(receipt_id)
    assert receipt is not None and receipt.status == "PROCESSING"
    assert receipt.snapshot is None and receipt.reason_code is None


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["complete", "fail"])
async def test_lease_expiring_while_waiting_for_lock_cannot_write(
    postgres: PostgresHarness, operation: str
) -> None:
    async with postgres.engine.begin() as connection:
        await _create_repository(postgres, connection, external_id="123")
    inbox = SqlAlchemyWebhookInbox(async_sessionmaker(postgres.engine))
    receipt_id = await inbox.accept(
        str(uuid.uuid4()), GithubRequest("owner", "repo", "123", 7, "a" * 40, "b" * 40)
    )
    assert receipt_id is not None
    postgres.track("webhook_receipt", receipt_id)
    claim_time = datetime.now(UTC) - timedelta(seconds=86)
    claim = await inbox.claim(receipt_id, now=claim_time)
    assert claim is not None
    application_name = f"webhook-lease-{uuid.uuid4().hex}"
    waiting_engine = create_async_engine(
        postgres.engine.url,
        connect_args={"server_settings": {"application_name": application_name}},
    )
    waiting_inbox = SqlAlchemyWebhookInbox(async_sessionmaker(waiting_engine))
    task = None
    try:
        async with postgres.engine.begin() as locker:
            await locker.execute(
                select(models.WebhookReceipt.id)
                .where(models.WebhookReceipt.id == receipt_id)
                .with_for_update()
            )
            task = asyncio.create_task(
                waiting_inbox.complete(claim, {"expired": True}, "private source")
                if operation == "complete"
                else waiting_inbox.fail(
                    claim, GithubFailure("VCS_ACCESS_DENIED", False)
                )
            )
            async with postgres.engine.connect() as observer:
                for _ in range(200):
                    waiting = await observer.scalar(
                        text(
                            "SELECT count(*) FROM pg_stat_activity "
                            "WHERE application_name=:name AND wait_event_type='Lock'"
                        ),
                        {"name": application_name},
                    )
                    if waiting:
                        break
                    await asyncio.sleep(0.01)
                else:
                    raise AssertionError("writer did not wait on the row lock")
                assert claim_time + timedelta(seconds=90) > datetime.now(UTC)
            await locker.execute(
                text(
                    "SELECT pg_sleep(GREATEST(0, EXTRACT(EPOCH FROM "
                    "CAST(:expires AS timestamptz) - clock_timestamp())) + 0.1)"
                ),
                {"expires": claim_time + timedelta(seconds=90)},
            )
        await asyncio.wait_for(task, timeout=5)
        receipt = await inbox.get(receipt_id)
        assert receipt is not None and receipt.status == "PROCESSING"
        assert receipt.snapshot is None and receipt.reason_code is None
    finally:
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await waiting_engine.dispose()


@pytest.mark.asyncio
async def test_transient_failure_retries_after_delay_and_stops_at_three_attempts(
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
    now = datetime.now(UTC)
    for attempt in range(1, 4):
        claim = await inbox.claim(receipt_id, now=now)
        assert claim is not None and claim.attempts == attempt
        await inbox.fail(claim, GithubFailure("VCS_RATE_LIMITED", True, 60), now=now)
        receipt = await inbox.get(receipt_id)
        assert receipt is not None and receipt.reason_code == "VCS_RATE_LIMITED"
        assert await inbox.claim(receipt_id, now=now + timedelta(seconds=59)) is None
        now += timedelta(seconds=60)
    receipt = await inbox.get(receipt_id)
    assert receipt is not None and receipt.status == "FAILED"
    assert await inbox.claim(receipt_id, now=now) is None


@pytest.mark.asyncio
async def test_recovery_lists_due_pending_and_expired_claims(
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
    now = datetime.now(UTC)
    assert receipt_id in await inbox.pending(now=now)
    assert await inbox.claim(receipt_id, now=now) is not None
    assert receipt_id not in await inbox.pending(now=now)
    assert receipt_id in await inbox.pending(now=now + timedelta(seconds=91))


@pytest.mark.asyncio
async def test_crash_recovery_cannot_claim_forever(postgres: PostgresHarness) -> None:

    async with postgres.engine.begin() as connection:
        await _create_repository(postgres, connection, external_id="123")
    inbox = SqlAlchemyWebhookInbox(async_sessionmaker(postgres.engine))
    receipt_id = await inbox.accept(
        str(uuid.uuid4()), GithubRequest("owner", "repo", "123", 7, "a" * 40, "b" * 40)
    )
    assert receipt_id is not None
    postgres.track("webhook_receipt", receipt_id)
    now = datetime.now(UTC)
    for attempt in range(3):
        assert (
            await inbox.claim(receipt_id, now=now + timedelta(seconds=91 * attempt))
            is not None
        )
    assert await inbox.claim(receipt_id, now=now + timedelta(seconds=273)) is None
    receipt = await inbox.get(receipt_id)
    assert receipt is not None and receipt.status == "FAILED"
    assert receipt_id not in await inbox.pending(now=now + timedelta(seconds=273))


@pytest.mark.asyncio
async def test_delivery_freezes_current_ignore_settings(
    postgres: PostgresHarness,
) -> None:

    async with postgres.engine.begin() as connection:
        repository_id = await _create_repository(
            postgres, connection, external_id="123"
        )
        settings_id = await _create_settings(postgres, connection, repository_id)
        await connection.execute(
            update(models.RepositorySettings)
            .where(models.RepositorySettings.id == settings_id)
            .values(ignores={"globs": ["tests/**"]})
        )
        await connection.execute(
            update(models.Repository)
            .where(models.Repository.id == repository_id)
            .values(current_settings_id=settings_id)
        )
    inbox = SqlAlchemyWebhookInbox(async_sessionmaker(postgres.engine))
    receipt_id = await inbox.accept(
        str(uuid.uuid4()), GithubRequest("owner", "repo", "123", 7, "a" * 40, "b" * 40)
    )
    assert receipt_id is not None
    postgres.track("webhook_receipt", receipt_id)
    async with postgres.engine.begin() as connection:
        await connection.execute(
            update(models.Repository)
            .where(models.Repository.id == repository_id)
            .values(current_settings_id=None)
        )
    receipt = await inbox.get(receipt_id)
    assert receipt is not None and receipt.ignore_globs == ("tests/**",)
