"""Two-layer response validity: JSON-schema shape, and diff-grounding.

A JSON-valid finding is not necessarily a true one, but a finding whose
location does not even exist on a changed line of the diff is definitely
hallucinated. This module checks both, independently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator
from pydantic import ValidationError

from app.review.schemas.context import FileDiff
from app.review.schemas.finding import LLMFindingV1
from app.review.validation import has_changed_anchor
from app.vcs.diff import parse_diff as parse_canonical_diff


@dataclass(slots=True)
class ChangedLines:
    new_lines: set[int] = field(default_factory=set)
    old_lines: set[int] = field(default_factory=set)
    diff: FileDiff | None = None


def validate_shape(response: Any, schema: dict[str, Any]) -> list[str]:
    """Return JSON-schema validation error messages (empty if valid)."""

    validator = Draft202012Validator(schema)
    return [f"schema:{error.validator}" for error in validator.iter_errors(response)]


def parse_diff(diff_text: str) -> dict[str, ChangedLines]:
    """Return, per file path, the set of changed OLD/NEW line numbers."""

    changed: dict[str, ChangedLines] = {}
    for file in parse_canonical_diff(diff_text).files:
        diff = file["diff"]
        for side, path_key, kind, number in (
            ("old_lines", "old_path", "deleted", "old_line"),
            ("new_lines", "new_path", "added", "new_line"),
        ):
            path = diff[path_key]
            if path is None:
                continue
            entry = changed.setdefault(
                path, ChangedLines(diff=FileDiff.model_validate(diff))
            )
            target = getattr(entry, side)
            for hunk in diff["hunks"]:
                target.update(
                    line[number] for line in hunk["lines"] if line["kind"] == kind
                )

    return changed


def is_grounded(
    finding: dict[str, Any], changed_lines: dict[str, ChangedLines]
) -> bool:
    """Whether the production same-hunk changed-anchor policy accepts a finding."""

    lines = changed_lines.get(finding["path"])
    if lines is None or lines.diff is None:
        return False
    try:
        location = LLMFindingV1.model_validate(
            {name: finding[name] for name in LLMFindingV1.model_fields}
        )
    except KeyError, ValidationError:
        return False
    return has_changed_anchor(location, [lines.diff])
