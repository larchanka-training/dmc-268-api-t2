"""OpenAI-compatible ``/chat/completions`` client (Eurorouter, spec §4.2).

Nothing here is Eurorouter-specific: name, base URL, model and key come from
configuration. One instance serves one API key.
"""

import json
import time

import httpx
from pydantic import BaseModel

from app.llm._http import parse_structured, post_json
from app.llm.client import BaseLLMClient, LLMCallResult, LLMRequest, known_token_usage
from app.llm.errors import TransientProviderError


class OpenAICompatibleClient(BaseLLMClient):
    def __init__(
        self,
        *,
        name: str,
        http: httpx.AsyncClient,
        base_url: str,
        model: str,
        api_key: str,
    ) -> None:
        self.name = name
        self._http = http
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        self._model = model
        self._api_key = api_key

    async def generate_structured[T: BaseModel](
        self, request: LLMRequest, response_model: type[T]
    ) -> LLMCallResult[T]:
        body = {
            "model": self._model,
            "messages": [m.model_dump() for m in request.messages],
            "temperature": request.temperature,
            "max_tokens": request.max_output_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "review_output",
                    "strict": True,
                    "schema": (
                        json.loads(request.output_schema_json)
                        if request.output_schema_json is not None
                        else response_model.model_json_schema()
                    ),
                },
            },
        }
        started = time.perf_counter()
        data = await post_json(
            self._http,
            self._url,
            body,
            timeout_s=request.timeout_s,
            headers={"Authorization": f"Bearer {self._api_key}"},
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        try:
            message = data["choices"][0]["message"]
        except KeyError, IndexError, TypeError:
            msg = "malformed chat completion envelope"
            raise TransientProviderError(msg) from None
        # A refusal comes without content: treat it as invalid output.
        content = message.get("content") or ""
        usage = data.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        input_tokens = known_token_usage(usage.get("prompt_tokens"))
        output_tokens = known_token_usage(usage.get("completion_tokens"))
        return LLMCallResult[T](
            parsed=parse_structured(
                content,
                response_model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_ms=latency_ms,
            ),
            raw_text_len=len(content),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=data.get("model") or self._model,
            reported_model=data.get("model"),
            model_digest=None,
            provider=self.name,
            latency_ms=latency_ms,
        )
