"""Prioritization and chunking (SD §6 steps 2-5, spec §5.2)."""

from collections.abc import Callable

from app.review.chunking import ChunkLimits, plan_chunks
from app.review.filtering import file_path
from app.review.schemas.context import FileDiff, SkipReason

MakeDiff = Callable[..., FileDiff]


class LineCounter:
    """One token per rendered line: keeps expected sizes easy to reason about."""

    def count(self, text: str) -> int:
        return len(text.splitlines())


def _paths(files: list[FileDiff]) -> list[str]:
    return [file_path(f) for f in files]


def test_small_files_of_one_priority_share_a_chunk(make_diff: MakeDiff) -> None:
    files = [make_diff("src/b.py"), make_diff("src/a.py")]

    plan = plan_chunks(files, LineCounter(), ChunkLimits(max_chunk_tokens=100))

    assert [_paths(c.files) for c in plan.chunks] == [["src/a.py", "src/b.py"]]
    assert plan.skipped == []


def test_code_then_config_then_docs_in_separate_chunks(make_diff: MakeDiff) -> None:
    files = [
        make_diff("README.md"),
        make_diff("docker-compose.yml"),
        make_diff("src/app.py"),
        make_diff("pyproject.toml"),
    ]

    plan = plan_chunks(files, LineCounter(), ChunkLimits(max_chunk_tokens=100))

    assert [_paths(c.files) for c in plan.chunks] == [
        ["src/app.py"],
        ["docker-compose.yml", "pyproject.toml"],
        ["README.md"],
    ]


def test_files_that_do_not_fit_start_a_new_chunk(make_diff: MakeDiff) -> None:
    # Each file renders to 1 file header + 1 hunk header + 3 lines = 5 tokens.
    files = [make_diff(f"src/m{i}.py", added=3) for i in range(3)]

    plan = plan_chunks(files, LineCounter(), ChunkLimits(max_chunk_tokens=10))

    assert [_paths(c.files) for c in plan.chunks] == [
        ["src/m0.py", "src/m1.py"],
        ["src/m2.py"],
    ]
    assert [c.estimated_tokens for c in plan.chunks] == [10, 5]


def test_oversized_file_is_split_by_hunks(make_diff: MakeDiff) -> None:
    # Whole file: 1 + 2 * (1 + 3) = 9 tokens; one hunk with header: 5 tokens.
    big = make_diff("src/big.py", added=3, hunks=2)

    plan = plan_chunks([big], LineCounter(), ChunkLimits(max_chunk_tokens=6))

    parts = [c.files for c in plan.chunks]
    assert [[h.hunk_id for f in files for h in f.hunks] for files in parts] == [
        ["src/big.py#h0"],
        ["src/big.py#h1"],
    ]
    assert {f.file_id for files in parts for f in files} == {"file:src/big.py"}


def test_oversized_hunk_is_split_into_line_ranges(make_diff: MakeDiff) -> None:
    # Limit 5 = file header + hunk header + 3 lines.
    big = make_diff("src/big.py", added=6, start=10)

    plan = plan_chunks([big], LineCounter(), ChunkLimits(max_chunk_tokens=5))

    hunks = [h for c in plan.chunks for f in c.files for h in f.hunks]
    assert [
        (h.hunk_id, h.new_start, h.new_count, [ln.new_line for ln in h.lines])
        for h in hunks
    ] == [
        ("src/big.py#h0:1", 10, 3, [10, 11, 12]),
        ("src/big.py#h0:2", 13, 3, [13, 14, 15]),
    ]
    assert plan.skipped == []


def test_changes_beyond_max_chunks_are_skipped_with_ranges(
    make_diff: MakeDiff,
) -> None:
    files = [make_diff(f"src/m{i}.py", added=3, start=20) for i in range(4)]

    plan = plan_chunks(
        files, LineCounter(), ChunkLimits(max_chunk_tokens=5, max_chunks=2)
    )

    assert [_paths(c.files) for c in plan.chunks] == [["src/m0.py"], ["src/m1.py"]]
    assert [
        (s.path, s.reason, [(r.start_line, r.end_line) for r in s.ranges])
        for s in plan.skipped
    ] == [
        ("src/m2.py", SkipReason.TOKEN_BUDGET, [(20, 22)]),
        ("src/m3.py", SkipReason.TOKEN_BUDGET, [(20, 22)]),
    ]


def test_admission_limits_skip_files_in_priority_order(make_diff: MakeDiff) -> None:
    files = [
        make_diff("README.md", added=1),
        make_diff("src/a.py", added=2),
        make_diff("src/b.py", added=2),
        make_diff("src/c.py", added=2),
    ]

    by_files = plan_chunks(
        files, LineCounter(), ChunkLimits(max_chunk_tokens=100, max_files=3)
    )
    by_lines = plan_chunks(
        files, LineCounter(), ChunkLimits(max_chunk_tokens=100, max_changed_lines=5)
    )

    assert [(s.path, s.reason) for s in by_files.skipped] == [
        ("README.md", SkipReason.FILE_LIMIT)
    ]
    # README.md (1 line) still fits after src/c.py is skipped.
    assert [(s.path, s.reason) for s in by_lines.skipped] == [
        ("src/c.py", SkipReason.LINE_LIMIT)
    ]


def test_single_line_larger_than_a_chunk_is_skipped_not_truncated(
    make_diff: MakeDiff,
) -> None:
    class ByteCounter:
        def count(self, text: str) -> int:
            return len(text)

    huge = make_diff("src/huge.py", added=1, start=7, text="x" * 500)

    plan = plan_chunks([huge], ByteCounter(), ChunkLimits(max_chunk_tokens=100))

    assert plan.chunks == []
    assert [(s.path, s.reason) for s in plan.skipped] == [
        ("src/huge.py", SkipReason.TOKEN_BUDGET)
    ]
