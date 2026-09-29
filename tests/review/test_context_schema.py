"""ContextPayload v1 contract (SD §6, spec §3.1)."""

from typing import Any

import pytest
from pydantic import ValidationError

from app.review.schemas.context import ContextPayload

_ADDED_NEW_LINE = 39


def test_valid_l1_payload_is_accepted(payload_data: dict[str, Any]) -> None:
    payload = ContextPayload.model_validate(payload_data)

    assert payload.chunk_id == "chunk_001"
    assert payload.files[0].diff.hunks[0].lines[2].new_line == _ADDED_NEW_LINE
    assert payload.files[0].whole_file is None


def test_unknown_field_is_rejected(payload_data: dict[str, Any]) -> None:
    payload_data["metadata"]["extra"] = "x"

    with pytest.raises(ValidationError):
        ContextPayload.model_validate(payload_data)


@pytest.mark.parametrize(
    ("kind", "old_line", "new_line"),
    [
        ("added", 5, 5),
        ("added", None, None),
        ("deleted", None, 5),
        ("context", 5, None),
    ],
)
def test_hunk_line_sides_must_match_kind(
    payload_data: dict[str, Any], kind: str, old_line: int | None, new_line: int | None
) -> None:
    line = payload_data["files"][0]["diff"]["hunks"][0]["lines"][2]
    line.update(kind=kind, old_line=old_line, new_line=new_line)

    with pytest.raises(ValidationError):
        ContextPayload.model_validate(payload_data)


@pytest.mark.parametrize(("field", "value"), [("old_count", 3), ("new_count", 1)])
def test_hunk_counts_must_match_lines(
    payload_data: dict[str, Any], field: str, value: int
) -> None:
    payload_data["files"][0]["diff"]["hunks"][0][field] = value

    with pytest.raises(ValidationError):
        ContextPayload.model_validate(payload_data)


def test_hunk_line_numbers_must_increase(payload_data: dict[str, Any]) -> None:
    lines = payload_data["files"][0]["diff"]["hunks"][0]["lines"]
    lines[2]["new_line"] = 38  # repeats the context line's new_line

    with pytest.raises(ValidationError):
        ContextPayload.model_validate(payload_data)


def test_range_start_must_not_exceed_end(payload_data: dict[str, Any]) -> None:
    payload_data["coverage"]["included"][0]["ranges"] = [
        {"start_line": 40, "end_line": 39}
    ]

    with pytest.raises(ValidationError):
        ContextPayload.model_validate(payload_data)


@pytest.mark.parametrize("path", ["/src/review.py", "src/../etc/passwd", "src\\a.py"])
def test_paths_must_be_normalized_relative(
    payload_data: dict[str, Any], path: str
) -> None:
    payload_data["files"][0]["diff"]["new_path"] = path

    with pytest.raises(ValidationError):
        ContextPayload.model_validate(payload_data)
