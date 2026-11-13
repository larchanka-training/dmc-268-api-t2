"""Bounded, inactive application coordinator; no HTTP/provider composition.

Reuse one coordinator on one event loop and keep its admission/executor resources
alive until ``aclose()`` returns True. Each instance owns at most one outstanding
boundary operation; overlapping calls fail closed with UNAVAILABLE. Closing has
its own bounded wait and can return False for work that ignores cancellation.
The task stays strongly referenced and its eventual exception is retrieved.
Creating replacement instances per timed-out call would bypass this local bound.

Executor receives an absolute deadline in the running loop's monotonic clock.
It must yield to the event loop, make a single attempt, own any resources it opens,
and return only a validated result after the protected inference has *confirmed*
successfully completed. Normal return is the sole post-dispatch release signal.
Exceptions, local task cancellation/completion and late return do not establish
remote termination. Lease expiry is the conservative recovery path: this is
admission suppression, not exactly-once execution or remote cancellation.

The 30s budget includes admission and preparation of the outcome; only cleanup
has an additional budget, at most 2s. Async deadlines require a responsive event
loop: blocking synchronous executor code cannot be preempted by Python tasks.
No retries, refunds, Redis payload writes, provider diagnostics or logging.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol
from uuid import UUID

from app.adapters.simulation_admission import AdmissionResult, Lease, ReleaseResult

_EXECUTION_BUDGET_S = 30.0
_CLEANUP_BUDGET_S = 2.0
_OWNER_HEX_LENGTH = 64

type ExecutionStatus = Literal[
    "SUCCESS",
    "DUPLICATE",
    "BUSY",
    "RATE_USER",
    "RATE_REPOSITORY",
    "RATE_GLOBAL",
    "INVALID_STATE",
    "UNAVAILABLE",
    "TIMEOUT",
    "EXECUTION_FAILED",
]
type CleanupStatus = Literal[
    "NOT_ATTEMPTED",
    "RELEASED",
    "NOT_OWNER",
    "INVALID_STATE",
    "UNAVAILABLE",
    "TIMEOUT",
]


class _DeadlineExceededError(Exception):
    pass


async def _invoke[R](operation: Callable[[], Awaitable[R]]) -> R:
    return await operation()


def _valid_admission(result: object) -> bool:
    if type(result) is not AdmissionResult:
        return False
    if type(result.status) is not str:
        return False
    if result.status == "ACQUIRED":
        lease = result.lease
        return (
            type(lease) is Lease
            and type(lease.owner_token) is str
            and len(lease.owner_token) == _OWNER_HEX_LENGTH
            and all(ch in "0123456789abcdef" for ch in lease.owner_token)
            and type(lease.expires_at_ms) is int
            and 0 < lease.expires_at_ms <= 2**53 - 1
        )
    return result.lease is None and result.status in (
        "BUSY",
        "DUPLICATE",
        "RATE_USER",
        "RATE_REPOSITORY",
        "RATE_GLOBAL",
        "INVALID_STATE",
        "UNAVAILABLE",
    )


class SimulationAdmission(Protocol):
    async def acquire(
        self, user_id: UUID, repository_id: UUID, idempotency_key: bytes
    ) -> AdmissionResult: ...

    async def release(self, owner_token: str) -> ReleaseResult: ...


@dataclass(frozen=True, slots=True)
class ExecutionOutcome[T]:
    status: ExecutionStatus
    value: T | None = field(default=None, repr=False)
    cleanup: CleanupStatus = "NOT_ATTEMPTED"


class SimulationExecutionCoordinator[T]:
    def __init__(
        self,
        admission: SimulationAdmission,
        *,
        timeout_s: float = _EXECUTION_BUDGET_S,
        cleanup_timeout_s: float = _CLEANUP_BUDGET_S,
    ) -> None:
        if not 0 < timeout_s <= _EXECUTION_BUDGET_S:
            raise ValueError("invalid_execution_budget")
        if not 0 < cleanup_timeout_s <= _CLEANUP_BUDGET_S:
            raise ValueError("invalid_cleanup_budget")
        self._admission = admission
        self._timeout_s = timeout_s
        self._cleanup_timeout_s = cleanup_timeout_s
        self._pending: asyncio.Task[object] | None = None
        self._running = False
        self._closed = False

    async def aclose(self) -> bool:
        """Stop accepting work; True permits disposal of caller-owned resources.

        False means an operation still owns those resources. Retain this instance
        and the resources, then call again after that operation completes. No task
        result/exception is logged or exposed. Cancellation of aclose propagates.
        """
        self._closed = True
        task = self._pending
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait({task}, timeout=self._cleanup_timeout_s)
        return not self._running and (task is None or task.done())

    def _observe(self, task: asyncio.Task[object]) -> None:
        if not task.cancelled():
            task.exception()
        if self._pending is task:
            self._pending = None

    def _start[R](self, operation: Callable[[], Awaitable[R]]) -> asyncio.Task[R]:
        task = asyncio.create_task(_invoke(operation))
        self._pending = task
        task.add_done_callback(self._observe)
        return task

    async def _wait[R](self, task: asyncio.Task[R], deadline: float) -> R:
        try:
            done, _ = await asyncio.wait(
                {task}, timeout=max(0, deadline - asyncio.get_running_loop().time())
            )
        except asyncio.CancelledError:
            task.cancel()
            raise
        if not done:
            task.cancel()
            raise _DeadlineExceededError
        return task.result()

    async def _release(self, owner_token: str) -> CleanupStatus:
        deadline = asyncio.get_running_loop().time() + self._cleanup_timeout_s
        task = self._start(lambda: self._admission.release(owner_token))
        try:
            result = await self._wait(task, deadline)
        except _DeadlineExceededError:
            return "TIMEOUT"
        except Exception:
            return "UNAVAILABLE"
        if type(result) is str and result in (
            "RELEASED",
            "NOT_OWNER",
            "INVALID_STATE",
            "UNAVAILABLE",
        ):
            return result
        return "UNAVAILABLE"

    async def execute(
        self,
        user_id: UUID,
        repository_id: UUID,
        idempotency_key: bytes,
        executor: Callable[[float], Awaitable[T]],
    ) -> ExecutionOutcome[T]:
        if (
            self._closed
            or self._running
            or (self._pending is not None and not self._pending.done())
        ):
            return ExecutionOutcome("UNAVAILABLE")
        self._running = True
        try:
            return await self._execute(
                user_id, repository_id, idempotency_key, executor
            )
        finally:
            self._running = False

    async def _execute(
        self,
        user_id: UUID,
        repository_id: UUID,
        idempotency_key: bytes,
        executor: Callable[[float], Awaitable[T]],
    ) -> ExecutionOutcome[T]:
        deadline = asyncio.get_running_loop().time() + self._timeout_s
        task = self._start(
            lambda: self._admission.acquire(user_id, repository_id, idempotency_key)
        )
        try:
            result = await self._wait(task, deadline)
        except _DeadlineExceededError:
            return ExecutionOutcome("TIMEOUT")
        except Exception:
            return ExecutionOutcome("UNAVAILABLE")
        except asyncio.CancelledError:
            if task.done() and not task.cancelled() and task.exception() is None:
                confirmed = task.result()
                if _valid_admission(confirmed) and confirmed.status == "ACQUIRED":
                    assert confirmed.lease is not None
                    await self._release(confirmed.lease.owner_token)
            raise
        if not _valid_admission(result):
            return ExecutionOutcome("INVALID_STATE")
        if result.status != "ACQUIRED":
            return ExecutionOutcome(result.status)
        assert result.lease is not None
        return await self._execute_admitted(result.lease, executor, deadline)

    async def _execute_admitted(
        self, lease: Lease, executor: Callable[[float], Awaitable[T]], deadline: float
    ) -> ExecutionOutcome[T]:
        if asyncio.get_running_loop().time() >= deadline:
            cleanup = await self._release(lease.owner_token)
            return ExecutionOutcome("TIMEOUT", cleanup=cleanup)
        caller = asyncio.current_task()
        dispatched = False

        async def dispatch() -> T:
            nonlocal dispatched
            if caller is not None and caller.cancelling():
                raise asyncio.CancelledError
            if asyncio.get_running_loop().time() >= deadline:
                raise _DeadlineExceededError
            dispatched = True
            return await executor(deadline)

        execution = self._start(dispatch)
        try:
            value = await self._wait(execution, deadline)
        except _DeadlineExceededError:
            cleanup = "NOT_ATTEMPTED"
            if not dispatched:
                cleanup = await self._release(lease.owner_token)
            return ExecutionOutcome("TIMEOUT", cleanup=cleanup)
        except asyncio.CancelledError:
            if not dispatched:
                await self._release(lease.owner_token)
            raise
        except Exception:
            return ExecutionOutcome("EXECUTION_FAILED")
        if asyncio.get_running_loop().time() >= deadline:
            return ExecutionOutcome("TIMEOUT")
        cleanup = await self._release(lease.owner_token)
        return ExecutionOutcome("SUCCESS", value, cleanup)
