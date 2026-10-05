"""Minimal OpenAI-compatible HTTP client, used only by --mode live.

Deliberately not a reuse of any backend LLM-gateway code: this harness is a
standalone tool and should not depend on src/app.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import httpx


class LlmConfigurationError(Exception):
    """Raised when live mode is requested without the required env vars."""


@dataclass(frozen=True, slots=True)
class LlmConfig:
    base_url: str
    api_key: str
    model: str

    @classmethod
    def from_env(cls) -> LlmConfig:
        base_url = os.environ.get("EVAL_LLM_BASE_URL")
        api_key = os.environ.get("EVAL_LLM_API_KEY")
        model = os.environ.get("EVAL_LLM_MODEL")
        if not base_url or not api_key or not model:
            raise LlmConfigurationError(
                "Live mode requires EVAL_LLM_BASE_URL, EVAL_LLM_API_KEY and "
                "EVAL_LLM_MODEL environment variables to be set"
            )
        return cls(base_url=base_url, api_key=api_key, model=model)


def call_llm(config: LlmConfig, *, system_prompt: str, user_message: str) -> str:
    """Call an OpenAI-compatible ``/chat/completions`` endpoint.

    Returns the raw assistant message content (expected to be a JSON string).
    """

    response = httpx.post(
        f"{config.base_url.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {config.api_key}"},
        json={
            "model": config.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
        },
        timeout=60.0,
    )
    response.raise_for_status()
    payload = response.json()
    return str(payload["choices"][0]["message"]["content"])
