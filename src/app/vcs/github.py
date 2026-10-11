"""GitHub snapshot capture through SHA-pinned REST reads."""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Any

import httpx

from app.application.github_ports import GithubFailure, GithubRequest, GithubSnapshot

__all__ = ["GithubClient", "GithubFailure", "GithubRequest", "GithubSnapshot"]

_OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?\Z")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+\Z")
_EXTERNAL_ID = re.compile(r"[1-9][0-9]*\Z")
_SHA = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")
_MAX_RESPONSE_BYTES = 10 * 1024 * 1024
_MAX_ERROR_BYTES = 16 * 1024
_MAX_RETRY_AFTER_DIGITS = 9
_MAX_RESET_EPOCH_DIGITS = 10
_TOTAL_TIMEOUT_SECONDS = 15
_SECONDARY_RETRY_AFTER_SECONDS = 60


def _secondary_rate_limit(body: bytes) -> bool:
    try:
        payload = json.loads(body)
    except ValueError, UnicodeError:
        return False
    if not isinstance(payload, dict) or not isinstance(payload.get("message"), str):
        return False
    message = payload["message"].casefold()
    return "secondary rate limit" in message or "abuse detection" in message


def _check_status(response: httpx.Response, error_body: bytes = b"") -> None:
    status = response.status_code
    if response.is_success:
        return
    secondary = status == HTTPStatus.FORBIDDEN and _secondary_rate_limit(error_body)
    if (
        status == HTTPStatus.TOO_MANY_REQUESTS
        or secondary
        or (
            status == HTTPStatus.FORBIDDEN
            and (
                response.headers.get("x-ratelimit-remaining") == "0"
                or "retry-after" in response.headers
            )
        )
    ):
        retry = response.headers.get("retry-after", "")
        retry_after = (
            int(retry)
            if len(retry) <= _MAX_RETRY_AFTER_DIGITS
            and retry.isascii()
            and retry.isdecimal()
            else _SECONDARY_RETRY_AFTER_SECONDS
            if secondary
            else None
        )
        if response.headers.get("x-ratelimit-remaining") == "0":
            reset = response.headers.get("x-ratelimit-reset", "")
            if (
                len(reset) <= _MAX_RESET_EPOCH_DIGITS
                and reset.isascii()
                and reset.isdecimal()
            ):
                until_reset = max(1, math.ceil(int(reset) - time.time()))
                retry_after = max(retry_after or 0, until_reset)
        raise GithubFailure(
            "VCS_RATE_LIMITED",
            retryable=True,
            retry_after=retry_after,
        )
    if status in {
        HTTPStatus.UNAUTHORIZED,
        HTTPStatus.FORBIDDEN,
        HTTPStatus.NOT_FOUND,
    }:
        raise GithubFailure("VCS_ACCESS_DENIED", retryable=False)
    if response.is_server_error:
        raise GithubFailure("VCS_UNAVAILABLE", retryable=True)
    raise GithubFailure("DIFF_UNTRUSTWORTHY", retryable=False)


def _json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        value = response.json()
    except ValueError, UnicodeError:
        raise GithubFailure("DIFF_UNTRUSTWORTHY", retryable=False) from None
    if not isinstance(value, dict):
        raise GithubFailure("DIFF_UNTRUSTWORTHY", retryable=False)
    return value


def _pull_metadata(response: httpx.Response, request: GithubRequest) -> dict[str, Any]:
    pull = _json_object(response)
    head = pull.get("head")
    base = pull.get("base")
    if not isinstance(head, dict) or not isinstance(base, dict):
        raise GithubFailure("DIFF_UNTRUSTWORTHY", retryable=False)
    head_repo = head.get("repo")
    base_repo = base.get("repo")
    if (
        not isinstance(head_repo, dict)
        or type(head_repo.get("id")) is not int
        or head_repo["id"] <= 0
        or not isinstance(base_repo, dict)
        or type(base_repo.get("id")) is not int
        or base_repo["id"] <= 0
        or not isinstance(head.get("sha"), str)
        or not isinstance(base.get("sha"), str)
        or not isinstance(head.get("ref"), str)
        or not isinstance(base.get("ref"), str)
        or not isinstance(pull.get("title"), str)
        or (pull.get("body") is not None and not isinstance(pull.get("body"), str))
        or not isinstance(pull.get("state"), str)
    ):
        raise GithubFailure("DIFF_UNTRUSTWORTHY", retryable=False)
    if (
        pull["state"] != "open"
        or head["sha"] != request.head_sha
        or base["sha"] != request.base_sha
    ):
        raise GithubFailure("PR_STALE_OR_CLOSED", retryable=False)
    if str(base_repo["id"]) != request.repository_external_id:
        raise GithubFailure("VCS_ACCESS_DENIED", retryable=False)
    return pull


class GithubClient:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def fetch(self, request: GithubRequest) -> GithubSnapshot:
        try:
            async with asyncio.timeout(_TOTAL_TIMEOUT_SECONDS):
                return await self._fetch(request)
        except TimeoutError:
            raise GithubFailure("VCS_UNAVAILABLE", retryable=True) from None

    async def _get(self, url: str, accept: str) -> httpx.Response:
        try:
            async with self._client.stream(
                "GET", url, headers={"Accept": accept}, follow_redirects=False
            ) as response:
                ambiguous_forbidden = (
                    response.status_code == HTTPStatus.FORBIDDEN
                    and response.headers.get("x-ratelimit-remaining") != "0"
                    and "retry-after" not in response.headers
                )
                if not ambiguous_forbidden:
                    _check_status(response)
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    limit = (
                        _MAX_ERROR_BYTES if ambiguous_forbidden else _MAX_RESPONSE_BYTES
                    )
                    if len(content) + len(chunk) > limit:
                        reason = (
                            "VCS_ACCESS_DENIED"
                            if ambiguous_forbidden
                            else "DIFF_UNTRUSTWORTHY"
                        )
                        raise GithubFailure(reason, retryable=False)
                    content.extend(chunk)
                if ambiguous_forbidden:
                    _check_status(response, bytes(content))
        except httpx.RequestError:
            raise GithubFailure("VCS_UNAVAILABLE", retryable=True) from None
        return httpx.Response(200, content=bytes(content))

    async def _fetch(self, request: GithubRequest) -> GithubSnapshot:
        if (
            not _OWNER.fullmatch(request.owner)
            or not _REPOSITORY.fullmatch(request.name)
            or request.name in {".", ".."}
            or not _EXTERNAL_ID.fullmatch(request.repository_external_id)
            or type(request.number) is not int
            or request.number < 1
            or not _SHA.fullmatch(request.head_sha)
            or not _SHA.fullmatch(request.base_sha)
        ):
            raise GithubFailure("DIFF_UNTRUSTWORTHY", retryable=False)
        repository = f"https://api.github.com/repos/{request.owner}/{request.name}"
        pull_url = f"{repository}/pulls/{request.number}"
        first = await self._get(pull_url, "application/vnd.github+json")
        pull = _pull_metadata(first, request)

        compare = await self._get(
            f"{repository}/compare/{request.base_sha}...{request.head_sha}",
            "application/vnd.github+json",
        )
        merge_base = _json_object(compare).get("merge_base_commit")
        if not isinstance(merge_base, dict):
            raise GithubFailure("DIFF_UNTRUSTWORTHY", retryable=False)
        merge_base_sha = merge_base.get("sha")
        if not isinstance(merge_base_sha, str) or not _SHA.fullmatch(merge_base_sha):
            raise GithubFailure("DIFF_UNTRUSTWORTHY", retryable=False)

        diff = await self._get(
            f"{repository}/compare/{merge_base_sha}...{request.head_sha}",
            "application/vnd.github.diff",
        )
        try:
            raw_diff = diff.content.decode("utf-8")
        except UnicodeDecodeError:
            raise GithubFailure("DIFF_UNTRUSTWORTHY", retryable=False) from None
        if raw_diff and not raw_diff.startswith("diff --git "):
            raise GithubFailure("DIFF_UNTRUSTWORTHY", retryable=False)
        final = await self._get(pull_url, "application/vnd.github+json")
        latest = _pull_metadata(final, request)
        if latest["head"]["repo"]["id"] != pull["head"]["repo"]["id"]:
            raise GithubFailure("PR_STALE_OR_CLOSED", retryable=False)

        return GithubSnapshot(
            metadata={
                "snapshot": {
                    "provider": "github",
                    "base_repository_id": str(pull["base"]["repo"]["id"]),
                    "head_repository_id": str(pull["head"]["repo"]["id"]),
                    "base_sha": request.base_sha,
                    "merge_base_sha": merge_base_sha,
                    "head_sha": request.head_sha,
                    "captured_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                    "provider_metadata": {
                        "pull_number": request.number,
                        "diff_version": (
                            f"{request.base_sha}:{merge_base_sha}:{request.head_sha}"
                        ),
                    },
                },
                "metadata": {
                    "title": pull["title"],
                    "body": pull["body"],
                    "base_ref": pull["base"]["ref"],
                    "head_ref": pull["head"]["ref"],
                    "commit_messages": [],
                    "languages": [],
                    "discussions": [],
                },
            },
            raw_diff=raw_diff,
        )
