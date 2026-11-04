"""New trusted admission selection cannot rewrite a saved historical identity."""

import uuid
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_frozen_configuration import capture
from test_postgres_integration import PostgresHarness
from test_review_start_api import HEAD, HEADERS, FakeStartGithub, StartScope
from test_runtime_manifest import PROMPTS, manifest_values, write_manifest

from app.api.review_start_dependencies import get_review_configuration_resolver
from app.llm.budget import BudgetLimits, RunBudget
from app.llm.client import ChatMessage, LLMRequest
from app.review.configuration import build_frozen_gateway, load_runtime_configuration
from app.review.schemas.result import LLMReviewOutput

pytest_plugins = ("test_review_start_api",)


async def test_changed_admission_keeps_historical_frozen_identity(  # noqa: PLR0913, PLR0917
    postgres: PostgresHarness,
    app: FastAPI,
    client: httpx.AsyncClient,
    scope: StartScope,
    providers: FakeStartGithub,
    tmp_path: Path,
) -> None:
    factory = async_sessionmaker(postgres.engine, expire_on_commit=False)
    original = manifest_values()
    original["admission"]["primary"]["profile_ref"] = "ollama/review-v1"
    policy = original["admission"]["policy"]
    policy["routing"]["primary_profile_ref"] = "ollama/review-v1"
    policy["generation"]["temperature"] = 0.3
    policy["limits"]["request_timeout_ms"] = 12345
    old = load_runtime_configuration(write_manifest(tmp_path, original))
    resolver = old.admission_resolver(factory, PROMPTS)
    app.dependency_overrides[get_review_configuration_resolver] = lambda: resolver
    response = await client.post(
        scope.url, json={"requested_head_sha": HEAD}, headers=HEADERS
    )
    assert response.status_code == 202  # noqa: PLR2004
    review_id = uuid.UUID(response.json()["review_id"])
    before = await old.configuration_reader(factory, PROMPTS).resolve(review_id)

    replacement = manifest_values()
    replacement["admission"]["primary"]["model_ref"] = "fallback-model"
    replacement["admission"]["policy"]["generation"]["temperature"] = 1.0
    runtime = load_runtime_configuration(write_manifest(tmp_path, replacement))
    assert runtime.identity != old.identity
    saved = await runtime.configuration_reader(factory, PROMPTS).resolve(review_id)
    assert saved == before
    assert saved.policy.generation.temperature == 0.3  # noqa: PLR2004
    assert saved.service_settings.request_timeout_s == 12.345  # noqa: PLR2004
    assert saved.policy.repository_settings_digest != "0" * 64
    assert saved.prompt_digest == original["admission"]["prompt_digest"]
    requests: list[dict[str, Any]] = []
    async with httpx.AsyncClient(transport=capture(requests)) as http:
        gateway = build_frozen_gateway(saved, runtime.profiles, http)
        await gateway.generate_structured(
            LLMRequest(
                messages=[ChatMessage(role="user", content="controlled test")],
                max_output_tokens=2000,
                temperature=saved.policy.generation.temperature,
                timeout_s=saved.service_settings.request_timeout_s,
            ),
            LLMReviewOutput,
            budget=RunBudget(BudgetLimits.from_settings(saved.service_settings)),
            input_tokens=100,
        )
    assert requests[0]["model"] == "test-model"
    assert requests[0]["options"]["temperature"] == 0.3  # noqa: PLR2004
