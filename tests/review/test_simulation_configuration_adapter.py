"""Prepared staging policy at the existing provider HTTP boundary; entirely offline."""

import asyncio
import json
import socket
import traceback
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.api.rule_simulations import (
    SimulationCodeInput,
    SimulationDiffInput,
    SimulationRules,
)
from app.core.config import Settings
from app.llm.openai_compatible import OpenAICompatibleClient
from app.llm.tokens import ConservativeTokenCounter
from app.review.simulation import CodeSimulationError, CodeSimulationService
from app.review.simulation_configuration import (
    SimulationConfiguration,
    prepare_simulation_configuration,
)

INPUT_CAP = 8192
OUTPUT_CAP = 2000
CONTEXT_WINDOW = 16384
TIMEOUT_S = 30
MANIFEST = Path(__file__).resolve().parents[2] / "deploy/analysis-runtime.staging.json"
RULES = SimulationRules(
    format="rules-md/1",
    markdown="# Security\n## SEC-1: No eval\nDo not eval.\n",
)


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("real network transport attempted")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)


@pytest.fixture
def prepared_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> SimulationConfiguration:
    monkeypatch.chdir(tmp_path)
    for name, value in {
        "SIMULATION__ENABLED": "true",
        "LLM__PRIMARY_PROVIDER": "eurorouter",
        "LLM__FALLBACK_ENABLED": "false",
        "LLM__EUROROUTER__BASE_URL": "https://api.eurouter.ai/api/v1",
        "LLM__EUROROUTER__MODEL": "mistral-small-4",
        "LLM__EUROROUTER__API_KEYS": "synthetic-first-key,synthetic-second-key",
    }.items():
        monkeypatch.setenv(name, value)
    config = prepare_simulation_configuration(Settings(), manifest_path=MANIFEST)
    assert config is not None
    return config


@asynccontextmanager
async def prepared_service(
    config: SimulationConfiguration,
    *,
    status: int = 200,
) -> AsyncIterator[tuple[CodeSimulationService, list[httpx.Request]]]:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(
            status, json={"choices": [{"message": {"content": '{"findings":[]}'}}]}
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), trust_env=False
    ) as http:
        adapter = OpenAICompatibleClient(
            name=config.provider,
            http=http,
            base_url=config.base_url,
            model=config.model,
            api_key=config.api_key.get_secret_value(),
        )
        yield (
            CodeSimulationService(
                adapter,
                max_input_tokens=config.max_input_tokens,
                max_output_tokens=config.max_output_tokens,
            ),
            sent,
        )


def simulation_input(kind: str) -> SimulationCodeInput | SimulationDiffInput:
    if kind == "diff":
        return SimulationDiffInput(
            kind="diff",
            unified_diff=(
                "diff --git a/snippet.py b/snippet.py\n"
                "--- a/snippet.py\n+++ b/snippet.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
            ),
        )
    return SimulationCodeInput(kind="code", path="snippet.py", source="x = 1\n")


@pytest.mark.parametrize("kind", ["code", "diff"])
async def test_prepared_policy_bounds_provider_output_to_two_thousand_tokens(
    prepared_policy: SimulationConfiguration,
    kind: str,
) -> None:
    async with prepared_service(prepared_policy) as (service, sent):
        result = await service.simulate(
            simulation_input(kind),
            RULES,
            deadline=asyncio.get_running_loop().time()
            + prepared_policy.execution_timeout_s,
        )
    assert result.findings == []
    assert len(sent) == 1
    body = json.loads(sent[0].content)
    assert body["max_tokens"] == OUTPUT_CAP
    assert body["model"] == "mistral-small-4"
    assert str(sent[0].url) == "https://api.eurouter.ai/api/v1/chat/completions"
    assert sent[0].headers["authorization"] == "Bearer synthetic-first-key"
    assert all(0 < t <= TIMEOUT_S for t in sent[0].extensions["timeout"].values())
    counter = ConservativeTokenCounter()
    input_tokens = sum(counter.count(m["content"]) + 4 for m in body["messages"])
    input_tokens += counter.count(
        json.dumps(body["response_format"]["json_schema"]["schema"])
    )
    assert input_tokens <= INPUT_CAP
    assert input_tokens + body["max_tokens"] <= CONTEXT_WINDOW


@pytest.mark.parametrize("kind", ["code", "diff"])
@pytest.mark.parametrize("status", [429, 503])
async def test_multiple_keys_never_retry_rotate_or_fallback_after_provider_failure(
    prepared_policy: SimulationConfiguration,
    kind: str,
    status: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async with prepared_service(prepared_policy, status=status) as (service, sent):
        with pytest.raises(CodeSimulationError) as error:
            await service.simulate(
                simulation_input(kind),
                RULES,
                deadline=asyncio.get_running_loop().time()
                + prepared_policy.execution_timeout_s,
            )
    assert len(sent) == 1
    assert sent[0].headers["authorization"] == "Bearer synthetic-first-key"
    diagnostics = (
        repr(prepared_policy)
        + caplog.text
        + "".join(traceback.format_exception(error.value))
    )
    assert "synthetic-first-key" not in diagnostics
    assert "synthetic-second-key" not in diagnostics


async def test_prepared_input_limit_rejects_oversized_prompt_before_http(
    prepared_policy: SimulationConfiguration,
) -> None:
    async with prepared_service(prepared_policy) as (service, sent):
        with pytest.raises(CodeSimulationError, match="prompt_too_large"):
            await service.simulate(
                SimulationCodeInput(kind="code", path="snippet.py", source="x" * 24000),
                RULES,
                deadline=asyncio.get_running_loop().time()
                + prepared_policy.execution_timeout_s,
            )
    assert sent == []
