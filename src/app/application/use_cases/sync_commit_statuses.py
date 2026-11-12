"""Mirror due review cycles to GitHub commit statuses with retry backoff."""

import random

import structlog

from app.application.commit_status_ports import (
    MAX_FAILED_ATTEMPTS,
    CommitStatusStore,
    CommitStatusWriter,
)
from app.application.github_ports import GithubFailure
from app.domain.commit_status import commit_status_for

BACKOFF_BASE_S = 5.0
BACKOFF_CAP_S = 300.0


def retry_delay_s(
    failed_attempts: int, retry_after: float | None, rng: random.Random
) -> float:
    """Exponential delay with jitter; a provider Retry-After is a lower bound."""
    delay = min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2.0**failed_attempts)
    return max(retry_after or 0.0, delay * rng.uniform(0.5, 1.0))


async def sync_commit_statuses(  # noqa: PLR0913 -- explicit runtime inputs
    store: CommitStatusStore,
    github: CommitStatusWriter,
    *,
    context: str,
    report_base_url: str,
    limit: int = 20,
    rng: random.Random | None = None,
) -> int:
    """Return the number of statuses GitHub accepted in this pass."""
    rng = rng or random.Random()
    sent = 0
    for target in await store.due(limit=limit):
        decision = commit_status_for(
            target.review_status, critical_findings=target.critical_findings
        )
        if decision.state == target.posted_state:
            continue
        try:
            await github.create_commit_status(
                target.owner,
                target.name,
                target.head_sha,
                state=decision.state,
                description=decision.description,
                context=context,
                target_url=(
                    f"{report_base_url}/reviews/{target.review_id}"
                    if report_base_url
                    else None
                ),
            )
        except GithubFailure as failure:
            attempt = target.failed_attempts + 1
            if not failure.retryable or attempt >= MAX_FAILED_ATTEMPTS:
                # Permanent rejection (e.g. missing statuses:write) never heals
                # by retrying; parking keeps it off the installation rate limit.
                structlog.get_logger().error(
                    "commit_status_abandoned",
                    review_id=str(target.review_id),
                    reason_code=failure.reason_code,
                    retryable=failure.retryable,
                    attempt=attempt,
                )
                await store.park(target.review_id)
                continue
            structlog.get_logger().warning(
                "commit_status_deferred",
                review_id=str(target.review_id),
                reason_code=failure.reason_code,
                attempt=attempt,
            )
            await store.record_failure(
                target.review_id,
                retry_after_s=retry_delay_s(
                    target.failed_attempts, failure.retry_after, rng
                ),
            )
            continue
        await store.record_posted(target.review_id, decision.state)
        sent += 1
    return sent
