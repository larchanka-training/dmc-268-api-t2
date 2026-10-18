"""GitHub snapshot capture through SHA-pinned REST reads."""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Any

import httpx

from app.application.github_ports import (
    GithubFailure,
    GithubRemoteComment,
    GithubRemoteReview,
    GithubRequest,
    GithubReviewReference,
    GithubSnapshot,
    GithubWriteFailure,
)

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
_MAX_READ_PAGES = 20
_MAX_PAGE = 2_147_483_647
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


def _listed_text(value: object, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not value and not allow_empty):
        raise GithubFailure("DIFF_UNTRUSTWORTHY", retryable=False)
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise GithubFailure("DIFF_UNTRUSTWORTHY", retryable=False) from None
    return value


def _listed_ref(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        raise GithubFailure("DIFF_UNTRUSTWORTHY", retryable=False)
    repository = value.get("repo")
    if (
        not isinstance(repository, dict)
        or type(repository.get("id")) is not int
        or repository["id"] < 1
    ):
        raise GithubFailure("DIFF_UNTRUSTWORTHY", retryable=False)
    sha = _listed_text(value.get("sha"))
    if not _SHA.fullmatch(sha):
        raise GithubFailure("DIFF_UNTRUSTWORTHY", retryable=False)
    return {
        "repository_external_id": str(repository["id"]),
        "ref": _listed_text(value.get("ref")),
        "sha": sha,
    }


def _listed_pull(value: object, external_id: str) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        raise GithubFailure("DIFF_UNTRUSTWORTHY", retryable=False)
    if value.get("state") == "closed":
        return None
    author = value.get("user")
    if (
        value.get("state") != "open"
        or type(value.get("number")) is not int
        or value["number"] < 1
        or not isinstance(author, dict)
    ):
        raise GithubFailure("DIFF_UNTRUSTWORTHY", retryable=False)
    base = _listed_ref(value.get("base"))
    if base["repository_external_id"] != external_id:
        raise GithubFailure("DIFF_UNTRUSTWORTHY", retryable=False)
    return {
        "number": value["number"],
        "title": _listed_text(value.get("title"), allow_empty=True),
        "author": {"login": _listed_text(author.get("login"))},
        "state": "OPEN",
        "source": _listed_ref(value.get("head")),
        "base": base,
    }


def _next_pull_page(  # noqa: PLR0913 - one provider page context
    response: httpx.Response,
    external_id: str,
    owner: str,
    name: str,
    *,
    limit: int,
    page: int,
) -> int | None:
    try:
        if not response.headers.get("Link", "").isascii():
            raise ValueError("Invalid continuation header")
        link = response.links.get("next")
        if link is None:
            return None
        url = httpx.URL(link["url"])
        params = url.params
        raw_page = params.get("page", "")
        if (
            url.scheme != "https"
            or url.host != "api.github.com"
            or url.port not in {None, 443}
            or url.userinfo
            or url.fragment
            or url.path
            not in {
                f"/repos/{owner}/{name}/pulls",
                f"/repositories/{external_id}/pulls",
            }
            or set(params) != {"state", "per_page", "page"}
            or len(params.multi_items()) != len({"state", "per_page", "page"})
            or params["state"] != "open"
            or params["per_page"] != str(limit)
            or not raw_page.isascii()
            or not raw_page.isdecimal()
            or len(raw_page) > len(str(_MAX_PAGE))
            or not page < int(raw_page) <= _MAX_PAGE
        ):
            raise ValueError("Invalid continuation")
        return int(raw_page)
    except ValueError, KeyError, httpx.InvalidURL:
        raise GithubFailure("DIFF_UNTRUSTWORTHY", retryable=False) from None


def _next_repository_page(
    response: httpx.Response, *, limit: int, page: int
) -> int | None:
    try:
        if not response.headers.get("Link", "").isascii():
            raise ValueError("Invalid continuation header")
        link = response.links.get("next")
        if link is None:
            return None
        url = httpx.URL(link["url"])
        params = url.params
        raw_page = params.get("page", "")
        if (
            url.scheme != "https"
            or url.host != "api.github.com"
            or url.port not in {None, 443}
            or url.userinfo
            or url.fragment
            or url.path != "/user/repos"
            or set(params) != {"per_page", "page"}
            or len(params.multi_items()) != 2  # noqa: PLR2004
            or params["per_page"] != str(limit)
            or not raw_page.isascii()
            or not raw_page.isdecimal()
            or len(raw_page) > len(str(_MAX_PAGE))
            or not page < int(raw_page) <= _MAX_PAGE
        ):
            raise ValueError("Invalid continuation")
        return int(raw_page)
    except ValueError, KeyError, httpx.InvalidURL:
        raise GithubFailure("DIFF_UNTRUSTWORTHY", retryable=False) from None


def _available_repository(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        raise GithubFailure("DIFF_UNTRUSTWORTHY", retryable=False)
    owner = value.get("owner")
    name = value.get("name")
    permissions = value.get("permissions")
    if (
        type(value.get("id")) is not int
        or value["id"] < 1
        or not isinstance(owner, dict)
        or not isinstance(owner.get("login"), str)
        or not _OWNER.fullmatch(owner["login"])
        or not isinstance(name, str)
        or not _REPOSITORY.fullmatch(name)
        or name in {".", ".."}
        or not isinstance(permissions, dict)
        or type(permissions.get("pull")) is not bool
    ):
        raise GithubFailure("DIFF_UNTRUSTWORTHY", retryable=False)
    if not permissions["pull"]:
        return None
    return {
        "provider": "github",
        "external_id": str(value["id"]),
        "owner": owner["login"],
        "name": name,
    }


def _validate_request(request: GithubRequest) -> None:
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


def _has_next_page(response: httpx.Response, path: str, page: int) -> bool:
    if not response.headers.get("link"):
        return False
    links = response.links
    if not links:
        raise GithubFailure("DIFF_UNTRUSTWORTHY", retryable=False)
    for relation, link in links.items():
        try:
            url = httpx.URL(link["url"])
        except KeyError, httpx.InvalidURL:
            raise GithubFailure("DIFF_UNTRUSTWORTHY", retryable=False) from None
        next_page = url.params.get("page", "")
        if (
            relation not in {"next", "prev", "first", "last"}
            or url.scheme != "https"
            or url.host != "api.github.com"
            or url.port not in {None, 443}
            or url.userinfo
            or url.fragment
            or url.path != path
            or sorted(url.params.multi_items())
            != [("page", next_page), ("per_page", "100")]
            or not next_page.isascii()
            or not next_page.isdecimal()
            or len(next_page) > _MAX_RETRY_AFTER_DIGITS
            or int(next_page) < 1
            or (relation == "next" and int(next_page) != page + 1)
        ):
            raise GithubFailure("DIFF_UNTRUSTWORTHY", retryable=False)
    return "next" in links


def _remote_review(item: dict[str, Any]) -> GithubRemoteReview:
    remote_id = item.get("id")
    body, commit_id, state = (
        item.get("body"),
        item.get("commit_id"),
        item.get("state"),
    )
    user = item.get("user")
    author_id = user.get("id") if isinstance(user, dict) else None
    if (
        type(remote_id) is not int
        or remote_id <= 0
        or not isinstance(body, str)
        or not isinstance(commit_id, str)
        or not _SHA.fullmatch(commit_id)
        or type(author_id) is not int
        or author_id <= 0
        or not isinstance(state, str)
        or not state
    ):
        raise GithubFailure("DIFF_UNTRUSTWORTHY", retryable=False)
    return GithubRemoteReview(str(remote_id), body, commit_id, str(author_id), state)


def _remote_comment(item: dict[str, Any]) -> GithubRemoteComment:
    remote_id = item.get("id")
    body, path, commit_id = item.get("body"), item.get("path"), item.get("commit_id")
    user = item.get("user")
    author_id = user.get("id") if isinstance(user, dict) else None
    line, start_line = item.get("line"), item.get("start_line")
    side, start_side = item.get("side"), item.get("start_side")
    if (
        type(remote_id) is not int
        or remote_id <= 0
        or not isinstance(body, str)
        or not isinstance(path, str)
        or not path
        or "\\" in path
        or "\x00" in path
        or any(part in {"", ".", ".."} for part in path.split("/"))
        or not isinstance(commit_id, str)
        or not _SHA.fullmatch(commit_id)
        or type(author_id) is not int
        or author_id <= 0
        or any(
            value is not None and (type(value) is not int or value < 1)
            for value in (line, start_line)
        )
        or side not in ("LEFT", "RIGHT", None)
        or start_side not in ("LEFT", "RIGHT", None)
    ):
        raise GithubFailure("DIFF_UNTRUSTWORTHY", retryable=False)
    return GithubRemoteComment(
        remote_comment_id=str(remote_id),
        body=body,
        path=path,
        line=line,
        start_line=start_line,
        side=side,
        start_side=start_side,
        commit_id=commit_id,
        author_id=str(author_id),
    )


class GithubClient:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        timeout_seconds: float = _TOTAL_TIMEOUT_SECONDS,
        authorize: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._client = client
        self._timeout_seconds = timeout_seconds
        self._authorize = authorize

    async def list_available_repositories(
        self, *, limit: int, page: int
    ) -> tuple[list[dict[str, Any]], int | None]:
        response = await self._get(
            f"https://api.github.com/user/repos?per_page={limit}&page={page}",
            "application/vnd.github+json",
        )
        try:
            payload = response.json()
        except ValueError, UnicodeError, RecursionError:
            raise GithubFailure("DIFF_UNTRUSTWORTHY", retryable=False) from None
        if not isinstance(payload, list) or len(payload) > limit:
            raise GithubFailure("DIFF_UNTRUSTWORTHY", retryable=False)
        items = []
        for repository in payload:
            item = _available_repository(repository)
            if item is not None:
                items.append(item)
        return items, _next_repository_page(response, limit=limit, page=page)

    async def get_repository(self, external_id: str) -> dict[str, Any]:
        if not _EXTERNAL_ID.fullmatch(external_id):
            raise GithubFailure("VCS_ACCESS_DENIED", retryable=False)
        response = await self._get(
            f"https://api.github.com/repositories/{external_id}",
            "application/vnd.github+json",
        )
        try:
            payload = _json_object(response)
        except RecursionError:
            raise GithubFailure("DIFF_UNTRUSTWORTHY", retryable=False) from None
        item = _available_repository(payload)
        if item is None or item["external_id"] != external_id:
            raise GithubFailure("VCS_ACCESS_DENIED", retryable=False)
        if type(payload["permissions"].get("admin")) is not bool:
            raise GithubFailure("DIFF_UNTRUSTWORTHY", retryable=False)
        item["role"] = "admin" if payload["permissions"]["admin"] else "reviewer"
        return item

    async def get_identity(self, token: str) -> tuple[str, str]:
        """Resolve the authenticated user using only the exchanged user token."""
        response = await self._get(
            "https://api.github.com/user", "application/vnd.github+json", token=token
        )
        payload = _json_object(response)
        login = payload.get("login")
        if (
            type(payload.get("id")) is not int
            or payload["id"] <= 0
            or not isinstance(login, str)
            or not _OWNER.fullmatch(login)
        ):
            raise GithubFailure("VCS_ACCESS_DENIED", retryable=False)
        return str(payload["id"]), login

    async def list_pull_requests(
        self, external_id: str, owner: str, name: str, *, limit: int, page: int
    ) -> tuple[list[dict[str, Any]], int | None]:
        try:
            async with asyncio.timeout(_TOTAL_TIMEOUT_SECONDS):
                return await self._list_pull_requests(
                    external_id, owner, name, limit=limit, page=page
                )
        except TimeoutError:
            raise GithubFailure("VCS_UNAVAILABLE", retryable=True) from None

    async def _list_pull_requests(
        self, external_id: str, owner: str, name: str, *, limit: int, page: int
    ) -> tuple[list[dict[str, Any]], int | None]:
        if (
            not _OWNER.fullmatch(owner)
            or not _REPOSITORY.fullmatch(name)
            or name in {".", ".."}
            or not _EXTERNAL_ID.fullmatch(external_id)
        ):
            raise GithubFailure("DIFF_UNTRUSTWORTHY", retryable=False)
        response = await self._get(
            f"https://api.github.com/repos/{owner}/{name}/pulls"
            f"?state=open&per_page={limit}&page={page}",
            "application/vnd.github+json",
        )
        try:
            payload = response.json()
        except ValueError, UnicodeError, RecursionError:
            raise GithubFailure("DIFF_UNTRUSTWORTHY", retryable=False) from None
        if not isinstance(payload, list) or len(payload) > limit:
            raise GithubFailure("DIFF_UNTRUSTWORTHY", retryable=False)
        items = []
        for pull in payload:
            item = _listed_pull(pull, external_id)
            if item is not None:
                items.append(item)
        next_page = _next_pull_page(
            response, external_id, owner, name, limit=limit, page=page
        )
        return items, next_page

    async def has_read_access(self, external_id: str) -> bool:
        """The caller must supply an HTTP client with this user's credentials."""
        if not _EXTERNAL_ID.fullmatch(external_id):
            raise GithubFailure("VCS_ACCESS_DENIED", retryable=False)
        try:
            response = await self._get(
                f"https://api.github.com/repositories/{external_id}",
                "application/vnd.github+json",
            )
        except GithubFailure as error:
            if error.reason_code == "VCS_ACCESS_DENIED":
                return False
            raise
        payload = _json_object(response)
        permissions = payload.get("permissions")
        return (
            type(payload.get("id")) is int
            and str(payload["id"]) == external_id
            and isinstance(permissions, dict)
            and permissions.get("pull") is True
        )

    def with_authority(
        self,
        authorize: Callable[[], Awaitable[None]],
    ) -> GithubClient:
        """Scope the fence to one task without mutating the shared client."""
        return GithubClient(
            self._client,
            timeout_seconds=self._timeout_seconds,
            authorize=authorize,
        )

    async def check_pull(self, request: GithubRequest) -> None:
        _validate_request(request)
        url = (
            f"https://api.github.com/repos/{request.owner}/{request.name}"
            f"/pulls/{request.number}"
        )
        try:
            async with asyncio.timeout(self._timeout_seconds):
                metadata = _pull_metadata(
                    await self._get(url, "application/vnd.github+json"), request
                )
                if (
                    type(metadata.get("number")) is not int
                    or metadata["number"] != request.number
                ):
                    raise GithubFailure("DIFF_UNTRUSTWORTHY", retryable=False)
        except TimeoutError:
            raise GithubFailure("VCS_UNAVAILABLE", retryable=True) from None

    async def list_reviews(self, request: GithubRequest) -> list[GithubRemoteReview]:
        _validate_request(request)
        path = f"/repos/{request.owner}/{request.name}/pulls/{request.number}/reviews"
        items = await self._read_pages(path)
        return [_remote_review(item) for item in items]

    async def list_review_comments(
        self, request: GithubRequest, remote_review_id: str
    ) -> list[GithubRemoteComment]:
        _validate_request(request)
        if not _EXTERNAL_ID.fullmatch(remote_review_id):
            raise GithubFailure("DIFF_UNTRUSTWORTHY", retryable=False)
        path = (
            f"/repos/{request.owner}/{request.name}/pulls/{request.number}"
            f"/reviews/{remote_review_id}/comments"
        )
        items = await self._read_pages(path)
        return [_remote_comment(item) for item in items]

    async def _read_pages(self, path: str) -> list[dict[str, Any]]:
        try:
            async with asyncio.timeout(self._timeout_seconds):
                items: list[dict[str, Any]] = []
                for page in range(1, _MAX_READ_PAGES + 1):
                    response = await self._get(
                        f"https://api.github.com{path}?per_page=100&page={page}",
                        "application/vnd.github+json",
                    )
                    try:
                        payload = response.json()
                    except ValueError, UnicodeError:
                        raise GithubFailure(
                            "DIFF_UNTRUSTWORTHY", retryable=False
                        ) from None
                    if not isinstance(payload, list) or any(
                        not isinstance(item, dict) for item in payload
                    ):
                        raise GithubFailure("DIFF_UNTRUSTWORTHY", retryable=False)
                    items.extend(payload)
                    if not _has_next_page(response, path, page):
                        return items
        except TimeoutError:
            raise GithubFailure("VCS_UNAVAILABLE", retryable=True) from None
        raise GithubFailure("VCS_UNAVAILABLE", retryable=True)

    async def create_review(
        self,
        request: GithubRequest,
        *,
        body: str,
        comments: Sequence[dict[str, object]],
    ) -> GithubReviewReference:
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
        if self._authorize is not None:
            await self._authorize()
        try:
            async with (
                asyncio.timeout(self._timeout_seconds),
                self._client.stream(
                    "POST",
                    f"https://api.github.com/repos/{request.owner}/{request.name}"
                    f"/pulls/{request.number}/reviews",
                    headers={"Accept": "application/vnd.github+json"},
                    json={
                        "commit_id": request.head_sha,
                        "body": body,
                        "event": "COMMENT",
                        "comments": list(comments),
                    },
                    follow_redirects=False,
                ) as upstream,
            ):
                content = bytearray()
                limit = _MAX_RESPONSE_BYTES if upstream.is_success else _MAX_ERROR_BYTES
                async for chunk in upstream.aiter_bytes():
                    if len(content) + len(chunk) > limit:
                        raise GithubWriteFailure(
                            "VCS_UNAVAILABLE", remote_effect="unknown"
                        )
                    content.extend(chunk)
                response = httpx.Response(
                    upstream.status_code,
                    headers={
                        name: value
                        for name, value in upstream.headers.items()
                        if name
                        not in {
                            "content-encoding",
                            "content-length",
                            "transfer-encoding",
                        }
                    },
                    content=bytes(content),
                )
        except httpx.RequestError, TimeoutError:
            raise GithubWriteFailure(
                "VCS_UNAVAILABLE", remote_effect="unknown"
            ) from None
        if not response.is_success:
            if response.status_code in {401, 403, 404, 422, 429}:
                try:
                    _check_status(response, response.content)
                except GithubFailure as error:
                    raise GithubWriteFailure(
                        error.reason_code,
                        remote_effect="absent",
                        retryable=error.retryable,
                        retry_after=error.retry_after,
                    ) from None
            raise GithubWriteFailure("VCS_UNAVAILABLE", remote_effect="unknown")
        try:
            result = response.json()
        except ValueError, UnicodeError:
            raise GithubWriteFailure(
                "VCS_UNAVAILABLE", remote_effect="unknown"
            ) from None
        remote_id = result.get("id") if isinstance(result, dict) else None
        if type(remote_id) is not int or remote_id <= 0:
            raise GithubWriteFailure("VCS_UNAVAILABLE", remote_effect="unknown")
        return GithubReviewReference(remote_review_id=str(remote_id))

    async def fetch(self, request: GithubRequest) -> GithubSnapshot:
        try:
            async with asyncio.timeout(_TOTAL_TIMEOUT_SECONDS):
                return await self._fetch(request)
        except TimeoutError:
            raise GithubFailure("VCS_UNAVAILABLE", retryable=True) from None

    async def _get(
        self, url: str, accept: str, *, token: str | None = None
    ) -> httpx.Response:
        if self._authorize is not None:
            await self._authorize()
        headers = {"Accept": accept}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        try:
            async with self._client.stream(
                "GET", url, headers=headers, follow_redirects=False
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
        return httpx.Response(
            200,
            content=bytes(content),
            headers=[
                (key, value)
                for key, value in response.headers.raw
                if key.lower() == b"link"
            ],
        )

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
