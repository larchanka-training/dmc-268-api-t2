"""External authorization boundary for starting a review."""

from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

from app.application.webhook_ports import AutomaticReceiptContext


class IdempotencyConflictError(Exception):
    """A live scoped key belongs to another request."""


class ReviewQuotaExceededError(Exception):
    """The authoritative admission quota cannot reserve another job."""


class ReviewAlreadyActiveError(Exception):
    """An equivalent unfinished review already exists."""


class PullRequestReviewActiveError(Exception):
    """Incremental review admits one unfinished review per pull request."""


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


@dataclass(frozen=True, slots=True)
class ReviewAdmissionRequest:
    actor_id: UUID
    repository_id: UUID
    number: int
    requested_head_sha: str
    rerun_of: str | None
    idempotency_key: str
    trace_id: str
    automatic: AutomaticReceiptContext | None = None


class ReviewAdmissionError(Exception):
    """Safe typed refusal shared by HTTP and durable intake adapters."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message
        self.retryable = retryable
        self.retry_after = retry_after


class AutomaticReceiptClaimLostError(Exception):
    """The trusted delivery/intent/fence no longer authorizes new admission."""
