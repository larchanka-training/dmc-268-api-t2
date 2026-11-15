"""PostgreSQL-only, one-shot authorization. Never replay or refund a claim."""

import asyncio
from typing import Literal
from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.simulation_hmac import (
    simulation_hmac_config_id,
    simulation_operation_digest,
)
from app.db.models import SimulationDispatchClaim, SimulationDispatchControl

_USER_HOURLY_LIMIT = 3
_REPOSITORY_HOURLY_LIMIT = 10
_GLOBAL_HOURLY_LIMIT = 20

type ClaimStatus = Literal[
    "AUTHORIZED",
    "DUPLICATE",
    "RATE_USER",
    "RATE_REPOSITORY",
    "RATE_GLOBAL",
    "UNAVAILABLE",
]


class PostgresSimulationDispatchLedger:
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], *, hmac_secret: bytes
    ) -> None:
        if not isinstance(hmac_secret, bytes) or not 32 <= len(hmac_secret) <= 1024:  # noqa: PLR2004
            raise ValueError("invalid_simulation_secret")
        self._sessions = sessions
        self._secret = hmac_secret
        self._blocked = False

    async def claim(
        self, user_id: UUID, repository_id: UUID, idempotency_key: bytes
    ) -> ClaimStatus:
        if self._blocked:
            return "UNAVAILABLE"
        try:
            return await self._claim(user_id, repository_id, idempotency_key)
        except SQLAlchemyError, OSError, TimeoutError:
            # Only a deny write may follow an uncertain commit, never a claim retry.
            await self.block()
            return "UNAVAILABLE"

    async def _claim(  # noqa: PLR0911 -- explicit fail-closed denial reasons
        self, user_id: UUID, repository_id: UUID, idempotency_key: bytes
    ) -> ClaimStatus:
        digest = simulation_operation_digest(
            self._secret, user_id, repository_id, idempotency_key
        )
        async with self._sessions() as session, session.begin():
            await session.execute(
                text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
            )
            await session.execute(text("SET LOCAL synchronous_commit = on"))
            # Shared by every API process; no connection survives this method.
            await session.execute(text("SELECT pg_advisory_xact_lock(268, 32)"))
            control = await session.get(SimulationDispatchControl, 1)
            if control is None or control.blocked:
                self._blocked = True
                return "UNAVAILABLE"
            config_id = simulation_hmac_config_id(self._secret)
            if control.hmac_config_id is None:
                control.hmac_config_id = config_id
            elif control.hmac_config_id != config_id:
                control.blocked = True
                self._blocked = True
                return "UNAVAILABLE"
            if (
                await session.scalar(
                    select(SimulationDispatchClaim.operation_digest).where(
                        SimulationDispatchClaim.operation_digest == digest
                    )
                )
                is not None
            ):
                return "DUPLICATE"
            cutoff = await session.scalar(
                text("SELECT clock_timestamp() - interval '1 hour'")
            )
            recent = (
                select(func.count())
                .select_from(SimulationDispatchClaim)
                .where(SimulationDispatchClaim.authorized_at >= cutoff)
            )
            user_count = (
                await session.execute(
                    recent.where(SimulationDispatchClaim.user_id == user_id)
                )
            ).scalar_one()
            if user_count >= _USER_HOURLY_LIMIT:
                return "RATE_USER"
            repository_count = (
                await session.execute(
                    recent.where(SimulationDispatchClaim.repository_id == repository_id)
                )
            ).scalar_one()
            if repository_count >= _REPOSITORY_HOURLY_LIMIT:
                return "RATE_REPOSITORY"
            if (await session.execute(recent)).scalar_one() >= _GLOBAL_HOURLY_LIMIT:
                return "RATE_GLOBAL"
            session.add(
                SimulationDispatchClaim(
                    operation_digest=digest,
                    user_id=user_id,
                    repository_id=repository_id,
                )
            )
        return "AUTHORIZED"

    async def finish(
        self,
        user_id: UUID,
        repository_id: UUID,
        idempotency_key: bytes,
        state: Literal["COMPLETED", "UNKNOWN"],
    ) -> None:
        if state not in ("COMPLETED", "UNKNOWN"):
            return
        digest = simulation_operation_digest(
            self._secret, user_id, repository_id, idempotency_key
        )
        try:
            async with self._sessions() as session, session.begin():
                await session.execute(
                    update(SimulationDispatchClaim)
                    .where(
                        SimulationDispatchClaim.operation_digest == digest,
                        SimulationDispatchClaim.state == "DISPATCH_AUTHORIZED",
                    )
                    .values(state=state)
                )
        except SQLAlchemyError, OSError, TimeoutError:
            # Outcome writes cannot rearm a claim. Storage faults deny new work.
            await self.block()

    async def block(self) -> None:
        """Deny locally immediately, persist globally when PostgreSQL is reachable."""
        self._blocked = True
        try:
            async with asyncio.timeout(2), self._sessions() as session, session.begin():
                await session.execute(text("SET LOCAL synchronous_commit = on"))
                await session.execute(text("SELECT pg_advisory_xact_lock(268, 32)"))
                await session.execute(
                    update(SimulationDispatchControl)
                    .where(SimulationDispatchControl.id == 1)
                    .values(blocked=True)
                )
        except SQLAlchemyError, OSError, TimeoutError:
            pass
