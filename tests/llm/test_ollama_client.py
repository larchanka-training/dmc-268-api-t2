"""Native Ollama /api/chat client, the fallback provider (spec §4.2)."""

import json
from collections.abc import Callable

import httpx
import pytest
from pydantic import BaseModel, ConfigDict

from app.llm.client import ChatMessage, LLMRequest
from app.llm.errors import SchemaValidationError, TransientProviderError
from app.llm.ollama import OllamaClient

_PROMPT_EVAL = 900
_EVAL = 40


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: str


def _request() -> LLMRequest:
    return LLMRequest(
        messages=[ChatMessage(role="user", content="diff")],
        max_output_tokens=2000,
        timeout_s=30,
    )


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> OllamaClient:
    return OllamaClient(
        name="ollama",
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        base_url="http://ollama:11434",
        model="qwen2.5-coder:14b",
        context_window=16384,
        model_digest="sha256:abc",
    )


def _chat(content: str) -> dict[str, object]:
    return {
        "model": "qwen2.5-coder:14b",
        "message": {"role": "assistant", "content": content},
        "done": True,
        "prompt_eval_count": _PROMPT_EVAL,
        "eval_count": _EVAL,
    }


async def test_sends_schema_as_format_and_returns_parsed_result() -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json=_chat('{"verdict": "ok"}'))

    result = await _client(handler).generate_structured(_request(), Answer)

    assert result.parsed == Answer(verdict="ok")
    assert (result.input_tokens, result.output_tokens) == (_PROMPT_EVAL, _EVAL)
    assert (result.provider, result.model) == ("ollama", "qwen2.5-coder:14b")
    assert result.model_digest == "sha256:abc"
    assert str(sent[0].url) == "http://ollama:11434/api/chat"
    assert "authorization" not in sent[0].headers
    assert json.loads(sent[0].content) == {
        "model": "qwen2.5-coder:14b",
        "messages": [{"role": "user", "content": "diff"}],
        "stream": False,
        "format": Answer.model_json_schema(),
        "options": {"temperature": 0.0, "num_predict": 2000, "num_ctx": 16384},
    }


async def test_invalid_content_raises_schema_validation_error() -> None:
    response = httpx.Response(200, json=_chat("{broken"))

    with pytest.raises(SchemaValidationError):
        await _client(lambda _: response).generate_structured(_request(), Answer)


async def test_unavailable_server_is_transient() -> None:
    response = httpx.Response(503)

    with pytest.raises(TransientProviderError):
        await _client(lambda _: response).generate_structured(_request(), Answer)


@pytest.mark.parametrize(
    "body", [{"done": True}, {"message": None}, {"message": "text"}]
)
async def test_malformed_chat_response_is_transient(body: dict[str, object]) -> None:
    response = httpx.Response(200, json=body)

    with pytest.raises(TransientProviderError):
        await _client(lambda _: response).generate_structured(_request(), Answer)
