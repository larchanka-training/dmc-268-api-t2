# ruff: noqa: PLR2004, RUF001 - literal contract examples and Russian fixtures
"""Saved result -> provider-ready review, through the publication service seam."""

from copy import deepcopy
from dataclasses import replace
from typing import Any
from uuid import UUID

import pytest

from app.application.github_publication import prepare_github_review
from app.application.publication_ports import PersistedFinding, PublicationInput
from app.vcs.diff import parse_diff

REVIEW_ID = UUID("00000000-0000-0000-0000-000000000024")
HEAD_SHA = "b" * 40
BASE_SHA = "a" * 40


def publication_input(payload_data: dict[str, Any]) -> PublicationInput:
    context = {**payload_data, "review_id": str(REVIEW_ID)}
    context["snapshot"] = {
        **context["snapshot"],
        "head_sha": HEAD_SHA,
        "base_sha": BASE_SHA,
        "merge_base_sha": "c" * 40,
    }
    return PublicationInput(
        review_id=REVIEW_ID,
        change_request_id=UUID("00000000-0000-0000-0000-000000000007"),
        owner="team",
        name="project",
        repository_external_id="100",
        number=7,
        head_sha=HEAD_SHA,
        base_sha=BASE_SHA,
        merge_base_sha="c" * 40,
        config_digest="config-v1",
        summary="Данные возвращаются без проверки.",
        coverage=context["coverage"],
        contexts=(context,),
        findings=(
            PersistedFinding(
                id=UUID("00000000-0000-0000-0000-000000000039"),
                fingerprint="finding-39",
                path="src/review.py",
                side="NEW",
                start_line=39,
                end_line=39,
                category="correctness",
                severity="high",
                title="Проверяйте входные данные",
                explanation="Нет валидации схемы.",
                evidence="return data",
                recommendation="Добавьте проверку схемы.",
                proposed_diff_fix=None,
            ),
        ),
    )


def test_prepare_review_maps_saved_new_line_and_builds_overview(
    payload_data: dict[str, Any],
) -> None:
    """AC-PREP-01: one saved NEW finding yields Overview and exact RIGHT line."""
    prepared = prepare_github_review(publication_input(payload_data))

    assert prepared.request.head_sha == HEAD_SHA
    assert prepared.comments == (
        {
            "path": "src/review.py",
            "line": 39,
            "side": "RIGHT",
            "body": (
                "**high: Проверяйте входные данные**\n\n"
                "Нет валидации схемы.\n\nДобавьте проверку схемы.\n\n"
                "<!-- dmc268-suppress:4a292c575fb4ed21b73d47d50bcb3ef17f192605730b7877"
                "30b515e74039b37d -->\n"
                "<!-- dmc268-finding:finding-39 -->"
            ),
        },
    )
    assert prepared.body.startswith(
        "<!-- dmc268-review:v1 review=00000000-0000-0000-0000-000000000024 "
        f"head={HEAD_SHA} config=config-v1 -->\n"
    )
    assert "Данные возвращаются без проверки." in prepared.body
    assert "src/review.py" in prepared.body
    assert "high: Проверяйте входные данные" in prepared.body


def test_prepare_review_maps_saved_deleted_line_to_left(
    payload_data: dict[str, Any],
) -> None:
    """AC-PREP-02: saved OLD coordinates remain on the old diff side."""
    review = publication_input(payload_data)
    review = replace(review, findings=(replace(review.findings[0], side="OLD"),))

    prepared = prepare_github_review(review)

    assert prepared.comments[0]["side"] == "LEFT"
    assert prepared.comments[0]["line"] == 39


def test_prepare_review_masks_secrets_in_overview_and_comment(
    payload_data: dict[str, Any],
) -> None:
    """AC-PREP-06: persisted text is masked again at the external write boundary."""
    review = publication_input(payload_data)
    review = replace(
        review,
        summary="Риск token=secret-value",
        findings=(
            replace(
                review.findings[0],
                title="Проблема password=private-value",
                explanation="Ключ api_key=provider-key",
                recommendation="Удалите token=token-key",
            ),
        ),
    )

    prepared = prepare_github_review(review)

    rendered = prepared.body + str(prepared.comments[0]["body"])
    assert "secret-value" not in rendered
    assert "private-value" not in rendered
    assert "provider-key" not in rendered
    assert "token-key" not in rendered
    assert "token=***" in rendered


def test_prepare_review_reports_empty_findings_and_skipped_coverage_honestly(
    payload_data: dict[str, Any],
) -> None:
    """AC-PREP-07: no findings does not imply that skipped code was reviewed."""
    review = publication_input(payload_data)
    review = replace(
        review,
        findings=(),
        coverage={
            **review.coverage,
            "skipped": [
                {"path": "src/large.py", "ranges": [], "reason": "token_budget"}
            ],
        },
    )

    prepared = prepare_github_review(review)

    assert prepared.comments == ()
    assert "Замечаний не обнаружено в проверенной части изменений" in prepared.body
    assert "src/large.py: token_budget" in prepared.body
    assert "diff_only" in prepared.body


def test_prepare_review_maps_multiline_range_without_losing_start(
    payload_data: dict[str, Any],
) -> None:
    """AC-PREP-03: a multiline finding keeps both exact GitHub endpoints."""
    review = publication_input(payload_data)
    hunk = review.contexts[0]["files"][0]["diff"]["hunks"][0]
    hunk["new_count"] = 3
    hunk["lines"].append(
        {
            "kind": "added",
            "text": "return checked",
            "old_line": None,
            "new_line": 40,
        }
    )
    review.coverage["included"][0]["ranges"][0]["end_line"] = 40
    review = replace(review, findings=(replace(review.findings[0], end_line=40),))

    prepared = prepare_github_review(review)

    assert {
        key: prepared.comments[0][key]
        for key in ("line", "side", "start_line", "start_side")
    } == {"line": 40, "side": "RIGHT", "start_line": 39, "start_side": "RIGHT"}


@pytest.mark.parametrize(
    "location",
    [
        {"start_line": 999, "end_line": 999},
        {"start_line": 38, "end_line": 38},
        {"end_line": 99},
        {"path": "src/other.py"},
        {"side": "INVALID"},
    ],
)
def test_prepare_review_keeps_unpublishable_anchor_in_overview_only(
    payload_data: dict[str, Any],
    location: dict[str, Any],
) -> None:
    """AC-PREP-04: unsafe/unmatched anchors never become inline comments."""
    review = publication_input(payload_data)
    review = replace(review, findings=(replace(review.findings[0], **location),))

    prepared = prepare_github_review(review)

    assert prepared.comments == ()
    assert "high: Проверяйте входные данные" in prepared.body
    assert "Не опубликованы inline: 1" in prepared.body


def test_prepare_review_uses_new_path_for_old_line_in_renamed_file(
    payload_data: dict[str, Any],
) -> None:
    """AC-PREP-05: GitHub's rename path is new_path even for LEFT lines."""
    review = publication_input(payload_data)
    diff = review.contexts[0]["files"][0]["diff"]
    diff.update(status="renamed", old_path="src/old.py")
    review = replace(
        review,
        findings=(
            replace(
                review.findings[0],
                path="src/old.py",
                side="OLD",
            ),
        ),
    )

    prepared = prepare_github_review(review)

    assert prepared.comments[0]["path"] == "src/review.py"
    assert prepared.comments[0]["side"] == "LEFT"


def test_prepare_review_deduplicates_saved_fingerprints_across_chunks(
    payload_data: dict[str, Any],
) -> None:
    """AC-PREP-08: repeated persisted fingerprint produces one inline comment."""
    review = publication_input(payload_data)
    review = replace(
        review,
        findings=(
            review.findings[0],
            replace(review.findings[0], title="Повтор из другого чанка"),
        ),
    )

    prepared = prepare_github_review(review)

    assert len(prepared.comments) == 1
    assert "Проверяйте входные данные" in str(prepared.comments[0]["body"])


def test_prepare_review_limits_inline_with_severity_and_stable_order(
    payload_data: dict[str, Any],
) -> None:
    """AC-PREP-09: bounded inline prioritizes critical and is order-stable."""
    review = publication_input(payload_data)
    findings = tuple(
        replace(
            review.findings[0],
            fingerprint=f"finding-{index:02d}",
            severity="critical" if index == 21 else "low",
            title=f"Риск {index:02d}",
        )
        for index in range(22)
    )
    review = replace(review, findings=findings)

    prepared = prepare_github_review(review)

    assert len(prepared.comments) == 20
    assert "critical: Риск 21" in str(prepared.comments[0]["body"])
    assert "Не опубликованы inline: 2" in prepared.body
    assert prepare_github_review(replace(review, findings=findings[::-1])).comments == (
        prepared.comments
    )


@pytest.mark.parametrize(
    "snapshot_change",
    [
        {"head_sha": "d" * 40},
        {"base_sha": "d" * 40},
        {"merge_base_sha": "d" * 40},
        {"base_repository_id": "999"},
        {"provider": "gitlab"},
    ],
)
def test_prepare_review_rejects_context_from_another_snapshot(
    payload_data: dict[str, Any],
    snapshot_change: dict[str, str],
) -> None:
    """AC-PREP-10: coordinates are valid only for the exact frozen snapshot."""
    review = publication_input(payload_data)
    review.contexts[0]["snapshot"].update(snapshot_change)

    with pytest.raises(ValueError, match="Frozen context snapshot mismatch"):
        prepare_github_review(review)


def test_prepare_review_never_posts_inline_for_a_skipped_file(
    payload_data: dict[str, Any],
) -> None:
    """AC-PREP-11: a retained finding outside reviewed coverage stays Overview-only."""
    review = publication_input(payload_data)
    review = replace(
        review,
        coverage={
            "included": [],
            "context_quality": "diff_only",
            "skipped": [
                {"path": "src/review.py", "ranges": [], "reason": "token_budget"}
            ],
        },
    )

    prepared = prepare_github_review(review)

    assert prepared.comments == ()
    assert "Не опубликованы inline: 1" in prepared.body
    assert "src/review.py: token_budget" in prepared.body


def test_prepare_review_excludes_new_range_outside_frozen_coverage(
    payload_data: dict[str, Any],
) -> None:
    """AC-PREP-12: a changed but uncovered NEW line is never published inline."""
    review = publication_input(payload_data)
    hunk = review.contexts[0]["files"][0]["diff"]["hunks"][0]
    hunk["new_count"] = 3
    hunk["lines"].append(
        {
            "kind": "added",
            "text": "unchecked",
            "old_line": None,
            "new_line": 40,
        }
    )
    review.coverage["skipped"] = [
        {
            "path": "src/review.py",
            "ranges": [{"start_line": 40, "end_line": 40}],
            "reason": "token_budget",
        }
    ]
    review = replace(
        review,
        findings=(
            replace(
                review.findings[0],
                start_line=40,
                end_line=40,
            ),
        ),
    )

    prepared = prepare_github_review(review)

    assert prepared.comments == ()
    assert "Не опубликованы inline: 1" in prepared.body


def test_prepare_review_allows_context_inside_a_covered_multiline_range(
    payload_data: dict[str, Any],
) -> None:
    """AC-PREP-13: coverage lists changed lines, not unchanged context in a range."""
    review = publication_input(payload_data)
    hunk = review.contexts[0]["files"][0]["diff"]["hunks"][0]
    hunk.update(old_count=3, new_count=4)
    hunk["lines"].extend(
        [
            {
                "kind": "context",
                "text": "next = checked",
                "old_line": 40,
                "new_line": 40,
            },
            {"kind": "added", "text": "return next", "old_line": None, "new_line": 41},
        ]
    )
    review.coverage["included"][0]["ranges"] = [
        {"start_line": 39, "end_line": 39},
        {"start_line": 41, "end_line": 41},
    ]
    review = replace(review, findings=(replace(review.findings[0], end_line=41),))

    prepared = prepare_github_review(review)

    assert len(prepared.comments) == 1
    assert prepared.comments[0]["start_line"] == 39
    assert prepared.comments[0]["line"] == 41


def test_prepare_review_excludes_uncovered_deleted_only_line(
    payload_data: dict[str, Any],
) -> None:
    """AC-PREP-14: without additions, coverage uses OLD deleted coordinates."""
    review = publication_input(payload_data)
    diff = review.contexts[0]["files"][0]["diff"]
    diff.update(status="deleted", new_path=None, new_blob_sha=None)
    diff["hunks"] = [
        {
            "hunk_id": "deleted_1",
            "old_start": 39,
            "old_count": 2,
            "new_start": 38,
            "new_count": 0,
            "lines": [
                {"kind": "deleted", "text": "first", "old_line": 39, "new_line": None},
                {"kind": "deleted", "text": "second", "old_line": 40, "new_line": None},
            ],
        }
    ]
    review.coverage["skipped"] = [
        {
            "path": "src/review.py",
            "ranges": [{"start_line": 40, "end_line": 40}],
            "reason": "token_budget",
        }
    ]
    review = replace(
        review,
        findings=(
            replace(
                review.findings[0],
                side="OLD",
                start_line=40,
                end_line=40,
            ),
        ),
    )

    prepared = prepare_github_review(review)

    assert prepared.comments == ()
    assert "Не опубликованы inline: 1" in prepared.body


def test_prepare_review_includes_retained_rule_reference(
    payload_data: dict[str, Any],
) -> None:
    """AC-PREP-15: v2 rule reference survives publication of a saved finding."""
    review = publication_input(payload_data)
    review = replace(review, findings=(replace(review.findings[0], rule_ref="R1"),))

    prepared = prepare_github_review(review)

    assert "Правило: R1" in str(prepared.comments[0]["body"])


def test_prepare_review_checks_old_coverage_per_mixed_change_block(
    payload_data: dict[str, Any],
) -> None:
    """AC-PREP-16: skip uncovered OLD changes while retaining covered OLD anchors."""
    review = publication_input(payload_data)
    parsed = parse_diff("""diff --git a/src/review.py b/src/review.py
index 1111111..2222222 100644
--- a/src/review.py
+++ b/src/review.py
@@ -10,3 +10,4 @@
-unchecked_before
+unchecked_after
+extra_line
 separator
-checked_before
+checked_after
""")
    review.contexts[0]["files"] = parsed.files
    review.coverage["included"][0]["ranges"] = [{"start_line": 13, "end_line": 13}]
    review.coverage["skipped"] = [
        {
            "path": "src/review.py",
            "ranges": [{"start_line": 10, "end_line": 11}],
            "reason": "token_budget",
        }
    ]
    review = replace(
        review,
        findings=(
            replace(
                review.findings[0],
                fingerprint="uncovered-old",
                side="OLD",
                start_line=10,
                end_line=10,
                evidence="unchecked_before",
            ),
            replace(
                review.findings[0],
                fingerprint="covered-old",
                side="OLD",
                start_line=12,
                end_line=12,
                evidence="checked_before",
            ),
        ),
    )

    prepared = prepare_github_review(review)

    assert [(comment["line"], comment["side"]) for comment in prepared.comments] == [
        (12, "LEFT")
    ]
    assert str(prepared.comments[0]["body"]).endswith(
        "<!-- dmc268-finding:covered-old -->"
    )
    assert "Не опубликованы inline: 1" in prepared.body


def test_prepare_review_keeps_unmapped_old_deletion_in_overview(
    payload_data: dict[str, Any],
) -> None:
    """AC-PREP-17: every OLD change in a mixed multiline range needs coverage proof."""
    review = publication_input(payload_data)
    parsed = parse_diff("""diff --git a/src/review.py b/src/review.py
index 1111111..2222222 100644
--- a/src/review.py
+++ b/src/review.py
@@ -10,3 +10,3 @@
-checked_before
+checked_after
+extra_line
 separator
-deleted_without_replacement
""")
    review.contexts[0]["files"] = parsed.files
    review.coverage["included"][0]["ranges"] = [{"start_line": 10, "end_line": 11}]
    review = replace(
        review,
        findings=(
            replace(
                review.findings[0],
                side="OLD",
                start_line=10,
                end_line=12,
                evidence="checked_before",
            ),
        ),
    )

    prepared = prepare_github_review(review)

    assert prepared.comments == ()
    assert "high: Проверяйте входные данные" in prepared.body
    assert "Не опубликованы inline: 1" in prepared.body


def _shifted_hunk() -> dict[str, Any]:
    """The fixture hunk with its added line moved from 39 to 45 (new commit)."""
    return {
        "hunk_id": "hunk_1",
        "old_start": 38,
        "old_count": 2,
        "new_start": 44,
        "new_count": 2,
        "lines": [
            {
                "kind": "context",
                "text": "def validate(data):",
                "old_line": 38,
                "new_line": 44,
            },
            {
                "kind": "deleted",
                "text": "    return data",
                "old_line": 39,
                "new_line": None,
            },
            {
                "kind": "added",
                "text": "    return Schema.model_validate(data)",
                "old_line": None,
                "new_line": 45,
            },
        ],
    }


# Line-free key of the fixture identity src/review.py|NEW|correctness|
# «Проверяйте входные данные», pinned literal — not recomputed via production code.
SUPPRESS_KEY = "4a292c575fb4ed21b73d47d50bcb3ef17f192605730b787730b515e74039b37d"


def _early_hunk() -> dict[str, Any]:
    """An added line at 10: a second function above the fixture's one."""
    return {
        "hunk_id": "hunk_0",
        "old_start": 9,
        "old_count": 2,
        "new_start": 9,
        "new_count": 2,
        "lines": [
            {
                "kind": "context",
                "text": "def parse(data):",
                "old_line": 9,
                "new_line": 9,
            },
            {
                "kind": "deleted",
                "text": "    return data",
                "old_line": 10,
                "new_line": None,
            },
            {
                "kind": "added",
                "text": "    return Schema.model_validate(data)",
                "old_line": None,
                "new_line": 10,
            },
        ],
    }


def test_prepare_review_publishes_second_finding_of_published_identity(
    payload_data: dict[str, Any],
) -> None:
    """A new finding of an already-published identity posts inline (#53 review).

    Two defects can share path/side/category/title (one per function), so the
    identity key alone cannot tell them apart. Earlier-cycle comments occupy
    one suppression slot each; the exact repeat (full fingerprint match)
    claims the slot first and the surplus publishes — even when it ranks
    before the repeat.
    """
    data = deepcopy(payload_data)
    data["files"][0]["diff"]["hunks"] = [_early_hunk(), _shifted_hunk()]
    data["coverage"]["included"][0]["ranges"] = [
        {"start_line": 45, "end_line": 45},
        {"start_line": 10, "end_line": 10},
    ]
    review = publication_input(data)
    copy = replace(
        review.findings[0],
        id=UUID("00000000-0000-0000-0000-000000000010"),
        fingerprint="finding-copy",
        start_line=10,
        end_line=10,
        explanation="Вторая функция без валидации.",
        evidence="return request.data",
    )
    original = replace(
        review.findings[0],
        fingerprint="finding-45",
        start_line=45,
        end_line=45,
    )
    review = replace(review, findings=(original, copy))

    prepared = prepare_github_review(
        review,
        published={SUPPRESS_KEY: 1, "finding-45": 1},
    )

    # The repeat stays suppressed; the new same-identity finding is posted.
    assert len(prepared.comments) == 1
    assert prepared.comments[0]["line"] == 10
    assert str(prepared.comments[0]["body"]).endswith(
        "<!-- dmc268-finding:finding-copy -->"
    )
    assert "finding-45" in prepared.body  # listed as already noted
    assert "finding-copy" not in prepared.body


def test_prepare_review_suppresses_line_shifted_repeat_by_identity(
    payload_data: dict[str, Any],
) -> None:
    """A repeat after a line shift between two commits is not posted again (#53).

    The full fingerprint pins the line range, so the moved finding carries a
    new fingerprint; suppression must still match on the line-free identity
    the earlier cycle published.
    """
    shifted = deepcopy(payload_data)
    shifted["files"][0]["diff"]["hunks"] = [_shifted_hunk()]
    shifted["coverage"]["included"][0]["ranges"] = [{"start_line": 45, "end_line": 45}]
    review = publication_input(shifted)
    moved = replace(
        review.findings[0],
        fingerprint="finding-45",
        start_line=45,
        end_line=45,
    )
    review = replace(review, findings=(moved,))

    prepared = prepare_github_review(review, published={SUPPRESS_KEY: 1})

    assert prepared.comments == ()
    assert "Уже отмечено в прошлых циклах" in prepared.body
    assert "finding-45" in prepared.body


def test_prepare_review_marks_inline_comment_with_line_free_identity(
    payload_data: dict[str, Any],
) -> None:
    """Inline comments publish the line-free identity for later cycles (#53).

    The finding marker stays the last line of the body (proof matching);
    the suppress marker goes on its own line right before it.
    """
    prepared = prepare_github_review(publication_input(payload_data))

    body = str(prepared.comments[0]["body"])
    assert f"<!-- dmc268-suppress:{SUPPRESS_KEY} -->" in body
    assert body.endswith("<!-- dmc268-finding:finding-39 -->")


def test_prepare_review_strips_forged_markers_from_model_text(
    payload_data: dict[str, Any],
) -> None:
    """RT-12: markers injected into finding text never reach outgoing bodies.

    Only the publisher-emitted marker tail survives rendering; a forged
    dmc268 marker inside title/explanation is stripped (dedup state is
    computed earlier from the raw stored text and stays unaffected).
    """
    review = publication_input(payload_data)
    poisoned = replace(
        review.findings[0],
        title="Проверяйте <!-- dmc268-finding:fake --> входные данные",
        explanation="Нет <!-- dmc268-suppress:abc --> валидации схемы.",
    )
    review = replace(review, findings=(poisoned,))

    prepared = prepare_github_review(review)

    comment_body = str(prepared.comments[0]["body"])
    assert comment_body.count("dmc268-") == 2  # publisher tail: suppress + finding
    assert "fake" not in comment_body
    assert "abc" not in comment_body
    assert prepared.body.count("dmc268-") == 1  # only the review marker
