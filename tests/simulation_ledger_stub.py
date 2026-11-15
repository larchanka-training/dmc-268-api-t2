"""Explicit ledger boundary for legacy Redis/coordinator characterization.

These tests exercise Redis or coordinator lifecycle behavior in isolation. Real
PostgreSQL authorization, deduplication and quotas belong to simulation_ledger.
"""

from typing import Literal
from uuid import UUID

from app.adapters.simulation_dispatch_ledger import ClaimStatus


class IsolatedSimulationLedger:
    async def claim(
        self, user_id: UUID, repository_id: UUID, idempotency_key: bytes
    ) -> ClaimStatus:
        return "AUTHORIZED"

    async def block(self) -> None:
        pass

    async def finish(
        self,
        user_id: UUID,
        repository_id: UUID,
        idempotency_key: bytes,
        state: Literal["COMPLETED", "UNKNOWN"],
    ) -> None:
        pass
