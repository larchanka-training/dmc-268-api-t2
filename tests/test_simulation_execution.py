"""Coordinator tests through the application interface; no providers involved."""

import asyncio
from contextlib import suppress
from uuid import UUID

import pytest

from app.adapters.simulation_admission import AdmissionResult, Lease, ReleaseResult
from app.application.simulation_execution import SimulationExecutionCoordinator

USER = UUID("00000000-0000-0000-0000-000000000001")
REPOSITORY = UUID("00000000-0000-0000-0000-000000000002")
KEY = b" Exact-Key\xff "
OWNER = "a" * 64


class Admission:
    def __init__(self, result: object = AdmissionResult("ACQUIRED", Lease(OWNER, 1))):
        self.result = result
        self.acquisitions: list[tuple[UUID, UUID, bytes]] = []
        self.releases: list[str] = []

    async def acquire(
        self, user_id: UUID, repository_id: UUID, idempotency_key: bytes
    ) -> AdmissionResult:
        self.acquisitions.append((user_id, repository_id, idempotency_key))
        return self.result  # type: ignore[return-value]

    async def release(self, owner_token: str) -> ReleaseResult:
        self.releases.append(owner_token)
        return "RELEASED"


class Executor:
    def __init__(self) -> None:
        self.deadlines: list[float] = []

    async def __call__(self, deadline: float) -> str:
        self.deadlines.append(deadline)
        return "verified-result"


@pytest.mark.parametrize(
    "status",
    [
        "BUSY",
        "DUPLICATE",
        "RATE_USER",
        "RATE_REPOSITORY",
        "RATE_GLOBAL",
        "INVALID_STATE",
        "UNAVAILABLE",
    ],
)
async def test_denied_admission_never_dispatches_or_releases(status: str) -> None:
    admission = Admission(AdmissionResult(status))  # type: ignore[arg-type]
    executor = Executor()
    result = await SimulationExecutionCoordinator[str](admission).execute(
        USER, REPOSITORY, KEY, executor
    )
    assert result.status == status
    assert executor.deadlines == []
    assert admission.releases == []
    assert admission.acquisitions == [(USER, REPOSITORY, KEY)]


async def test_confirmed_admission_dispatches_exactly_once() -> None:
    admission = Admission()
    executor = Executor()
    result = await SimulationExecutionCoordinator[str](admission).execute(
        USER, REPOSITORY, KEY, executor
    )
    assert result.status == "SUCCESS"
    assert result.value == "verified-result"
    assert len(executor.deadlines) == 1
    assert admission.acquisitions == [(USER, REPOSITORY, KEY)]


@pytest.mark.parametrize(
    "malformed",
    [
        None,
        "ACQUIRED",
        {"status": "ACQUIRED"},
        AdmissionResult("ACQUIRED"),
        AdmissionResult("ACQUIRED", Lease("", 1)),
        AdmissionResult("ACQUIRED", Lease(OWNER, 0)),
        AdmissionResult("BUSY", Lease(OWNER, 1)),
        AdmissionResult("surprise"),  # type: ignore[arg-type]
    ],
)
async def test_malformed_admission_fails_closed(malformed: object) -> None:
    admission = Admission(malformed)
    executor = Executor()
    result = await SimulationExecutionCoordinator[str](admission).execute(
        USER, REPOSITORY, KEY, executor
    )
    assert result.status == "INVALID_STATE"
    assert executor.deadlines == []
    assert admission.releases == []


async def test_deadline_starts_before_admission() -> None:
    executor = Executor()
    loop = asyncio.get_running_loop()

    class SlowAdmission(Admission):
        async def acquire(self, *args: object) -> AdmissionResult:
            await asyncio.sleep(0.01)
            return AdmissionResult("ACQUIRED", Lease(OWNER, 1))

    started = loop.time()
    result = await SimulationExecutionCoordinator[str](SlowAdmission()).execute(
        USER, REPOSITORY, KEY, executor
    )
    assert result.status == "SUCCESS"
    assert started + 29.9 <= executor.deadlines[0] <= started + 30.002


async def test_expired_budget_before_dispatch_never_executes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()
    now = loop.time()

    class ExpiringAdmission(Admission):
        async def acquire(self, *args: object) -> AdmissionResult:
            monkeypatch.setattr(loop, "time", lambda: now + 31)
            return AdmissionResult("ACQUIRED", Lease(OWNER, 1))

    executor = Executor()
    result = await SimulationExecutionCoordinator[str](ExpiringAdmission()).execute(
        USER, REPOSITORY, KEY, executor
    )
    assert result.status == "TIMEOUT"
    assert executor.deadlines == []


async def test_confirmed_but_undispatched_lease_is_released_on_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()
    now = loop.time()

    class ExpiringAdmission(Admission):
        async def acquire(self, *args: object) -> AdmissionResult:
            monkeypatch.setattr(loop, "time", lambda: now + 31)
            return AdmissionResult("ACQUIRED", Lease(OWNER, 1))

    admission = ExpiringAdmission()
    result = await SimulationExecutionCoordinator[str](admission).execute(
        USER, REPOSITORY, KEY, Executor()
    )
    assert result.status == "TIMEOUT"
    assert result.cleanup == "RELEASED"
    assert admission.releases == [OWNER]


async def test_execution_deadline_returns_without_waiting_for_ignored_cancel() -> None:
    admission = Admission()
    finish = asyncio.Event()
    started = asyncio.Event()
    cancelled = asyncio.Event()
    calls = 0

    async def stubborn(deadline: float) -> str:
        nonlocal calls
        calls += 1
        started.set()
        try:
            await finish.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await finish.wait()
        return "late-private-result"

    coordinator = SimulationExecutionCoordinator[str](admission, timeout_s=0.01)
    request = asyncio.create_task(coordinator.execute(USER, REPOSITORY, KEY, stubborn))
    await started.wait()
    try:
        done, _ = await asyncio.wait({request}, timeout=0.1)
        assert request in done, "caller still blocked by executor"
        result = request.result()
        assert result.status == "TIMEOUT"
        assert result.value is None
        await asyncio.wait_for(cancelled.wait(), 0.1)
        assert admission.releases == []
        assert calls == 1
    finally:
        finish.set()
        await request


async def test_late_success_is_never_reported_as_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()
    now = loop.time()
    admission = Admission()

    async def late(deadline: float) -> str:
        monkeypatch.setattr(loop, "time", lambda: now + 31)
        return "late-private-result"

    result = await SimulationExecutionCoordinator[str](admission).execute(
        USER, REPOSITORY, KEY, late
    )
    assert result.status == "TIMEOUT"
    assert result.value is None
    assert admission.releases == []


async def test_admission_wait_is_part_of_bounded_budget() -> None:
    finish = asyncio.Event()

    class StalledAdmission(Admission):
        async def acquire(self, *args: object) -> AdmissionResult:
            await finish.wait()
            return AdmissionResult("ACQUIRED", Lease(OWNER, 1))

    admission = StalledAdmission()
    executor = Executor()
    coordinator = SimulationExecutionCoordinator[str](admission, timeout_s=0.01)
    request = asyncio.create_task(coordinator.execute(USER, REPOSITORY, KEY, executor))
    try:
        done, _ = await asyncio.wait({request}, timeout=0.1)
        assert request in done, "admission exceeded total budget"
        assert request.result().status == "TIMEOUT"
        assert executor.deadlines == []
        assert admission.releases == []
    finally:
        finish.set()
        await request


async def test_success_releases_only_confirmed_owner() -> None:
    admission = Admission()
    result = await SimulationExecutionCoordinator[str](admission).execute(
        USER, REPOSITORY, KEY, Executor()
    )
    assert result.status == "SUCCESS"
    assert result.value == "verified-result"
    assert result.cleanup == "RELEASED"
    assert admission.releases == [OWNER]


@pytest.mark.parametrize("error", [RuntimeError, TimeoutError, ValueError])
async def test_unknown_provider_failure_retains_lease_and_is_private(
    error: type[Exception],
) -> None:
    admission = Admission()
    calls = 0

    async def failed(deadline: float) -> str:
        nonlocal calls
        calls += 1
        raise error("PRIVATE prompt source rules response credentials")

    result = await SimulationExecutionCoordinator[str](admission).execute(
        USER, REPOSITORY, KEY, failed
    )
    assert result.status == "EXECUTION_FAILED"
    assert result.value is None
    assert "PRIVATE" not in repr(result)
    assert result.cleanup == "NOT_ATTEMPTED"
    assert admission.releases == []
    assert calls == 1


@pytest.mark.parametrize("cleanup", ["UNAVAILABLE", "INVALID_STATE", "NOT_OWNER"])
async def test_release_denial_preserves_original_success(
    cleanup: ReleaseResult,
) -> None:
    class DeniedRelease(Admission):
        async def release(self, owner_token: str) -> ReleaseResult:
            self.releases.append(owner_token)
            return cleanup

    admission = DeniedRelease()
    executor = Executor()
    result = await SimulationExecutionCoordinator[str](admission).execute(
        USER, REPOSITORY, KEY, executor
    )
    assert result.status == "SUCCESS"
    assert result.value == "verified-result"
    assert result.cleanup == cleanup
    assert admission.releases == [OWNER]
    assert len(executor.deadlines) == 1


@pytest.mark.parametrize("error", [TimeoutError, RuntimeError])
async def test_release_exception_preserves_original_success(
    error: type[Exception],
) -> None:
    class FailedRelease(Admission):
        async def release(self, owner_token: str) -> ReleaseResult:
            self.releases.append(owner_token)
            raise error("PRIVATE Redis state secret")

    admission = FailedRelease()
    executor = Executor()
    result = await SimulationExecutionCoordinator[str](admission).execute(
        USER, REPOSITORY, KEY, executor
    )
    assert result.status == "SUCCESS"
    assert result.value == "verified-result"
    assert result.cleanup == "UNAVAILABLE"
    assert "PRIVATE" not in repr(result)
    assert len(executor.deadlines) == 1
    assert admission.releases == [OWNER]


async def test_malformed_release_has_only_safe_cleanup_code() -> None:
    class BadRelease(Admission):
        async def release(self, owner_token: str) -> ReleaseResult:
            return "PRIVATE raw state"  # type: ignore[return-value]

    result = await SimulationExecutionCoordinator[str](BadRelease()).execute(
        USER, REPOSITORY, KEY, Executor()
    )
    assert result.status == "SUCCESS"
    assert result.cleanup == "UNAVAILABLE"
    assert "PRIVATE" not in repr(result)


async def test_cleanup_wait_is_bounded_even_when_release_ignores_cancel() -> None:
    finish = asyncio.Event()
    started = asyncio.Event()

    class StalledRelease(Admission):
        async def release(self, owner_token: str) -> ReleaseResult:
            self.releases.append(owner_token)
            started.set()
            try:
                await finish.wait()
            except asyncio.CancelledError:
                await finish.wait()
            return "NOT_OWNER"

    admission = StalledRelease()
    coordinator = SimulationExecutionCoordinator[str](admission, cleanup_timeout_s=0.01)
    executor = Executor()
    request = asyncio.create_task(coordinator.execute(USER, REPOSITORY, KEY, executor))
    await started.wait()
    try:
        done, _ = await asyncio.wait({request}, timeout=0.1)
        assert request in done, "cleanup still blocking caller"
        assert request.result().status == "SUCCESS"
        assert request.result().value == "verified-result"
        assert request.result().cleanup == "TIMEOUT"
        assert len(executor.deadlines) == 1
    finally:
        finish.set()
        await request


@pytest.mark.parametrize("phase", ["before", "admission", "execution", "cleanup"])
async def test_caller_cancellation_always_propagates(phase: str) -> None:
    entered = asyncio.Event()
    finish = asyncio.Event()

    class CancellableAdmission(Admission):
        async def acquire(self, *args: object) -> AdmissionResult:
            if phase == "admission":
                entered.set()
                await finish.wait()
            return await super().acquire(USER, REPOSITORY, KEY)

        async def release(self, owner_token: str) -> ReleaseResult:
            if phase == "cleanup":
                entered.set()
                await finish.wait()
            return await super().release(owner_token)

    async def execute(deadline: float) -> str:
        if phase == "execution":
            entered.set()
            await finish.wait()
        return "verified-result"

    admission = CancellableAdmission()
    coordinator = SimulationExecutionCoordinator[str](admission)
    request = asyncio.create_task(coordinator.execute(USER, REPOSITORY, KEY, execute))
    if phase != "before":
        await asyncio.wait_for(entered.wait(), 0.2)
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    await asyncio.sleep(0)
    assert admission.releases == []
    if phase == "before":
        assert admission.acquisitions == []


async def test_cancel_after_acquired_before_dispatch_releases_known_lease() -> None:
    executor = Executor()
    request = asyncio.current_task()
    assert request is not None

    class CancellingAdmission(Admission):
        async def acquire(self, *args: object) -> AdmissionResult:
            assert request is not None
            asyncio.get_running_loop().call_soon(request.cancel)
            return AdmissionResult("ACQUIRED", Lease(OWNER, 1))

    admission = CancellingAdmission()
    with pytest.raises(asyncio.CancelledError):
        await SimulationExecutionCoordinator[str](admission).execute(
            USER, REPOSITORY, KEY, executor
        )
    assert executor.deadlines == []
    assert admission.releases == [OWNER]


async def test_cancellation_before_scheduled_executor_starts_releases_lease() -> None:
    admission = Admission()
    executor = Executor()
    loop = asyncio.get_running_loop()
    request = asyncio.current_task()
    assert request is not None
    # A task factory is an event-loop scheduling boundary: cancel the caller
    # when dispatch is queued, before the callback can have run.
    created = 0

    def factory(
        loop: asyncio.AbstractEventLoop, coro: object, **kwargs: object
    ) -> asyncio.Task[object]:
        nonlocal created
        created += 1
        task: asyncio.Task[object] = asyncio.Task(coro, loop=loop, **kwargs)  # type: ignore[arg-type]
        if created == 2:  # noqa: PLR2004 -- second boundary is queued dispatch
            assert request is not None
            request.cancel()
        return task

    loop.set_task_factory(factory)  # type: ignore[arg-type]
    try:
        with pytest.raises(asyncio.CancelledError):
            await SimulationExecutionCoordinator[str](admission).execute(
                USER, REPOSITORY, KEY, executor
            )
    finally:
        loop.set_task_factory(None)
    assert executor.deadlines == []
    assert admission.releases == [OWNER]


async def test_reentrant_request_cannot_overwrite_task_supervision() -> None:
    executor = Executor()
    nested: list[str] = []
    attempts = 0

    class ReentrantAdmission(Admission):
        async def acquire(self, *args: object) -> AdmissionResult:
            nonlocal attempts
            attempts += 1
            if attempts > 1:
                return AdmissionResult("BUSY")
            task = asyncio.current_task()
            assert task is not None

            async def second_request() -> None:
                outcome = await coordinator.execute(USER, REPOSITORY, KEY, executor)
                nested.append(outcome.status)

            # Exercise an eager caller at completion, before the first caller
            # has resumed from admission. This is supported by Python 3.14.
            task.add_done_callback(
                lambda _: asyncio.create_task(second_request(), eager_start=True)
            )
            return AdmissionResult("ACQUIRED", Lease(OWNER, 1))

    coordinator = SimulationExecutionCoordinator[str](ReentrantAdmission())
    await coordinator.execute(USER, REPOSITORY, KEY, executor)
    await asyncio.sleep(0)
    assert nested == ["UNAVAILABLE"]
    assert attempts == 1


async def test_unknown_admission_exception_is_fail_closed_and_private() -> None:
    class FailedAdmission(Admission):
        async def acquire(self, *args: object) -> AdmissionResult:
            raise RuntimeError("PRIVATE key and Redis state")

    admission = FailedAdmission()
    executor = Executor()
    result = await SimulationExecutionCoordinator[str](admission).execute(
        USER, REPOSITORY, KEY, executor
    )
    assert result.status == "UNAVAILABLE"
    assert result.value is None
    assert "PRIVATE" not in repr(result)
    assert executor.deadlines == []
    assert admission.releases == []


@pytest.mark.parametrize("budget", [0, -1, 30.001, float("inf"), float("nan")])
def test_execution_budget_cannot_exceed_safety_bound(budget: float) -> None:
    with pytest.raises(ValueError, match="invalid_execution_budget"):
        SimulationExecutionCoordinator[str](Admission(), timeout_s=budget)


@pytest.mark.parametrize("budget", [0, -1, 2.001, float("inf"), float("nan")])
def test_cleanup_budget_cannot_exceed_safety_bound(budget: float) -> None:
    with pytest.raises(ValueError, match="invalid_cleanup_budget"):
        SimulationExecutionCoordinator[str](Admission(), cleanup_timeout_s=budget)


async def test_late_exception_is_observed_and_shutdown_is_bounded() -> None:
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    errors: list[dict[str, object]] = []
    loop.set_exception_handler(lambda _, context: errors.append(context))
    finish = asyncio.Event()
    entered = asyncio.Event()
    completed = asyncio.Event()

    async def stubborn(deadline: float) -> str:
        entered.set()
        try:
            while not finish.is_set():
                with suppress(asyncio.CancelledError):
                    await finish.wait()
            raise RuntimeError("PRIVATE late provider exception")
        finally:
            completed.set()

    admission = Admission()
    coordinator = SimulationExecutionCoordinator[str](
        admission, timeout_s=0.01, cleanup_timeout_s=0.01
    )
    try:
        result = await coordinator.execute(USER, REPOSITORY, KEY, stubborn)
        assert entered.is_set()
        assert result.status == "TIMEOUT"
        assert await asyncio.wait_for(coordinator.aclose(), 0.1) is False
        executor = Executor()
        refused = await coordinator.execute(USER, REPOSITORY, b"different", executor)
        assert refused.status == "UNAVAILABLE"
        assert executor.deadlines == []
    finally:
        finish.set()
        await asyncio.wait_for(completed.wait(), 0.1)
        await asyncio.sleep(0)
        loop.set_exception_handler(previous_handler)
    assert await coordinator.aclose() is True
    assert admission.releases == []
    assert errors == []


async def test_admission_status_must_be_a_plain_status_code() -> None:
    class ForeignStatus(str):
        def __repr__(self) -> str:
            return "PRIVATE Redis contents"

    admission = Admission(AdmissionResult(ForeignStatus("BUSY")))  # type: ignore[arg-type]
    executor = Executor()
    result = await SimulationExecutionCoordinator[str](admission).execute(
        USER, REPOSITORY, KEY, executor
    )
    assert result.status == "INVALID_STATE"
    assert "PRIVATE" not in repr(result)
    assert executor.deadlines == []
    assert admission.releases == []


async def test_success_after_caller_cancel_never_releases_uncertain_inference() -> None:
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    finish = asyncio.Event()
    completed = asyncio.Event()
    admission = Admission()

    async def executor(deadline: float) -> str:
        entered.set()
        try:
            await finish.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await finish.wait()
        completed.set()
        return "late-success"

    coordinator = SimulationExecutionCoordinator[str](admission)
    request = asyncio.create_task(coordinator.execute(USER, REPOSITORY, KEY, executor))
    await asyncio.wait_for(entered.wait(), 0.1)
    try:
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(request, 0.1)
        await asyncio.wait_for(cancelled.wait(), 0.1)
        refused = await coordinator.execute(USER, REPOSITORY, b"another", Executor())
        assert refused.status == "UNAVAILABLE"
        assert len(admission.acquisitions) == 1
    finally:
        finish.set()
        await asyncio.wait_for(completed.wait(), 0.1)
    assert await coordinator.aclose()
    assert admission.releases == []


async def test_ignored_admission_cancellation_cannot_dispatch_late_lease() -> None:
    finish = asyncio.Event()
    completed = asyncio.Event()

    class StubbornAdmission(Admission):
        async def acquire(self, *args: object) -> AdmissionResult:
            while not finish.is_set():
                with suppress(asyncio.CancelledError):
                    await finish.wait()
            completed.set()
            return AdmissionResult("ACQUIRED", Lease(OWNER, 1))

    admission = StubbornAdmission()
    executor = Executor()
    coordinator = SimulationExecutionCoordinator[str](
        admission, timeout_s=0.01, cleanup_timeout_s=0.01
    )
    try:
        result = await asyncio.wait_for(
            coordinator.execute(USER, REPOSITORY, KEY, executor), 0.1
        )
        assert result.status == "TIMEOUT"
        assert await coordinator.aclose() is False
    finally:
        finish.set()
        await asyncio.wait_for(completed.wait(), 0.1)
    assert await coordinator.aclose()
    assert executor.deadlines == []
    assert admission.releases == []


async def test_closed_coordinator_does_not_admit_even_after_drain() -> None:
    admission = Admission()
    coordinator = SimulationExecutionCoordinator[str](admission)
    assert await coordinator.aclose()
    result = await coordinator.execute(USER, REPOSITORY, KEY, Executor())
    assert result.status == "UNAVAILABLE"
    assert admission.acquisitions == []
