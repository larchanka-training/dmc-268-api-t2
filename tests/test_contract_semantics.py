from typing import Any

import pytest
from test_contracts import _context_payload, _review_result

from app.domain.contract_validation import validate_hunks, validate_ranges


@pytest.mark.parametrize("kind", ["context", "result", "public"])
def test_semantic_validation_rejects_reversed_ranges(kind: str) -> None:
    payload: dict[str, Any]
    if kind == "context":
        payload = _context_payload()
        validate_ranges(payload)
        payload["coverage"]["included"][0]["ranges"][0].update(start_line=9, end_line=2)
    elif kind == "result":
        payload = _review_result()
        validate_ranges(payload)
        payload["findings"][0].update(start_line=9, end_line=2)
    else:
        payload = {"items": [{"start_line": 9, "end_line": 2}]}
    with pytest.raises(ValueError, match="end_line"):
        validate_ranges(payload)


@pytest.mark.parametrize("public", [False, True])
def test_semantic_validation_rejects_incorrect_hunk_counts(public: bool) -> None:
    hunk = _context_payload()["files"][0]["diff"]["hunks"][0]
    validate_hunks([hunk])
    hunk["old_count"] = 999
    if public:
        hunk = dict(hunk)
        hunk["old_lines"] = hunk.pop("old_count")
        hunk["new_lines"] = hunk.pop("new_count")
    with pytest.raises(ValueError, match="count"):
        validate_hunks([hunk], public=public)


@pytest.mark.parametrize("numbers", [[2, 3], [1, 3], [2, 1]])
def test_semantic_validation_rejects_line_maps_not_matching_hunk_start(
    numbers: list[int],
) -> None:
    hunk = _context_payload()["files"][0]["diff"]["hunks"][0]
    validate_hunks([hunk])
    for line, number in zip(hunk["lines"], numbers, strict=True):
        line["new_line"] = number
    with pytest.raises(ValueError, match="coordinates"):
        validate_hunks([hunk])
