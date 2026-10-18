"""Read saved configuration without policy overrides or worker activation.

No budget is constructed here. Lifecycle/durable-budget composition belongs to
later stages; controlled tests may explicitly supply their existing RunBudget.
"""

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from pydantic import Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.ports import StartReviewCommand
from app.application.review_start_ports import (
    FrozenAnalysisConfiguration,
    RepositoryIdentity,
)
from app.application.use_cases.start_review import compute_config_digest
from app.db.models import RepositorySettings, ReviewJob
from app.domain.frozen_configuration import (
    Digest,
    ExecutionModel,
    ExecutionSettings,
    FrozenConfigurationError,
    FrozenDTO,
    ModelRoute,
    ModelSettings,
    ProfileRef,
    RepositoryInstructions,
    Selector,
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
from app.review.preflight import PreflightIssue, PreflightReport, offline_preflight
from app.review.prompt import PromptBuilder, PromptConfigError
from app.review.schemas.context import ContextPayload


@dataclass(frozen=True, slots=True)
class RuntimeProfile:
    """Transport only. A profile cannot supply a replacement model/policy."""

    provider: str
    base_url: str
    api_keys: tuple[str, ...] = field(repr=False)
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


def validate_profile_structure(
    route: ModelRoute, profiles: Mapping[str, RuntimeProfile]
) -> RuntimeProfile:
    if route.model_digest is not None:
        raise FrozenConfigurationError("model_digest_unattestable")
    profile = profiles.get(route.profile_ref)
    provider = route.profile_ref.split("/")[0]
    try:
        url = urlsplit(profile.base_url if profile else "")
        valid_url = (
            bool(url.hostname)
            and url.scheme
            in ({"https"} if provider == "eurorouter" else {"http", "https"})
            and not url.username
            and not url.password
            and not url.query
            and not url.fragment
            and (url.port is None or url.port > 0)
            and profile is not None
            and not any(c.isspace() or not c.isprintable() for c in profile.base_url)
            and "\\" not in profile.base_url
        )
    except ValueError:
        valid_url = False
    if (
        profile is None
        or route.profile_ref not in {"eurorouter/review-v1", "ollama/review-v1"}
        or profile.provider != provider
        or route.model_ref not in profile.allowed_models
        or not valid_url
    ):
        raise FrozenConfigurationError("unsupported_model_profile")
    return profile


def _profile(
    route: ModelRoute, profiles: Mapping[str, RuntimeProfile]
) -> RuntimeProfile:
    return validate_profile_structure(route, profiles)


def validate_execution_credentials(profile: RuntimeProfile) -> None:
    """Local readiness only; never claim provider-side validity or print keys."""
    if profile.provider == "eurorouter" and (
        not profile.api_keys
        or any(
            not isinstance(key, str)
            or not key
            or any(ord(char) < 33 or ord(char) > 126 or char == "," for char in key)  # noqa: PLR2004 -- printable ASCII header values
            for key in profile.api_keys
        )
    ):
        raise FrozenConfigurationError("invalid_execution_credentials")


class ProfileDescriptor(FrozenDTO):
    provider: Literal["eurorouter", "ollama"]
    base_url: Annotated[str, Field(min_length=1, max_length=2048)]
    allowed_models: Annotated[tuple[Selector, ...], Field(min_length=1, max_length=100)]
    credential_env: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,127}$")] | None


class AdmissionSelection(FrozenDTO):
    primary: ModelRoute
    policy: ModelSettings
    prompt_version: Literal["v1", "v2", "v3"]
    prompt_digest: Digest


class RuntimeManifest(FrozenDTO):
    schema_version: Literal[1]
    profiles: Annotated[dict[ProfileRef, ProfileDescriptor], Field(min_length=1)]
    admission: AdmissionSelection


@dataclass(frozen=True, slots=True)
class TrustedRuntimeConfiguration:
    """Validated nonsecret inventory; admission is never a historical default."""

    identity: str
    profiles: Mapping[str, RuntimeProfile]
    credential_env: Mapping[str, str | None]
    admission: AdmissionSelection

    def execution_profiles(
        self, required: tuple[str, ...], *, environ: Mapping[str, str] | None = None
    ) -> Mapping[str, RuntimeProfile]:
        environment = os.environ if environ is None else environ
        profiles = dict(self.profiles)
        for ref in required:
            if ref not in profiles:
                raise FrozenConfigurationError("unsupported_model_profile")
            name = self.credential_env[ref]
            keys: tuple[str, ...] = ()
            if name is not None:
                raw = environment.get(name)
                if (
                    not isinstance(raw, str)
                    or not raw.isascii()
                    or not raw.isprintable()
                ):
                    raise FrozenConfigurationError("invalid_execution_credentials")
                keys = tuple(key.strip() for key in raw.split(","))
            profile = replace(profiles[ref], api_keys=keys)
            validate_execution_credentials(profile)
            profiles[ref] = profile
        return MappingProxyType(profiles)

    def admission_resolver(
        self, factory: async_sessionmaker[AsyncSession], prompt_root: Path
    ) -> FrozenAdmissionResolver:
        return FrozenAdmissionResolver(
            factory,
            prompt_root,
            self.profiles,
            primary=self.admission.primary,
            policy=self.admission.policy,
            prompt_version=self.admission.prompt_version,
            prompt_digest=self.admission.prompt_digest,
        )

    def configuration_reader(
        self, factory: async_sessionmaker[AsyncSession], prompt_root: Path
    ) -> FrozenConfigurationReader:
        return FrozenConfigurationReader(factory, prompt_root, self.profiles)

    async def offline_preflight(
        self,
        factory: async_sessionmaker[AsyncSession],
        prompt_root: Path,
        *,
        scan_limit: int = 100,
    ) -> PreflightReport:
        return await offline_preflight(
            self, factory, prompt_root, scan_limit=scan_limit
        )

    async def deployment_preflight(
        self,
        factory: async_sessionmaker[AsyncSession],
        prompt_root: Path,
        *,
        environ: Mapping[str, str] | None = None,
        scan_limit: int = 100,
    ) -> PreflightReport:
        report = await self.offline_preflight(
            factory, prompt_root, scan_limit=scan_limit
        )
        if not report.ready:
            return report
        try:
            self.execution_profiles(report.required_profiles, environ=environ)
        except FrozenConfigurationError as exc:
            return replace(
                report, issues=(*report.issues, PreflightIssue(None, exc.reason))
            )
        return report


def _unique_manifest_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for key, value in pairs:
        if key in values:
            raise ValueError("duplicate key")
        values[key] = value
    return values


def load_runtime_configuration(path: Path | None = None) -> TrustedRuntimeConfiguration:
    """Load a server-owned JSON file. Never inspect credential environment values."""
    try:
        if path is None:
            configured = os.environ.get("ANALYSIS__CONFIG_PATH", "")
            if not configured:
                raise ValueError("missing path")
            path = Path(configured)
        raw = path.read_bytes()
        values = json.loads(raw, object_pairs_hook=_unique_manifest_keys)
        manifest = RuntimeManifest.model_validate_json(
            json.dumps(values, allow_nan=False)
        )
        profiles: dict[str, RuntimeProfile] = {}
        credentials: dict[str, str | None] = {}
        for ref, descriptor in manifest.profiles.items():
            if (
                descriptor.provider != ref.split("/")[0]
                or (
                    descriptor.provider == "eurorouter"
                    and descriptor.credential_env is None
                )
                or (
                    descriptor.provider == "ollama"
                    and descriptor.credential_env is not None
                )
            ):
                raise ValueError("invalid provider credentials descriptor")
            profile = RuntimeProfile(
                descriptor.provider, descriptor.base_url, (), descriptor.allowed_models
            )
            profiles[ref] = profile
            credentials[ref] = descriptor.credential_env
            for model in descriptor.allowed_models:
                validate_profile_structure(
                    ModelRoute(profile_ref=ref, model_ref=model, model_digest=None),
                    profiles,
                )
        admission = manifest.admission
        if (
            admission.primary.profile_ref
            != admission.policy.routing.primary_profile_ref
        ):
            raise ValueError("primary mismatch")
        for route in (admission.primary, *admission.policy.routing.fallback):
            validate_profile_structure(route, profiles)
        identity = hashlib.sha256(
            json.dumps(
                manifest.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
    except OSError, ValueError, TypeError:
        raise FrozenConfigurationError("invalid_runtime_manifest") from None
    return TrustedRuntimeConfiguration(
        identity, MappingProxyType(profiles), MappingProxyType(credentials), admission
    )


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
        validate_execution_credentials(profile)
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
                    Endpoint(
                        client,
                        expected_model=route.model_ref,
                        expected_digest=route.model_digest,
                    )
                    for client in clients
                ],
                timeout_s=config.service_settings.request_timeout_s,
                profile_ref=route.profile_ref,
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
        prompt_digest: str | None = None,
    ) -> None:
        self._factory = factory
        self._prompts = prompt_root
        self._profiles = MappingProxyType(dict(profiles))
        self._primary = primary
        self._policy = policy
        self._version = prompt_version
        self._digest = prompt_digest

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
                    self._prompts,
                    self._version,
                    expected_digest=self._digest,
                    repository_instructions=instructions,
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
