"""Spike 1 evidence: label table from real ``git diff`` output."""

from pathlib import Path

import pytest
from review_spikes.difflabels import (
    InvalidDiff,
    Kind,
    Unresolvable,
    build_label_tables,
    render_for_prompt,
    resolve,
)

FIXTURES = Path(__file__).parent / "fixtures" / "diffs"


@pytest.fixture(scope="module")
def mixed():
    return build_label_tables((FIXTURES / "mixed.diff").read_text("utf-8"))


def by_path(tables, path):
    return next(t for t in tables if t.path == path)


def test_file_statuses(mixed):
    statuses = {t.path: (t.status, t.is_binary) for t in mixed}
    assert statuses == {
        "docs/noeol.txt": ("modified", False),
        "img.bin": ("modified", True),
        "src/added.py": ("added", False),
        "src/app.py": ("modified", False),
        "src/crlf.py": ("modified", False),
        "src/gone.py": ("deleted", False),
        "src/new_name.py": ("renamed", False),
        "src/pkg_moved.py": ("renamed", False),
    }


def test_labels_for_replacement_and_insertion(mixed):
    table = by_path(mixed, "src/app.py")
    assert table.lines["L5"].kind is Kind.DELETED
    assert table.lines["R5"].text == "line5_changed"
    assert table.lines["R7"].text == "inserted_after_6"
    # context line carries both numbers; they differ after an insertion
    assert table.lines["C8/7"].text == "line7"


def test_second_hunk_numbers_shift_after_insert(mixed):
    table = by_path(mixed, "src/app.py")
    assert table.lines["L30"].text == "line30"
    assert table.lines["L31"].text == "line31"
    assert table.lines["C31/32"].text == "line32"
    assert table.lines["R39"].text == "line40_changed"
    assert {line.hunk_index for line in table.lines.values()} == {1, 2, 3}


def test_labels_are_unique_per_file(mixed):
    for table in mixed:
        assert len(table.lines) == len(set(table.lines))


def test_new_file_has_only_r_labels_and_deleted_only_l(mixed):
    assert set(by_path(mixed, "src/added.py").lines) == {"R1", "R2", "R3"}
    assert set(by_path(mixed, "src/gone.py").lines) == {"L1", "L2", "L3"}


def test_binary_and_pure_rename_have_no_labels(mixed):
    assert by_path(mixed, "img.bin").lines == {}
    assert by_path(mixed, "src/pkg_moved.py").lines == {}
    assert by_path(mixed, "src/pkg_moved.py").old_path == "src/pure_move.py"


def test_rename_with_edit_keeps_both_paths(mixed):
    table = by_path(mixed, "src/new_name.py")
    assert (table.old_path, table.new_path) == ("src/old_name.py", "src/new_name.py")
    assert table.lines["R3"].text == "def f3(): return 33"


def test_no_newline_marker_is_flag_not_line(mixed):
    table = by_path(mixed, "docs/noeol.txt")
    assert set(table.lines) == {"C1/1", "L2", "R2", "R3"}
    assert table.lines["L2"].no_eol and table.lines["R3"].no_eol
    assert not table.lines["R2"].no_eol


def test_crlf_is_stripped_from_text(mixed):
    assert by_path(mixed, "src/crlf.py").lines["R2"].text == "y = 22"


def test_github_legacy_position_counts_later_headers(mixed):
    lines = by_path(mixed, "src/app.py").lines
    # first "@@" is position 0 -> first line below it is 1
    assert lines["C2/2"].position == 1
    assert lines["R5"].position == 5
    first_hunk_len = sum(1 for line in lines.values() if line.hunk_index == 1)
    # 2nd header counts as a line, so first line of hunk 2 is len + 2
    second = next(line for line in lines.values() if line.hunk_index == 2)
    assert second.position == first_hunk_len + 2


def test_quoted_non_ascii_path_is_decoded():
    tables = build_label_tables((FIXTURES / "quoted.diff").read_text("utf-8"))
    assert tables[0].path == "src/файл.py"
    assert set(tables[0].lines) == {"R1", "R2"}


def test_render_for_prompt_shows_labels_not_raw_numbers(mixed):
    text = render_for_prompt(mixed)
    assert "R5" in text and "line5_changed" in text
    assert "binary: content not shown" in text
    assert "no content changes" in text
    assert "@@ -" not in text


# --- resolve -----------------------------------------------------------------


def test_resolve_single_added_line(mixed):
    anchor = resolve(mixed, "src/app.py", "R5")
    assert (anchor.side, anchor.start_line, anchor.end_line) == ("NEW", 5, 5)
    assert not anchor.is_range


def test_resolve_deleted_line_is_old_side(mixed):
    anchor = resolve(mixed, "src/app.py", "L30", "L31")
    assert (anchor.side, anchor.start_line, anchor.end_line) == ("OLD", 30, 31)
    assert anchor.is_range


def test_resolve_range_allows_context_after_start(mixed):
    anchor = resolve(mixed, "src/app.py", "R7", "C8/7")
    assert (anchor.side, anchor.end_line) == ("NEW", 8)


def test_resolve_deleted_file_uses_old_path(mixed):
    anchor = resolve(mixed, "src/gone.py", "L1")
    assert anchor.file_path == "src/gone.py" and anchor.side == "OLD"


def test_resolve_old_side_on_renamed_file_requires_old_path(mixed):
    # Label L3 lives on the OLD side, so the OLD path must be given.
    assert resolve(mixed, "src/old_name.py", "L3").side == "OLD"
    with pytest.raises(Unresolvable) as err:
        resolve(mixed, "src/new_name.py", "L3")
    assert err.value.reason == "path_side_mismatch"
    # VCS path for either is the NEW path
    assert resolve(mixed, "src/old_name.py", "L3").file_path == "src/new_name.py"


@pytest.mark.parametrize(
    ("path", "start", "end", "reason"),
    [
        ("src/app.py", "R9999", None, "unknown_label"),
        ("src/nope.py", "R1", None, "unknown_file"),
        ("src/app.py", "r5", None, "bad_label_syntax"),
        ("src/app.py", "R5; DROP", None, "bad_label_syntax"),
        ("src/app.py", "R5", "R39", "cross_hunk"),
        ("src/app.py", "R7", "R5", "reversed_range"),
        ("src/app.py", "L5", "R5", "mixed_sides"),
        ("src/app.py", "C2/2", None, "context_start"),
        ("img.bin", "R1", None, "binary_file"),
        ("src/pkg_moved.py", "R1", None, "unknown_label"),
        ("src/added.py", "L1", None, "unknown_label"),
    ],
)
def test_resolve_rejects_instead_of_guessing(mixed, path, start, end, reason):
    with pytest.raises(Unresolvable) as err:
        resolve(mixed, path, start, end)
    assert err.value.reason == reason


def test_path_traversal_in_diff_is_rejected():
    bad = (
        "diff --git a/../x b/../x\n--- a/../x\n+++ b/../x\n@@ -1 +1 @@\n-a\n+b\n"
    )
    with pytest.raises(InvalidDiff):
        build_label_tables(bad)
