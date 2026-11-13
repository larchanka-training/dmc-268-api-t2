"""Diff simulation through the real LLM adapter, using MockTransport only."""

import json
import math
import socket
import traceback
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

from app.api.rule_simulations import SimulationDiffInput, SimulationRules
from app.llm.openai_compatible import OpenAICompatibleClient
from app.review.simulation import CodeSimulationError, CodeSimulationService

_BASE_URL = "https://provider.invalid/v1"
_API_KEY = "diff-offline-credential-sentinel"
_TIMEOUT_LIMIT_S = 30
_DIFF = (
    "diff --git a/before.py b/after.py\n"
    "similarity index 75%\nrename from before.py\nrename to after.py\n"
    "--- a/before.py\n+++ b/after.py\n@@ -7 +11 @@\n"
    "-eval(diff_private_old_sentinel)\n+eval(diff_private_new_sentinel)\n"
)
_RULES = SimulationRules(
    format="rules-md/1",
    markdown="# Security\n## SEC-1: No eval\ndiff_private_rules_sentinel\n",
)
_FINDING = {
    "path": "after.py",
    "side": "new",
    "start_line": 11,
    "end_line": 11,
    "rule_ref": "SEC-1",
    "title": "Unsafe evaluation",
    "explanation": "User input is executed.",
    "evidence": "eval(diff_private_new_sentinel)",
}


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("real network transport attempted")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)


@asynccontextmanager
async def adapter_service(
    outcome: str | httpx.Response | Exception,
) -> AsyncIterator[tuple[CodeSimulationService, list[httpx.Request]]]:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        assert len(sent) == 1, "simulation must not repeat a provider request"
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, httpx.Response):
            return outcome
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": outcome}}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        adapter = OpenAICompatibleClient(
            name="diff-offline-test",
            http=http,
            base_url=_BASE_URL,
            model="offline-model",
            api_key=_API_KEY,
        )
        yield CodeSimulationService(adapter), sent


def diff_input() -> SimulationDiffInput:
    return SimulationDiffInput(kind="diff", unified_diff=_DIFF)


@pytest.mark.parametrize(
    "finding",
    [
        _FINDING,
        _FINDING
        | {
            "path": "before.py",
            "side": "old",
            "start_line": 7,
            "end_line": 7,
            "evidence": "eval(diff_private_old_sentinel)",
        },
    ],
    ids=["new-path-new-line", "old-path-old-line"],
)
async def test_rename_finding_survives_real_adapter_validation(
    finding: dict[str, Any],
) -> None:
    async with adapter_service(json.dumps({"findings": [finding]})) as (service, sent):
        result = await service.simulate(diff_input(), _RULES)

    assert [item.model_dump() for item in result.findings] == [finding]
    assert result.rejected_count == 0
    assert len(sent) == 1
    assert sent[0].method == "POST"
    assert str(sent[0].url) == f"{_BASE_URL}/chat/completions"


async def test_diff_wire_schema_requires_side_and_uses_bounded_request() -> None:
    async with adapter_service('{"findings":[]}') as (service, sent):
        result = await service.simulate(diff_input(), _RULES)

    assert result.findings == []
    assert len(sent) == 1
    request = sent[0]
    body = json.loads(request.content)
    assert body["temperature"] == 0.0
    assert body["max_tokens"] == 2048  # noqa: PLR2004 -- inherited output cap
    timeouts = request.extensions["timeout"]
    assert set(timeouts) == {"connect", "read", "write", "pool"}
    assert all(
        0 < value <= _TIMEOUT_LIMIT_S and math.isfinite(value)
        for value in timeouts.values()
    )
    response_format = body["response_format"]
    assert response_format["type"] == "json_schema"
    # Compatibility mode on the wire; the real adapter validates locally.
    assert response_format["json_schema"]["strict"] is False
    schema = response_format["json_schema"]["schema"]
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["findings"]
    finding = schema["$defs"]["DiffFinding"]
    assert finding["additionalProperties"] is False
    assert set(finding["required"]) == set(finding["properties"]) == set(_FINDING)
    assert finding["properties"]["side"]["enum"] == ["old", "new"]
    assert finding["properties"]["start_line"]["type"] == "integer"
    assert [message["role"] for message in body["messages"]] == ["system", "user"]
    data = json.loads(body["messages"][1]["content"])
    assert data["rules"] == ["[SEC-1] Security: No eval\ndiff_private_rules_sentinel"]
    assert data["files"][0]["old_path"] == "before.py"
    assert data["files"][0]["new_path"] == "after.py"
    assert data["files"][0]["hunks"][0]["lines"] == [
        {
            "kind": "deleted",
            "text": "eval(diff_private_old_sentinel)",
            "old_line": 7,
            "new_line": None,
        },
        {
            "kind": "added",
            "text": "eval(diff_private_new_sentinel)",
            "old_line": None,
            "new_line": 11,
        },
    ]
    for anchor in ("snapshot", "pull_number", "review_id", "head_sha", "github"):
        assert anchor not in json.dumps(data)
    assert _API_KEY not in request.content.decode()


@pytest.mark.parametrize(
    "answer",
    [
        f"invalid JSON {_DIFF} {_RULES.markdown} {_API_KEY}",
        json.dumps({"findings": [{k: v for k, v in _FINDING.items() if k != "side"}]}),
        json.dumps({"findings": [_FINDING | {"side": "LEFT"}]}),
        json.dumps({"findings": [_FINDING | {"start_line": "11"}]}),
        json.dumps({"findings": [_FINDING | {"rule_ref": None}]}),
        json.dumps({"findings": [], "private": _API_KEY}),
        json.dumps({"findings": [_FINDING | {"private": _API_KEY}]}),
    ],
    ids=[
        "invalid-json",
        "missing-side",
        "invalid-side",
        "string-line",
        "null-ref",
        "extra-root",
        "extra-finding",
    ],
)
async def test_invalid_diff_completion_is_safe_without_repair(
    answer: str, caplog: pytest.LogCaptureFixture
) -> None:
    async with adapter_service(answer) as (service, sent):
        with pytest.raises(CodeSimulationError, match=r"^invalid_llm_output$") as error:
            await service.simulate(diff_input(), _RULES)

    assert len(sent) == 1
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert vars(error.value) == {}
    diagnostics = str(error.value) + repr(error.value)
    diagnostics += "".join(traceback.format_exception(error.value)) + caplog.text
    for private in (_DIFF, _RULES.markdown, _API_KEY, answer):
        assert private not in diagnostics


@pytest.mark.parametrize(
    ("outcome", "error_code"),
    [
        (
            httpx.Response(429, headers={"Retry-After": "7"}, text=_API_KEY),
            "rate_limited",
        ),
        (httpx.Response(503, text=_API_KEY), "provider_unavailable"),
        (
            httpx.Response(
                503,
                json={
                    "error": {"code": _API_KEY, "message": f"{_DIFF} {_RULES.markdown}"}
                },
            ),
            "provider_unavailable",
        ),
        (httpx.ReadTimeout(_API_KEY), "provider_timeout"),
        (httpx.ConnectTimeout(_API_KEY), "provider_timeout"),
    ],
    ids=["rate-limit", "unavailable", "private-error-code", "read", "connect"],
)
async def test_diff_provider_failure_is_static_without_retry(
    outcome: httpx.Response | Exception,
    error_code: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async with adapter_service(outcome) as (service, sent):
        with pytest.raises(CodeSimulationError) as error:
            await service.simulate(diff_input(), _RULES)

    assert str(error.value) == error_code
    assert len(sent) == 1
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert vars(error.value) == {}
    diagnostics = str(error.value) + repr(error.value)
    diagnostics += "".join(traceback.format_exception(error.value)) + caplog.text
    for private in (_DIFF, _RULES.markdown, _API_KEY):
        assert private not in diagnostics
