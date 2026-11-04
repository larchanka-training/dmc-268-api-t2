"""Provider responses obey the verified wire schema before model coercion."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.llm.client import BaseLLMClient, ChatMessage, LLMRequest
from app.llm.errors import SchemaValidationError
from app.llm.ollama import OllamaClient
from app.llm.openai_compatible import OpenAICompatibleClient
from app.review.prompt import PromptBuilder
from app.review.schemas.result import provider_output_model

_REPO = Path(__file__).resolve().parents[2]


def _output(version: int) -> dict[str, Any]:
    example = json.loads(
        (_REPO / f"schemas/examples/review-result.v{version}.json").read_text()
    )
    return {key: example[key] for key in ("summary", "findings", "limitations")}


def _request(version: int) -> LLMRequest:
    return LLMRequest(
        messages=[ChatMessage(role="user", content="diff")],
        max_output_tokens=2000,
        timeout_s=30,
        output_schema_json=PromptBuilder.load(
            _REPO / ".agents/reviewer", "v1" if version == 1 else "v3"
        ).output_schema_json,
    )


def _client(
    content: str, provider: str, sent: list[httpx.Request] | None = None
) -> BaseLLMClient:
    response: dict[str, Any] = {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 30},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if sent is not None:
            sent.append(request)
        return httpx.Response(200, json=response)

    if provider == "ollama":
        response = {
            "message": {"content": content},
            "prompt_eval_count": 120,
            "eval_count": 30,
        }
        return OllamaClient(
            name="test",
            http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            base_url="https://provider.example",
            model="reviewer",
            context_window=16384,
        )
    return OpenAICompatibleClient(
        name="test",
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        base_url="https://provider.example/v1",
        model="reviewer",
        api_key="test",
    )


@pytest.mark.parametrize("provider", ["openai", "ollama"])
@pytest.mark.parametrize("version", [1, 2])
async def test_verified_schema_rejects_string_coordinates_before_coercion(
    provider: str, version: int
) -> None:
    output = _output(version)
    output["findings"][0]["start_line"] = "2"
    content = json.dumps(output)

    with pytest.raises(SchemaValidationError) as error:
        await _client(content, provider).generate_structured(
            _request(version),
            provider_output_model(f"schemas/llm-output/v{version}.json"),
        )

    assert error.value.raw_text == content
    assert error.value.details
    assert (error.value.input_tokens, error.value.output_tokens) == (120, 30)
    assert error.value.latency_ms is not None


@pytest.mark.parametrize("provider", ["openai", "ollama"])
async def test_missing_fields_preserve_historical_safe_repair_diagnostics(
    provider: str,
) -> None:
    with pytest.raises(SchemaValidationError) as error:
        await _client('{"summary": "x"}', provider).generate_structured(
            _request(1), provider_output_model("schemas/llm-output/v1.json")
        )

    assert error.value.details == [
        "findings: Field required",
        "limitations: Field required",
    ]


@pytest.mark.parametrize("provider", ["openai", "ollama"])
@pytest.mark.parametrize("version", [1, 2])
async def test_verified_wire_versions_round_trip_with_exact_selected_schema(
    provider: str, version: int
) -> None:
    output = _output(version)
    if version == 2:  # noqa: PLR2004
        output["findings"][0]["rule_ref"] = None
    sent: list[httpx.Request] = []
    request = _request(version)

    result = await _client(json.dumps(output), provider, sent).generate_structured(
        request, provider_output_model(f"schemas/llm-output/v{version}.json")
    )

    assert result.parsed.model_dump(mode="json") == output
    body = json.loads(sent[0].content)
    wire_schema = (
        body["format"]
        if provider == "ollama"
        else body["response_format"]["json_schema"]["schema"]
    )
    assert wire_schema == json.loads(request.output_schema_json or "null")


@pytest.mark.parametrize("provider", ["openai", "ollama"])
@pytest.mark.parametrize("version", [1, 2])
async def test_rule_reference_presence_is_enforced_by_selected_wire_schema(
    provider: str, version: int
) -> None:
    output = _output(version)
    if version == 1:
        output["findings"][0]["rule_ref"] = None
    else:
        del output["findings"][0]["rule_ref"]

    with pytest.raises(SchemaValidationError) as error:
        await _client(json.dumps(output), provider).generate_structured(
            _request(version),
            provider_output_model(f"schemas/llm-output/v{version}.json"),
        )

    assert error.value.details[0].startswith("schema.")


@pytest.mark.parametrize("provider", ["openai", "ollama"])
async def test_verified_error_diagnostics_exclude_arbitrary_response_keys_and_values(
    provider: str, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "PRIVATE_PROVIDER_RESPONSE_VALUE"
    output = _output(1)
    output[secret] = secret
    content = json.dumps(output)

    with pytest.raises(SchemaValidationError) as error:
        await _client(content, provider).generate_structured(
            _request(1), provider_output_model("schemas/llm-output/v1.json")
        )

    assert error.value.raw_text == content
    assert secret not in str(error.value)
    assert secret not in str(error.value.details)
    assert secret not in caplog.text


@pytest.mark.parametrize("provider", ["openai", "ollama"])
async def test_generic_requests_keep_existing_model_coercion(provider: str) -> None:
    output = _output(1)
    output["findings"][0]["start_line"] = "2"
    request = _request(1).model_copy(update={"output_schema_json": None})

    result = await _client(json.dumps(output), provider).generate_structured(
        request, provider_output_model("schemas/llm-output/v1.json")
    )

    assert result.parsed.findings[0].start_line == 2  # noqa: PLR2004
