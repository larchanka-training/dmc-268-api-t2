"""Characterize the Playground service with the native Ollama wire protocol."""

import json
import socket
import traceback
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.rule_simulations import (
    SimulationCodeInput,
    SimulationDiffInput,
    SimulationRules,
    get_simulation_executor,
)
from app.llm.ollama import OllamaClient
from app.review.simulation import CodeSimulationError, CodeSimulationService

_SOURCE = "eval(synthetic_input)\n"
_RULES = SimulationRules(
    format="rules-md/1",
    markdown="# Security\n## SEC-1: No eval\nDo not call eval.\n",
)
_FINDING = {
    "path": "snippet.py",
    "start_line": 1,
    "end_line": 1,
    "rule_ref": "SEC-1",
    "title": "Unsafe evaluation",
    "explanation": "The supplied rule prohibits eval.",
    "evidence": "eval(synthetic_input)",
}
_PRIVATE = "private-provider-response-sentinel"
_TIMEOUT_LIMIT_S = 30


@pytest.fixture(autouse=True)
def forbid_external_io(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("network or persistence attempted outside MockTransport")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    monkeypatch.setattr(AsyncSession, "execute", forbidden)
    monkeypatch.setattr(AsyncSession, "commit", forbidden)
    monkeypatch.setattr(Redis, "execute_command", forbidden)


@pytest.fixture(params=["code", "diff"])
def simulation_input(
    request: pytest.FixtureRequest,
) -> SimulationCodeInput | SimulationDiffInput:
    if request.param == "code":
        return SimulationCodeInput(kind="code", path="snippet.py", source=_SOURCE)
    return SimulationDiffInput(
        kind="diff",
        unified_diff=(
            "diff --git a/snippet.py b/snippet.py\n"
            "--- a/snippet.py\n+++ b/snippet.py\n"
            "@@ -1 +1 @@\n-safe(synthetic_input)\n+eval(synthetic_input)\n"
        ),
    )


@asynccontextmanager
async def service_for(
    outcome: dict[str, Any] | str | httpx.Response | Exception,
    *,
    max_input_tokens: int = 4096,
) -> AsyncIterator[tuple[CodeSimulationService, list[httpx.Request]]]:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        assert len(sent) == 1, "Ollama must receive no automatic retries"
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, httpx.Response):
            return outcome
        return httpx.Response(
            200,
            json={
                "message": {
                    "content": json.dumps(outcome)
                    if isinstance(outcome, dict)
                    else outcome
                },
                "done": True,
                "model": "local-test",
                "prompt_eval_count": 500,
                "eval_count": 80,
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), trust_env=False
    ) as http:
        client = OllamaClient(
            name="ollama-characterization",
            http=http,
            base_url="http://127.0.0.1:11434",
            model="local-test",
            context_window=8192,
        )
        yield CodeSimulationService(client, max_input_tokens=max_input_tokens), sent


async def test_structured_findings_use_supplied_rules_and_native_schema_once(
    simulation_input: SimulationCodeInput | SimulationDiffInput,
) -> None:
    finding = _FINDING | ({"side": "new"} if simulation_input.kind == "diff" else {})
    async with service_for({"findings": [finding]}) as (service, sent):
        result = await service.simulate(simulation_input, _RULES)
    assert result.model_dump() == {"findings": [finding], "rejected_count": 0}
    assert len(sent) == 1
    request = sent[0]
    assert request.method == "POST"
    assert str(request.url) == "http://127.0.0.1:11434/api/chat"
    assert "authorization" not in request.headers
    body = json.loads(request.content)
    assert body["model"] == "local-test"
    assert body["stream"] is False
    assert body["options"] == {"temperature": 0.0, "num_predict": 2048, "num_ctx": 8192}
    assert all(
        0 < value <= _TIMEOUT_LIMIT_S
        for value in request.extensions["timeout"].values()
    )
    assert [message["role"] for message in body["messages"]] == ["system", "user"]
    data = json.loads(body["messages"][1]["content"])
    assert data["rules"] == ["[SEC-1] Security: No eval\nDo not call eval."]
    if simulation_input.kind == "code":
        assert data["source_lines"] == ["eval(synthetic_input)"]
    else:
        assert data["files"][0]["hunks"][0]["lines"][1] == {
            "kind": "added",
            "text": "eval(synthetic_input)",
            "old_line": None,
            "new_line": 1,
        }
    schema = body["format"]
    assert schema["required"] == ["findings"]
    assert schema["additionalProperties"] is False
    assert "rejected_count" not in schema["properties"]
    item = schema["$defs"][
        "DiffFinding" if simulation_input.kind == "diff" else "CodeFinding"
    ]
    assert item["additionalProperties"] is False
    assert set(item["required"]) == set(finding)


@pytest.mark.parametrize(
    "changed",
    [
        {"rule_ref": "SEC-99"},
        {"path": "other.py"},
        {"end_line": 99},
        {"start_line": 2, "end_line": 1},
        {"evidence": "fabricated"},
    ],
)
async def test_unsubstantiated_finding_is_rejected_after_ollama_validation(
    simulation_input: SimulationCodeInput | SimulationDiffInput, changed: dict[str, Any]
) -> None:
    finding = _FINDING | ({"side": "new"} if simulation_input.kind == "diff" else {})
    async with service_for({"findings": [finding | changed]}) as (service, sent):
        result = await service.simulate(simulation_input, _RULES)
    assert result.model_dump() == {"findings": [], "rejected_count": 1}
    assert len(sent) == 1


@pytest.mark.parametrize(
    ("outcome", "code"),
    [
        (_PRIVATE, "invalid_llm_output"),
        ({"findings": [], "extra": _PRIVATE}, "invalid_llm_output"),
        (httpx.Response(200, json={"message": None}), "provider_unavailable"),
        (httpx.Response(400, json={"error": _PRIVATE}), "provider_request_error"),
        (
            httpx.Response(503, json={"error": {"code": _PRIVATE}}),
            "provider_unavailable",
        ),
        (httpx.Response(429, headers={"Retry-After": "1"}), "rate_limited"),
        (httpx.ReadTimeout(_PRIVATE), "provider_timeout"),
    ],
)
async def test_ollama_failure_is_safe_without_retry_or_repair(
    simulation_input: SimulationCodeInput | SimulationDiffInput,
    outcome: dict[str, Any] | str | httpx.Response | Exception,
    code: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async with service_for(outcome) as (service, sent):
        with pytest.raises(CodeSimulationError) as error:
            await service.simulate(simulation_input, _RULES)
    assert str(error.value) == code
    assert len(sent) == 1
    assert error.value.__cause__ is error.value.__context__ is None
    assert vars(error.value) == {}
    diagnostics = "".join(traceback.format_exception(error.value)) + caplog.text
    assert _PRIVATE not in diagnostics
    assert _SOURCE.strip() not in diagnostics
    assert _RULES.markdown not in diagnostics


async def test_prompt_budget_prevents_ollama_dispatch(
    simulation_input: SimulationCodeInput | SimulationDiffInput,
) -> None:
    async with service_for({"findings": []}, max_input_tokens=1) as (service, sent):
        with pytest.raises(CodeSimulationError, match=r"^prompt_too_large$"):
            await service.simulate(simulation_input, _RULES)
    assert sent == []


async def test_normal_application_keeps_executor_unavailable() -> None:
    assert await get_simulation_executor() is None
