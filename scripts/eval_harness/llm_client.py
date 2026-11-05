"""Explicit live acceptance configuration; production transport/Gateway reuse."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import UUID

import httpx

from app.domain.frozen_configuration import ExecutionModel, ExecutionSettings
from app.llm.budget import BudgetLimits, RunBudget
from app.review.configuration import (
    ExecutionConfiguration,
    TrustedRuntimeConfiguration,
    build_frozen_gateway,
    load_runtime_configuration,
)
from app.review.prompt import PromptBuilder
from scripts.eval_harness.dataset import Fixture
from scripts.eval_harness.runtime import Observation, gateway_response


class LlmConfigurationError(ValueError):
    """Safe configuration failure; no credential or provider exception text."""


@dataclass(frozen=True)
class LlmConfig:
    runtime: TrustedRuntimeConfiguration
    cap: Decimal
    input_price: Decimal
    output_price: Decimal

    @classmethod
    def approved(
        cls, path: Path, cap: str, input_price: str, output_price: str, fixtures: int
    ) -> LlmConfig:
        try:
            values = [Decimal(value) for value in (cap, input_price, output_price)]
            if (
                not all(value.is_finite() and value >= 0 for value in values)
                or values[0] <= 0
            ):
                raise ValueError
            runtime = load_runtime_configuration(path)
            config = cls(runtime, *values)
            limits = runtime.admission.policy.limits
            worst = config.cost(
                limits.run_max_input_tokens * fixtures,
                limits.run_max_output_tokens * fixtures,
            )
            if fixtures <= 0 or worst > config.cap:
                raise ValueError
            return config
        except ValueError, InvalidOperation:
            raise LlmConfigurationError(
                "Invalid live configuration or spend cap below reserved upper bound"
            ) from None

    def cost(self, input_tokens: int, output_tokens: int) -> Decimal:
        return (
            input_tokens * self.input_price + output_tokens * self.output_price
        ) / Decimal(1000000)


async def call_llm(
    fixture: Fixture,
    builder: PromptBuilder,
    config: LlmConfig,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Observation:
    runtime = config.runtime
    selection = runtime.admission
    policy = selection.policy
    limits = policy.limits
    routes = (selection.primary, *policy.routing.fallback)
    # Credentials are obtained only after explicit approval/cap validation.
    profiles = runtime.execution_profiles(tuple(route.profile_ref for route in routes))
    models = {
        route.profile_ref.split("/")[0]: ExecutionModel(model=route.model_ref)
        for route in routes
    }
    settings = ExecutionSettings(
        eurorouter=models.get("eurorouter", ExecutionModel(model="")),
        ollama=models.get("ollama", ExecutionModel(model="")),
        context_window=limits.context_window,
        max_input_tokens=limits.max_input_tokens,
        max_output_tokens=limits.max_output_tokens,
        request_timeout_s=limits.request_timeout_ms / 1000,
        run_max_requests=limits.run_max_requests,
        run_max_input_tokens=limits.run_max_input_tokens,
        run_max_output_tokens=limits.run_max_output_tokens,
        run_deadline_s=limits.analysis_duration_ms / 1000,
        max_chunks=limits.max_chunks,
        temperature=policy.generation.temperature,
    )
    execution = ExecutionConfiguration(
        review_id=UUID(int=0),
        repository_id=UUID(int=0),
        repository_settings_id=UUID(int=0),
        model_ref=selection.primary.model_ref,
        model_digest=selection.primary.model_digest,
        prompt_version=selection.prompt_version,
        prompt_digest=selection.prompt_digest,
        policy=policy,
        instructions=(),
        output_language="ru",
        ignores=(),
        surrounding_lines=0,
        service_settings=settings,
    )
    budget = RunBudget(
        BudgetLimits(
            limits.run_max_requests,
            limits.run_max_input_tokens,
            limits.run_max_output_tokens,
            limits.analysis_duration_ms / 1000,
        )
    )
    async with httpx.AsyncClient(transport=transport, follow_redirects=False) as http:
        gateway = build_frozen_gateway(execution, profiles, http)
        return await gateway_response(
            fixture,
            builder,
            gateway,
            budget,
            max_input_tokens=limits.max_input_tokens,
            max_output_tokens=limits.max_output_tokens,
            timeout_s=limits.request_timeout_ms / 1000,
            temperature=policy.generation.temperature,
        )
