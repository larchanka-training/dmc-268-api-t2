"""Monotonic deadline behavior at the public Code/Diff service boundary."""

import asyncio
import json

import pytest
from pydantic import BaseModel
from test_code_simulation import FakeLLM, code_input, request_rules
from test_diff_simulation import diff_input, finding

from app.llm.client import LLMCallResult, LLMRequest
from app.review.simulation import CodeSimulationError, CodeSimulationService


async def test_legacy_call_keeps_one_request_with_thirty_second_timeout() -> None:
    client = FakeLLM('{"findings":[]}')
    result = await CodeSimulationService(client).simulate(code_input(), request_rules())
    assert result.model_dump() == {"findings": [], "rejected_count": 0}
    assert len(client.requests) == 1
    assert client.requests[0].timeout_s == 30.0  # noqa: PLR2004 -- inherited cap


@pytest.mark.parametrize("kind", ["code", "diff"])
async def test_deadline_is_checked_after_finding_validation(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = (
        finding()
        if kind == "diff"
        else {
            key: value
            for key, value in finding(start_line=1, end_line=1).items()
            if key != "side"
        }
    )
    client = FakeLLM(json.dumps({"findings": [item]}))
    # The response arrives within budget; time reaches the deadline at the
    # next observation after service-side finding validation/masking.
    after_response = iter([109.0, 110.0])
    with monkeypatch.context() as clock:
        clock.setattr(
            asyncio.get_running_loop(),
            "time",
            lambda: next(after_response, 110.0) if client.requests else 100.0,
        )
        with pytest.raises(CodeSimulationError, match=r"^provider_timeout$"):
            await CodeSimulationService(client).simulate(
                code_input() if kind == "code" else diff_input(),
                request_rules(),
                deadline=110.0,
            )
    assert len(client.requests) == 1


async def test_service_accepts_deadline_without_raising_timeout_cap() -> None:
    client = FakeLLM('{"findings":[]}')
    result = await CodeSimulationService(client).simulate(
        code_input(), request_rules(), deadline=asyncio.get_running_loop().time() + 120
    )
    assert result.model_dump() == {"findings": [], "rejected_count": 0}
    assert len(client.requests) == 1
    assert 0 < client.requests[0].timeout_s <= 30.0  # noqa: PLR2004 -- simulation cap


async def test_request_timeout_uses_remaining_running_loop_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeLLM('{"findings":[]}')
    with monkeypatch.context() as clock:
        clock.setattr(asyncio.get_running_loop(), "time", lambda: 100.0)
        await CodeSimulationService(client).simulate(
            code_input(), request_rules(), deadline=107.5
        )
    assert len(client.requests) == 1
    assert client.requests[0].timeout_s == 7.5  # noqa: PLR2004 -- known remaining budget


async def test_expired_deadline_prevents_dispatch() -> None:
    client = FakeLLM('{"findings":[]}')
    with pytest.raises(CodeSimulationError, match=r"^provider_timeout$"):
        await CodeSimulationService(client).simulate(
            code_input(), request_rules(), deadline=asyncio.get_running_loop().time()
        )
    assert client.requests == []


class WaitingLLM(FakeLLM):
    def __init__(self) -> None:
        super().__init__('{"findings":[]}')
        self.entered = asyncio.Event()

    async def generate_structured[T: BaseModel](
        self, request: LLMRequest, response_model: type[T]
    ) -> LLMCallResult[T]:
        self.requests.append(request)
        self.entered.set()
        await asyncio.Event().wait()
        raise AssertionError("waiting provider unexpectedly resumed")


async def test_deadline_bounds_await_even_if_provider_has_not_returned() -> None:
    client = WaitingLLM()
    with pytest.raises(CodeSimulationError, match=r"^provider_timeout$"):
        await asyncio.wait_for(
            CodeSimulationService(client).simulate(
                code_input(),
                request_rules(),
                deadline=asyncio.get_running_loop().time() + 0.02,
            ),
            timeout=0.5,
        )
    assert len(client.requests) == 1


@pytest.mark.parametrize("with_deadline", [False, True])
async def test_existing_caller_cancellation_propagates_without_retry(
    with_deadline: bool,
) -> None:
    client = WaitingLLM()
    task = asyncio.create_task(
        CodeSimulationService(client).simulate(
            code_input(),
            request_rules(),
            deadline=asyncio.get_running_loop().time() + 30 if with_deadline else None,
        )
    )
    await asyncio.wait_for(client.entered.wait(), 0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(client.requests) == 1


@pytest.mark.parametrize("kind", ["code", "diff"])
async def test_late_validated_result_is_not_success(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [100.0]

    class LateLLM(FakeLLM):
        async def generate_structured[T: BaseModel](
            self, request: LLMRequest, response_model: type[T]
        ) -> LLMCallResult[T]:
            result = await super().generate_structured(request, response_model)
            now[0] = 110.0
            return result

    client = LateLLM('{"findings":[]}')
    with monkeypatch.context() as clock:
        clock.setattr(asyncio.get_running_loop(), "time", lambda: now[0])
        with pytest.raises(CodeSimulationError, match=r"^provider_timeout$"):
            await CodeSimulationService(client).simulate(
                code_input() if kind == "code" else diff_input(),
                request_rules(),
                deadline=110.0,
            )
    assert len(client.requests) == 1


async def test_distant_deadline_cannot_extend_total_simulation_beyond_thirty_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [100.0]

    class SlowLLM(FakeLLM):
        async def generate_structured[T: BaseModel](
            self, request: LLMRequest, response_model: type[T]
        ) -> LLMCallResult[T]:
            result = await super().generate_structured(request, response_model)
            now[0] = 130.0
            return result

    client = SlowLLM('{"findings":[]}')
    with monkeypatch.context() as clock:
        clock.setattr(asyncio.get_running_loop(), "time", lambda: now[0])
        with pytest.raises(CodeSimulationError, match=r"^provider_timeout$"):
            await CodeSimulationService(client).simulate(
                code_input(), request_rules(), deadline=220.0
            )
    assert len(client.requests) == 1
