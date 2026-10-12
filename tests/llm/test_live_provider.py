"""Opt-in smoke test against a real LLM provider (spec §4.2, SD §7).

Mocked tests cannot prove that the configured model accepts ``temperature``,
``max_tokens``/``num_predict`` and the strict JSON Schema. Run before admitting
a model into configuration:

    DMC268_LLM_SMOKE=1 LLM__EUROROUTER__BASE_URL=... LLM__EUROROUTER__MODEL=... \\
    LLM__EUROROUTER__API_KEYS=... uv run pytest tests/llm/test_live_provider.py

Each provider is checked on its own (no fallback), so a passing Ollama call
cannot hide a failing Eurorouter one.
"""

import os
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.core.config import LLMConfigError, ProviderName, Settings
from app.llm.budget import BudgetLimits, RunBudget
from app.llm.providers import build_gateway
from app.llm.tokens import ConservativeTokenCounter
from app.review.prompt import PromptBuilder
from app.review.schemas.context import ContextPayload
from app.review.schemas.result import ChunkStatus
from app.review.service import ReviewService
from app.review.validation import FindingValidator

_PROMPTS = Path(__file__).resolve().parents[2] / ".agents" / "reviewer"

pytestmark = pytest.mark.skipif(
    os.environ.get("DMC268_LLM_SMOKE") != "1",
    reason="set DMC268_LLM_SMOKE=1 and LLM__* to call a real provider",
)


@pytest.mark.parametrize("provider", ["eurorouter", "ollama"])
async def test_provider_returns_schema_valid_review(
    provider: ProviderName, payload_data: dict[str, Any]
) -> None:
    settings = Settings().llm.model_copy(
        update={"primary_provider": provider, "fallback_enabled": False}
    )
    try:
        settings.check_provider_chain()
    except LLMConfigError as exc:
        pytest.skip(str(exc))
    counter = ConservativeTokenCounter()
    async with httpx.AsyncClient() as http:
        service = ReviewService(
            gateway=build_gateway(settings, http),
            prompt_builder=PromptBuilder.load(
                _PROMPTS, settings.prompt_version, counter
            ),
            validator=FindingValidator(),
            token_counter=counter,
            settings=settings,
        )
        budget = RunBudget(
            BudgetLimits(
                max_requests=settings.run_max_requests,
                max_input_tokens=settings.run_max_input_tokens,
                max_output_tokens=settings.run_max_output_tokens,
                deadline_s=settings.run_deadline_s,
            )
        )

        outcome = await service.review_chunk(
            ContextPayload.model_validate(payload_data), budget
        )

    assert (outcome.status, outcome.error_code) == (ChunkStatus.COMPLETED, None)
    assert outcome.provider == provider
    # One request: the model honoured the schema without a format-repair retry.
    assert outcome.usage.requests == 1
