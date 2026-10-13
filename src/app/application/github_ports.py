"""Application-facing contract for a GitHub snapshot capture."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol


@dataclass(frozen=True, slots=True)
class GithubRequest:
    owner: str
    name: str
    repository_external_id: str
    number: int
    head_sha: str
    base_sha: str


@dataclass(frozen=True, slots=True)
class GithubSnapshot:
    metadata: dict[str, Any]
    raw_diff: str


@dataclass(frozen=True, slots=True)
class GithubReviewReference:
    remote_review_id: str


@dataclass(frozen=True, slots=True)
class GithubRemoteReview:
    remote_review_id: str
    body: str
    commit_id: str
    author_id: str
    state: str


@dataclass(frozen=True, slots=True)
class GithubRemoteComment:
    remote_comment_id: str
    body: str
    path: str
    line: int | None
    start_line: int | None
    side: str | None
    start_side: str | None
    commit_id: str
    author_id: str


class GithubFailure(Exception):  # noqa: N818 - public gateway contract
    def __init__(
        self, reason_code: str, retryable: bool, retry_after: int | None = None
    ) -> None:
        self.reason_code = reason_code
        self.retryable = retryable
        self.retry_after = retry_after
        super().__init__(reason_code)


class GithubWriteFailure(GithubFailure):
    def __init__(
        self,
        reason_code: str,
        *,
        remote_effect: Literal["absent", "unknown"],
        retryable: bool = False,
        retry_after: int | None = None,
    ) -> None:
        self.remote_effect = remote_effect
        super().__init__(reason_code, retryable, retry_after)


class GithubPort(Protocol):
    async def fetch(self, request: GithubRequest) -> GithubSnapshot: ...
