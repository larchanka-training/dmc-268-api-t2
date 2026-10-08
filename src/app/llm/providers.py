"""Build the provider chain from settings (spec §6.1)."""

import asyncio
import random
import time
from collections.abc import Callable

import httpx
import structlog

from app.core.config import LLM
from app.llm.client import BaseLLMClient
from app.llm.gateway import Endpoint, LLMGateway, ProviderPool, Sleep
from app.llm.ollama import OllamaClient
from app.llm.openai_compatible import OpenAICompatibleClient

logger = structlog.get_logger(__name__)


def build_gateway(
    settings: LLM,
    http: httpx.AsyncClient,
    *,
    sleep: Sleep = asyncio.sleep,
    rng: random.Random | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> LLMGateway:
    """Gateway over ``settings.provider_chain``: primary first, then fallback."""
    timeouts = {
        "eurorouter": settings.eurorouter.timeout_s,
        "ollama": settings.ollama.timeout_s,
    }
    pools = [
        ProviderPool(
            name,
            [Endpoint(client) for client in _clients(name, settings, http)],
            timeout_s=min(timeouts[name], settings.request_timeout_s),
        )
        for name in settings.provider_chain
    ]
    logger.info(
        "llm_provider_chain",
        chain=settings.provider_chain,
        eurorouter_keys=len(settings.eurorouter.api_keys),
    )
    return LLMGateway(pools, sleep=sleep, rng=rng, clock=clock)


def _clients(name: str, settings: LLM, http: httpx.AsyncClient) -> list[BaseLLMClient]:
    if name == "eurorouter":
        euro = settings.eurorouter
        return [
            OpenAICompatibleClient(
                name=name,
                http=http,
                base_url=euro.base_url,
                model=euro.model,
                api_key=key.get_secret_value(),
            )
            for key in euro.api_keys
        ]
    return [
        OllamaClient(
            name=name,
            http=http,
            base_url=settings.ollama.base_url,
            model=settings.ollama.model,
            context_window=settings.context_window,
        )
    ]
