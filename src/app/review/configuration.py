"""Read saved configuration without policy overrides or worker activation.

No budget is constructed here. Lifecycle/durable-budget composition belongs to
later stages; controlled tests may explicitly supply their existing RunBudget.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from uuid import UUID

import httpx
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.ports import StartReviewCommand
from app.application.review_start_ports import (
    FrozenAnalysisConfiguration,
    RepositoryIdentity,
)
from app.application.use_cases.start_review import compute_config_digest
from app.db.models import RepositorySettings, ReviewJob
from app.domain.frozen_configuration import (
    ExecutionModel,
    ExecutionSettings,
    FrozenConfigurationError,
    ModelRoute,
    ModelSettings,
    RepositoryInstructions,
)
from app.domain.settings import (
    compute_repository_settings_digest,
    compute_rules_digest,
    project_historical_empty_settings,
)
from app.llm.client import BaseLLMClient
from app.llm.gateway import Endpoint, LLMGateway, ProviderPool
from app.llm.ollama import OllamaClient
from app.llm.openai_compatible import OpenAICompatibleClient
from app.review.prompt import PromptBuilder, PromptConfigError
from app.review.schemas.context import ContextPayload


@dataclass(frozen=True, slots=True)
class RuntimeProfile:
    """Transport only. A profile cannot supply a replacement model/policy."""

    provider: str
    base_url: str
    api_keys: tuple[str, ...]
    allowed_models: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ExecutionConfiguration:
    review_id: UUID
    repository_id: UUID
    repository_settings_id: UUID
    model_ref: str
    model_digest: str | None
    prompt_version: str
    prompt_digest: str
    policy: ModelSettings
    instructions: tuple[str, ...]
    output_language: str
    ignores: tuple[str, ...]
    surrounding_lines: int
    service_settings: ExecutionSettings

    def validate_context(self, payload: ContextPayload) -> None:
        if (
            payload.review_id != str(self.review_id)
            or payload.metadata.rules_version != str(self.repository_settings_id)
            or payload.metadata.output_language != self.output_language
            or payload.budget.limit_input_tokens != self.policy.limits.max_input_tokens
            or payload.budget.reserved_output_tokens
            != self.policy.limits.max_output_tokens
        ):
            raise FrozenConfigurationError("context_configuration_mismatch")


def _profile(
    route: ModelRoute, profiles: Mapping[str, RuntimeProfile]
) -> RuntimeProfile:
    if route.model_digest is not None:
        raise FrozenConfigurationError("model_digest_unattestable")
    profile = profiles.get(route.profile_ref)
    provider = route.profile_ref.split("/")[0]
    if (
        profile is None
        or profile.provider != provider
        or route.model_ref not in profile.allowed_models
        or not profile.base_url
        or (
            provider == "eurorouter"
            and (not profile.base_url.startswith("https://") or not profile.api_keys)
        )
    ):
        raise FrozenConfigurationError("unsupported_model_profile")
    return profile


def _routes(config: ExecutionConfiguration) -> tuple[ModelRoute, ...]:
    return (
        ModelRoute(
            profile_ref=config.policy.routing.primary_profile_ref,
            model_ref=config.model_ref,
            model_digest=config.model_digest,
        ),
        *config.policy.routing.fallback,
    )


class FrozenConfigurationReader:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        prompt_root: Path,
        profiles: Mapping[str, RuntimeProfile],
    ) -> None:
        self._factory = factory
        self._prompts = prompt_root
        self._profiles = MappingProxyType(dict(profiles))

    async def resolve(self, review_id: UUID) -> ExecutionConfiguration:
        async with self._factory() as session:
            job = await session.get(ReviewJob, review_id)
            if job is None:
                raise FrozenConfigurationError("review_not_found")
            if job.frozen_configuration_version is None:
                raise FrozenConfigurationError("legacy_frozen_configuration")
            if job.frozen_configuration_version != 1:
                raise FrozenConfigurationError(
                    "unsupported_frozen_configuration_version"
                )
            settings = await session.get(RepositorySettings, job.repository_settings_id)
            if settings is None or settings.repository_id != job.repository_id:
                raise FrozenConfigurationError("repository_settings_mismatch")
            try:
                policy = ModelSettings.from_stored(job.model_settings)
                primary = ModelRoute(
                    profile_ref=policy.routing.primary_profile_ref,
                    model_ref=job.model_ref,
                    model_digest=job.model_digest,
                )
            except ValidationError, TypeError, ValueError:
                raise FrozenConfigurationError("unsupported_model_settings") from None
            command = StartReviewCommand(
                change_request_id=job.change_request_id,
                repository_settings_id=job.repository_settings_id,
                requested_head_sha=job.requested_head_sha,
                rules_digest=job.rules_digest,
                trigger_type=job.trigger_type,
                initiator_user_id=job.initiator_user_id,
                model_ref=job.model_ref,
                model_digest=job.model_digest,
                model_settings=job.model_settings,
                prompt_version=job.prompt_version,
                prompt_digest=job.prompt_digest,
                trace_id="configuration-reader",
            )
            if compute_config_digest(command) != job.config_digest:
                raise FrozenConfigurationError("config_digest_mismatch")
            if (
                compute_rules_digest(settings.rules) != job.rules_digest
                or settings.rules_digest != job.rules_digest
            ):
                raise FrozenConfigurationError("rules_digest_mismatch")
            if (
                compute_repository_settings_digest(
                    id=settings.id,
                    repository_id=settings.repository_id,
                    rules=settings.rules,
                    ignores=settings.ignores,
                    output_language=settings.output_language,
                    surrounding_lines=settings.surrounding_lines,
                )
                != policy.repository_settings_digest
            ):
                raise FrozenConfigurationError("repository_settings_digest_mismatch")
            try:
                instructions = RepositoryInstructions(
                    instructions=tuple(
                        project_historical_empty_settings(
                            settings.rules, "instructions"
                        )["instructions"]
                    )
                )
                PromptBuilder.load(
                    self._prompts,
                    job.prompt_version,
                    expected_digest=job.prompt_digest,
                    repository_instructions=instructions,
                )
            except PromptConfigError, ValidationError, KeyError, TypeError:
                raise FrozenConfigurationError("unsupported_prompt_bundle") from None
            for route in (primary, *policy.routing.fallback):
                _profile(route, self._profiles)
            limits = policy.limits
            models = {
                route.profile_ref.split("/")[0]: ExecutionModel(model=route.model_ref)
                for route in (primary, *policy.routing.fallback)
            }
            service = ExecutionSettings(
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
            return ExecutionConfiguration(
                review_id=job.id,
                repository_id=job.repository_id,
                repository_settings_id=settings.id,
                model_ref=job.model_ref,
                model_digest=job.model_digest,
                prompt_version=job.prompt_version,
                prompt_digest=job.prompt_digest,
                policy=policy,
                instructions=instructions.instructions,
                output_language=settings.output_language,
                ignores=tuple(
                    project_historical_empty_settings(settings.ignores, "globs")[
                        "globs"
                    ]
                ),
                surrounding_lines=settings.surrounding_lines,
                service_settings=service,
            )


def build_frozen_gateway(
    config: ExecutionConfiguration,
    profiles: Mapping[str, RuntimeProfile],
    http: httpx.AsyncClient,
) -> LLMGateway:
    """Use saved ordered routes and limits; transport cannot alter identities."""
    pools = []
    for route in _routes(config):
        profile = _profile(route, profiles)
        clients: list[BaseLLMClient]
        if profile.provider == "eurorouter":
            clients = [
                OpenAICompatibleClient(
                    name=profile.provider,
                    http=http,
                    base_url=profile.base_url,
                    model=route.model_ref,
                    api_key=key,
                )
                for key in profile.api_keys
            ]
        else:
            clients = [
                OllamaClient(
                    name=profile.provider,
                    http=http,
                    base_url=profile.base_url,
                    model=route.model_ref,
                    context_window=config.policy.limits.context_window,
                    model_digest=route.model_digest,
                )
            ]
        pools.append(
            ProviderPool(
                profile.provider,
                [
                    Endpoint(client, expected_model=route.model_ref)
                    for client in clients
                ],
                timeout_s=config.service_settings.request_timeout_s,
            )
        )
    return LLMGateway(pools)


class FrozenAdmissionResolver:
    """Inject trusted selection explicitly; no environment read or default wiring."""

    def __init__(  # noqa: PLR0913 -- trusted inputs, never client request fields
        self,
        factory: async_sessionmaker[AsyncSession],
        prompt_root: Path,
        profiles: Mapping[str, RuntimeProfile],
        *,
        primary: ModelRoute,
        policy: ModelSettings,
        prompt_version: str = "v2",
    ) -> None:
        self._factory = factory
        self._prompts = prompt_root
        self._profiles = MappingProxyType(dict(profiles))
        self._primary = primary
        self._policy = policy
        self._version = prompt_version

    async def resolve(
        self, *, repository: RepositoryIdentity, settings_id: UUID, rules_digest: str
    ) -> FrozenAnalysisConfiguration:
        if self._primary.profile_ref != self._policy.routing.primary_profile_ref:
            raise FrozenConfigurationError("unsupported_model_profile")
        for route in (self._primary, *self._policy.routing.fallback):
            _profile(route, self._profiles)
        async with self._factory() as session:
            settings = await session.get(RepositorySettings, settings_id)
            if settings is None or settings.repository_id != repository.id:
                raise FrozenConfigurationError("repository_settings_mismatch")
            if (
                settings.rules_digest != rules_digest
                or compute_rules_digest(settings.rules) != rules_digest
            ):
                raise FrozenConfigurationError("rules_digest_mismatch")
            digest = compute_repository_settings_digest(
                id=settings.id,
                repository_id=settings.repository_id,
                rules=settings.rules,
                ignores=settings.ignores,
                output_language=settings.output_language,
                surrounding_lines=settings.surrounding_lines,
            )
            try:
                instructions = RepositoryInstructions(
                    instructions=tuple(
                        project_historical_empty_settings(
                            settings.rules, "instructions"
                        )["instructions"]
                    )
                )
                bundle = PromptBuilder.load(
                    self._prompts, self._version, repository_instructions=instructions
                )
            except PromptConfigError, ValidationError, KeyError, TypeError:
                raise FrozenConfigurationError("unsupported_prompt_bundle") from None
            policy_values = self._policy.model_dump(mode="json")
            policy_values["repository_settings_digest"] = digest
            policy = ModelSettings.from_stored(policy_values)
            assert bundle.prompt_digest is not None
            return FrozenAnalysisConfiguration(
                model_ref=self._primary.model_ref,
                model_digest=self._primary.model_digest,
                model_settings=policy.model_dump(mode="json"),
                prompt_version=self._version,
                prompt_digest=bundle.prompt_digest,
            )


__all__ = [
    "ExecutionConfiguration",
    "FrozenAdmissionResolver",
    "FrozenConfigurationError",
    "FrozenConfigurationReader",
    "RuntimeProfile",
    "build_frozen_gateway",
]
