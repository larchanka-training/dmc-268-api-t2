"""Spike 2: Anchor -> GitHub / GitLab payloads, placement fallbacks, errors (ADR-8/9).

Field sources (checked 2026-10-29):
* GitHub create review      https://docs.github.com/en/rest/pulls/reviews#create-a-review-for-a-pull-request
* GitHub create comment     https://docs.github.com/en/rest/pulls/comments#create-a-review-comment-for-a-pull-request
* GitLab MR diff threads    https://docs.gitlab.com/ee/api/discussions.html#create-a-new-thread-in-the-merge-request-diff
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

from review_spikes.difflabels import (
    Anchor,
    DiffLine,
    FileTable,
    Kind,
    Unresolvable,
    resolve,
)

Provider = Literal["github", "gitlab"]

# Not confirmed by the docs fetched for this spike; do not rely on them in the spec
# without a live check (the spec carries the same "TODO: verify" markers).
UNVERIFIED = {
    "github": (
        "comments[].subject_type is documented for POST /pulls/{n}/comments only, "
        "not for comments[] of POST /pulls/{n}/reviews",
        "GitHub: a range whose start_side differs from side",
        "GitHub: 'position' counting of '\\ No newline' markers",
    ),
    "gitlab": (
        "position.line_range.*.line_code: docs say <SHA>_<old>_<new>; which SHA "
        "and the old/new value for added/removed lines is not specified",
        "position for position_type=file: required fields",
        "old_path for an added file / new_path for a deleted file",
        "top-level old_line/new_line when line_range is present",
    ),
}


@dataclass(frozen=True)
class DiffRefs:
    """GitLab MR ``diff_refs``: base = merge-base, start = target tip, head."""

    base_sha: str
    start_sha: str
    head_sha: str


@dataclass(frozen=True)
class PublishContext:
    provider: Provider
    head_sha: str
    refs: DiffRefs | None = None
    # POST /reviews comments[] may not accept subject_type (see UNVERIFIED).
    github_file_level: bool = False


class Placement(StrEnum):
    INLINE = "inline"
    FILE = "file"
    SUMMARY = "summary"


@dataclass(frozen=True)
class Planned:
    placement: Placement
    payload: dict[str, Any] | None
    reason: str | None = None


# --- GitHub ------------------------------------------------------------------


def github_comment(anchor: Anchor, body: str) -> dict[str, Any]:
    side = "LEFT" if anchor.side == "OLD" else "RIGHT"
    comment: dict[str, Any] = {
        "path": anchor.file_path,
        "line": anchor.end_line,
        "side": side,
        "body": body,
    }
    if anchor.is_range:
        comment["start_line"] = anchor.start_line
        comment["start_side"] = side
    return comment


def github_review(
    commit_id: str, body: str, comments: list[dict[str, Any]]
) -> dict[str, Any]:
    # ``event`` is fixed in code: model output can never choose APPROVE.
    return {
        "commit_id": commit_id,
        "body": body,
        "event": "COMMENT",
        "comments": comments,
    }


def github_legacy_position(anchor: Anchor) -> int:
    """Diff-relative ``position`` (the superseded addressing, kept for comparison)."""
    return anchor.end.position


# --- GitLab ------------------------------------------------------------------


def gitlab_line_code(path: str, old: int | None, new: int | None) -> str:
    # TODO: verify against GitLab (see UNVERIFIED["gitlab"][0]).
    return f"{hashlib.sha1(path.encode()).hexdigest()}_{old or 0}_{new or 0}"  # noqa: S324


def _line_fields(line: DiffLine) -> dict[str, int]:
    if line.kind is Kind.ADDED:
        return {"new_line": _req(line.new_line)}
    if line.kind is Kind.DELETED:
        return {"old_line": _req(line.old_line)}
    return {"old_line": _req(line.old_line), "new_line": _req(line.new_line)}


def _req(value: int | None) -> int:
    if value is None:
        raise Unresolvable("line_not_on_side")
    return value


def _range_end(path: str, line: DiffLine) -> dict[str, Any]:
    return {
        "line_code": gitlab_line_code(path, line.old_line, line.new_line),
        "type": "new" if line.kind is Kind.ADDED else "old",
        **{k: v for k, v in _line_fields(line).items()},
    }


def gitlab_position(anchor: Anchor, refs: DiffRefs) -> dict[str, Any]:
    path = anchor.file_path
    position: dict[str, Any] = {
        "position_type": "text",
        "base_sha": refs.base_sha,
        "start_sha": refs.start_sha,
        "head_sha": refs.head_sha,
        "old_path": anchor.old_path or path,
        "new_path": anchor.new_path or path,
        **_line_fields(anchor.end),
    }
    if anchor.is_range:
        position["line_range"] = {
            "start": _range_end(path, anchor.start),
            "end": _range_end(path, anchor.end),
        }
    return position


def gitlab_file_position(table: FileTable, refs: DiffRefs) -> dict[str, Any]:
    return {
        "position_type": "file",
        "base_sha": refs.base_sha,
        "start_sha": refs.start_sha,
        "head_sha": refs.head_sha,
        "old_path": table.old_path or table.path,
        "new_path": table.new_path or table.path,
    }


# --- placement with fallbacks --------------------------------------------------


def plan_comment(
    ctx: PublishContext,
    tables: list[FileTable],
    path: str,
    start_label: str,
    end_label: str | None,
    body: str,
) -> Planned:
    """INLINE if the labels resolve; else FILE if supported; else SUMMARY."""
    try:
        anchor = resolve(tables, path, start_label, end_label)
    except Unresolvable as exc:
        return _fallback(ctx, tables, path, body, exc.reason)
    if ctx.provider == "github":
        return Planned(Placement.INLINE, github_comment(anchor, body))
    assert ctx.refs is not None
    return Planned(
        Placement.INLINE,
        {"body": body, "position": gitlab_position(anchor, ctx.refs)},
    )


def _fallback(
    ctx: PublishContext, tables: list[FileTable], path: str, body: str, reason: str
) -> Planned:
    table = next((t for t in tables if path in (t.new_path, t.old_path)), None)
    if table is None or reason == "unknown_file":
        return Planned(Placement.SUMMARY, None, reason)
    if ctx.provider == "gitlab" and ctx.refs is not None:
        return Planned(
            Placement.FILE,
            {"body": body, "position": gitlab_file_position(table, ctx.refs)},
            reason,
        )
    if ctx.provider == "github" and ctx.github_file_level:
        return Planned(
            Placement.FILE,
            {"path": table.path, "subject_type": "file", "body": body},
            reason,
        )
    return Planned(Placement.SUMMARY, None, reason)


# --- error handling ------------------------------------------------------------


class Action(StrEnum):
    SKIP_STALE_HEAD = "skip_stale_head"
    DEGRADE_TO_SUMMARY = "degrade_to_summary"
    RECONCILE_THEN_DECIDE = "reconcile_then_decide"
    RETRY_LATER = "retry_later"
    ABORT_AUTHORITY = "abort_authority"
    FAIL = "fail"


def classify_error(
    provider: Provider,
    status: int,
    *,
    head_moved: bool,
    retry_after: int | None = None,
) -> Action:
    """Proposal for the spec table; payload wording of errors is TODO: verify."""
    if head_moved:
        return Action.SKIP_STALE_HEAD
    if status == 429 or (status == 403 and retry_after is not None):
        return Action.RETRY_LATER
    if status in (401, 403):
        return Action.ABORT_AUTHORITY
    if (provider == "github" and status == 422) or (
        provider == "gitlab" and status == 400
    ):
        return Action.DEGRADE_TO_SUMMARY
    if status >= 500:
        # The POST may have taken effect: never blind-retry (marker reconcile).
        return Action.RECONCILE_THEN_DECIDE
    return Action.FAIL
