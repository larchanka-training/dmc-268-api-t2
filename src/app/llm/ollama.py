"""Native Ollama ``/api/chat`` client (fallback provider, spec §4.2).

No authorization: Ollama lives in the closed application network.
"""

import json
import time

import httpx
from pydantic import BaseModel

from app.llm._http import parse_structured, post_json
from app.llm.client import BaseLLMClient, LLMCallResult, LLMRequest, known_token_usage
from app.llm.errors import TransientProviderError


class OllamaClient(BaseLLMClient):
    def __init__(  # noqa: PLR0913
        self,
        *,
        name: str,
        http: httpx.AsyncClient,
        base_url: str,
        model: str,
        context_window: int,
        model_digest: str | None = None,
    ) -> None:
        self.name = name
        self._http = http
        self._url = f"{base_url.rstrip('/')}/api/chat"
        self._model = model
        self._context_window = context_window
        self._model_digest = model_digest

    async def generate_structured[T: BaseModel](
        self, request: LLMRequest, response_model: type[T]
    ) -> LLMCallResult[T]:
        body = {
            "model": self._model,
            "messages": [m.model_dump() for m in request.messages],
            "stream": False,
            "format": (
                json.loads(request.output_schema_json)
                if request.output_schema_json is not None
                else response_model.model_json_schema()
            ),
            "options": {
                "temperature": request.temperature,
                "num_predict": request.max_output_tokens,
                "num_ctx": self._context_window,
            },
        }
        started = time.perf_counter()
        data = await post_json(self._http, self._url, body, timeout_s=request.timeout_s)
        latency_ms = int((time.perf_counter() - started) * 1000)
        try:
            content = data["message"]["content"] or ""
        except KeyError, TypeError:
            msg = "malformed Ollama chat response"
            raise TransientProviderError(msg) from None
        input_tokens = known_token_usage(data.get("prompt_eval_count"))
        output_tokens = known_token_usage(data.get("eval_count"))
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
            model_digest=self._model_digest,
            provider=self.name,
            latency_ms=latency_ms,
        )
