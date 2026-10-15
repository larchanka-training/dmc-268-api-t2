"""Trusted configuration selection; user/provider authorization stays fail closed."""

from pathlib import Path
from uuid import UUID

from app.api.errors import ApiError
from app.application.review_start_ports import (
    FrozenAnalysisConfiguration,
    RepositoryIdentity,
    ReviewConfigurationResolver,
    ReviewStartAuthorizer,
)
from app.db.session import async_session_factory
from app.review.configuration import (
    FrozenConfigurationError,
    load_runtime_configuration,
)


class _UnavailableAuthorizer:
    async def authorize_start(
        self, *, user_id: UUID, repository: RepositoryIdentity
    ) -> None:
        raise ApiError(
            503, "SERVICE_UNAVAILABLE", "Service is unavailable.", retryable=True
        )


def get_review_start_authorizer() -> ReviewStartAuthorizer:
    return _UnavailableAuthorizer()


class _UnavailableConfigurationResolver:
    async def resolve(
        self, *, repository: RepositoryIdentity, settings_id: UUID, rules_digest: str
    ) -> FrozenAnalysisConfiguration:
        raise ApiError(
            503,
            "SERVICE_UNAVAILABLE",
            "Review configuration is unavailable.",
            retryable=True,
        )


def get_review_configuration_resolver() -> ReviewConfigurationResolver:
    try:
        runtime = load_runtime_configuration()
    except FrozenConfigurationError:
        return _UnavailableConfigurationResolver()
    return runtime.admission_resolver(
        async_session_factory, Path(__file__).resolve().parents[3] / ".agents/reviewer"
    )
