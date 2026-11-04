"""Lazy personal GitHub authorization and trusted configuration selection."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import AuthenticatedPrincipal
from app.api.errors import ApiError
from app.api.repositories import get_repository_reader
from app.application.github_ports import GithubFailure
from app.application.review_start_ports import (
    FrozenAnalysisConfiguration,
    RepositoryIdentity,
    ReviewConfigurationResolver,
    ReviewStartAuthorizer,
)
from app.core.config import Settings, get_settings
from app.db.session import async_session_factory, get_session
from app.review.configuration import (
    FrozenConfigurationError,
    load_runtime_configuration,
)

_READ_TIMEOUT_SECONDS = 15


@dataclass
class _GithubReviewStartAuthorizer:
    settings: Settings
    session: AsyncSession

    async def authorize_start(
        self, *, user_id: UUID, repository: RepositoryIdentity
    ) -> None:
        # Resolve credentials only when the handler reaches provider revalidation.
        async with asynccontextmanager(get_repository_reader)(
            AuthenticatedPrincipal(user_id), self.settings, self.session
        ) as reader:
            try:
                async with asyncio.timeout(_READ_TIMEOUT_SECONDS):
                    allowed = await reader.has_read_access(repository.external_id)
            except TimeoutError:
                raise GithubFailure("VCS_UNAVAILABLE", retryable=True) from None
            except RecursionError:
                raise GithubFailure("VCS_ACCESS_DENIED", retryable=False) from None
            except GithubFailure as error:
                if error.reason_code != "DIFF_UNTRUSTWORTHY":
                    raise
                # A malformed permission response cannot establish read access.
                raise GithubFailure("VCS_ACCESS_DENIED", retryable=False) from None
            if allowed:
                return
        raise GithubFailure("VCS_ACCESS_DENIED", retryable=False)


def get_review_start_authorizer(
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ReviewStartAuthorizer:
    return _GithubReviewStartAuthorizer(settings, session)


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
