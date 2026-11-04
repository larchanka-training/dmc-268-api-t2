"""Prioritization and chunking (SD §6 steps 2-5, spec §5.2).

Hidden truncation is forbidden: whatever does not fit ends up in ``skipped``
with a reason.
"""

from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import PurePosixPath

from app.llm.tokens import TokenCounter
from app.review.diff_render import render_file_diff
from app.review.filtering import file_path
from app.review.schemas.context import (
    FileDiff,
    Hunk,
    HunkLine,
    LineRange,
    SkippedItem,
    SkipReason,
)


class Priority(IntEnum):
    CODE = 0
    CONFIG = 1
    DOCS = 2


_DOC_SUFFIXES = frozenset({".md", ".rst", ".txt", ".adoc"})
_CONFIG_SUFFIXES = frozenset(
    {".yml", ".yaml", ".toml", ".json", ".ini", ".cfg", ".conf", ".env", ".xml"}
)
_CONFIG_NAMES = frozenset({"Dockerfile", "Makefile", ".env.example"})


def priority_of(path: str) -> Priority:
    """Changed executable code first; unknown types count as code."""
    pure = PurePosixPath(path)
    if pure.suffix.lower() in _DOC_SUFFIXES:
        return Priority.DOCS
    if pure.suffix.lower() in _CONFIG_SUFFIXES or pure.name in _CONFIG_NAMES:
        return Priority.CONFIG
    return Priority.CODE


@dataclass(frozen=True)
class Chunk:
    files: list[FileDiff]
    estimated_tokens: int


@dataclass(frozen=True)
class ChunkPlan:
    chunks: list[Chunk] = field(default_factory=list)
    skipped: list[SkippedItem] = field(default_factory=list)


@dataclass(frozen=True)
class ChunkLimits:
    max_chunk_tokens: int  # diff tokens per chunk
    max_chunks: int = 5
    max_files: int = 50  # admission limits, SD §14
    max_changed_lines: int = 2000


def plan_chunks(
    files: list[FileDiff], counter: TokenCounter, limits: ChunkLimits
) -> ChunkPlan:
    """Plan chunks within per-chunk tokens and run admission limits (SD §14)."""
    max_chunk_tokens, max_chunks = limits.max_chunk_tokens, limits.max_chunks
    ordered = sorted(files, key=lambda f: (priority_of(file_path(f)), file_path(f)))
    admitted, skipped = _admit(ordered, limits.max_files, limits.max_changed_lines)
    units = [
        piece
        for diff in admitted
        for piece in _fit_file(diff, counter=counter, limit=max_chunk_tokens)
    ]
    too_big = [diff for diff, tokens in units if tokens > max_chunk_tokens]
    units = [(diff, tokens) for diff, tokens in units if tokens <= max_chunk_tokens]
    chunks: list[Chunk] = []
    current: list[FileDiff] = []
    current_tokens = 0
    current_priority: Priority | None = None
    for diff, tokens in units:
        priority = priority_of(file_path(diff))
        if current and (
            priority != current_priority or current_tokens + tokens > max_chunk_tokens
        ):
            chunks.append(Chunk(current, current_tokens))
            current, current_tokens = [], 0
        current.append(diff)
        current_tokens += tokens
        current_priority = priority
    if current:
        chunks.append(Chunk(current, current_tokens))
    overflow = [diff for chunk in chunks[max_chunks:] for diff in chunk.files]
    return ChunkPlan(
        chunks=chunks[:max_chunks],
        skipped=[*skipped, *_skip([*too_big, *overflow], SkipReason.TOKEN_BUDGET)],
    )


def _admit(
    ordered: list[FileDiff], max_files: int, max_changed_lines: int
) -> tuple[list[FileDiff], list[SkippedItem]]:
    admitted: list[FileDiff] = []
    skipped: list[SkippedItem] = []
    changed_lines = 0
    for diff in ordered:
        lines = sum(1 for h in diff.hunks for ln in h.lines if ln.kind != "context")
        if len(admitted) >= max_files:
            skipped.extend(_skip([diff], SkipReason.FILE_LIMIT))
        elif changed_lines + lines > max_changed_lines:
            skipped.extend(_skip([diff], SkipReason.LINE_LIMIT))
        else:
            admitted.append(diff)
            changed_lines += lines
    return admitted, skipped


def changed_ranges(diff: FileDiff) -> list[LineRange]:
    """Contiguous changed lines: added lines on the new side, or deleted lines
    on the old side for a file without additions."""
    added = [ln.new_line for h in diff.hunks for ln in h.lines if ln.kind == "added"]
    deleted = [
        ln.old_line for h in diff.hunks for ln in h.lines if ln.kind == "deleted"
    ]
    numbers = [n for n in (added or deleted) if n is not None]
    ranges: list[LineRange] = []
    for number in numbers:
        if ranges and ranges[-1].end_line + 1 == number:
            ranges[-1] = LineRange(start_line=ranges[-1].start_line, end_line=number)
        else:
            ranges.append(LineRange(start_line=number, end_line=number))
    return ranges


def _skip(files: list[FileDiff], reason: SkipReason) -> list[SkippedItem]:
    """One skipped item per path, merging the pieces of split files."""
    by_path: dict[str, list[LineRange]] = {}
    for diff in files:
        by_path.setdefault(file_path(diff), []).extend(changed_ranges(diff))
    return [
        SkippedItem(path=path, ranges=ranges, reason=reason)
        for path, ranges in by_path.items()
    ]


def _fit_file(
    diff: FileDiff, *, counter: TokenCounter, limit: int
) -> list[tuple[FileDiff, int]]:
    """Split a file that exceeds ``limit`` into pieces of consecutive hunks."""
    tokens = counter.count(render_file_diff(diff))
    if tokens <= limit:
        return [(diff, tokens)]
    pieces: list[tuple[FileDiff, int]] = []
    hunks: list[Hunk] = []
    fitted = [
        part
        for hunk in diff.hunks
        for part in _fit_hunk(diff, hunk, counter=counter, limit=limit)
    ]
    for hunk in fitted:
        candidate = diff.model_copy(update={"hunks": [*hunks, hunk]})
        if hunks and counter.count(render_file_diff(candidate)) > limit:
            piece = diff.model_copy(update={"hunks": hunks})
            pieces.append((piece, counter.count(render_file_diff(piece))))
            hunks = []
        hunks.append(hunk)
    if hunks:
        piece = diff.model_copy(update={"hunks": hunks})
        pieces.append((piece, counter.count(render_file_diff(piece))))
    return pieces


def _fit_hunk(
    diff: FileDiff, hunk: Hunk, *, counter: TokenCounter, limit: int
) -> list[Hunk]:
    """Split a hunk that alone exceeds ``limit`` into consecutive line ranges.

    Original line numbers are kept; starts and counts are recomputed so every
    part is a valid ``Hunk``.
    """

    def size(lines: list[HunkLine]) -> int:
        part = _sub_hunk(hunk, lines, part_no=1)
        return counter.count(
            render_file_diff(diff.model_copy(update={"hunks": [part]}))
        )

    if size(list(hunk.lines)) <= limit:
        return [hunk]
    groups: list[list[HunkLine]] = [[]]
    for line in hunk.lines:
        if groups[-1] and size([*groups[-1], line]) > limit:
            groups.append([])
        groups[-1].append(line)
    parts: list[Hunk] = []
    for number, lines in enumerate(groups, start=1):
        parts.append(_sub_hunk(hunk, lines, part_no=number))
    return parts


def _sub_hunk(hunk: Hunk, lines: list[HunkLine], *, part_no: int) -> Hunk:
    old = [ln.old_line for ln in lines if ln.old_line is not None]
    new = [ln.new_line for ln in lines if ln.new_line is not None]
    return Hunk(
        hunk_id=f"{hunk.hunk_id}:{part_no}",
        old_start=old[0] if old else _start_before(hunk, lines[0], "old_line"),
        old_count=len(old),
        new_start=new[0] if new else _start_before(hunk, lines[0], "new_line"),
        new_count=len(new),
        lines=lines,
    )


def _start_before(hunk: Hunk, first: HunkLine, side: str) -> int:
    """Unified-diff start for a side with no lines in the part: the last line of
    that side before the part (or the hunk's own start)."""
    last = getattr(hunk, side.replace("line", "start"))
    for line in hunk.lines:
        if line is first:
            break
        last = getattr(line, side) or last
    return int(last)
