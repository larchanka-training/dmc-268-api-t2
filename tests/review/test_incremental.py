"""Incremental review: omit diff fragments a prior review already covered."""

from typing import Any

from app.domain.contract_validation import validate_hunks
from app.review.incremental import omit_reviewed, reviewed_fragments
from app.vcs.diff import parse_diff

FUNCTIONS = 10


def _module(changed: int | None = None) -> list[str]:
    lines: list[str] = []
    for n in range(FUNCTIONS):
        body = f"    return x * {n}" if n == changed else f"    return x + {n}"
        lines += [f"def f{n}(x):", body, ""]
    return lines[:-1]


def _added(path: str, lines: list[str]) -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "index 0000000..2222222\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n" + "".join(f"+{line}\n" for line in lines)
    )


def _context(raw: str) -> dict[str, Any]:
    return {"files": parse_diff(raw).files}


def _kept_lines(parsed: Any) -> list[str]:
    return [
        line["text"]
        for file in parsed.files
        for hunk in file["diff"]["hunks"]
        for line in hunk["lines"]
        if line["kind"] != "context"
    ]


def test_without_prior_reviews_the_diff_is_unchanged() -> None:
    parsed = parse_diff(_added("src/m.py", _module()))

    assert omit_reviewed(parsed, frozenset()) == parsed


def test_one_changed_function_of_ten_is_the_only_fragment_kept() -> None:
    """DoD 1 at payload level: only the edited function reaches the LLM."""
    prior = reviewed_fragments([_context(_added("src/m.py", _module()))])

    parsed = omit_reviewed(parse_diff(_added("src/m.py", _module(changed=3))), prior)

    assert _kept_lines(parsed) == ["def f3(x):", "    return x * 3"]
    hunks = parsed.files[0]["diff"]["hunks"]
    validate_hunks(hunks)
    assert (hunks[0]["new_start"], hunks[0]["new_count"]) == (10, 2)
    assert parsed.coverage["included"] == [
        {
            "path": "src/m.py",
            "ranges": [
                {"start_line": 10, "end_line": 10},
                {"start_line": 11, "end_line": 11},
            ],
        }
    ]
    assert [item["detail"] for item in parsed.skipped] == ["already_reviewed"]
    assert parsed.coverage["skipped"][0]["reason"] == "policy"


def test_rebase_without_code_changes_keeps_nothing() -> None:
    """DoD 5: identical code (CRLF and trailing spaces normalized) is omitted."""
    prior = reviewed_fragments([_context(_added("src/m.py", _module()))])
    rewritten = [f"{line}  \r" if line else line for line in _module()]

    parsed = omit_reviewed(parse_diff(_added("src/m.py", rewritten)), prior)

    assert parsed.files == []
    assert [item["detail"] for item in parsed.skipped] == ["already_reviewed"]


def test_unchanged_file_is_dropped_and_changed_hunk_of_other_file_kept() -> None:
    """DoD 5: force-push with one changed hunk sends only that hunk."""
    first = _added("src/a.py", _module()) + _added("src/b.py", ["value = 1"])
    second = _added("src/a.py", _module()) + _added("src/b.py", ["value = 2"])
    prior = reviewed_fragments([_context(first)])

    parsed = omit_reviewed(parse_diff(second), prior)

    assert [file["diff"]["new_path"] for file in parsed.files] == ["src/b.py"]
    assert _kept_lines(parsed) == ["value = 2"]
    assert {item["path"] for item in parsed.skipped} == {"src/a.py"}


def test_identical_fragments_in_one_hunk_are_kept_independently() -> None:
    twins = ["def f(x):", "    return x", "", "def f(x):", "    return x"]
    prior = reviewed_fragments([_context(_added("src/old.py", ["value = 1"]))])

    parsed = omit_reviewed(parse_diff(_added("src/t.py", twins)), prior)

    assert _kept_lines(parsed) == ["def f(x):", "    return x"] * 2
    assert parsed.skipped == []
    validate_hunks(parsed.files[0]["diff"]["hunks"])


def test_modified_file_fragment_identity_ignores_hunk_position() -> None:
    def modified(start: int, new: str) -> str:
        return (
            "diff --git a/src/c.py b/src/c.py\n"
            "index 1111111..2222222 100644\n"
            "--- a/src/c.py\n"
            "+++ b/src/c.py\n"
            f"@@ -{start},3 +{start},3 @@\n"
            " def g(x):\n"
            "-    return x\n"
            f"+    {new}\n"
            "     # end\n"
        )

    prior = reviewed_fragments([_context(modified(10, "return x + 1"))])

    assert omit_reviewed(parse_diff(modified(40, "return x + 1")), prior).files == []
    kept = omit_reviewed(parse_diff(modified(40, "return x + 2")), prior)
    assert _kept_lines(kept) == ["    return x", "    return x + 2"]
    validate_hunks(kept.files[0]["diff"]["hunks"])
