"""Execution-local cancellation; PostgreSQL remains the admission authority."""

import asyncio
import time
from collections.abc import Callable
from contextvars import ContextVar

from app.application.ports import LeaseLostError, TaskLeaseSnapshot


class LiveAnalysisAuthority:
    def __init__(
        self,
        lease: TaskLeaseSnapshot,
        *,
        started: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.lease = lease
        self._clock = clock
        self._expires = float("inf")
        self._lost = False
        self._changed = asyncio.Event()
        self.confirm(lease, started=started)

    def check(self) -> None:
        if self._lost or self._clock() >= self._expires:
            self.cancel()
            raise LeaseLostError("Last confirmed lease authority expired")

    def cancel(self) -> None:
        self._lost = True
        self._changed.set()

    def confirm(self, lease: TaskLeaseSnapshot, *, started: float) -> None:
        self.check()
        if (lease.review_job_id, lease.task_kind, lease.owner, lease.fencing_token) != (
            self.lease.review_job_id,
            self.lease.task_kind,
            self.lease.owner,
            self.lease.fencing_token,
        ):
            self.cancel()
            raise LeaseLostError("Lease renewal identity changed")
        sampled = lease.sampled_at
        if sampled is None:
            self.cancel()
            raise LeaseLostError("Lease confirmation requires a DB clock sample")
        self._expires = started + (lease.expires_at - sampled).total_seconds()
        self._changed.set()
        self.check()

    async def watch(self) -> None:
        while True:
            self.check()
            self._changed.clear()
            try:
                async with asyncio.timeout(max(0, self._expires - self._clock())):
                    await self._changed.wait()
            except TimeoutError:
                self.check()


current_analysis_authority: ContextVar[LiveAnalysisAuthority | None] = ContextVar(
    "current_analysis_authority", default=None
)
