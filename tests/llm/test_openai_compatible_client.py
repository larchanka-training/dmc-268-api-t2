"""OpenAI-compatible client used for Eurorouter (spec §4.2-4.3)."""

import json
from collections.abc import Callable

import httpx
import pytest
from pydantic import BaseModel, ConfigDict

from app.llm.client import ChatMessage, LLMRequest
from app.llm.errors import (
    AuthError,
    ContextOverflowError,
    LLMGatewayError,
    ProviderRequestError,
    RateLimitedError,
    SchemaValidationError,
    TransientProviderError,
)
from app.llm.openai_compatible import OpenAICompatibleClient

_BASE_URL = "https://eurorouter.example/v1"
_PROMPT_TOKENS = 120
_COMPLETION_TOKENS = 30


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: str


def _request() -> LLMRequest:
    return LLMRequest(
        messages=[
            ChatMessage(role="system", content="rules"),
            ChatMessage(role="user", content="diff"),
        ],
        max_output_tokens=2000,
        timeout_s=30,
    )


def _completion(content: str, **extra: object) -> dict[str, object]:
    body: dict[str, object] = {
        "model": "routed/actual-model",
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {
            "prompt_tokens": _PROMPT_TOKENS,
            "completion_tokens": _COMPLETION_TOKENS,
        },
    }
    body.update(extra)
    return body


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> OpenAICompatibleClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OpenAICompatibleClient(
        name="eurorouter",
        http=http,
        base_url=_BASE_URL,
        model="configured-model",
        api_key="key-one",
    )


async def test_sends_strict_json_schema_and_returns_parsed_result() -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json=_completion('{"verdict": "ok"}'))

    result = await _client(handler).generate_structured(_request(), Answer)

    assert result.parsed == Answer(verdict="ok")
    assert (result.input_tokens, result.output_tokens) == (
        _PROMPT_TOKENS,
        _COMPLETION_TOKENS,
    )
    assert result.model == "routed/actual-model"
    assert result.provider == "eurorouter"
    request = sent[0]
    assert str(request.url) == f"{_BASE_URL}/chat/completions"
    assert request.headers["authorization"] == "Bearer key-one"
    body = json.loads(request.content)
    assert body["model"] == "configured-model"
    assert body["temperature"] == 0.0
    assert body["max_tokens"] == 2000  # noqa: PLR2004
    assert body["messages"][0] == {"role": "system", "content": "rules"}
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "review_output",
            "strict": True,
            "schema": Answer.model_json_schema(),
        },
    }


@pytest.mark.parametrize(
    ("response", "error_type"),
    [
        (httpx.Response(401), AuthError),
        (httpx.Response(403), AuthError),
        (httpx.Response(500), TransientProviderError),
        (httpx.Response(502), TransientProviderError),
        (httpx.Response(503), TransientProviderError),
        (httpx.Response(504), TransientProviderError),
        (httpx.Response(404), ProviderRequestError),
        (
            httpx.Response(400, json={"error": {"code": "invalid_json_schema"}}),
            ProviderRequestError,
        ),
        (
            httpx.Response(
                400,
                json={
                    "error": {
                        "code": "context_length_exceeded",
                        "message": "This model's maximum context length is 16384",
                    }
                },
            ),
            ContextOverflowError,
        ),
    ],
)
async def test_http_errors_map_to_gateway_errors(
    response: httpx.Response, error_type: type[LLMGatewayError]
) -> None:
    with pytest.raises(error_type):
        await _client(lambda _: response).generate_structured(_request(), Answer)


async def test_rate_limit_carries_retry_after() -> None:
    response = httpx.Response(429, headers={"Retry-After": "7"})

    with pytest.raises(RateLimitedError) as error:
        await _client(lambda _: response).generate_structured(_request(), Answer)

    assert error.value.retry_after == 7  # noqa: PLR2004


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("refused"),
        httpx.ReadTimeout("slow"),
        httpx.RemoteProtocolError("broken"),
    ],
)
async def test_transport_errors_are_transient(exc: Exception) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise exc

    with pytest.raises(TransientProviderError):
        await _client(handler).generate_structured(_request(), Answer)


@pytest.mark.parametrize("content", ["not json", '{"verdict": 1}', '{"other": "x"}'])
async def test_invalid_content_raises_schema_validation_error(content: str) -> None:
    response = httpx.Response(200, json=_completion(content))

    with pytest.raises(SchemaValidationError) as error:
        await _client(lambda _: response).generate_structured(_request(), Answer)

    assert error.value.details
    assert error.value.raw_text == content


async def test_missing_usage_and_model_fall_back_to_unknown_and_config() -> None:
    body = {"choices": [{"message": {"content": '{"verdict": "ok"}'}}]}
    response = httpx.Response(200, json=body)

    result = await _client(lambda _: response).generate_structured(_request(), Answer)

    assert (result.input_tokens, result.output_tokens) == (None, None)
    assert result.model == "configured-model"


async def test_refusal_without_content_is_invalid_output() -> None:
    body = _completion("")
    body["choices"] = [{"message": {"content": None, "refusal": "no"}}]
    response = httpx.Response(200, json=body)

    with pytest.raises(SchemaValidationError):
        await _client(lambda _: response).generate_structured(_request(), Answer)


async def test_malformed_envelope_is_transient() -> None:
    response = httpx.Response(200, json={"unexpected": True})

    with pytest.raises(TransientProviderError):
        await _client(lambda _: response).generate_structured(_request(), Answer)


async def test_request_error_keeps_provider_code_but_not_message() -> None:
    body = {
        "error": {
            "code": "invalid_json_schema",
            "message": "schema rejected near 'SELECT * FROM users'",
        }
    }
    response = httpx.Response(400, json=body)

    with pytest.raises(ProviderRequestError) as error:
        await _client(lambda _: response).generate_structured(_request(), Answer)

    assert "invalid_json_schema" in str(error.value)
    assert "SELECT" not in str(error.value)


async def test_success_status_with_non_json_body_is_transient() -> None:
    response = httpx.Response(200, text="<html>gateway error</html>")

    with pytest.raises(TransientProviderError):
        await _client(lambda _: response).generate_structured(_request(), Answer)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(400, text="Bad Request"),
        httpx.Response(400, json={"error": "invalid request"}),
        httpx.Response(400, json={"error": {"code": "not a safe code!"}}),
    ],
)
async def test_request_error_without_safe_provider_code_has_status_only(
    response: httpx.Response,
) -> None:
    with pytest.raises(ProviderRequestError) as error:
        await _client(lambda _: response).generate_structured(_request(), Answer)

    assert str(error.value) == "HTTP 400"
