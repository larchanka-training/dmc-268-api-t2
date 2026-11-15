"""Local Playground configuration preparation; no execution or client lifecycle."""

from dataclasses import dataclass, field
from pathlib import Path

from pydantic import SecretStr

from app.core.config import Settings
from app.domain.frozen_configuration import FrozenConfigurationError
from app.review.configuration import load_runtime_configuration


@dataclass(frozen=True, slots=True)
class SimulationConfiguration:
    """One direct provider call; never pass this through the review Gateway."""

    provider: str
    model: str
    base_url: str
    api_key: SecretStr = field(repr=False)
    max_input_tokens: int
    max_output_tokens: int
    execution_timeout_s: float


def prepare_simulation_configuration(
    settings: Settings, *, manifest_path: Path | None = None
) -> SimulationConfiguration | None:
    """Prepare locally; this function neither activates HTTP nor creates clients.

    The caller supplies the existing server-owned manifest's explicit path;
    ANALYSIS__CONFIG_PATH and analysis-worker activation are not consulted.
    Manifest model/endpoint are authoritative, effective Settings must match,
    and execution_profiles validates raw LLM__EUROROUTER__API_KEYS. Only its
    first key is retained; no rotation, retries, repair or fallback is allowed.
    A future executor must apply these token caps and pass a running-loop
    deadline bounded by execution_timeout_s to CodeSimulationService.
    """
    if not settings.simulation.enabled:
        return None
    if manifest_path is None:
        raise FrozenConfigurationError("simulation_manifest_path_required")
    runtime = load_runtime_configuration(manifest_path)
    route = runtime.admission.primary
    profile = runtime.profiles[route.profile_ref]
    if (
        route.profile_ref != "eurorouter/review-v1"
        or settings.llm.primary_provider != "eurorouter"
        or route.model_ref != "mistral-small-4"
        or settings.llm.eurorouter.model != route.model_ref
        or settings.llm.eurorouter.base_url.rstrip("/") != profile.base_url.rstrip("/")
    ):
        raise FrozenConfigurationError("simulation_provider_mismatch")
    if runtime.credential_env[route.profile_ref] != "LLM__EUROROUTER__API_KEYS":
        raise FrozenConfigurationError("simulation_credentials_source_mismatch")
    if settings.llm.fallback_enabled or runtime.admission.policy.routing.fallback:
        raise FrozenConfigurationError("simulation_fallback_forbidden")
    execution = runtime.execution_profiles((route.profile_ref,))[route.profile_ref]
    limits = runtime.admission.policy.limits
    return SimulationConfiguration(
        profile.provider,
        route.model_ref,
        profile.base_url,
        SecretStr(execution.api_keys[0]),
        min(8192, limits.max_input_tokens, limits.run_max_input_tokens),
        min(2000, limits.max_output_tokens, limits.run_max_output_tokens),
        min(30.0, limits.request_timeout_ms / 1000, limits.analysis_duration_ms / 1000),
    )
