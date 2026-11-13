"""Coordinator compatibility with actual disposable Redis and RESP faults."""

# ruff: noqa: F811 -- imported pytest fixtures are injected by name

import asyncio
import json
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from test_simulation_admission import (  # existing real Redis/socket fixtures
    _ReplyGate,
    admissions,  # noqa: F401
    gated_admission,  # noqa: F401
    redis_client,  # noqa: F401
    state_key,  # noqa: F401
)

from app.adapters.simulation_admission import RedisSimulationAdmission, ReleaseResult
from app.application.simulation_execution import SimulationExecutionCoordinator


@pytest.mark.parametrize("ending", ["success", "exception", "timeout", "cancel"])
async def test_consumption_and_allowance_survive_every_execution_outcome(
    ending: str,
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
    redis_client: Redis,
    state_key: str,
) -> None:
    user, repository = uuid4(), uuid4()
    key = b" Exact-Case-\xff "
    entered = asyncio.Event()
    finish = asyncio.Event()
    calls = 0

    async def executor(deadline: float) -> str:
        nonlocal calls
        calls += 1
        entered.set()
        if ending == "exception":
            raise RuntimeError("private provider body")
        if ending in ("timeout", "cancel"):
            await finish.wait()
        return "validated-result"

    coordinator = SimulationExecutionCoordinator[str](admissions[0], timeout_s=0.05)
    request = asyncio.create_task(coordinator.execute(user, repository, key, executor))
    await asyncio.wait_for(entered.wait(), 1)
    if ending == "cancel":
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
    else:
        result = await request
        assert (
            result.status
            == {
                "success": "SUCCESS",
                "exception": "EXECUTION_FAILED",
                "timeout": "TIMEOUT",
            }[ending]
        )
        assert result.cleanup == (
            "RELEASED" if ending == "success" else "NOT_ATTEMPTED"
        )
    assert await coordinator.aclose()
    raw = await redis_client.get(state_key)
    assert raw is not None
    state = json.loads(raw)
    assert len(state["admissions"]) == len(state["consumed_keys"]) == 1
    assert (state["lease"] is None) == (ending == "success")
    duplicate = await SimulationExecutionCoordinator[str](admissions[1]).execute(
        user, repository, key, executor
    )
    assert duplicate.status == "DUPLICATE"
    assert calls == 1
    assert await redis_client.get(state_key) == raw
    # Simulate expiry without sleeping 90s; both histories must remain consumed.
    if state["lease"] is not None:
        state["lease"]["expires_at_ms"] = 1
        await redis_client.set(state_key, json.dumps(state))
    for extra in (b"second", b"third"):
        result = await SimulationExecutionCoordinator[str](admissions[1]).execute(
            user, repository, extra, lambda _: asyncio.sleep(0, result="ok")
        )
        assert result.status == "SUCCESS"
    assert (
        await admissions[1].acquire(user, repository, b"fourth")
    ).status == "RATE_USER"
    assert (await admissions[1].acquire(user, repository, key)).status == "DUPLICATE"


@pytest.mark.parametrize("loss", ["disconnect", "timeout", "cancel", "deadline"])
async def test_ambiguous_committed_admission_never_dispatches(
    loss: str,
    gated_admission: tuple[RedisSimulationAdmission, _ReplyGate],
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
    redis_client: Redis,
    state_key: str,
) -> None:
    admission, gate = gated_admission
    gate.mode = loss
    user, repository = uuid4(), uuid4()
    calls = 0

    async def executor(deadline: float) -> str:
        nonlocal calls
        calls += 1
        return "must-not-run"

    coordinator = SimulationExecutionCoordinator[str](
        admission, timeout_s=0.1 if loss == "deadline" else 2
    )
    request = asyncio.create_task(
        coordinator.execute(user, repository, b"lost", executor)
    )
    await asyncio.wait_for(gate.committed.wait(), 1)
    if loss == "cancel":
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
    else:
        result = await request
        assert result.status == ("TIMEOUT" if loss == "deadline" else "UNAVAILABLE")
    await asyncio.wait_for(gate.disconnected.wait(), 1)
    assert await coordinator.aclose()
    assert calls == 0
    assert gate.eval_connections == [1]
    raw = await redis_client.get(state_key)
    assert raw is not None
    state = json.loads(raw)
    assert state["lease"] is not None
    assert len(state["admissions"]) == len(state["consumed_keys"]) == 1
    assert (
        await admissions[1].acquire(user, repository, b"lost")
    ).status == "DUPLICATE"
    assert await redis_client.get(state_key) == raw


async def test_delayed_release_cannot_release_replacement_owner(
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
    redis_client: Redis,
    state_key: str,
) -> None:
    first, second = admissions
    user, repository = uuid4(), uuid4()
    replacement: list[str] = []

    class DelayedAdmission:
        acquire = first.acquire

        async def release(self, owner_token: str) -> ReleaseResult:
            raw = await redis_client.get(state_key)
            assert raw is not None
            state = json.loads(raw)
            state["lease"]["expires_at_ms"] = 1
            await redis_client.set(state_key, json.dumps(state))
            new = await second.acquire(user, repository, b"new-owner")
            assert new.lease is not None
            replacement.append(new.lease.owner_token)
            return await first.release(owner_token)

    result = await SimulationExecutionCoordinator[str](DelayedAdmission()).execute(
        user, repository, b"old-owner", lambda _: asyncio.sleep(0, result="verified")
    )
    assert result.status == "SUCCESS"
    assert result.value == "verified"
    assert result.cleanup == "NOT_OWNER"
    assert (await second.acquire(user, repository, b"third")).status == "BUSY"
    assert await second.release(replacement[0]) == "RELEASED"
    assert (await second.acquire(user, repository, b"old-owner")).status == "DUPLICATE"


@pytest.mark.parametrize("loss", ["timeout", "cancel"])
async def test_real_release_reply_loss_never_repeats_successful_execution(
    loss: str,
    gated_admission: tuple[RedisSimulationAdmission, _ReplyGate],
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
    redis_client: Redis,
    state_key: str,
) -> None:
    admission, gate = gated_admission
    user, repository = uuid4(), uuid4()
    calls = 0

    async def executor(deadline: float) -> str:
        nonlocal calls
        calls += 1
        gate.mode = loss
        return "verified-result"

    coordinator = SimulationExecutionCoordinator[str](admission)
    request = asyncio.create_task(
        coordinator.execute(user, repository, b"key", executor)
    )
    await asyncio.wait_for(gate.committed.wait(), 1)
    if loss == "cancel":
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
    else:
        result = await request
        assert result.status == "SUCCESS"
        assert result.value == "verified-result"
        assert result.cleanup == "UNAVAILABLE"
    await asyncio.wait_for(gate.disconnected.wait(), 1)
    assert await coordinator.aclose()
    assert gate.eval_connections == [1, 1]
    assert calls == 1
    raw = await redis_client.get(state_key)
    assert raw is not None
    state = json.loads(raw)
    assert state["lease"] is None  # EVAL completed, only its reply was lost.
    assert len(state["admissions"]) == len(state["consumed_keys"]) == 1
    assert (await admissions[1].acquire(user, repository, b"key")).status == "DUPLICATE"
    assert await redis_client.get(state_key) == raw
