"""Application-facing contract for a GitHub snapshot capture."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


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


class GithubFailure(Exception):  # noqa: N818 - public gateway contract
    def __init__(
        self, reason_code: str, retryable: bool, retry_after: int | None = None
    ) -> None:
        self.reason_code = reason_code
        self.retryable = retryable
        self.retry_after = retry_after
        super().__init__(reason_code)


class GithubPort(Protocol):
    async def fetch(self, request: GithubRequest) -> GithubSnapshot: ...
