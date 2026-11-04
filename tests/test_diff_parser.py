import pytest

from app.vcs.diff import InvalidDiff, parse_diff


def test_parse_diff_preserves_changed_line_coordinates_and_coverage() -> None:
    raw = """diff --git a/src/example.py b/src/example.py
index 1111111..2222222 100644
--- a/src/example.py
+++ b/src/example.py
@@ -1 +1,2 @@
 value = 1
+return value
"""

    parsed = parse_diff(raw)

    assert parsed.raw_file_count == 1
    assert len(parsed.files) == 1
    assert parsed.skipped == []
    file = parsed.files[0]
    assert file["source_windows"] == []
    assert file["whole_file"] is None
    diff = file["diff"]
    assert isinstance(diff["file_id"], str) and diff["file_id"]
    assert diff["old_path"] == "src/example.py"
    assert diff["new_path"] == "src/example.py"
    assert diff["status"] == "modified"
    assert diff["old_blob_sha"] is None
    assert diff["new_blob_sha"] is None
    assert diff["language"] is None
    assert len(diff["hunks"]) == 1
    hunk = diff["hunks"][0]
    assert isinstance(hunk["hunk_id"], str) and hunk["hunk_id"]
    assert (hunk["old_start"], hunk["old_count"]) == (1, 1)
    assert (hunk["new_start"], hunk["new_count"]) == (1, 2)
    assert hunk["lines"] == [
        {
            "kind": "context",
            "text": "value = 1",
            "old_line": 1,
            "new_line": 1,
        },
        {
            "kind": "added",
            "text": "return value",
            "old_line": None,
            "new_line": 2,
        },
    ]
    assert parsed.coverage == {
        "included": [
            {"path": "src/example.py", "ranges": [{"start_line": 2, "end_line": 2}]}
        ],
        "skipped": [],
        "context_quality": "diff_only",
    }


def test_added_file_has_null_old_path_and_zero_length_old_hunk_side() -> None:
    raw = """diff --git a/new.py b/new.py
new file mode 100644
index 0000000..2222222
--- /dev/null
+++ b/new.py
@@ -0,0 +1 @@
+answer = 42
"""

    parsed = parse_diff(raw)

    diff = parsed.files[0]["diff"]
    assert diff["status"] == "added"
    assert diff["old_path"] is None
    assert diff["new_path"] == "new.py"
    assert diff["hunks"][0]["old_start"] == 0
    assert diff["hunks"][0]["old_count"] == 0
    assert diff["hunks"][0]["lines"] == [
        {
            "kind": "added",
            "text": "answer = 42",
            "old_line": None,
            "new_line": 1,
        }
    ]
    assert parsed.coverage["included"] == [
        {"path": "new.py", "ranges": [{"start_line": 1, "end_line": 1}]}
    ]


def test_deleted_file_uses_old_path_and_old_line_coverage() -> None:
    raw = """diff --git a/old.py b/old.py
deleted file mode 100644
index 2222222..0000000
--- a/old.py
+++ /dev/null
@@ -1 +0,0 @@
-answer = 42
"""

    parsed = parse_diff(raw)

    diff = parsed.files[0]["diff"]
    assert diff["status"] == "deleted"
    assert diff["old_path"] == "old.py"
    assert diff["new_path"] is None
    assert (diff["hunks"][0]["new_start"], diff["hunks"][0]["new_count"]) == (0, 0)
    assert parsed.coverage["included"] == [
        {"path": "old.py", "ranges": [{"start_line": 1, "end_line": 1}]}
    ]


def test_truncated_hunk_fails_closed_with_public_exception() -> None:
    raw = """diff --git a/x.py b/x.py
--- a/x.py
+++ b/x.py
@@ -1 +1,2 @@
 x = 1
"""

    with pytest.raises(InvalidDiff):
        parse_diff(raw)


def test_parent_traversal_path_is_rejected() -> None:
    raw = """diff --git a/../secret.py b/../secret.py
index 1111111..2222222 100644
--- a/../secret.py
+++ b/../secret.py
@@ -1 +1 @@
-x = 1
+x = 2
"""

    with pytest.raises(InvalidDiff):
        parse_diff(raw)


def test_metadata_only_rename_is_preserved() -> None:
    raw = """diff --git a/old_name.py b/new_name.py
similarity index 100%
rename from old_name.py
rename to new_name.py
"""

    parsed = parse_diff(raw)

    assert parsed.raw_file_count == 1
    assert parsed.files[0]["diff"]["status"] == "renamed"
    assert parsed.files[0]["diff"]["old_path"] == "old_name.py"
    assert parsed.files[0]["diff"]["new_path"] == "new_name.py"
    assert parsed.files[0]["diff"]["hunks"] == []


def test_ignore_glob_records_policy_skip() -> None:
    raw = """diff --git a/src/private.py b/src/private.py
index 1111111..2222222 100644
--- a/src/private.py
+++ b/src/private.py
@@ -1 +1 @@
-x = 1
+x = 2
"""

    parsed = parse_diff(raw, ignore_globs=("src/*.py",))

    assert parsed.raw_file_count == 1
    assert parsed.files == []
    assert parsed.coverage["skipped"] == [
        {
            "path": "src/private.py",
            "ranges": [{"start_line": 1, "end_line": 1}],
            "reason": "policy",
        }
    ]


def test_git_quoted_utf8_path_is_decoded() -> None:
    raw = """diff --git \"a/caf\\303\\251 file.py\" \"b/caf\\303\\251 file.py\"
index 1111111..2222222 100644
--- \"a/caf\\303\\251 file.py\"
+++ \"b/caf\\303\\251 file.py\"
@@ -1 +1 @@
-x = 1
+x = 2
"""

    parsed = parse_diff(raw)

    assert parsed.files[0]["diff"]["old_path"] == "café file.py"
    assert parsed.files[0]["diff"]["new_path"] == "café file.py"


@pytest.mark.parametrize("path", ["dist/app.js", "package-lock.json", "src/app.min.js"])
def test_default_policy_skips_build_and_lock_artifacts(path: str) -> None:
    raw = f"""diff --git a/{path} b/{path}
index 1111111..2222222 100644
--- a/{path}
+++ b/{path}
@@ -1 +1 @@
-x = 1
+x = 2
"""

    parsed = parse_diff(raw)

    assert parsed.raw_file_count == 1
    assert parsed.files == []
    assert parsed.skipped[0]["path"] == path
    assert parsed.skipped[0]["reason"] == "policy"


def test_binary_patch_is_recorded_as_skipped() -> None:
    raw = """diff --git a/image.png b/image.png
index 1111111..2222222 100644
GIT binary patch
literal 3
KcmZQzU|?Vb00001
"""

    parsed = parse_diff(raw)

    assert parsed.raw_file_count == 1
    assert parsed.files == []
    assert parsed.skipped == [{"path": "image.png", "ranges": [], "reason": "policy"}]


def test_malformed_nonempty_diff_is_not_silently_empty() -> None:
    with pytest.raises(InvalidDiff):
        parse_diff("diff --git a/x.py b/x.py\nnot a valid patch\n")

    assert parse_diff("").raw_file_count == 0


def test_raw_diff_over_10_mib_is_rejected_before_parsing() -> None:
    with pytest.raises(InvalidDiff, match="size"):
        parse_diff("x" * (10 * 1024 * 1024 + 1))


def test_more_than_50_raw_files_is_rejected() -> None:
    raw = "".join(
        f"diff --git a/file_{i}.py b/file_{i}.py\n"
        f"index 1111111..2222222 100644\n"
        f"--- a/file_{i}.py\n"
        f"+++ b/file_{i}.py\n"
        "@@ -1 +1 @@\n-x = 1\n+x = 2\n"
        for i in range(51)
    )

    with pytest.raises(InvalidDiff, match="file limit"):
        parse_diff(raw)


def test_more_than_2000_changed_lines_is_rejected() -> None:
    raw = (
        "diff --git a/x.py b/x.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/x.py\n"
        "+++ b/x.py\n"
        "@@ -1,1001 +1,1001 @@\n" + "-old\n" * 1001 + "+new\n" * 1001
    )

    with pytest.raises(InvalidDiff, match="line limit"):
        parse_diff(raw)


def test_mismatched_git_and_file_headers_are_rejected() -> None:
    raw = """diff --git a/x.py b/x.py
index 1111111..2222222 100644
--- a/other.py
+++ b/x.py
@@ -1 +1 @@
-x = 1
+x = 2
"""

    with pytest.raises(InvalidDiff):
        parse_diff(raw)


def test_generated_marker_skips_file() -> None:
    raw = """diff --git a/src/client.py b/src/client.py
index 1111111..2222222 100644
--- a/src/client.py
+++ b/src/client.py
@@ -1 +1 @@
-# handwritten
+# @generated by codegen
"""

    parsed = parse_diff(raw)

    assert parsed.files == []
    assert parsed.skipped[0]["path"] == "src/client.py"
    assert parsed.skipped[0]["reason"] == "policy"


def test_long_minified_javascript_line_is_skipped() -> None:
    raw = (
        "diff --git a/src/app.js b/src/app.js\n"
        "index 1111111..2222222 100644\n"
        "--- a/src/app.js\n"
        "+++ b/src/app.js\n"
        "@@ -0,0 +1 @@\n"
        "+const a=" + "x" * 600 + ";\n"
    )

    parsed = parse_diff(raw)

    assert parsed.files == []
    assert parsed.skipped[0]["reason"] == "policy"


def test_nonempty_hunk_side_cannot_start_at_zero() -> None:
    raw = """diff --git a/x.py b/x.py
index 1111111..2222222 100644
--- a/x.py
+++ b/x.py
@@ -0,1 +1 @@
-old
+new
"""

    with pytest.raises(InvalidDiff):
        parse_diff(raw)


def test_multiple_hunks_and_no_newline_marker_preserve_coordinates() -> None:
    raw = """diff --git a/x.py b/x.py
index 1111111..2222222 100644
--- a/x.py
+++ b/x.py
@@ -1 +1 @@
-old
+new
@@ -10 +10 @@
-tail
\\ No newline at end of file
+last
\\ No newline at end of file
"""

    parsed = parse_diff(raw)

    hunks = parsed.files[0]["diff"]["hunks"]
    assert [hunk["old_start"] for hunk in hunks] == [1, 10]
    assert hunks[0]["hunk_id"] != hunks[1]["hunk_id"]
    assert hunks[1]["lines"] == [
        {"kind": "deleted", "text": "tail", "old_line": 10, "new_line": None},
        {"kind": "added", "text": "last", "old_line": None, "new_line": 10},
    ]


def test_nonempty_text_without_git_diff_header_is_rejected() -> None:
    with pytest.raises(InvalidDiff):
        parse_diff("unexpected provider response\n")


def test_quoted_path_like_text_inside_hunk_is_not_rewritten() -> None:
    raw = """diff --git a/x.txt b/x.txt
index 1111111..2222222 100644
--- a/x.txt
+++ b/x.txt
@@ -0,0 +1 @@
+++ "a/caf\\303\\251.txt"
"""

    parsed = parse_diff(raw)

    assert (
        parsed.files[0]["diff"]["hunks"][0]["lines"][0]["text"]
        == '++ "a/caf\\303\\251.txt"'
    )


def test_full_blob_shas_are_preserved() -> None:
    old_sha = "a" * 40
    new_sha = "b" * 40
    raw = f"""diff --git a/x.py b/x.py
index {old_sha}..{new_sha} 100644
--- a/x.py
+++ b/x.py
@@ -1 +1 @@
-x = 1
+x = 2
"""

    parsed = parse_diff(raw)

    assert parsed.files[0]["diff"]["old_blob_sha"] == old_sha
    assert parsed.files[0]["diff"]["new_blob_sha"] == new_sha


def test_unexpected_trailing_text_after_valid_patch_is_rejected() -> None:
    raw = """diff --git a/x.py b/x.py
index 1111111..2222222 100644
--- a/x.py
+++ b/x.py
@@ -1 +1 @@
-x = 1
+x = 2
unexpected provider trailer
"""

    with pytest.raises(InvalidDiff):
        parse_diff(raw)


def test_noncanonical_ignore_glob_is_rejected() -> None:
    with pytest.raises(InvalidDiff):
        parse_diff("", ignore_globs=("src/../secret.py",))


def test_quoted_alias_cannot_collide_with_real_path() -> None:
    raw = """diff --git \"a/caf\\303\\251.py\" \"b/caf\\303\\251.py\"
index 1111111..2222222 100644
--- \"a/caf\\303\\251.py\"
+++ \"b/caf\\303\\251.py\"
@@ -1 +1 @@
-x = 1
+x = 2
diff --git a/__git_quoted_1__ b/__git_quoted_1__
index 1111111..2222222 100644
--- a/__git_quoted_1__
+++ b/__git_quoted_1__
@@ -1 +1 @@
-y = 1
+y = 2
"""

    parsed = parse_diff(raw)

    assert [file["diff"]["old_path"] for file in parsed.files] == [
        "café.py",
        "__git_quoted_1__",
    ]
    assert [file["diff"]["new_path"] for file in parsed.files] == [
        "café.py",
        "__git_quoted_1__",
    ]


def test_generated_phrase_in_code_string_does_not_skip_file() -> None:
    raw = """diff --git a/src/client.py b/src/client.py
index 1111111..2222222 100644
--- a/src/client.py
+++ b/src/client.py
@@ -1 +1 @@
-message = "hello"
+message = "do not edit this note"
"""

    parsed = parse_diff(raw)

    assert len(parsed.files) == 1
    assert parsed.skipped == []


def test_generated_filename_is_skipped() -> None:
    raw = """diff --git a/src/client.generated.py b/src/client.generated.py
index 1111111..2222222 100644
--- a/src/client.generated.py
+++ b/src/client.generated.py
@@ -1 +1 @@
-x = 1
+x = 2
"""

    parsed = parse_diff(raw)

    assert parsed.files == []
    assert parsed.skipped[0]["reason"] == "policy"


def test_empty_new_file_metadata_is_preserved_without_hunks() -> None:
    raw = """diff --git a/empty.txt b/empty.txt
new file mode 100644
index 0000000..e69de29
"""

    parsed = parse_diff(raw)

    assert parsed.raw_file_count == 1
    assert parsed.files[0]["diff"]["status"] == "added"
    assert parsed.files[0]["diff"]["old_path"] is None
    assert parsed.files[0]["diff"]["new_path"] == "empty.txt"
    assert parsed.files[0]["diff"]["hunks"] == []


def test_git_quoted_control_character_path_is_rejected() -> None:
    raw = """diff --git \"a/evil\\012name.py\" \"b/evil\\012name.py\"
index 1111111..2222222 100644
--- \"a/evil\\012name.py\"
+++ \"b/evil\\012name.py\"
@@ -1 +1 @@
-x = 1
+x = 2
"""

    with pytest.raises(InvalidDiff):
        parse_diff(raw)


def test_oversized_hunk_coordinate_uses_public_error() -> None:
    raw = (
        "diff --git a/x.py b/x.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/x.py\n"
        "+++ b/x.py\n"
        "@@ -" + "9" * 5000 + " +1 @@\n"
        "-old\n+new\n"
    )

    with pytest.raises(InvalidDiff):
        parse_diff(raw)
