"""Issue #24: GitHub publication and reconciliation transport contracts."""

import asyncio
import gzip
import json
from dataclasses import replace
from http import HTTPStatus
from typing import Any

import httpx
import pytest

from app.application.github_ports import (
    GithubFailure,
    GithubRequest,
    GithubWriteFailure,
)
from app.vcs.github import GithubClient

HEAD_SHA = "b" * 40
BASE_SHA = "a" * 40
_FINAL_TEST_PAGE = 22
_SECOND_PAGE = 2


async def test_create_review_sends_overview_and_inline_to_frozen_commit() -> None:
    """AC-GH-01: submit one prepared COMMENT review and return its remote ID."""
    observed: list[tuple[str, str, object]] = []
    overview = (
        "Проверен src/validation.py.\n\n"
        "Риски: непроверенные данные передаются вызывающему коду."
    )
    comments: list[dict[str, object]] = [
        {
            "path": "src/validation.py",
            "line": 39,
            "side": "RIGHT",
            "body": "Проверьте данные перед возвратом вызывающему коду.",
        }
    ]

    def respond(request: httpx.Request) -> httpx.Response:
        observed.append((request.method, str(request.url), json.loads(request.content)))
        return httpx.Response(
            200,
            json={
                "id": 9001,
                "state": "COMMENTED",
                "commit_id": HEAD_SHA,
                "body": overview,
                "html_url": (
                    "https://github.com/team/project/pull/7#pullrequestreview-9001"
                ),
                "user": {"id": 9002, "login": "reviewer[bot]", "type": "Bot"},
            },
        )

    pull = GithubRequest(
        owner="team",
        name="project",
        repository_external_id="100",
        number=7,
        head_sha=HEAD_SHA,
        base_sha=BASE_SHA,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        result = await GithubClient(http).create_review(
            pull, body=overview, comments=comments
        )

    assert observed == [
        (
            "POST",
            "https://api.github.com/repos/team/project/pulls/7/reviews",
            {
                "commit_id": HEAD_SHA,
                "body": overview,
                "event": "COMMENT",
                "comments": [
                    {
                        "path": "src/validation.py",
                        "line": 39,
                        "side": "RIGHT",
                        "body": "Проверьте данные перед возвратом вызывающему коду.",
                    }
                ],
            },
        )
    ]
    assert result.remote_review_id == "9001"


@pytest.mark.parametrize(
    "invalid_target",
    [
        {"owner": "../other"},
        {"name": "project/other"},
        {"repository_external_id": "100/other"},
        {"number": 0},
        {"head_sha": "not-a-sha"},
        {"base_sha": "a" * 39},
    ],
)
async def test_create_review_rejects_invalid_target_before_http(
    invalid_target: dict[str, Any],
) -> None:
    """AC-GH-02: invalid frozen target never reaches GitHub."""
    observed: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        observed.append(str(request.url))
        return httpx.Response(200, json={"id": 9001})

    pull = GithubRequest("team", "project", "100", 7, HEAD_SHA, BASE_SHA)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(GithubFailure) as error:
            await GithubClient(http).create_review(
                replace(pull, **invalid_target), body="Overview", comments=[]
            )

    assert (error.value.reason_code, error.value.retryable) == (
        "DIFF_UNTRUSTWORTHY",
        False,
    )
    assert observed == []


async def test_check_pull_obeys_total_metadata_deadline() -> None:
    """AC-GH-10: a stalled preflight cannot outlive the VCS read deadline."""
    observed: list[str] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        observed.append(request.method)
        await asyncio.sleep(0.1)
        return httpx.Response(
            200,
            json={
                "state": "open",
                "number": 7,
                "title": "Review",
                "body": None,
                "head": {"sha": HEAD_SHA, "ref": "topic", "repo": {"id": 200}},
                "base": {"sha": BASE_SHA, "ref": "main", "repo": {"id": 100}},
            },
        )

    pull = GithubRequest("team", "project", "100", 7, HEAD_SHA, BASE_SHA)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(GithubFailure) as error:
            await GithubClient(http, timeout_seconds=0.01).check_pull(pull)

    assert (error.value.reason_code, error.value.retryable) == ("VCS_UNAVAILABLE", True)
    assert observed == ["GET"]


async def test_create_review_does_not_retry_ambiguous_transport_failure() -> None:
    """AC-GH-03: a lost POST response is unknown, never a blind retry."""
    observed: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        observed.append(request.method)
        raise httpx.ReadTimeout("provider response contains secret", request=request)

    pull = GithubRequest("team", "project", "100", 7, HEAD_SHA, BASE_SHA)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(GithubWriteFailure) as error:
            await GithubClient(http).create_review(pull, body="Overview", comments=[])

    assert error.value.reason_code == "VCS_UNAVAILABLE"
    assert error.value.retryable is False
    assert error.value.remote_effect == "unknown"
    assert "secret" not in str(error.value)
    assert observed == ["POST"]


async def test_create_review_obeys_total_request_deadline() -> None:
    """AC-GH-07: deadline expiry has an unknown effect and no second POST."""
    observed: list[str] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        observed.append(request.method)
        await asyncio.sleep(0.1)
        return httpx.Response(200, json={"id": 9001})

    pull = GithubRequest("team", "project", "100", 7, HEAD_SHA, BASE_SHA)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(GithubWriteFailure) as error:
            await GithubClient(http, timeout_seconds=0.01).create_review(
                pull, body="Overview", comments=[]
            )

    assert error.value.remote_effect == "unknown"
    assert observed == ["POST"]


@pytest.mark.parametrize(
    ("status", "headers", "reason", "effect", "retryable"),
    [
        (401, {}, "VCS_ACCESS_DENIED", "absent", False),
        (403, {}, "VCS_ACCESS_DENIED", "absent", False),
        (404, {}, "VCS_ACCESS_DENIED", "absent", False),
        (422, {}, "DIFF_UNTRUSTWORTHY", "absent", False),
        (429, {"Retry-After": "12"}, "VCS_RATE_LIMITED", "absent", True),
        (503, {}, "VCS_UNAVAILABLE", "unknown", False),
        (
            302,
            {"Location": "https://other.invalid"},
            "VCS_UNAVAILABLE",
            "unknown",
            False,
        ),
    ],
)
async def test_create_review_classifies_remote_effect_without_provider_body(
    status: int,
    headers: dict[str, str],
    reason: str,
    effect: str,
    retryable: bool,
) -> None:
    """AC-GH-04: classify a rejected/ambiguous POST without leaking its body."""
    observed: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        observed.append(request.url.host)
        return httpx.Response(status, headers=headers, json={"message": "secret"})

    pull = GithubRequest("team", "project", "100", 7, HEAD_SHA, BASE_SHA)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(GithubWriteFailure) as error:
            await GithubClient(http).create_review(pull, body="Overview", comments=[])

    assert (
        error.value.reason_code,
        error.value.remote_effect,
        error.value.retryable,
    ) == (
        reason,
        effect,
        retryable,
    )
    assert error.value.retry_after == (
        12 if status == HTTPStatus.TOO_MANY_REQUESTS else None
    )
    assert str(error.value) == reason
    assert observed == ["api.github.com"]


@pytest.mark.parametrize(
    "payload", ["not-json", "[]", "{}", '{"id":true}', '{"id":-1}']
)
async def test_create_review_treats_unusable_success_as_unknown(payload: str) -> None:
    """AC-GH-05: unusable success response cannot prove remote absence."""

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=payload)

    pull = GithubRequest("team", "project", "100", 7, HEAD_SHA, BASE_SHA)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(GithubWriteFailure) as error:
            await GithubClient(http).create_review(pull, body="Overview", comments=[])

    assert (
        error.value.reason_code,
        error.value.remote_effect,
        error.value.retryable,
    ) == (
        "VCS_UNAVAILABLE",
        "unknown",
        False,
    )


async def test_create_review_bounds_provider_response() -> None:
    """AC-GH-06: an oversized POST response leaves the effect unknown."""

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'{"id":9001}' + b" " * (10 * 1024 * 1024))

    pull = GithubRequest("team", "project", "100", 7, HEAD_SHA, BASE_SHA)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(GithubWriteFailure) as error:
            await GithubClient(http).create_review(pull, body="Overview", comments=[])

    assert error.value.remote_effect == "unknown"
    assert error.value.retryable is False


async def test_check_pull_rejects_changed_snapshot_with_only_metadata_read() -> None:
    """AC-GH-08: preflight rejects a changed head before any publication."""
    observed: list[tuple[str, str]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        observed.append((request.method, request.url.path))
        return httpx.Response(
            200,
            json={
                "state": "open",
                "number": 7,
                "title": "Review",
                "body": None,
                "head": {"sha": "d" * 40, "ref": "topic", "repo": {"id": 200}},
                "base": {"sha": BASE_SHA, "ref": "main", "repo": {"id": 100}},
            },
        )

    pull = GithubRequest("team", "project", "100", 7, HEAD_SHA, BASE_SHA)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(GithubFailure, match="PR_STALE_OR_CLOSED"):
            await GithubClient(http).check_pull(pull)

    assert observed == [("GET", "/repos/team/project/pulls/7")]


@pytest.mark.parametrize(
    "invalid_target",
    [
        {"owner": "../other"},
        {"name": "project/other"},
        {"repository_external_id": "100/other"},
        {"number": 0},
        {"head_sha": "not-a-sha"},
        {"base_sha": "a" * 39},
    ],
)
async def test_check_pull_rejects_invalid_target_before_http(
    invalid_target: dict[str, Any],
) -> None:
    """AC-GH-09: preflight rejects malformed target without provider I/O."""
    observed: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        observed.append(str(request.url))
        return httpx.Response(200, json={})

    pull = GithubRequest("team", "project", "100", 7, HEAD_SHA, BASE_SHA)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(GithubFailure, match="DIFF_UNTRUSTWORTHY"):
            await GithubClient(http).check_pull(replace(pull, **invalid_target))

    assert observed == []


async def test_list_reviews_reads_all_pages_as_typed_references() -> None:
    """AC-GH-11: reconciliation sees every review, including the next page."""
    observed: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        observed.append(str(request.url))
        second = request.url.params["page"] == "2"
        return httpx.Response(
            200,
            headers={}
            if second
            else {
                "Link": "<https://api.github.com/repos/team/project/pulls/7/reviews"
                '?per_page=100&page=2>; rel="next"'
            },
            json=[
                {
                    "id": 9003 if second else 9001,
                    "body": "Second overview" if second else "First overview",
                    "commit_id": HEAD_SHA,
                    "user": {"id": 9002},
                    "state": "COMMENTED",
                }
            ],
        )

    pull = GithubRequest("team", "project", "100", 7, HEAD_SHA, BASE_SHA)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        reviews = await GithubClient(http).list_reviews(pull)

    assert [
        (r.remote_review_id, r.body, r.commit_id, r.author_id, r.state) for r in reviews
    ] == [
        ("9001", "First overview", HEAD_SHA, "9002", "COMMENTED"),
        ("9003", "Second overview", HEAD_SHA, "9002", "COMMENTED"),
    ]
    assert observed == [
        "https://api.github.com/repos/team/project/pulls/7/reviews?per_page=100&page=1",
        "https://api.github.com/repos/team/project/pulls/7/reviews?per_page=100&page=2",
    ]


async def test_list_reviews_rejects_incomplete_history_at_page_limit() -> None:
    """AC-GH-12: a truncated scan never masquerades as complete remote history."""
    observed: list[int] = []

    def respond(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        observed.append(page)
        headers = (
            {
                "Link": "<https://api.github.com/repos/team/project/pulls/7/reviews"
                f'?per_page=100&page={page + 1}>; rel="next"'
            }
            if page < _FINAL_TEST_PAGE
            else {}
        )
        return httpx.Response(200, headers=headers, json=[])

    pull = GithubRequest("team", "project", "100", 7, HEAD_SHA, BASE_SHA)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(GithubFailure, match="VCS_UNAVAILABLE"):
            await GithubClient(http).list_reviews(pull)

    assert observed == list(range(1, 21))


async def test_list_reviews_shares_deadline_across_pages() -> None:
    """AC-GH-13: pagination cannot reset the total VCS read deadline."""
    observed: list[int] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        observed.append(page)
        await asyncio.sleep(0.02)
        return httpx.Response(
            200,
            headers={}
            if page == _SECOND_PAGE
            else {
                "Link": "<https://api.github.com/repos/team/project/pulls/7/reviews"
                '?per_page=100&page=2>; rel="next"'
            },
            json=[],
        )

    pull = GithubRequest("team", "project", "100", 7, HEAD_SHA, BASE_SHA)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(GithubFailure) as error:
            await GithubClient(http, timeout_seconds=0.03).list_reviews(pull)

    assert (error.value.reason_code, error.value.retryable) == ("VCS_UNAVAILABLE", True)
    assert observed == [1, 2]


@pytest.mark.parametrize(
    "link",
    [
        '<https://private-token@other.invalid/reviews?page=2>; rel="next"',
        "<https://api.github.com/repos/team/project/pulls/8/reviews"
        '?per_page=100&page=2>; rel="next"',
        "<https://api.github.com/repos/team/project/pulls/7/reviews"
        '?per_page=100&page=1>; rel="next"',
        "unparseable-private-token",
    ],
)
async def test_list_reviews_rejects_untrustworthy_next_page(link: str) -> None:
    """AC-GH-14: an invalid pagination token cannot prove complete history."""
    observed: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        observed.append(str(request.url))
        return httpx.Response(
            200, headers={"Link": link} if len(observed) == 1 else {}, json=[]
        )

    pull = GithubRequest("team", "project", "100", 7, HEAD_SHA, BASE_SHA)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(GithubFailure) as error:
            await GithubClient(http).list_reviews(pull)

    assert error.value.reason_code == "DIFF_UNTRUSTWORTHY"
    assert "private-token" not in str(error.value)
    assert observed == [
        "https://api.github.com/repos/team/project/pulls/7/reviews?per_page=100&page=1"
    ]


def _review_data(**overrides: object) -> dict[str, object]:
    return {
        "id": 9001,
        "body": "Overview",
        "commit_id": HEAD_SHA,
        "user": {"id": 9002},
        "state": "COMMENTED",
        **overrides,
    }


@pytest.mark.parametrize(
    "payload",
    [
        "not-json-private-token",
        {"message": "private-token"},
        [{}],
        [_review_data(id=True)],
        [_review_data(id=0)],
        [_review_data(body=None)],
        [_review_data(commit_id="private-token")],
        [_review_data(user=None)],
        [_review_data(user={"id": -1})],
        [_review_data(state=1)],
    ],
)
async def test_list_reviews_rejects_unusable_identity_with_safe_error(
    payload: object,
) -> None:
    """AC-GH-15: malformed history never yields trustworthy reconciliation IDs."""

    def respond(_request: httpx.Request) -> httpx.Response:
        if isinstance(payload, str):
            return httpx.Response(200, text=payload)
        return httpx.Response(200, json=payload)

    pull = GithubRequest("team", "project", "100", 7, HEAD_SHA, BASE_SHA)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(GithubFailure) as error:
            await GithubClient(http).list_reviews(pull)

    assert (error.value.reason_code, error.value.retryable) == (
        "DIFF_UNTRUSTWORTHY",
        False,
    )
    assert "private-token" not in str(error.value)


async def test_list_review_comments_reads_all_pages_with_typed_locations() -> None:
    """AC-GH-16: reconciliation sees typed comment identity and exact ranges."""
    observed: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        observed.append(str(request.url))
        second = request.url.params["page"] == "2"
        return httpx.Response(
            200,
            headers={}
            if second
            else {
                "Link": "<https://api.github.com/repos/team/project/pulls/7/reviews"
                '/9001/comments?per_page=100&page=2>; rel="next"'
            },
            json=[
                {
                    "id": 9102 if second else 9101,
                    "body": "Outdated comment" if second else "Range comment",
                    "path": "src/validation.py",
                    "line": None if second else 41,
                    "start_line": None if second else 39,
                    "side": "RIGHT",
                    "start_side": None if second else "RIGHT",
                    "commit_id": HEAD_SHA,
                    "user": {"id": 9002},
                }
            ],
        )

    pull = GithubRequest("team", "project", "100", 7, HEAD_SHA, BASE_SHA)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        comments = await GithubClient(http).list_review_comments(pull, "9001")

    assert [
        (
            c.remote_comment_id,
            c.body,
            c.path,
            c.line,
            c.start_line,
            c.side,
            c.start_side,
            c.commit_id,
            c.author_id,
        )
        for c in comments
    ] == [
        (
            "9101",
            "Range comment",
            "src/validation.py",
            41,
            39,
            "RIGHT",
            "RIGHT",
            HEAD_SHA,
            "9002",
        ),
        (
            "9102",
            "Outdated comment",
            "src/validation.py",
            None,
            None,
            "RIGHT",
            None,
            HEAD_SHA,
            "9002",
        ),
    ]
    assert observed == [
        "https://api.github.com/repos/team/project/pulls/7/reviews/9001/comments"
        "?per_page=100&page=1",
        "https://api.github.com/repos/team/project/pulls/7/reviews/9001/comments"
        "?per_page=100&page=2",
    ]


@pytest.mark.parametrize("remote_id", ["0", "-1", "true", "9001/../9002", "01"])
async def test_list_review_comments_rejects_invalid_review_id_before_http(
    remote_id: str,
) -> None:
    """AC-GH-17: a remote review reference cannot change the request target."""
    observed: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        observed.append(str(request.url))
        return httpx.Response(200, json=[])

    pull = GithubRequest("team", "project", "100", 7, HEAD_SHA, BASE_SHA)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(GithubFailure, match="DIFF_UNTRUSTWORTHY"):
            await GithubClient(http).list_review_comments(pull, remote_id)

    assert observed == []


async def test_list_review_comments_preserves_legacy_without_line_coordinates() -> None:
    """AC-GH-18: legacy position-only comments keep identity with unknown lines."""

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "id": 9101,
                    "body": "Legacy comment",
                    "path": "src/validation.py",
                    "position": 1,
                    "original_position": 4,
                    "commit_id": HEAD_SHA,
                    "user": {"id": 9002},
                }
            ],
        )

    pull = GithubRequest("team", "project", "100", 7, HEAD_SHA, BASE_SHA)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        comments = await GithubClient(http).list_review_comments(pull, "9001")

    assert len(comments) == 1
    assert comments[0].remote_comment_id == "9101"
    assert (
        comments[0].line,
        comments[0].start_line,
        comments[0].side,
        comments[0].start_side,
    ) == (None, None, None, None)


def _comment_data(**overrides: object) -> dict[str, object]:
    return {
        "id": 9101,
        "body": "Comment",
        "path": "src/validation.py",
        "line": 39,
        "start_line": None,
        "side": "RIGHT",
        "start_side": None,
        "commit_id": HEAD_SHA,
        "user": {"id": 9002},
        **overrides,
    }


@pytest.mark.parametrize(
    "item",
    [
        {},
        _comment_data(id=True),
        _comment_data(id=-1),
        _comment_data(body=None),
        _comment_data(path="../private-token"),
        _comment_data(line=True),
        _comment_data(line=0),
        _comment_data(start_line=-1),
        _comment_data(side="private-token"),
        _comment_data(start_side=1),
        _comment_data(commit_id="private-token"),
        _comment_data(user=None),
        _comment_data(user={"id": True}),
    ],
)
async def test_list_review_comments_rejects_unusable_identity_with_safe_error(
    item: dict[str, object],
) -> None:
    """AC-GH-19: invalid comment identity or coordinates fail closed."""

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[item])

    pull = GithubRequest("team", "project", "100", 7, HEAD_SHA, BASE_SHA)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(GithubFailure) as error:
            await GithubClient(http).list_review_comments(pull, "9001")

    assert (error.value.reason_code, error.value.retryable) == (
        "DIFF_UNTRUSTWORTHY",
        False,
    )
    assert "private-token" not in str(error.value)


@pytest.mark.parametrize("remote_number", [8, True, None])
async def test_check_pull_rejects_metadata_for_another_pull(
    remote_number: object,
) -> None:
    """AC-GH-20: metadata must identify the requested PR, not only its SHA."""

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "state": "open",
                "number": remote_number,
                "title": "Review",
                "body": None,
                "head": {"sha": HEAD_SHA, "ref": "topic", "repo": {"id": 200}},
                "base": {"sha": BASE_SHA, "ref": "main", "repo": {"id": 100}},
            },
        )

    pull = GithubRequest("team", "project", "100", 7, HEAD_SHA, BASE_SHA)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(GithubFailure, match="DIFF_UNTRUSTWORTHY"):
            await GithubClient(http).check_pull(pull)


async def test_list_reviews_decodes_gzip_pages_once() -> None:
    """AC-GH-21: ordinary compressed GitHub responses retain usable pagination."""
    observed: list[int] = []

    def respond(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        observed.append(page)
        headers = {"Content-Encoding": "gzip"}
        if page == 1:
            headers["Link"] = (
                "<https://api.github.com/repos/team/project/pulls/7/reviews"
                '?per_page=100&page=2>; rel="next"'
            )
        return httpx.Response(
            200,
            headers=headers,
            content=gzip.compress(json.dumps([_review_data(id=9000 + page)]).encode()),
        )

    pull = GithubRequest("team", "project", "100", 7, HEAD_SHA, BASE_SHA)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        reviews = await GithubClient(http).list_reviews(pull)

    assert [review.remote_review_id for review in reviews] == ["9001", "9002"]
    assert observed == [1, 2]


async def test_create_review_decodes_gzip_success_once() -> None:
    """AC-GH-22: a compressed confirmed POST success preserves its review ID."""

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Encoding": "gzip"},
            content=gzip.compress(b'{"id":9001}'),
        )

    pull = GithubRequest("team", "project", "100", 7, HEAD_SHA, BASE_SHA)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        result = await GithubClient(http).create_review(
            pull, body="Overview", comments=[]
        )

    assert result.remote_review_id == "9001"
