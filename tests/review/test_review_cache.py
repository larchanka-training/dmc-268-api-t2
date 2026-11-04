"""Exact review cache identity: line-number free, context and config bound."""

from typing import Any

from app.application.ports import ReviewChunkResult, ReviewFinding, TokenUsage
from app.domain.enums import FindingCategory, FindingSeverity, FindingSide
from app.review.cache import cache_identity, from_cached, to_cached
from app.vcs.diff import parse_diff

DIGEST = "config-digest"


def _body(path: str, start: int, *, title: str = "PR", neighbour: str = "x = 0") -> Any:
    raw = (
        f"diff --git a/{path} b/{path}\n"
        "index 1111111..2222222 100644\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        f"@@ -{start},2 +{start},2 @@\n"
        f" {neighbour}\n"
        "-return value\n"
        "+return value / 0\n"
    )
    return {
        "metadata": {"title": title, "body": None, "output_language": "ru"},
        "snapshot": {"head_sha": title * 10},
        "files": parse_diff(raw).files,
        "related_symbols": [],
    }


def _finding(line: int) -> ReviewFinding:
    return ReviewFinding(
        path="src/a.py",
        side=FindingSide.NEW,
        start_line=line,
        end_line=line,
        category=FindingCategory.CORRECTNESS,
        severity=FindingSeverity.HIGH,
        title="Division by zero",
        explanation="Always raises.",
        evidence="return value / 0",
        recommendation="Remove it.",
        proposed_diff_fix=None,
    )


def test_same_fragment_elsewhere_in_another_pr_has_the_same_key() -> None:
    first, _ = cache_identity(_body("src/a.py", 10, title="PR one"), DIGEST)
    second, _ = cache_identity(_body("src/a.py", 70, title="PR two"), DIGEST)

    assert first == second
    assert first.startswith("review:cache:v1:")


def test_context_or_configuration_change_changes_the_key() -> None:
    key, _ = cache_identity(_body("src/a.py", 10), DIGEST)

    assert cache_identity(_body("src/a.py", 10, neighbour="x = 1"), DIGEST)[0] != key
    assert cache_identity(_body("src/b.py", 10), DIGEST)[0] != key
    assert cache_identity(_body("src/a.py", 10), "other-model-or-prompt")[0] != key
    assert cache_identity(_body("src/a.py", 10), DIGEST, version=2)[0] != key


def test_cached_findings_are_relative_and_restored_at_current_lines() -> None:
    _, anchors_then = cache_identity(_body("src/a.py", 10), DIGEST)
    _, anchors_now = cache_identity(_body("src/a.py", 70), DIGEST)
    result = ReviewChunkResult(
        schema_version=2,
        summary="Summary",
        limitations=[],
        usage=TokenUsage(100, 40),
        latency_ms=900,
        findings=[_finding(11)],
    )

    stored = to_cached(result, anchors_then)
    restored = from_cached(stored, anchors_now)

    assert stored["findings"][0]["start_line"] == 1
    assert restored is not None
    assert [(f.start_line, f.end_line) for f in restored.findings] == [(71, 71)]
    assert restored.findings[0].side is FindingSide.NEW
    assert restored.usage == TokenUsage(0, 0)  # a hit spends no tokens
    assert restored.summary == "Summary" and restored.schema_version == 2  # noqa: PLR2004


def test_cached_finding_for_unknown_path_is_not_restored() -> None:
    _, anchors = cache_identity(_body("src/a.py", 10), DIGEST)
    stored = to_cached(ReviewChunkResult(2, "S", [], None, 1, [_finding(11)]), anchors)

    assert from_cached(stored, {}) is None
