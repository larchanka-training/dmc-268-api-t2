"""Benchmark the verified initial prompt and production Gateway without a DB.

This is not a worker E2E: admission/fencing/repair/publication are tested by the
existing application integration suites. Deterministic mode replaces HTTP only.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid5

import httpx

from app.llm.budget import BudgetLimits, RunBudget
from app.llm.client import LLMRequest
from app.llm.errors import LLMGatewayError, SchemaValidationError
from app.llm.gateway import Endpoint, LLMGateway, ProviderPool
from app.llm.openai_compatible import OpenAICompatibleClient
from app.llm.tokens import ConservativeTokenCounter
from app.review.prompt import PromptBuilder, PromptTooLargeError
from app.review.schemas.context import ContextPayload
from app.review.schemas.result import provider_output_model
from app.vcs.diff import parse_diff
from scripts.eval_harness.dataset import Fixture


@dataclass(frozen=True)
class Observation:
    response: Any
    requests: int
    input_tokens: int
    output_tokens: int
    error_code: str | None = None
    provider: str | None = None
    model: str | None = None


def fixture_context(fixture: Fixture) -> ContextPayload:
    parsed = parse_diff(fixture.diff_text)
    key = str(uuid5(UUID(int=0), fixture.id))
    return ContextPayload.model_validate(
        {
            "schema_version": 1,
            "review_id": key,
            "chunk_id": key,
            "snapshot": {
                "provider": "github",
                "base_repository_id": "1",
                "head_repository_id": "1",
                "base_sha": "0" * 40,
                "merge_base_sha": "0" * 40,
                "head_sha": "1" * 40,
                "captured_at": datetime(2026, 1, 1, tzinfo=UTC),
                "provider_metadata": {
                    "pull_number": 1,
                    "diff_version": "gold-dataset-v1",
                },
            },
            "metadata": {
                "title": fixture.title,
                "body": fixture.description,
                "base_ref": "main",
                "head_ref": "benchmark",
                "commit_messages": [],
                "languages": [fixture.language] if fixture.language else [],
                "discussions": [],
                "rules_version": "gold-dataset-v1",
                "output_language": "ru",
            },
            "files": parsed.files,
            "related_symbols": [],
            "coverage": parsed.coverage,
            "budget": {
                "estimated_input_tokens": 0,
                "reserved_output_tokens": 2000,
                "limit_input_tokens": 10000,
            },
        }
    )


async def gateway_response(  # noqa: PLR0913 -- independent captured call limits
    fixture: Fixture,
    builder: PromptBuilder,
    gateway: LLMGateway,
    budget: RunBudget,
    *,
    max_input_tokens: int = 10000,
    max_output_tokens: int = 2000,
    timeout_s: float = 120,
    temperature: float = 0,
) -> Observation:
    counter = ConservativeTokenCounter()
    try:
        prompt = builder.build(
            fixture_context(fixture), max_input_tokens=max_input_tokens
        )
        input_tokens = prompt.estimated_tokens + counter.count(
            builder.output_schema_json
        )
        if input_tokens > max_input_tokens:
            raise PromptTooLargeError
    except PromptTooLargeError:
        return Observation(None, 0, 0, 0, "prompt_too_large")
    actual_provider = actual_model = None
    try:
        call = await gateway.generate_structured(
            LLMRequest(
                messages=prompt.messages,
                temperature=temperature,
                max_output_tokens=max_output_tokens,
                timeout_s=timeout_s,
                output_schema_json=builder.output_schema_json,
            ),
            provider_output_model(builder.provider_schema_path),
            budget=budget,
            input_tokens=input_tokens,
        )
        response: Any = call.parsed.model_dump(mode="json")
        error = None
        actual_provider, actual_model = call.provider, call.model
    except SchemaValidationError as exc:
        try:
            response = json.loads(exc.raw_text)
        except ValueError:
            response = None
        error = exc.error_code
    except LLMGatewayError as exc:
        response, error = None, exc.error_code
    state = budget.state()
    return Observation(
        response,
        state.requests_used,
        state.input_tokens_used,
        state.output_tokens_used,
        error,
        actual_provider,
        actual_model,
    )


async def deterministic_response(
    fixture: Fixture, builder: PromptBuilder
) -> Observation:
    def replay(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "gold-replay",
                "choices": [
                    {"message": {"content": json.dumps(fixture.golden_response)}}
                ],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(replay)) as http:
        client = OpenAICompatibleClient(
            name="mock",
            http=http,
            base_url="https://benchmark.invalid/v1",
            model="gold-replay",
            api_key="not-a-credential",
        )
        gateway = LLMGateway([ProviderPool("mock", [Endpoint(client)], timeout_s=120)])
        return await gateway_response(
            fixture, builder, gateway, RunBudget(BudgetLimits(8, 80000, 16000, 600))
        )
