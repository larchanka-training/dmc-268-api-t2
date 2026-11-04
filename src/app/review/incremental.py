"""Incremental review (dmc-268-ui-t2#33): omit fragments a prior review covered.

A hunk is split into fragments at definition boundaries (a heuristic, no
parser: a blank line followed by a definition or a top-level statement). A
fragment's identity is its path, first line and normalized changed lines, never
its line numbers, so it survives unrelated edits, rebases and force-pushes.
Kept fragments stay in the coordinates of the current PR diff.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.vcs.diff import ParsedDiff
from app.vcs.input_guards import FileSkipReason


class IncrementalPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # Prior COMPLETED/PARTIAL reviews of the PR whose fragments count as reviewed.
    max_prior_reviews: int = Field(default=20, ge=1)


_DEFINITION = re.compile(
    r"\s*(?:@|(?:export\s+)?(?:default\s+)?(?:async\s+)?"
    r"(?:def|class|function|func|fn|impl|interface|struct|enum|type|module"
    r"|namespace|public|private|protected|static|const|let|var)\b)"
)
_CLOSING = ("}", ")", "]")


def _text(line: Mapping[str, Any]) -> str:
    return str(line["text"]).rstrip()


def _starts_fragment(previous: Mapping[str, Any], line: Mapping[str, Any]) -> bool:
    text = _text(line)
    if _text(previous) or not text:
        return False
    top_level = not text[0].isspace() and not text.startswith(_CLOSING)
    return top_level or bool(_DEFINITION.match(text))


def _fragments(hunk: Mapping[str, Any]) -> list[list[dict[str, Any]]]:
    """Contiguous line groups; blank lines at the edges are dropped."""
    groups: list[list[dict[str, Any]]] = []
    for index, line in enumerate(hunk["lines"]):
        if not groups or (index and _starts_fragment(hunk["lines"][index - 1], line)):
            groups.append([])
        groups[-1].append(line)
    trimmed = []
    for group in groups:
        while group and not _text(group[-1]):
            group.pop()
        while group and not _text(group[0]):
            group.pop(0)
        if group:
            trimmed.append(group)
    return trimmed


def _changed(group: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [line for line in group if line["kind"] != "context"]


def _fingerprint(path: str, group: list[dict[str, Any]]) -> str:
    identity = [
        path,
        _text(group[0]),
        [(line["kind"], _text(line)) for line in _changed(group) if _text(line)],
    ]
    encoded = json.dumps(identity, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _file_fingerprint(diff: Mapping[str, Any]) -> str:
    """Metadata-only change (mode, empty file): identity is path and status."""
    return _fingerprint(_path(diff), [{"kind": diff["status"], "text": "<no hunks>"}])


def _path(diff: Mapping[str, Any]) -> str:
    return str(diff["new_path"] or diff["old_path"])


def _file_fragments(diff: Mapping[str, Any]) -> Iterable[str]:
    if not diff["hunks"]:
        yield _file_fingerprint(diff)
    for hunk in diff["hunks"]:
        for group in _fragments(hunk):
            if _changed(group):
                yield _fingerprint(_path(diff), group)


def reviewed_fragments(contexts: Iterable[Mapping[str, Any]]) -> frozenset[str]:
    """Fragment identities of stored ContextPayload bodies (prior reviews)."""
    return frozenset(
        fingerprint
        for context in contexts
        for file in context["files"]
        for fingerprint in _file_fragments(file["diff"])
    )


def _sub_hunk(
    hunk: Mapping[str, Any], group: list[dict[str, Any]], part: int
) -> dict[str, Any]:
    first = hunk["lines"].index(group[0])
    before = hunk["lines"][:first]
    sides: dict[str, int] = {}
    for side in ("old", "new"):
        numbers = [line[f"{side}_line"] for line in group if line[f"{side}_line"]]
        previous = [line[f"{side}_line"] for line in before if line[f"{side}_line"]]
        start, count = hunk[f"{side}_start"], hunk[f"{side}_count"]
        empty_start = previous[-1] if previous else (start - 1 if count else start)
        sides[f"{side}_start"] = numbers[0] if numbers else empty_start
        sides[f"{side}_count"] = len(numbers)
    return {
        **hunk,
        "hunk_id": f"{hunk['hunk_id']}_part_{part}",
        **sides,
        "lines": group,
    }


def _ranges(diff: Mapping[str, Any], lines: Iterable[Mapping[str, Any]]) -> list[Any]:
    side, kind = ("old", "deleted") if diff["status"] == "deleted" else ("new", "added")
    return [
        {"start_line": line[f"{side}_line"], "end_line": line[f"{side}_line"]}
        for line in lines
        if line["kind"] == kind
    ]


def omit_reviewed(parsed: ParsedDiff, reviewed: frozenset[str]) -> ParsedDiff:
    """Drop fragments in ``reviewed``; each omitted part is a coverage skip."""
    if not reviewed:
        return parsed
    files: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for file in parsed.files:
        diff = file["diff"]
        path = _path(diff)
        if not diff["hunks"]:
            if _file_fingerprint(diff) in reviewed:
                skipped.append(_skip(path, []))
            else:
                files.append(file)
            continue
        hunks: list[dict[str, Any]] = []
        omitted: list[dict[str, Any]] = []
        for hunk in diff["hunks"]:
            groups = _fragments(hunk)
            kept = [
                group
                for group in groups
                if _changed(group) and _fingerprint(path, group) not in reviewed
            ]
            if len(kept) == len(groups) == 1 and kept[0] == hunk["lines"]:
                hunks.append(hunk)
                continue
            hunks.extend(
                _sub_hunk(hunk, group, part) for part, group in enumerate(kept, 1)
            )
            omitted.extend(
                line for group in groups if group not in kept for line in group
            )
        if omitted:
            skipped.append(_skip(path, _ranges(diff, omitted)))
        if hunks:
            files.append({**file, "diff": {**diff, "hunks": hunks}})
    return ParsedDiff(
        files=files,
        skipped=[*parsed.skipped, *skipped],
        raw_file_count=parsed.raw_file_count,
    )


def _skip(path: str, ranges: list[Any]) -> dict[str, Any]:
    return {
        "path": path,
        "ranges": ranges,
        "reason": "policy",
        "detail": FileSkipReason.ALREADY_REVIEWED.value,
    }
