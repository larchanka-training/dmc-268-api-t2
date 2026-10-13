"""External authorization boundary for starting a review."""

from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID


class IdempotencyConflictError(Exception):
    """A live scoped key belongs to another request."""


class ReviewQuotaExceededError(Exception):
    """The authoritative admission quota cannot reserve another job."""


class ReviewAlreadyActiveError(Exception):
    """An equivalent unfinished review already exists."""


class SettingsVersionChangedError(Exception):
    """Current settings changed after configuration resolution."""


class AdmissionResourceNotFoundError(Exception):
    """The request no longer addresses an accessible resource."""


class ReviewHeadMismatchError(Exception):
    """The authoritative head no longer matches the requested SHA."""


@dataclass(frozen=True, slots=True)
class ChangeRequestIdentity:
    number: str
    head_sha: str
    base_sha: str
    source_repository_external_id: str | None
    source_ref: str
    base_ref: str
    status: str


@dataclass(frozen=True, slots=True)
class RepositoryIdentity:
    id: UUID
    provider: str
    external_id: str
    owner: str
    name: str


class ReviewStartAuthorizer(Protocol):
    async def authorize_start(
        self, *, user_id: UUID, repository: RepositoryIdentity
    ) -> None:
        """Revalidate the user's current provider permission or refuse the start."""
        ...


@dataclass(frozen=True, slots=True)
class FrozenAnalysisConfiguration:
    model_ref: str
    model_digest: str | None
    model_settings: dict[str, object]
    prompt_version: str
    prompt_digest: str
    frozen_configuration_version: Literal[1] = 1


class ReviewConfigurationResolver(Protocol):
    async def resolve(
        self,
        *,
        repository: RepositoryIdentity,
        settings_id: UUID,
        rules_digest: str,
    ) -> FrozenAnalysisConfiguration:
        """Resolve immutable server-owned identities outside DB transactions."""
        ...
