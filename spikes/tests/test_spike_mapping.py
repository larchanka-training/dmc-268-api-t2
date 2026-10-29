"""Spike 2 evidence: label -> GitHub / GitLab payloads, fallbacks, errors, fingerprint."""

from pathlib import Path

import pytest
from review_spikes.difflabels import build_label_tables, resolve
from review_spikes.fingerprint import (
    MARKER_RE,
    fingerprint_v2,
    marker_v2,
    rule_key,
)
from review_spikes.mapping import (
    Action,
    DiffRefs,
    Placement,
    PublishContext,
    classify_error,
    github_comment,
    github_legacy_position,
    github_review,
    gitlab_position,
    plan_comment,
)

FIXTURES = Path(__file__).parent / "fixtures" / "diffs"
HEAD = "h" * 40
REFS = DiffRefs(base_sha="b" * 40, start_sha="s" * 40, head_sha=HEAD)
GH = PublishContext("github", HEAD)
GL = PublishContext("gitlab", HEAD, REFS)


@pytest.fixture(scope="module")
def tables():
    return build_label_tables((FIXTURES / "mixed.diff").read_text("utf-8"))


# --- GitHub --------------------------------------------------------------------


def test_github_single_added_line(tables):
    comment = github_comment(resolve(tables, "src/app.py", "R5"), "b")
    assert comment == {"path": "src/app.py", "line": 5, "side": "RIGHT", "body": "b"}


def test_github_deleted_range_is_left_with_start_fields(tables):
    comment = github_comment(resolve(tables, "src/app.py", "L30", "L31"), "b")
    assert comment == {
        "path": "src/app.py",
        "line": 31,
        "side": "LEFT",
        "start_line": 30,
        "start_side": "LEFT",
        "body": "b",
    }


def test_github_renamed_file_uses_new_path_even_for_left_side(tables):
    comment = github_comment(resolve(tables, "src/old_name.py", "L3"), "b")
    assert comment["path"] == "src/new_name.py" and comment["side"] == "LEFT"


def test_github_deleted_file_uses_old_path(tables):
    assert github_comment(resolve(tables, "src/gone.py", "L2"), "b")["path"] == (
        "src/gone.py"
    )


def test_github_review_event_is_fixed_to_comment():
    payload = github_review(HEAD, "overview", [])
    assert payload["event"] == "COMMENT" and payload["commit_id"] == HEAD
    assert set(payload) == {"commit_id", "body", "event", "comments"}


def test_legacy_position_differs_from_line_and_depends_on_hunk_offsets(tables):
    anchor = resolve(tables, "src/app.py", "R39")  # last hunk
    comment = github_comment(anchor, "b")
    assert comment["line"] == 39
    assert github_legacy_position(anchor) != 36  # evidence for ADR-8 (line > position)


# --- GitLab --------------------------------------------------------------------


def test_gitlab_added_line_has_only_new_line(tables):
    pos = gitlab_position(resolve(tables, "src/app.py", "R5"), REFS)
    assert pos == {
        "position_type": "text",
        "base_sha": REFS.base_sha,
        "start_sha": REFS.start_sha,
        "head_sha": HEAD,
        "old_path": "src/app.py",
        "new_path": "src/app.py",
        "new_line": 5,
    }


def test_gitlab_deleted_line_has_only_old_line(tables):
    pos = gitlab_position(resolve(tables, "src/app.py", "L30"), REFS)
    assert pos["old_line"] == 30 and "new_line" not in pos


def test_gitlab_context_end_has_both_lines(tables):
    pos = gitlab_position(resolve(tables, "src/app.py", "R7", "C8/7"), REFS)
    assert (pos["old_line"], pos["new_line"]) == (7, 8)
    assert pos["line_range"]["start"]["type"] == "new"
    assert pos["line_range"]["end"]["type"] == "old"


def test_gitlab_added_file_repeats_new_path_as_old_path(tables):
    pos = gitlab_position(resolve(tables, "src/added.py", "R1"), REFS)
    assert pos["old_path"] == pos["new_path"] == "src/added.py"


def test_gitlab_rename_keeps_both_paths(tables):
    pos = gitlab_position(resolve(tables, "src/new_name.py", "R3"), REFS)
    assert (pos["old_path"], pos["new_path"]) == ("src/old_name.py", "src/new_name.py")


# --- placement ------------------------------------------------------------------


def test_resolvable_label_is_inline_on_both_providers(tables):
    gh = plan_comment(GH, tables, "src/app.py", "R5", None, "b")
    gl = plan_comment(GL, tables, "src/app.py", "R5", None, "b")
    assert gh.placement is gl.placement is Placement.INLINE
    assert "position" in gl.payload and gl.payload["body"] == "b"


@pytest.mark.parametrize("label", ["R9999", "C2/2", "L5"])
def test_line_outside_changes_on_github_goes_to_summary(tables, label):
    planned = plan_comment(GH, tables, "src/crlf.py", label, None, "b")
    assert planned.placement is Placement.SUMMARY and planned.payload is None
    assert planned.reason in {"unknown_label", "context_start"}


def test_out_of_hunk_on_gitlab_becomes_file_level(tables):
    planned = plan_comment(GL, tables, "src/crlf.py", "R9999", None, "b")
    assert planned.placement is Placement.FILE
    assert planned.payload["position"]["position_type"] == "file"
    assert "new_line" not in planned.payload["position"]


def test_github_file_level_only_when_capability_enabled(tables):
    ctx = PublishContext("github", HEAD, github_file_level=True)
    planned = plan_comment(ctx, tables, "src/crlf.py", "R9999", None, "b")
    assert planned.placement is Placement.FILE
    assert planned.payload["subject_type"] == "file"


def test_binary_file_never_inline(tables):
    planned = plan_comment(GH, tables, "img.bin", "R1", None, "b")
    assert planned.placement is Placement.SUMMARY and planned.reason == "binary_file"


def test_unknown_file_always_summary_even_on_gitlab(tables):
    planned = plan_comment(GL, tables, "not/in/diff.py", "R1", None, "b")
    assert planned.placement is Placement.SUMMARY and planned.reason == "unknown_file"


# --- errors ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("provider", "status", "kwargs", "expected"),
    [
        ("github", 422, {"head_moved": True}, Action.SKIP_STALE_HEAD),
        ("gitlab", 400, {"head_moved": True}, Action.SKIP_STALE_HEAD),
        ("github", 422, {"head_moved": False}, Action.DEGRADE_TO_SUMMARY),
        ("gitlab", 400, {"head_moved": False}, Action.DEGRADE_TO_SUMMARY),
        ("github", 403, {"head_moved": False}, Action.ABORT_AUTHORITY),
        ("github", 403, {"head_moved": False, "retry_after": 30}, Action.RETRY_LATER),
        ("github", 429, {"head_moved": False}, Action.RETRY_LATER),
        ("github", 502, {"head_moved": False}, Action.RECONCILE_THEN_DECIDE),
        ("gitlab", 404, {"head_moved": False}, Action.FAIL),
    ],
)
def test_error_classification(provider, status, kwargs, expected):
    assert classify_error(provider, status, **kwargs) is expected


# --- fingerprint v2 ----------------------------------------------------------------


def test_fingerprint_is_stable_when_lines_shift():
    a = fingerprint_v2("SEC-1", "src/a.py", ["  token = request.args['t']"])
    b = fingerprint_v2("SEC-1", "src/a.py", ["token   =  request.args['t']"])
    assert a == b  # same content wherever it moves; whitespace-insensitive


@pytest.mark.parametrize(
    "other",
    [
        ("SEC-2", "src/a.py", ["x = 1"]),
        ("SEC-1", "src/b.py", ["x = 1"]),
        ("SEC-1", "src/a.py", ["x = 2"]),
    ],
)
def test_fingerprint_differs_on_rule_path_or_content(other):
    assert fingerprint_v2("SEC-1", "src/a.py", ["x = 1"]) != fingerprint_v2(*other)


def test_identical_lines_collide_without_occurrence_index():
    """Known limitation to document: two `return None` lines, same rule."""
    one = fingerprint_v2("cat:correctness", "a.py", ["return None"])
    two = fingerprint_v2("cat:correctness", "a.py", ["return None"])
    third = fingerprint_v2("cat:correctness", "a.py", ["return None"], occurrence=1)
    assert one == two != third


def test_rule_key_falls_back_to_category():
    assert rule_key("SEC-1", "security") == "SEC-1"
    assert rule_key(None, "security") == "cat:security"


def test_marker_roundtrip():
    fp = fingerprint_v2("SEC-1", "a.py", ["x"])
    found = MARKER_RE.search(f"text\n{marker_v2(fp)}")
    assert found and found.group(1) == fp
