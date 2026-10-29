"""Spike 1: unified diff -> line label table (ADR-8).

Labels shown to the model, unique inside one file:

* ``R<n>``       added line, ``n`` is the NEW line number (GitHub side RIGHT);
* ``L<n>``       deleted line, ``n`` is the OLD line number (GitHub side LEFT);
* ``C<new>/<old>`` context line, both numbers.

The model returns ``path`` + labels; ``resolve`` turns them into coordinates, so
the model never has to decide OLD vs NEW itself.
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

from unidiff import PatchSet

Side = Literal["OLD", "NEW"]
_LABEL = re.compile(r"^(?:R(\d+)|L(\d+)|C(\d+)/(\d+))$")
_ESCAPE = re.compile(r'\\([0-7]{3}|[abfnrtv"\\])')
_SIMPLE = {"a": 7, "b": 8, "f": 12, "n": 10, "r": 13, "t": 9, "v": 11, '"': 34, "\\": 92}
_CONTROL_LIMIT = 32


class InvalidDiff(ValueError):  # noqa: N818 - spike exception
    pass


class Unresolvable(ValueError):  # noqa: N818 - carries a machine-readable reason
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Kind(StrEnum):
    ADDED = "added"
    DELETED = "deleted"
    CONTEXT = "context"


@dataclass(frozen=True)
class DiffLine:
    label: str
    kind: Kind
    old_line: int | None
    new_line: int | None
    text: str
    hunk_index: int
    seq: int
    # GitHub legacy ``position``: lines below the first "@@" header (1-based);
    # later "@@" headers and "\ No newline" markers count as lines.
    position: int
    no_eol: bool = False


@dataclass
class FileTable:
    old_path: str | None
    new_path: str | None
    status: str  # added | deleted | modified | renamed
    is_binary: bool
    lines: dict[str, DiffLine] = field(default_factory=dict)

    @property
    def path(self) -> str:
        return self.new_path or self.old_path or ""


@dataclass(frozen=True)
class Anchor:
    old_path: str | None
    new_path: str | None
    side: Side
    start: DiffLine
    end: DiffLine

    @property
    def file_path(self) -> str:
        """Path the VCS knows the file by: new path, old path for deletions."""
        return self.new_path or self.old_path or ""

    @property
    def start_line(self) -> int:
        return _number(self.start, self.side)

    @property
    def end_line(self) -> int:
        return _number(self.end, self.side)

    @property
    def is_range(self) -> bool:
        return self.start_line != self.end_line


def _number(line: DiffLine, side: Side) -> int:
    value = line.new_line if side == "NEW" else line.old_line
    if value is None:  # guarded by resolve()
        raise Unresolvable("line_not_on_side")
    return value


def _unquote(token: str) -> str:
    if not (len(token) > 1 and token.startswith('"') and token.endswith('"')):
        return token
    body, raw, pos = token[1:-1], bytearray(), 0
    for match in _ESCAPE.finditer(body):
        raw += body[pos : match.start()].encode()
        esc = match.group(1)
        raw.append(int(esc, 8) if esc.isdigit() and len(esc) == 3 else _SIMPLE[esc])
        pos = match.end()
    raw += body[pos:].encode()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidDiff("invalid quoted path") from exc


def _path(token: str, prefix: str) -> str | None:
    token = _unquote(token)
    if token == "/dev/null":
        return None
    if not token.startswith(prefix):
        raise InvalidDiff("unexpected path prefix")
    path = token[len(prefix) :]
    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or any(ord(c) < _CONTROL_LIMIT or ord(c) == 127 for c in path)
        or any(part in ("", ".", "..") for part in path.split("/"))
    ):
        raise InvalidDiff("invalid diff path")
    return path


def build_label_tables(raw_diff: str) -> list[FileTable]:
    try:
        patches = PatchSet(raw_diff)
    except Exception as exc:  # unidiff raises several types
        raise InvalidDiff("malformed unified diff") from exc
    tables: list[FileTable] = []
    for patch in patches:
        old = _path(patch.source_file, "a/")
        new = _path(patch.target_file, "b/")
        if patch.is_added_file:
            status = "added"
        elif patch.is_removed_file:
            status = "deleted"
        else:
            status = "renamed" if old != new else "modified"
        table = FileTable(old, new, status, bool(patch.is_binary_file))
        position = 0
        seq = 0
        last_label: str | None = None
        for hunk_index, hunk in enumerate(patch, 1):
            if hunk_index > 1:
                position += 1  # a later "@@" header is itself a counted line
            for raw in hunk:
                position += 1
                if raw.line_type == "\\":
                    if last_label is not None:
                        prev = table.lines[last_label]
                        table.lines[last_label] = dataclasses.replace(prev, no_eol=True)
                    continue
                text = raw.value.removesuffix("\n").removesuffix("\r")
                if raw.line_type == "+":
                    kind, label = Kind.ADDED, f"R{raw.target_line_no}"
                elif raw.line_type == "-":
                    kind, label = Kind.DELETED, f"L{raw.source_line_no}"
                else:
                    kind = Kind.CONTEXT
                    label = f"C{raw.target_line_no}/{raw.source_line_no}"
                table.lines[label] = DiffLine(
                    label=label,
                    kind=kind,
                    old_line=raw.source_line_no,
                    new_line=raw.target_line_no,
                    text=text,
                    hunk_index=hunk_index,
                    seq=seq,
                    position=position,
                )
                seq += 1
                last_label = label
        tables.append(table)
    return tables


_MARKER = {Kind.ADDED: "+", Kind.DELETED: "-", Kind.CONTEXT: " "}


def render_for_prompt(tables: list[FileTable]) -> str:
    out: list[str] = []
    for table in tables:
        head = f"### FILE {table.path} [{table.status}]"
        if table.is_binary:
            out.append(f"{head} binary: content not shown, no labels")
            continue
        if table.status == "renamed" and table.old_path:
            head += f" renamed from {table.old_path}"
        out.append(head)
        if not table.lines:
            out.append("(no content changes: nothing to anchor a comment to)")
            continue
        current = 0
        for line in table.lines.values():
            if line.hunk_index != current:
                current = line.hunk_index
                out.append(f"@@ hunk {current}")
            out.append(f"{line.label:<12}{_MARKER[line.kind]}{line.text}")
    return "\n".join(out)


def resolve(
    tables: list[FileTable],
    path: str,
    start_label: str,
    end_label: str | None = None,
    *,
    allow_context_start: bool = False,
) -> Anchor:
    """Label(s) -> Anchor, or ``Unresolvable(reason)``. Never guesses a neighbour."""
    end_label = end_label or start_label
    for label in (start_label, end_label):
        if not _LABEL.fullmatch(label):
            raise Unresolvable("bad_label_syntax")
    candidates = [t for t in tables if path in (t.new_path, t.old_path)]
    if not candidates:
        raise Unresolvable("unknown_file")
    first_error: Unresolvable | None = None
    for table in candidates:
        try:
            return _resolve_in(table, path, start_label, end_label, allow_context_start)
        except Unresolvable as exc:
            first_error = first_error or exc
    assert first_error is not None
    raise first_error


def _resolve_in(
    table: FileTable, path: str, start_label: str, end_label: str, allow_context: bool
) -> Anchor:
    if table.is_binary:
        raise Unresolvable("binary_file")
    start, end = table.lines.get(start_label), table.lines.get(end_label)
    if start is None or end is None:
        raise Unresolvable("unknown_label")
    if start.kind is Kind.CONTEXT and not allow_context:
        raise Unresolvable("context_start")
    if start.seq > end.seq:
        raise Unresolvable("reversed_range")
    if start.hunk_index != end.hunk_index:
        raise Unresolvable("cross_hunk")
    kinds = {start.kind, end.kind}
    if Kind.ADDED in kinds and Kind.DELETED in kinds:
        raise Unresolvable("mixed_sides")
    side: Side = "OLD" if Kind.DELETED in kinds else "NEW"
    if side == "NEW" and table.new_path != path:
        raise Unresolvable("path_side_mismatch")
    if side == "OLD" and table.old_path != path:
        raise Unresolvable("path_side_mismatch")
    return Anchor(table.old_path, table.new_path, side, start, end)
