"""Precision/recall scoring of predicted findings against hand-labeled ones.

A predicted finding matches an expected one when they share the same
``path`` and ``category`` and their line ranges overlap. Free-text fields and
``severity`` are ignored. Matching is greedy one-to-one: each expected
finding can be consumed by at most one predicted finding, so duplicate
predictions are not double-counted as extra true positives.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class MatchCounts:
    true_positives: int
    false_positives: int
    false_negatives: int


def _ranges_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return a_start <= b_end and b_start <= a_end


def _is_match(predicted: dict[str, Any], expected: dict[str, Any]) -> bool:
    return (
        predicted["path"] == expected["path"]
        and predicted["category"] == expected["category"]
        and _ranges_overlap(
            predicted["start_line"],
            predicted["end_line"],
            expected["start_line"],
            expected["end_line"],
        )
    )


def match_findings(
    predicted: list[dict[str, Any]], expected: list[dict[str, Any]]
) -> MatchCounts:
    """Greedily match predicted findings to expected ones and count outcomes."""

    remaining_expected = list(expected)
    true_positives = 0

    for candidate in predicted:
        match_index = next(
            (
                index
                for index, target in enumerate(remaining_expected)
                if _is_match(candidate, target)
            ),
            None,
        )
        if match_index is not None:
            true_positives += 1
            remaining_expected.pop(match_index)

    return MatchCounts(
        true_positives=true_positives,
        false_positives=len(predicted) - true_positives,
        false_negatives=len(remaining_expected),
    )
