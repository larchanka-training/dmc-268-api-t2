"""Thin publication handler compatible with the injected review-worker boundary."""

from typing import Protocol
from uuid import UUID

from app.application.github_publication import GithubPublicationGateway, GithubPublisher
from app.application.publication_ports import PublicationAuthority, PublicationStore


class PublicationTaskView(Protocol):
    @property
    def schema_version(self) -> int: ...

    @property
    def review_id(self) -> str: ...

    @property
    def task_kind(self) -> str: ...


class PublicationLeaseView(Protocol):
    @property
    def review_job_id(self) -> UUID: ...

    @property
    def owner(self) -> str: ...

    @property
    def fencing_token(self) -> int: ...

    @property
    def task_kind(self) -> str: ...


class PublicationRecoveryRequiredError(RuntimeError):
    """A durable failed/ambiguous publication needs the queue owner's recovery."""

    def __init__(self, review_id: UUID, status: str) -> None:
        self.review_id = review_id
        self.status = status
        super().__init__("Publication requires recovery")


class PublicationTaskHandler[T]:
    def __init__(
        self,
        store: PublicationStore,
        github: GithubPublicationGateway,
        *,
        bot_user_id: str,
        completed_outcome: T,
    ) -> None:
        self._store = store
        self._github = github
        self._bot_user_id = bot_user_id
        self._completed_outcome = completed_outcome

    async def handle(self, task: PublicationTaskView, lease: PublicationLeaseView) -> T:
        try:
            review_id = UUID(task.review_id)
        except ValueError:
            raise ValueError("Invalid publication task") from None
        if (
            type(task.schema_version) is not int
            or task.schema_version != 1
            or task.task_kind != "publish"
            or lease.task_kind != "publish"
            or review_id != lease.review_job_id
        ):
            raise ValueError("Invalid publication task")
        outcome = await GithubPublisher(
            self._store,
            self._github,
            authority=PublicationAuthority(
                lease.review_job_id,
                lease.owner,
                lease.fencing_token,
                lease.task_kind,
            ),
            bot_user_id=self._bot_user_id,
        ).publish(review_job_id=review_id)
        status = outcome.get("status")
        if status not in ("PUBLISHED", "PARTIAL", "SKIPPED"):
            raise PublicationRecoveryRequiredError(
                review_id, "FAILED" if status == "FAILED" else "UNKNOWN"
            )
        return self._completed_outcome
