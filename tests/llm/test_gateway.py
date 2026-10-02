"""LLMGateway routing, key rotation, backoff and failover (spec §4.3-4.4)."""

import json
import random
from collections import defaultdict, deque

import httpx
import pytest
from pydantic import BaseModel, ConfigDict, SecretStr

from app.core.config import LLM, Eurorouter, Ollama
from app.llm.budget import BudgetLimits, RunBudget
from app.llm.client import ChatMessage, LLMRequest
from app.llm.errors import (
    BudgetExceededError,
    ContextOverflowError,
    LLMUnavailableError,
    ProviderRequestError,
)
from app.llm.gateway import LLMGateway
from app.llm.providers import build_gateway

_OK = '{"verdict": "ok"}'


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: str


class Provider:
    """Scripted Eurorouter/Ollama behind one MockTransport.

    Responses are queued per endpoint label (``key-one``, ``key-two`` or
    ``ollama``); an empty queue answers with a valid result.
    """

    def __init__(self) -> None:
        self.script: dict[str, deque[httpx.Response | Exception]] = defaultdict(deque)
        self.calls: list[str] = []
        self.timeouts: list[float] = []

    def queue(self, label: str, *responses: httpx.Response | Exception) -> None:
        self.script[label].extend(responses)

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "ollama":
            label = "ollama"
        else:
            label = request.headers["authorization"].removeprefix("Bearer ")
        self.calls.append(label)
        self.timeouts.append(request.extensions["timeout"]["read"])
        if self.script[label]:
            item = self.script[label].popleft()
            if isinstance(item, Exception):
                raise item
            return item
        if label == "ollama":
            return httpx.Response(
                200, json={"model": "qwen", "message": {"content": _OK}}
            )
        return httpx.Response(
            200, json={"model": "euro", "choices": [{"message": {"content": _OK}}]}
        )


class Sleeper:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def provider() -> Provider:
    return Provider()


@pytest.fixture
def sleeper() -> Sleeper:
    return Sleeper()


@pytest.fixture
def clock() -> Clock:
    return Clock()


def _settings(*, primary: str = "eurorouter", fallback: bool = True) -> LLM:
    return LLM(
        primary_provider=primary,  # type: ignore[arg-type]
        fallback_enabled=fallback,
        eurorouter=Eurorouter(
            base_url="https://euro.example/v1",
            model="euro",
            api_keys=[SecretStr("key-one"), SecretStr("key-two")],
        ),
        ollama=Ollama(base_url="http://ollama:11434", model="qwen"),
    )


def _gateway(
    provider: Provider,
    sleeper: Sleeper,
    clock: Clock,
    settings: LLM | None = None,
) -> LLMGateway:
    return build_gateway(
        settings or _settings(),
        httpx.AsyncClient(transport=httpx.MockTransport(provider.handler)),
        sleep=sleeper,
        rng=random.Random(0),
        clock=clock,
    )


def _budget(clock: Clock, max_requests: int = 8) -> RunBudget:
    limits = BudgetLimits(
        max_requests=max_requests,
        max_input_tokens=80_000,
        max_output_tokens=16_000,
        deadline_s=600,
    )
    return RunBudget(limits, clock=clock)


def _request() -> LLMRequest:
    return LLMRequest(
        messages=[ChatMessage(role="user", content="diff")],
        max_output_tokens=2000,
        timeout_s=120,
    )


async def _call(gateway: LLMGateway, budget: RunBudget) -> str:
    result = await gateway.generate_structured(
        _request(), Answer, budget=budget, input_tokens=1000
    )
    return result.provider


async def test_primary_provider_answers_first(
    provider: Provider, sleeper: Sleeper, clock: Clock
) -> None:
    gateway = _gateway(provider, sleeper, clock)

    assert await _call(gateway, _budget(clock)) == "eurorouter"
    assert provider.calls == ["key-one"]
    assert json.dumps(provider.timeouts) == "[120.0]"


async def test_rate_limited_key_cools_down_and_next_key_is_used(
    provider: Provider, sleeper: Sleeper, clock: Clock
) -> None:
    gateway = _gateway(provider, sleeper, clock)
    provider.queue("key-one", httpx.Response(429, headers={"Retry-After": "30"}))
    budget = _budget(clock)

    assert await _call(gateway, budget) == "eurorouter"
    assert await _call(gateway, budget) == "eurorouter"  # key-one still cooling
    clock.now += 31
    assert await _call(gateway, budget) == "eurorouter"

    assert provider.calls == ["key-one", "key-two", "key-two", "key-one"]
    assert sleeper.delays == []


async def test_rejected_key_is_disabled_until_restart(
    provider: Provider, sleeper: Sleeper, clock: Clock
) -> None:
    gateway = _gateway(provider, sleeper, clock)
    provider.queue("key-one", httpx.Response(401))
    budget = _budget(clock)

    await _call(gateway, budget)
    clock.now += 300  # far beyond any backoff, inside the run deadline
    await _call(gateway, budget)
    await _call(gateway, budget)

    assert provider.calls == ["key-one", "key-two", "key-two", "key-two"]
    assert sleeper.delays == []


async def test_falls_back_when_all_primary_keys_are_unavailable(
    provider: Provider, sleeper: Sleeper, clock: Clock
) -> None:
    gateway = _gateway(provider, sleeper, clock)
    provider.queue("key-one", httpx.Response(429, headers={"Retry-After": "5"}))
    provider.queue("key-two", httpx.Response(403))
    budget = _budget(clock)

    assert await _call(gateway, budget) == "ollama"
    clock.now += 10
    assert await _call(gateway, budget) == "eurorouter"

    assert provider.calls == ["key-one", "key-two", "ollama", "key-one"]
    assert gateway.primary_provider == "eurorouter"


async def test_transient_error_backs_off_with_jitter_then_retries(
    provider: Provider, sleeper: Sleeper, clock: Clock
) -> None:
    gateway = _gateway(provider, sleeper, clock)
    provider.queue("key-one", httpx.Response(503))

    assert await _call(gateway, _budget(clock)) == "eurorouter"

    assert provider.calls == ["key-one", "key-two"]
    # attempt 1: min(20, 1 * 2**1) = 2 s, jitter x[0.5, 1.0]
    assert len(sleeper.delays) == 1
    assert 1.0 <= sleeper.delays[0] <= 2.0  # noqa: PLR2004


async def test_repeated_transient_errors_fail_over_to_fallback(
    provider: Provider, sleeper: Sleeper, clock: Clock
) -> None:
    gateway = _gateway(provider, sleeper, clock)
    provider.queue("key-one", httpx.Response(500))
    provider.queue("key-two", httpx.ReadTimeout("slow"))

    assert await _call(gateway, _budget(clock)) == "ollama"

    assert provider.calls == ["key-one", "key-two", "ollama"]


async def test_gives_up_after_three_attempts(
    provider: Provider, sleeper: Sleeper, clock: Clock
) -> None:
    gateway = _gateway(provider, sleeper, clock)
    provider.queue("key-one", httpx.Response(502), httpx.Response(502))
    provider.queue("key-two", httpx.Response(502))
    provider.queue("ollama", httpx.Response(503))
    budget = _budget(clock)

    with pytest.raises(LLMUnavailableError) as error:
        await _call(gateway, budget)

    assert provider.calls == ["key-one", "key-two", "ollama"]
    assert error.value.last_error_code == "provider_unavailable"
    assert budget.state().requests_used == 3  # noqa: PLR2004


async def test_without_fallback_unavailable_primary_fails_the_call(
    provider: Provider, sleeper: Sleeper, clock: Clock
) -> None:
    gateway = _gateway(provider, sleeper, clock, _settings(fallback=False))
    provider.queue("key-one", httpx.Response(429))
    provider.queue("key-two", httpx.Response(429))

    with pytest.raises(LLMUnavailableError):
        await _call(gateway, _budget(clock))

    assert provider.calls == ["key-one", "key-two"]


async def test_request_timeout_is_capped_by_provider_and_run_deadline(
    provider: Provider, sleeper: Sleeper, clock: Clock
) -> None:
    settings = _settings()
    settings = settings.model_copy(
        update={"eurorouter": settings.eurorouter.model_copy(update={"timeout_s": 60})}
    )
    gateway = _gateway(provider, sleeper, clock, settings)
    budget = _budget(clock)

    await _call(gateway, budget)
    clock.now += 590  # 10 s left of the 600 s run deadline
    await _call(gateway, budget)

    assert provider.timeouts == [60, 10]


async def test_backoff_beyond_deadline_stops_without_sleeping(
    provider: Provider, sleeper: Sleeper, clock: Clock
) -> None:
    gateway = _gateway(provider, sleeper, clock)
    provider.queue("key-one", httpx.Response(503))
    budget = _budget(clock)
    clock.now += 599  # 1 s left, backoff needs at least 1 s

    with pytest.raises(BudgetExceededError):
        await _call(gateway, budget)

    assert sleeper.delays == []


@pytest.mark.parametrize(
    ("response", "error_type"),
    [
        (httpx.Response(422), ProviderRequestError),
        (
            httpx.Response(400, json={"error": {"code": "context_length_exceeded"}}),
            ContextOverflowError,
        ),
    ],
)
async def test_request_errors_are_not_retried_or_failed_over(
    provider: Provider,
    sleeper: Sleeper,
    clock: Clock,
    response: httpx.Response,
    error_type: type[Exception],
) -> None:
    gateway = _gateway(provider, sleeper, clock)
    provider.queue("key-one", response)

    with pytest.raises(error_type):
        await _call(gateway, _budget(clock))

    assert provider.calls == ["key-one"]
