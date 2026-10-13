"""Usage is validated at the provider boundary before Pydantic coercion."""

import httpx
import pytest
from pydantic import BaseModel

from app.llm.client import BaseLLMClient, ChatMessage, LLMRequest
from app.llm.ollama import OllamaClient
from app.llm.openai_compatible import OpenAICompatibleClient


class Answer(BaseModel):
    verdict: str


@pytest.mark.parametrize("provider", ["openai", "ollama"])
@pytest.mark.parametrize("usage", [True, -1, "0", 1.5, 2**63])
async def test_provider_invalid_dimension_is_unknown(
    provider: str, usage: object
) -> None:
    body = {
        "model": "model",
        "choices": [{"message": {"content": '{"verdict":"ok"}'}}],
        "message": {"content": '{"verdict":"ok"}'},
        "usage": {"prompt_tokens": usage, "completion_tokens": 20},
        "prompt_eval_count": usage,
        "eval_count": 20,
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    ) as http:
        client: BaseLLMClient
        if provider == "openai":
            client = OpenAICompatibleClient(
                name="provider",
                http=http,
                base_url="https://llm.invalid",
                model="model",
                api_key="test",
            )
        else:
            client = OllamaClient(
                name="provider",
                http=http,
                base_url="http://ollama.invalid",
                model="model",
                context_window=16384,
            )
        result = await client.generate_structured(
            LLMRequest(
                messages=[ChatMessage(role="user", content="review")],
                max_output_tokens=2000,
                timeout_s=1,
            ),
            Answer,
        )
    assert (result.input_tokens, result.output_tokens) == (None, 20)
