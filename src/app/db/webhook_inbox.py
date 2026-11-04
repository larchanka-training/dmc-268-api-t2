"""PostgreSQL-backed GitHub webhook intake."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from asyncpg import PostgresError  # type: ignore[import-untyped]
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.github_ports import GithubFailure, GithubRequest
from app.application.webhook_ports import InboxUnavailable, WebhookDelivery
from app.db.models import Repository, RepositorySettings, WebhookReceipt

_MAX_ATTEMPTS = 3


class SqlAlchemyWebhookInbox:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[AsyncSession]:
        try:
            async with self._sessions() as session, session.begin():
                yield session
        except SQLAlchemyError, PostgresError, OSError:
            raise InboxUnavailable from None

    async def pending(
        self, *, now: datetime | None = None, limit: int = 100
    ) -> list[UUID]:
        now = now or datetime.now(UTC)
        async with self._session() as session:
            return list(
                (
                    await session.execute(
                        select(WebhookReceipt.id)
                        .where(
                            or_(
                                WebhookReceipt.processing_status == "PENDING",
                                and_(
                                    WebhookReceipt.processing_status == "PROCESSING",
                                    WebhookReceipt.lease_until <= now,
                                ),
                            ),
                            or_(
                                WebhookReceipt.retry_at.is_(None),
                                WebhookReceipt.retry_at <= now,
                            ),
                        )
                        .order_by(WebhookReceipt.received_at, WebhookReceipt.id)
                        .limit(limit)
                    )
                ).scalars()
            )

    async def accept(self, delivery_id: str, request: GithubRequest) -> UUID | None:
        async with self._session() as session:
            repository = (
                await session.execute(
                    select(Repository)
                    .where(
                        Repository.provider_name == "github",
                        Repository.external_id == request.repository_external_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if repository is None:
                return None
            stored_request = replace(
                request, owner=repository.owner, name=repository.repo_name
            )
            settings = (
                await session.get(RepositorySettings, repository.current_settings_id)
                if repository.current_settings_id
                else None
            )
            ignore_settings = settings.ignores if settings is not None else {}
            if not isinstance(ignore_settings, dict) or (
                ignore_settings and set(ignore_settings) != {"globs"}
            ):
                raise InboxUnavailable
            ignores = ignore_settings.get("globs", [])
            if not isinstance(ignores, list) or any(
                not isinstance(pattern, str) for pattern in ignores
            ):
                raise InboxUnavailable
            return (
                await session.execute(
                    insert(WebhookReceipt)
                    .values(
                        provider_name="github",
                        delivery_id=delivery_id,
                        processing_status="PENDING",
                        request_data={
                            "schema_version": "1.0",
                            "request": asdict(stored_request),
                            "ignore_globs": ignores,
                        },
                    )
                    .on_conflict_do_nothing(
                        constraint="uq_webhook_receipt_provider_delivery"
                    )
                    .returning(WebhookReceipt.id)
                )
            ).scalar_one_or_none()

    async def get(self, receipt_id: UUID) -> WebhookDelivery | None:
        async with self._session() as session:
            receipt = await session.get(WebhookReceipt, receipt_id)
            if receipt is None or receipt.request_data is None:
                return None
            return WebhookDelivery(
                receipt.id,
                receipt.processing_status,
                GithubRequest(**receipt.request_data["request"]),
                receipt.snapshot_data,
                receipt.attempts,
                receipt.raw_diff,
                receipt.reason_code,
                tuple(receipt.request_data["ignore_globs"]),
            )

    async def get_by_delivery(self, delivery_id: str) -> WebhookDelivery | None:
        async with self._session() as session:
            receipt_id = (
                await session.execute(
                    select(WebhookReceipt.id).where(
                        WebhookReceipt.provider_name == "github",
                        WebhookReceipt.delivery_id == delivery_id,
                    )
                )
            ).scalar_one_or_none()
        return await self.get(receipt_id) if receipt_id is not None else None

    async def claim(
        self, receipt_id: UUID, *, now: datetime | None = None
    ) -> WebhookDelivery | None:
        now = now or datetime.now(UTC)
        async with self._session() as session:
            await session.execute(
                update(WebhookReceipt)
                .where(
                    WebhookReceipt.id == receipt_id,
                    WebhookReceipt.processing_status == "PROCESSING",
                    WebhookReceipt.lease_until <= now,
                    WebhookReceipt.attempts >= _MAX_ATTEMPTS,
                )
                .values(
                    processing_status="FAILED",
                    reason_code="VCS_UNAVAILABLE",
                    lease_until=None,
                )
            )
            receipt = (
                await session.execute(
                    update(WebhookReceipt)
                    .where(
                        WebhookReceipt.id == receipt_id,
                        WebhookReceipt.attempts < _MAX_ATTEMPTS,
                        or_(
                            WebhookReceipt.retry_at.is_(None),
                            WebhookReceipt.retry_at <= now,
                        ),
                        or_(
                            WebhookReceipt.processing_status == "PENDING",
                            and_(
                                WebhookReceipt.processing_status == "PROCESSING",
                                WebhookReceipt.lease_until <= now,
                            ),
                        ),
                    )
                    .values(
                        processing_status="PROCESSING",
                        attempts=WebhookReceipt.attempts + 1,
                        lease_until=now + timedelta(seconds=90),
                    )
                    .returning(WebhookReceipt)
                )
            ).scalar_one_or_none()
            if receipt is None or receipt.request_data is None:
                return None
            return WebhookDelivery(
                receipt.id,
                receipt.processing_status,
                GithubRequest(**receipt.request_data["request"]),
                receipt.snapshot_data,
                receipt.attempts,
                receipt.raw_diff,
                receipt.reason_code,
                tuple(receipt.request_data["ignore_globs"]),
            )

    async def _lock_claim(self, session: AsyncSession, claim: WebhookDelivery) -> bool:
        # Check expiry only after any row-lock wait, not before it.
        receipt_id = await session.scalar(
            select(WebhookReceipt.id)
            .where(
                WebhookReceipt.id == claim.id,
                WebhookReceipt.processing_status == "PROCESSING",
                WebhookReceipt.attempts == claim.attempts,
            )
            .with_for_update()
        )
        return receipt_id is not None

    async def complete(
        self, claim: WebhookDelivery, snapshot: dict[str, Any], raw_diff: str
    ) -> bool:
        async with self._session() as session:
            if not await self._lock_claim(session, claim):
                return False
            result = await session.execute(
                update(WebhookReceipt)
                .where(
                    WebhookReceipt.id == claim.id,
                    WebhookReceipt.processing_status == "PROCESSING",
                    WebhookReceipt.attempts == claim.attempts,
                    WebhookReceipt.lease_until > func.clock_timestamp(),
                )
                .values(
                    processing_status="READY",
                    snapshot_data=snapshot,
                    raw_diff=raw_diff,
                    reason_code=None,
                    lease_until=None,
                    retry_at=None,
                )
                .returning(WebhookReceipt.id)
            )
            return result.scalar_one_or_none() is not None

    async def fail(
        self,
        claim: WebhookDelivery,
        failure: GithubFailure,
        *,
        now: datetime | None = None,
    ) -> None:
        retry = failure.retryable and claim.attempts < _MAX_ATTEMPTS
        async with self._session() as session:
            if not await self._lock_claim(session, claim):
                return
            lease_clock = now if now is not None else func.clock_timestamp()
            now = now or datetime.now(UTC)
            await session.execute(
                update(WebhookReceipt)
                .where(
                    WebhookReceipt.id == claim.id,
                    WebhookReceipt.processing_status == "PROCESSING",
                    WebhookReceipt.attempts == claim.attempts,
                    WebhookReceipt.lease_until > lease_clock,
                )
                .values(
                    processing_status="PENDING" if retry else "FAILED",
                    reason_code=failure.reason_code,
                    lease_until=None,
                    retry_at=now + timedelta(seconds=failure.retry_after or 60)
                    if retry
                    else None,
                )
            )
