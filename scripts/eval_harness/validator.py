"""Two-layer response validity: JSON-schema shape, and diff-grounding.

A JSON-valid finding is not necessarily a true one, but a finding whose
location does not even exist on a changed line of the diff is definitely
hallucinated. This module checks both, independently.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator

_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_NEW_FILE_HEADER = re.compile(r"^\+\+\+ (?:b/)?(.+)$")
_OLD_FILE_HEADER = re.compile(r"^--- (?:a/)?(.+)$")


@dataclass(slots=True)
class ChangedLines:
    new_lines: set[int] = field(default_factory=set)
    old_lines: set[int] = field(default_factory=set)


def validate_shape(response: Any, schema: dict[str, Any]) -> list[str]:
    """Return JSON-schema validation error messages (empty if valid)."""

    validator = Draft202012Validator(schema)
    return [error.message for error in validator.iter_errors(response)]


def parse_diff(diff_text: str) -> dict[str, ChangedLines]:
    """Return, per file path, the set of changed OLD/NEW line numbers."""

    changed: dict[str, ChangedLines] = {}
    current_path: str | None = None
    old_line = new_line = 0

    for line in diff_text.splitlines():
        new_file_match = _NEW_FILE_HEADER.match(line)
        if new_file_match:
            path = new_file_match.group(1)
            current_path = None if path == "/dev/null" else path
            if current_path is not None:
                changed.setdefault(current_path, ChangedLines())
            continue
        if _OLD_FILE_HEADER.match(line):
            continue

        hunk_match = _HUNK_HEADER.match(line)
        if hunk_match:
            old_line = int(hunk_match.group(1))
            new_line = int(hunk_match.group(2))
            continue

        if current_path is None:
            continue

        if line.startswith("+"):
            changed[current_path].new_lines.add(new_line)
            new_line += 1
        elif line.startswith("-"):
            changed[current_path].old_lines.add(old_line)
            old_line += 1
        elif line.startswith(" "):
            old_line += 1
            new_line += 1

    return changed


def is_grounded(
    finding: dict[str, Any], changed_lines: dict[str, ChangedLines]
) -> bool:
    """Whether a finding's location lands entirely on changed lines of the diff."""

    lines = changed_lines.get(finding["path"])
    if lines is None:
        return False
    target = lines.new_lines if finding["side"] == "NEW" else lines.old_lines
    return all(
        line in target for line in range(finding["start_line"], finding["end_line"] + 1)
    )
