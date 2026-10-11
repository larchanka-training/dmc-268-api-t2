"""ContextPayload v1 contract (SD §6, spec §3.1)."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from app.review.schemas.context import ContextPayload

_ADDED_NEW_LINE = 39
_EXAMPLE = Path(__file__).resolve().parents[2] / "schemas/examples/context.v1.json"


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


_RELATED_SYMBOL = {
    "qualified_name": "app.review.validate",
    "kind": "function",
    "signature": "def validate(data)",
    "path": "src/review.py",
    "commit_sha": "head-commit-sha",
    "range": {"start_line": 38, "end_line": 39},
    "relation": "calls",
    "relation_to_change": "changed",
    "source_excerpt": "def validate(data):",
}


def _set(path: str, value: Any) -> Callable[[dict[str, Any]], None]:
    """Mutation setting a dotted path (list indexes as digits) to ``value``."""

    def apply(data: dict[str, Any]) -> None:
        *parents, last = path.split(".")
        target: Any = data
        for key in parents:
            target = target[int(key)] if key.isdigit() else target[key]
        target[int(last) if last.isdigit() else last] = value

    return apply


def _related(**overrides: Any) -> Callable[[dict[str, Any]], None]:
    return _set("related_symbols", [{**_RELATED_SYMBOL, **overrides}])


_HUNK = "files.0.diff.hunks.0"

# (mutation, accepted by the canonical schema)
_CASES: dict[str, tuple[Callable[[dict[str, Any]], None], bool]] = {
    "fixture": (lambda _: None, True),
    "historical_language": (_set("metadata.output_language", "en"), True),
    "related_symbol": (_related(), True),
    "empty_language": (_set("metadata.output_language", ""), False),
    "gitlab_provider": (_set("snapshot.provider", "gitlab"), False),
    "raw_provider_metadata": (
        _set("snapshot.provider_metadata", {"pull_number": 42, "raw": {}}),
        False,
    ),
    "no_diff_version": (_set("snapshot.provider_metadata", {"pull_number": 42}), False),
    "zero_pull_number": (
        _set("snapshot.provider_metadata", {"pull_number": 0, "diff_version": "v"}),
        False,
    ),
    "empty_review_id": (_set("review_id", ""), False),
    "empty_head_sha": (_set("snapshot.head_sha", ""), False),
    "empty_rules_version": (_set("metadata.rules_version", ""), False),
    "duplicate_languages": (_set("metadata.languages", ["Python", "Python"]), False),
    "empty_file_language": (_set("files.0.diff.language", ""), False),
    "empty_hunk_id": (_set(f"{_HUNK}.hunk_id", ""), False),
    "nonempty_side_from_zero": (_set(f"{_HUNK}.new_start", 0), False),
    "nul_in_path": (_set("files.0.diff.new_path", "src/a\x00.py"), False),
    "null_symbol_signature": (_related(signature=None), False),
    "null_symbol_path": (_related(path=None), False),
    "null_symbol_range": (_related(range=None), False),
    "null_symbol_commit": (_related(commit_sha=None), False),
    "no_source_excerpt": (
        _set(
            "related_symbols",
            [{k: v for k, v in _RELATED_SYMBOL.items() if k != "source_excerpt"}],
        ),
        False,
    ),
}


@pytest.mark.parametrize("case", _CASES)
def test_model_agrees_with_canonical_context_schema(
    payload_data: dict[str, Any],
    canonical_validator: Callable[[str], Draft202012Validator],
    case: str,
) -> None:
    mutate, expected = _CASES[case]
    mutate(payload_data)

    schema_accepts = canonical_validator("context/v1.json").is_valid(payload_data)
    try:
        ContextPayload.model_validate(payload_data)
        model_accepts = True
    except ValidationError:
        model_accepts = False

    assert schema_accepts is expected  # the mutation means what it says
    assert model_accepts is expected


def test_canonical_example_round_trips(
    canonical_validator: Callable[[str], Draft202012Validator],
) -> None:
    example = json.loads(_EXAMPLE.read_text(encoding="utf-8"))

    dumped = ContextPayload.model_validate(example).model_dump(mode="json")

    canonical_validator("context/v1.json").validate(dumped)
