"""Resolve a parked UNKNOWN GitHub publication without manual SQL (issue #52).

The reconciliation seam (`GithubPublisher.reconcile`) existed from the start,
but production had no way to obtain the authority it requires: the lease store
grants publish claims only for PENDING work, so an ambiguous outcome stayed
UNRESOLVED without hand-written SQL. This CLI is the documented operator path:
it takes over the parked lease with a fresh fencing token and lets the
publisher resolve the outcome through provider reads — never another POST.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from typing import TYPE_CHECKING

from app.application.publication_ports import PublicationAuthorityError

if TYPE_CHECKING:
    import httpx
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.core.config import Settings


async def run_once(
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings,
    review_id: uuid.UUID,
    *,
    owner: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, object]:
    """Grant operator authority over a parked UNKNOWN and reconcile it.

    Raises PublicationAuthorityError when the publication carries no
    reconcilable UNKNOWN outcome; the provider is never written to twice.
    """
    from app.application.github_publication import GithubPublisher  # noqa: PLC0415
    from app.core.publication_runtime import (  # noqa: PLC0415
        github_publisher_connection,
    )
    from app.db.publications import PostgresPublicationStore  # noqa: PLC0415
    from app.db.task_leases import PostgresTaskLeaseStore  # noqa: PLC0415

    authority = await PostgresTaskLeaseStore(sessions).acquire_reconciliation(
        review_id, owner=owner
    )
    if authority is None:
        raise PublicationAuthorityError(
            "Publication has no reconcilable UNKNOWN outcome"
        )
    async with github_publisher_connection(settings, transport=transport) as github:
        publisher = GithubPublisher(
            PostgresPublicationStore(sessions),
            github,
            authority=authority,
            bot_user_id=settings.github.bot_user_id,
        )
        return await publisher.reconcile(review_job_id=review_id)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "review_id",
        type=uuid.UUID,
        help="Review job whose publication is parked in UNKNOWN.",
    )
    parser.add_argument(
        "--owner",
        default=None,
        help="Claim owner recorded on the lease (default: reconcile-<uuid>).",
    )
    args = parser.parse_args()
    import structlog  # noqa: PLC0415 -- keep argument parsing independent

    from app.core.config import Logging, get_settings  # noqa: PLC0415
    from app.core.logging_config import configure_logging  # noqa: PLC0415
    from app.core.sentry import configure_sentry  # noqa: PLC0415
    from app.db.session import async_session_factory  # noqa: PLC0415

    configure_logging(Logging(format="json", file_enabled=False))
    log = structlog.get_logger()
    try:
        configure_sentry(get_settings())
        outcome = asyncio.run(
            run_once(
                async_session_factory,
                get_settings(),
                args.review_id,
                owner=args.owner or f"reconcile-{uuid.uuid4()}",
            )
        )
    except Exception as error:
        log.error("publication_reconcile_failed", error_type=type(error).__name__)
        raise SystemExit(1) from None
    print(json.dumps(outcome, sort_keys=True))
    log.info("publication_reconcile_finished", **outcome)
    if outcome["status"] == "UNKNOWN":
        # Absence alone cannot release the operation; a non-zero exit keeps
        # the unresolved ambiguity visible to operators and scripts.
        raise SystemExit(3)


if __name__ == "__main__":
    main()
