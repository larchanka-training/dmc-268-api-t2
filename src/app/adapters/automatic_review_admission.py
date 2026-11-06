"""Compose the existing admission for a server-selected, fenced webhook receipt."""

import asyncio
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.personal_github import (
    PersonalCredentialUnavailableError,
    personal_github_connection,
)
from app.adapters.review_admission import admit_review
from app.application.github_ports import GithubFailure
from app.application.review_start_ports import (
    AutomaticReceiptClaimLostError,
    RepositoryIdentity,
    ReviewAdmissionError,
    ReviewAdmissionRequest,
)
from app.application.webhook_ports import AutomaticReceiptContext
from app.core.config import Settings, get_settings
from app.db.review_starts import find_automatic_replay
from app.db.webhook_inbox import SqlAlchemyWebhookInbox
from app.review.configuration import (
    FrozenConfigurationError,
    load_runtime_configuration,
)

_RENEW_INTERVAL_SECONDS = 30


@dataclass
class _CurrentAdminAuthorizer:
    settings: Settings
    session: AsyncSession

    async def authorize_start(
        self, *, user_id: UUID, repository: RepositoryIdentity
    ) -> None:
        try:
            async with (
                asyncio.timeout(15),
                personal_github_connection(
                    user_id, self.settings, self.session
                ) as reader,
            ):
                current = await reader.get_repository(repository.external_id)
                if current["role"] != "admin":
                    raise GithubFailure("VCS_ACCESS_DENIED", False)
        except PersonalCredentialUnavailableError, TimeoutError:
            raise GithubFailure("VCS_UNAVAILABLE", True) from None
        except RecursionError:
            raise GithubFailure("VCS_ACCESS_DENIED", False) from None


class AutomaticReviewAdmission:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def admit(self, context: AutomaticReceiptContext) -> None:
        admission = asyncio.create_task(self._admit(context))
        heartbeat = asyncio.create_task(self._renew(context))
        try:
            done, _ = await asyncio.wait(
                (admission, heartbeat), return_when=asyncio.FIRST_COMPLETED
            )
            if admission in done:
                await admission
            else:
                await heartbeat
        finally:
            admission.cancel()
            heartbeat.cancel()
            await asyncio.gather(admission, heartbeat, return_exceptions=True)

    async def _renew(self, context: AutomaticReceiptContext) -> None:
        inbox = SqlAlchemyWebhookInbox(self._factory)
        while True:
            await asyncio.sleep(_RENEW_INTERVAL_SECONDS)
            if not await inbox.renew(context.claim):
                return

    async def _admit(self, context: AutomaticReceiptContext) -> None:
        intent = context.claim.automatic
        if intent is None:
            raise AutomaticReceiptClaimLostError
        try:
            async with self._factory() as session:
                # This survives normal key expiry, disabled actors and new settings.
                if await find_automatic_replay(session, context) is not None:
                    return
                await session.rollback()
                runtime = load_runtime_configuration()
                resolver = runtime.admission_resolver(
                    self._factory,
                    Path(__file__).resolve().parents[3] / ".agents/reviewer",
                )
                await admit_review(
                    ReviewAdmissionRequest(
                        actor_id=intent.actor_user_id,
                        repository_id=intent.repository_id,
                        number=context.claim.request.number,
                        requested_head_sha=context.claim.request.head_sha,
                        rerun_of=None,
                        idempotency_key=str(context.claim.id),
                        trace_id=f"webhook:{context.claim.id}",
                        automatic=context,
                    ),
                    session=session,
                    authorizer=_CurrentAdminAuthorizer(get_settings(), session),
                    resolver=resolver,
                )
        except AutomaticReceiptClaimLostError:
            return
        except ReviewAdmissionError as error:
            if error.retryable or error.code == "QUOTA_EXCEEDED":
                raise GithubFailure(
                    "AUTO_ADMISSION_UNAVAILABLE", True, error.retry_after
                ) from None
            reasons = {
                "AUTO_BASE_FILTERED": "AUTO_BASE_FILTERED",
                "ACCESS_DENIED": "AUTO_ACTOR_DENIED",
                "RESOURCE_NOT_FOUND": "AUTO_RESOURCE_UNAVAILABLE",
                "PR_HEAD_MISMATCH": "AUTO_PR_STALE",
                "SETTINGS_VERSION_CONFLICT": "AUTO_SETTINGS_CHANGED",
                "INTEGRATION_UNHEALTHY": "AUTO_INTEGRATION_UNHEALTHY",
                "LEGACY_SETTINGS_UNSUPPORTED": "AUTO_SETTINGS_UNSUPPORTED",
            }
            await SqlAlchemyWebhookInbox(self._factory).complete(
                context.claim,
                context.snapshot,
                context.raw_diff,
                reason_code=reasons.get(error.code, "AUTO_ADMISSION_REFUSED"),
            )
        except FrozenConfigurationError, SQLAlchemyError, OSError:
            raise GithubFailure("AUTO_ADMISSION_UNAVAILABLE", True) from None
