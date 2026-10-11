"""Cross-field checks applied after structural JSON Schema validation."""

from collections.abc import Mapping, Sequence
from typing import Any


def validate_ranges(payload: object) -> None:
    """Reject reversed inclusive ranges in schema-validated JSON objects."""
    if isinstance(payload, dict):
        if (
            "start_line" in payload
            and "end_line" in payload
            and payload["end_line"] < payload["start_line"]
        ):
            raise ValueError("end_line must be greater than or equal to start_line")
        for value in payload.values():
            validate_ranges(value)
    elif isinstance(payload, list):
        for value in payload:
            validate_ranges(value)


def validate_hunks(hunks: Sequence[Mapping[str, Any]], *, public: bool = False) -> None:
    """Validate ContextPayload hunks, or HTTP DiffHunk with public=True."""
    count_suffix = "lines" if public else "count"
    for hunk in hunks:
        for side in ("old", "new"):
            numbers = [
                line[f"{side}_line"]
                for line in hunk["lines"]
                if line[f"{side}_line"] is not None
            ]
            if len(numbers) != hunk[f"{side}_{count_suffix}"]:
                raise ValueError(f"hunk {side} count does not match lines")
            if any(
                number != hunk[f"{side}_start"] + offset
                for offset, number in enumerate(numbers)
            ):
                raise ValueError(
                    f"hunk {side} coordinates do not match start and count"
                )
