"""Commit status policy and sync through fake store/GitHub boundaries."""

import uuid
from dataclasses import dataclass, field

import pytest

from app.application.commit_status_ports import (
    MAX_FAILED_ATTEMPTS,
    CommitStatusTarget,
)
from app.application.github_ports import GithubFailure
from app.application.use_cases.sync_commit_statuses import sync_commit_statuses
from app.domain.commit_status import commit_status_for

HEAD = "a" * 40
CONTEXT = "dmc268/ai-review"
_RETRY_AFTER_S = 42


@pytest.mark.parametrize(
    "status", ["QUEUED", "FETCHING_DIFF", "PARSING_CONTEXT", "LLM_PROCESSING"]
)
def test_active_review_is_pending(status: str) -> None:
    assert commit_status_for(status, critical_findings=0).state == "pending"


@pytest.mark.parametrize("status", ["COMPLETED", "PARTIAL"])
def test_critical_finding_fails_and_blocks_the_merge(status: str) -> None:
    decision = commit_status_for(status, critical_findings=2)

    assert decision.state == "failure"
    assert "2" in decision.description


@pytest.mark.parametrize("status", ["COMPLETED", "PARTIAL", "SKIPPED"])
def test_review_without_critical_findings_succeeds(status: str) -> None:
    assert commit_status_for(status, critical_findings=0).state == "success"


def test_failed_review_reports_error() -> None:
    assert commit_status_for("FAILED", critical_findings=0).state == "error"


def _target(**values: object) -> CommitStatusTarget:
    defaults: dict[str, object] = {
        "review_id": uuid.uuid4(),
        "owner": "team",
        "name": "project",
        "head_sha": HEAD,
        "review_status": "COMPLETED",
        "critical_findings": 0,
        "posted_state": None,
        "failed_attempts": 0,
    }
    return CommitStatusTarget(**(defaults | values))  # type: ignore[arg-type]


@dataclass
class _Store:
    targets: list[CommitStatusTarget]
    posted: list[tuple[uuid.UUID, str]] = field(default_factory=list)
    failures: list[tuple[uuid.UUID, float]] = field(default_factory=list)
    parked: list[uuid.UUID] = field(default_factory=list)

    async def due(self, *, limit: int) -> list[CommitStatusTarget]:
        return self.targets[:limit]

    async def record_posted(self, review_id: uuid.UUID, state: str) -> None:
        self.posted.append((review_id, state))

    async def record_failure(
        self, review_id: uuid.UUID, *, retry_after_s: float
    ) -> None:
        self.failures.append((review_id, retry_after_s))

    async def park(self, review_id: uuid.UUID) -> None:
        self.parked.append(review_id)


@dataclass
class _Github:
    fail: GithubFailure | None = None
    calls: list[dict[str, object]] = field(default_factory=list)

    async def create_commit_status(  # noqa: PLR0913 -- mirrors the gateway
        self,
        owner: str,
        name: str,
        sha: str,
        *,
        state: str,
        description: str,
        context: str,
        target_url: str | None,
    ) -> None:
        self.calls.append(
            {
                "owner": owner,
                "name": name,
                "sha": sha,
                "state": state,
                "context": context,
                "target_url": target_url,
            }
        )
        if self.fail is not None:
            raise self.fail


async def test_sync_posts_status_with_report_link_and_records_it() -> None:
    target = _target(critical_findings=1)
    store, github = _Store([target]), _Github()

    sent = await sync_commit_statuses(
        store, github, context=CONTEXT, report_base_url="https://ui.example"
    )

    assert sent == 1
    assert github.calls == [
        {
            "owner": "team",
            "name": "project",
            "sha": HEAD,
            "state": "failure",
            "context": CONTEXT,
            "target_url": f"https://ui.example/reviews/{target.review_id}",
        }
    ]
    assert store.posted == [(target.review_id, "failure")]


async def test_sync_omits_report_link_without_configured_ui() -> None:
    github = _Github()

    await sync_commit_statuses(
        _Store([_target()]), github, context=CONTEXT, report_base_url=""
    )

    assert github.calls[0]["target_url"] is None


async def test_sync_skips_already_posted_state_without_http() -> None:
    store, github = (
        _Store([_target(review_status="QUEUED", posted_state="pending")]),
        _Github(),
    )

    assert (
        await sync_commit_statuses(store, github, context=CONTEXT, report_base_url="")
        == 0
    )
    assert github.calls == []
    assert store.posted == []


async def test_failed_post_schedules_exponential_retry() -> None:
    first, later = _target(), _target(failed_attempts=3)
    store = _Store([first, later])
    github = _Github(fail=GithubFailure("VCS_UNAVAILABLE", retryable=True))

    assert (
        await sync_commit_statuses(store, github, context=CONTEXT, report_base_url="")
        == 0
    )

    assert store.posted == []
    (_, first_delay), (_, later_delay) = store.failures
    # base 5s doubled per failed attempt, jitter keeps 50-100% of it
    assert 2.5 <= first_delay <= 5  # noqa: PLR2004
    assert 20 <= later_delay <= 40  # noqa: PLR2004


async def test_failed_post_honours_provider_retry_after() -> None:
    store = _Store([_target()])
    github = _Github(
        fail=GithubFailure(
            "VCS_RATE_LIMITED", retryable=True, retry_after=_RETRY_AFTER_S
        )
    )

    await sync_commit_statuses(store, github, context=CONTEXT, report_base_url="")

    assert store.failures[0][1] >= _RETRY_AFTER_S


async def test_one_failed_target_does_not_stop_the_batch() -> None:
    failing, healthy = _target(owner="broken"), _target()
    store = _Store([failing, healthy])

    class _Partial(_Github):
        async def create_commit_status(
            self, owner: str, *args: object, **kwargs: object
        ) -> None:
            if owner == "broken":
                raise GithubFailure("VCS_ACCESS_DENIED", retryable=False)

    await sync_commit_statuses(store, _Partial(), context=CONTEXT, report_base_url="")

    assert store.posted == [(healthy.review_id, "success")]
    assert store.parked == [failing.review_id]
    assert store.failures == []


@pytest.mark.parametrize(
    "failure",
    [
        GithubFailure("VCS_ACCESS_DENIED", retryable=False),
        GithubFailure("DIFF_UNTRUSTWORTHY", retryable=False),
    ],
)
async def test_permanent_failure_parks_the_cycle_without_retry(
    failure: GithubFailure,
) -> None:
    target = _target()
    store = _Store([target])

    await sync_commit_statuses(
        store, _Github(fail=failure), context=CONTEXT, report_base_url=""
    )

    assert store.parked == [target.review_id]
    assert store.failures == []


async def test_retryable_failure_parks_once_attempts_are_exhausted() -> None:
    last, earlier = (
        _target(failed_attempts=MAX_FAILED_ATTEMPTS - 1),
        _target(failed_attempts=MAX_FAILED_ATTEMPTS - 2),
    )
    store = _Store([last, earlier])
    github = _Github(fail=GithubFailure("VCS_UNAVAILABLE", retryable=True))

    await sync_commit_statuses(store, github, context=CONTEXT, report_base_url="")

    assert store.parked == [last.review_id]
    assert [item[0] for item in store.failures] == [earlier.review_id]
