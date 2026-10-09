from __future__ import annotations

import asyncio
import time
from dataclasses import FrozenInstanceError, replace
from typing import Any

import httpx
import pytest

from app.application import github_ports
from app.vcs import github as github_module
from app.vcs.github import GithubClient, GithubFailure, GithubRequest

BASE = "a" * 40
HEAD = "b" * 40
MERGE_BASE = "c" * 40
_TWO_READS = 2
_MINUTE_SECONDS = 60
_HOUR_SECONDS = 3600


def test_github_gateway_contract_lives_in_application_layer() -> None:
    assert github_ports.GithubFailure is GithubFailure
    assert github_ports.GithubRequest is GithubRequest
    assert github_ports.GithubPort.fetch.__annotations__["return"] == "GithubSnapshot"
    snapshot = github_ports.GithubSnapshot(metadata={}, raw_diff="")
    with pytest.raises(FrozenInstanceError):
        snapshot.raw_diff = "changed"  # type: ignore[misc]


def _pull(*, head: str = HEAD, base: str = BASE) -> dict[str, Any]:
    return {
        "state": "open",
        "number": 7,
        "title": "Fix validation",
        "body": "Keep errors explicit.",
        "head": {"sha": head, "ref": "topic", "repo": {"id": 200}},
        "base": {"sha": base, "ref": "main", "repo": {"id": 100}},
    }


def _request() -> GithubRequest:
    return GithubRequest(
        owner="team",
        name="project",
        repository_external_id="100",
        number=7,
        head_sha=HEAD,
        base_sha=BASE,
    )


async def test_fetch_captures_pinned_merge_base_diff_and_rechecks_pull() -> None:
    seen: list[tuple[str, str, str]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(
            (request.url.host, request.url.path, request.headers.get("accept", ""))
        )
        if request.url.path.endswith("/pulls/7"):
            return httpx.Response(200, json=_pull())
        if request.url.path.endswith(f"/compare/{BASE}...{HEAD}"):
            return httpx.Response(200, json={"merge_base_commit": {"sha": MERGE_BASE}})
        if request.url.path.endswith(f"/compare/{MERGE_BASE}...{HEAD}"):
            return httpx.Response(200, text="diff --git a/a.py b/a.py\n+new line\n")
        raise AssertionError(f"unexpected request: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        snapshot = await GithubClient(http).fetch(_request())

    assert snapshot.raw_diff == "diff --git a/a.py b/a.py\n+new line\n"
    assert snapshot.metadata["snapshot"] == {
        "provider": "github",
        "base_repository_id": "100",
        "head_repository_id": "200",
        "base_sha": BASE,
        "merge_base_sha": MERGE_BASE,
        "head_sha": HEAD,
        "captured_at": snapshot.metadata["snapshot"]["captured_at"],
        "provider_metadata": {
            "pull_number": 7,
            "diff_version": f"{BASE}:{MERGE_BASE}:{HEAD}",
        },
    }
    assert snapshot.metadata["metadata"] == {
        "title": "Fix validation",
        "body": "Keep errors explicit.",
        "base_ref": "main",
        "head_ref": "topic",
        "commit_messages": [],
        "languages": [],
        "discussions": [],
    }
    assert seen == [
        (
            "api.github.com",
            "/repos/team/project/pulls/7",
            "application/vnd.github+json",
        ),
        (
            "api.github.com",
            f"/repos/team/project/compare/{BASE}...{HEAD}",
            "application/vnd.github+json",
        ),
        (
            "api.github.com",
            f"/repos/team/project/compare/{MERGE_BASE}...{HEAD}",
            "application/vnd.github.diff",
        ),
        (
            "api.github.com",
            "/repos/team/project/pulls/7",
            "application/vnd.github+json",
        ),
    ]


async def test_fetch_rejects_stale_head_before_reading_diff() -> None:
    seen: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json=_pull(head="d" * 40))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(GithubFailure) as error:
            await GithubClient(http).fetch(_request())

    assert error.value.reason_code == "PR_STALE_OR_CLOSED"
    assert error.value.retryable is False
    assert seen == ["/repos/team/project/pulls/7"]


async def test_fetch_rejects_pull_that_moves_during_pinned_reads() -> None:
    pull_reads = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal pull_reads
        if request.url.path.endswith("/pulls/7"):
            pull_reads += 1
            return httpx.Response(
                200, json=_pull(head=HEAD if pull_reads == 1 else "d" * 40)
            )
        if request.headers["accept"] == "application/vnd.github+json":
            return httpx.Response(200, json={"merge_base_commit": {"sha": MERGE_BASE}})
        return httpx.Response(200, text="diff --git a/a.py b/a.py\n+changed\n")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(GithubFailure) as error:
            await GithubClient(http).fetch(_request())

    assert error.value.reason_code == "PR_STALE_OR_CLOSED"
    assert pull_reads == _TWO_READS


async def test_fetch_rejects_unexpected_base_repository() -> None:
    seen: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        pull = _pull()
        pull["base"]["repo"]["id"] = 999
        return httpx.Response(200, json=pull)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(GithubFailure) as error:
            await GithubClient(http).fetch(_request())

    assert error.value.reason_code == "VCS_ACCESS_DENIED"
    assert seen == ["/repos/team/project/pulls/7"]


async def test_fetch_rejects_missing_fork_repository() -> None:
    def respond(_request: httpx.Request) -> httpx.Response:
        pull = _pull()
        pull["head"]["repo"] = None
        return httpx.Response(200, json=pull)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(GithubFailure) as error:
            await GithubClient(http).fetch(_request())

    assert error.value.reason_code == "DIFF_UNTRUSTWORTHY"
    assert error.value.retryable is False


@pytest.mark.parametrize("invalid_id", [0, -1])
async def test_fetch_rejects_nonpositive_fork_repository_id(invalid_id: int) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pulls/7"):
            pull = _pull()
            pull["head"]["repo"]["id"] = invalid_id
            return httpx.Response(200, json=pull)
        if request.headers["accept"] == "application/vnd.github+json":
            return httpx.Response(200, json={"merge_base_commit": {"sha": MERGE_BASE}})
        return httpx.Response(200, text="diff --git a/a.py b/a.py\n+changed\n")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(GithubFailure) as error:
            await GithubClient(http).fetch(_request())

    assert error.value.reason_code == "DIFF_UNTRUSTWORTHY"


@pytest.mark.parametrize(
    "bad_request",
    [
        {"owner": "../other"},
        {"name": "project/other"},
        {"head_sha": "not-a-sha"},
        {"base_sha": "a" * 39},
        {"number": 0},
        {"repository_external_id": "100/other"},
    ],
)
async def test_fetch_rejects_invalid_request_before_http(
    bad_request: dict[str, Any],
) -> None:
    seen: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=_pull())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(GithubFailure) as error:
            await GithubClient(http).fetch(replace(_request(), **bad_request))

    assert error.value.reason_code == "DIFF_UNTRUSTWORTHY"
    assert seen == []


async def test_fetch_rejects_invalid_merge_base_before_diff_request() -> None:
    seen: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/pulls/7"):
            return httpx.Response(200, json=_pull())
        return httpx.Response(200, json={"merge_base_commit": {"sha": "../other"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(GithubFailure) as error:
            await GithubClient(http).fetch(_request())

    assert error.value.reason_code == "DIFF_UNTRUSTWORTHY"
    assert len(seen) == _TWO_READS


@pytest.mark.parametrize(
    ("status", "headers", "reason", "retryable", "retry_after"),
    [
        (401, {}, "VCS_ACCESS_DENIED", False, None),
        (403, {}, "VCS_ACCESS_DENIED", False, None),
        (
            403,
            {"X-RateLimit-Remaining": "0", "Retry-After": "7"},
            "VCS_RATE_LIMITED",
            True,
            7,
        ),
        (404, {}, "VCS_ACCESS_DENIED", False, None),
        (429, {"Retry-After": "12"}, "VCS_RATE_LIMITED", True, 12),
        (503, {}, "VCS_UNAVAILABLE", True, None),
        (422, {}, "DIFF_UNTRUSTWORTHY", False, None),
    ],
)
async def test_fetch_maps_provider_status_without_exposing_response(
    status: int,
    headers: dict[str, str],
    reason: str,
    retryable: bool,
    retry_after: int | None,
) -> None:
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status, headers=headers, text="private-token-and-source-code"
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(GithubFailure) as error:
            await GithubClient(http).fetch(_request())

    assert (
        error.value.reason_code,
        error.value.retryable,
        error.value.retry_after,
    ) == (
        reason,
        retryable,
        retry_after,
    )
    assert "private-token" not in str(error.value)


async def test_fetch_maps_transport_timeout_without_leaking_details() -> None:
    def respond(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("private-token-and-source-code")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(GithubFailure) as error:
            await GithubClient(http).fetch(_request())

    assert error.value.reason_code == "VCS_UNAVAILABLE"
    assert error.value.retryable is True
    assert "private-token" not in str(error.value)


async def test_fetch_has_one_deadline_across_all_four_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(github_module, "_TOTAL_TIMEOUT_SECONDS", 0.03, raising=False)

    async def respond(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.02)
        if request.url.path.endswith("/pulls/7"):
            return httpx.Response(200, json=_pull())
        if request.headers["accept"] == "application/vnd.github+json":
            return httpx.Response(200, json={"merge_base_commit": {"sha": MERGE_BASE}})
        return httpx.Response(200, text="diff --git a/a.py b/a.py\n+changed\n")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(GithubFailure) as error:
            await GithubClient(http).fetch(_request())

    assert error.value.reason_code == "VCS_UNAVAILABLE"
    assert error.value.retryable is True


async def test_fetch_ignores_unbounded_retry_after_header() -> None:
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "9" * 5000})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(GithubFailure) as error:
            await GithubClient(http).fetch(_request())

    assert error.value.reason_code == "VCS_RATE_LIMITED"
    assert error.value.retry_after is None


async def test_fetch_maps_secondary_rate_limit_without_rate_headers() -> None:
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            headers={"X-RateLimit-Remaining": "4999"},
            json={"message": "You have exceeded a secondary rate limit. private-token"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(GithubFailure) as error:
            await GithubClient(http).fetch(_request())

    assert error.value.reason_code == "VCS_RATE_LIMITED"
    assert error.value.retryable is True
    assert error.value.retry_after == _MINUTE_SECONDS
    assert "private-token" not in str(error.value)


@pytest.mark.parametrize("status", [403, 429])
async def test_fetch_honors_primary_rate_reset_over_shorter_retry_after(
    status: int,
) -> None:
    reset = int(time.time()) + _HOUR_SECONDS

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            headers={
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(reset),
                "Retry-After": "7",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(GithubFailure) as error:
            await GithubClient(http).fetch(_request())

    assert error.value.reason_code == "VCS_RATE_LIMITED"
    assert error.value.retryable is True
    assert error.value.retry_after is not None
    assert _HOUR_SECONDS - 1 <= error.value.retry_after <= _HOUR_SECONDS


async def test_fetch_rejects_diff_over_ten_mebibytes() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pulls/7"):
            return httpx.Response(200, json=_pull())
        if request.headers["accept"] == "application/vnd.github+json":
            return httpx.Response(200, json={"merge_base_commit": {"sha": MERGE_BASE}})
        return httpx.Response(200, content=b"x" * (10 * 1024 * 1024 + 1))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(GithubFailure) as error:
            await GithubClient(http).fetch(_request())

    assert error.value.reason_code == "DIFF_UNTRUSTWORTHY"


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        b"[]",
        b'{"state":"open","head":{},"base":{}}',
    ],
)
async def test_fetch_rejects_malformed_pull_metadata(payload: bytes) -> None:
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(GithubFailure) as error:
            await GithubClient(http).fetch(_request())

    assert error.value.reason_code == "DIFF_UNTRUSTWORTHY"


async def test_fetch_rechecks_base_repository_identity_after_diff() -> None:
    pull_reads = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal pull_reads
        if request.url.path.endswith("/pulls/7"):
            pull_reads += 1
            pull = _pull()
            if pull_reads == _TWO_READS:
                pull["base"]["repo"]["id"] = 999
            return httpx.Response(200, json=pull)
        if request.headers["accept"] == "application/vnd.github+json":
            return httpx.Response(200, json={"merge_base_commit": {"sha": MERGE_BASE}})
        return httpx.Response(200, text="diff --git a/a.py b/a.py\n+changed\n")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(GithubFailure) as error:
            await GithubClient(http).fetch(_request())

    assert error.value.reason_code == "VCS_ACCESS_DENIED"
    assert pull_reads == _TWO_READS


async def test_fetch_rechecks_head_repository_identity_after_diff() -> None:
    pull_reads = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal pull_reads
        if request.url.path.endswith("/pulls/7"):
            pull_reads += 1
            pull = _pull()
            if pull_reads == _TWO_READS:
                pull["head"]["repo"]["id"] = 999
            return httpx.Response(200, json=pull)
        if request.headers["accept"] == "application/vnd.github+json":
            return httpx.Response(200, json={"merge_base_commit": {"sha": MERGE_BASE}})
        return httpx.Response(200, text="diff --git a/a.py b/a.py\n+changed\n")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(GithubFailure) as error:
            await GithubClient(http).fetch(_request())

    assert error.value.reason_code == "PR_STALE_OR_CLOSED"
    assert pull_reads == _TWO_READS


@pytest.mark.parametrize("payload", [b"not json", b"{}", b"[]"])
async def test_fetch_rejects_malformed_compare_metadata(payload: bytes) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pulls/7"):
            return httpx.Response(200, json=_pull())
        return httpx.Response(200, content=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(GithubFailure) as error:
            await GithubClient(http).fetch(_request())

    assert error.value.reason_code == "DIFF_UNTRUSTWORTHY"


@pytest.mark.parametrize(
    "payload", [b"<html>error</html>", b"diff --git a/a b/a\n\xff"]
)
async def test_fetch_rejects_malformed_raw_diff(payload: bytes) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pulls/7"):
            return httpx.Response(200, json=_pull())
        if request.headers["accept"] == "application/vnd.github+json":
            return httpx.Response(200, json={"merge_base_commit": {"sha": MERGE_BASE}})
        return httpx.Response(200, content=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(GithubFailure) as error:
            await GithubClient(http).fetch(_request())

    assert error.value.reason_code == "DIFF_UNTRUSTWORTHY"


async def test_fetch_accepts_empty_diff_for_identical_trees() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pulls/7"):
            return httpx.Response(200, json=_pull())
        if request.headers["accept"] == "application/vnd.github+json":
            return httpx.Response(200, json={"merge_base_commit": {"sha": MERGE_BASE}})
        return httpx.Response(200, content=b"")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        snapshot = await GithubClient(http).fetch(_request())

    assert snapshot.raw_diff == ""
