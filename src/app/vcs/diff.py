import codecs
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from unidiff import PatchSet  # type: ignore[import-untyped]
from unidiff.errors import UnidiffParseError  # type: ignore[import-untyped]

from app.domain.contract_validation import validate_hunks


class InvalidDiff(ValueError):  # noqa: N818 - public parser exception
    pass


_GIT_TOKEN = r'"(?:\\.|[^"\\])*"|[^ \t\n]+'
_GIT_HEADER = re.compile(rf"^diff --git ({_GIT_TOKEN}) ({_GIT_TOKEN})\n?$")
_FILE_HEADER = re.compile(r'^([+-]{3}) ("(?:\\.|[^"\\])*"|[^\t\n]+)(\t[^\n]+)?\n?$')
_GIT_ESCAPE = re.compile(r'\\(?:[0-7]{3}|[abfnrtv"\\])')
_INDEX = re.compile(r"^index ([0-9a-fA-F]+)\.\.([0-9a-fA-F]+)(?: \d+)?\n?$")
_EXCLUDED_DIRS = frozenset({"dist", "vendor", "node_modules"})
_LOCK_FILES = frozenset(
    {
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "Cargo.lock",
        "poetry.lock",
        "uv.lock",
    }
)
_METADATA_PREFIXES = (
    "index ",
    "new file mode ",
    "deleted file mode ",
    "old mode ",
    "new mode ",
    "similarity index ",
    "dissimilarity index ",
    "rename from ",
    "rename to ",
    "copy from ",
    "copy to ",
    "Binary files ",
)
_MAX_RAW_BYTES = 10 * 1024 * 1024
_MAX_FILES = 50
_MAX_CHANGED_LINES = 2000
_GENERATED_HEADER_LINES = 10
_MINIFIED_LINE_LENGTH = 500
_CONTROL_BOUNDARY = 32
_DELETE_CONTROL = 127


def _validate_structure(raw_diff: str) -> None:  # noqa: PLR0912 - patch grammar states
    state = "start"
    for line in raw_diff.splitlines():
        if not line and state == "start":
            continue
        if line.startswith("diff --git "):
            if not _GIT_HEADER.fullmatch(line):
                raise InvalidDiff("malformed Git diff header")
            state = "metadata"
        elif state == "binary":
            continue
        elif state == "metadata":
            if line.startswith("GIT binary patch"):
                state = "binary"
            elif line.startswith("@@ -"):
                state = "hunk"
            elif line.startswith(_METADATA_PREFIXES) or line.startswith(
                ("--- ", "+++ ")
            ):
                continue
            else:
                raise InvalidDiff("unexpected diff metadata")
        elif state == "hunk":
            if line.startswith("@@ -") or line.startswith((" ", "+", "-")):
                continue
            if line == "\\ No newline at end of file":
                continue
            raise InvalidDiff("unexpected diff trailer")
        else:
            raise InvalidDiff("unexpected diff content")


def _quoted_path(token: str) -> str:
    if not (token.startswith('"') and token.endswith('"')):
        return token
    value = token[1:-1]
    if _GIT_ESCAPE.sub("", value).find("\\") >= 0:
        raise InvalidDiff("invalid quoted diff path")
    try:
        return codecs.escape_decode(value.encode("utf-8"))[0].decode("utf-8")
    except UnicodeError as exc:
        raise InvalidDiff("invalid quoted diff path") from exc


def _normalize_headers(raw_diff: str) -> tuple[str, dict[str, str]]:
    aliases: dict[str, str] = {}
    by_path: dict[str, str] = {}

    def alias(token: str) -> str:
        decoded = _quoted_path(token)
        if decoded == token:
            return token
        if decoded not in by_path:
            candidate = len(by_path) + 1
            replacement = f"{decoded[:2]}__git_quoted_{candidate}__"
            while replacement in raw_diff or replacement in aliases:
                candidate += 1
                replacement = f"{decoded[:2]}__git_quoted_{candidate}__"
            by_path[decoded] = replacement
            aliases[replacement] = decoded
        return by_path[decoded]

    output = []
    in_hunk = False
    for line in raw_diff.splitlines(keepends=True):
        header = _GIT_HEADER.fullmatch(line)
        if header:
            in_hunk = False
            output.append(f"diff --git {alias(header[1])} {alias(header[2])}\n")
            continue
        if line.startswith("@@ "):
            in_hunk = True
        if in_hunk:
            output.append(line)
            continue
        file_header = _FILE_HEADER.fullmatch(line)
        if file_header:
            output.append(
                f"{file_header[1]} {alias(file_header[2])}{file_header[3] or ''}\n"
            )
            continue
        output.append(line)
    return "".join(output), aliases


def _path(value: str, prefix: str, aliases: dict[str, str]) -> str:
    value = aliases.get(value, value)
    if not value.startswith(prefix):
        raise InvalidDiff("invalid diff path")
    path = value[len(prefix) :]
    if (
        not path
        or "\\" in path
        or "\x00" in path
        or any(
            ord(char) < _CONTROL_BOUNDARY or ord(char) == _DELETE_CONTROL
            for char in path
        )
        or any(segment in ("", ".", "..") for segment in path.split("/"))
    ):
        raise InvalidDiff("invalid diff path")
    return path


def _default_policy(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return (
        bool(_EXCLUDED_DIRS.intersection(parts[:-1]))
        or parts[-1] in _LOCK_FILES
        or path.endswith(".min.js")
        or ".generated." in parts[-1]
    )


def _generated(hunks: list[dict[str, Any]]) -> bool:
    return any(
        marker in line["text"].lower()
        for hunk in hunks
        for line in hunk["lines"]
        if line["new_line"] is not None and line["new_line"] <= _GENERATED_HEADER_LINES
        if line["text"].lstrip().startswith(("#", "//", "/*", "*", "<!--"))
        for marker in ("@generated", "do not edit", "generated by")
    )


def _minified_js(path: str, hunks: list[dict[str, Any]]) -> bool:
    if not path.endswith((".js", ".mjs", ".cjs")):
        return False
    return any(
        len(line["text"]) > _MINIFIED_LINE_LENGTH
        and line["text"].count(" ") * 20 < len(line["text"])
        for hunk in hunks
        for line in hunk["lines"]
        if line["kind"] == "added"
    )


def _blob_shas(patch_info: Sequence[str]) -> tuple[str | None, str | None]:
    for line in patch_info:
        match = _INDEX.fullmatch(line)
        if match:
            return tuple(
                value.lower()
                if len(value) in (40, 64) and set(value) != {"0"}
                else None
                for value in match.groups()
            )  # type: ignore[return-value]
    return None, None


@dataclass(frozen=True, slots=True)
class ParsedDiff:
    files: list[dict[str, Any]]
    skipped: list[dict[str, Any]]
    raw_file_count: int

    @property
    def coverage(self) -> dict[str, Any]:
        included: list[dict[str, Any]] = []
        for file in self.files:
            diff = file["diff"]
            path = diff["new_path"] or diff["old_path"]
            deleted = diff["status"] == "deleted"
            ranges = [
                {
                    "start_line": line["old_line"] if deleted else line["new_line"],
                    "end_line": line["old_line"] if deleted else line["new_line"],
                }
                for hunk in diff["hunks"]
                for line in hunk["lines"]
                if line["kind"] == ("deleted" if deleted else "added")
            ]
            included.append({"path": path, "ranges": ranges})
        return {
            "included": included,
            "skipped": self.skipped,
            "context_quality": "diff_only",
        }


def parse_diff(  # noqa: PLR0912 - bounded patch-to-context mapping
    raw_diff: str, *, ignore_globs: Sequence[str] = ()
) -> ParsedDiff:
    for glob in ignore_globs:
        if glob.startswith("!") or not glob.strip():
            raise InvalidDiff("invalid ignore glob")
        _path(f"g/{glob}", "g/", {})
    if len(raw_diff.encode("utf-8")) > _MAX_RAW_BYTES:
        raise InvalidDiff("diff size exceeds 10 MiB")
    _validate_structure(raw_diff)
    normalized, aliases = _normalize_headers(raw_diff)
    try:
        patches = PatchSet(normalized)
    except (UnidiffParseError, ValueError) as exc:
        raise InvalidDiff("malformed unified diff") from exc
    if raw_diff.strip() and not patches:
        raise InvalidDiff("missing Git diff headers")
    if len(patches) > _MAX_FILES:
        raise InvalidDiff("diff file limit exceeded")
    if (
        sum(
            1
            for patch in patches
            for hunk in patch
            for line in hunk
            if line.is_added or line.is_removed
        )
        > _MAX_CHANGED_LINES
    ):
        raise InvalidDiff("diff line limit exceeded")
    files: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for file_index, patch in enumerate(patches, 1):
        if (
            not patch
            and not patch.is_binary_file
            and not any(
                line.startswith(
                    (
                        "rename from ",
                        "rename to ",
                        "old mode ",
                        "new mode ",
                        "new file mode ",
                        "deleted file mode ",
                    )
                )
                for line in patch.patch_info
            )
        ):
            raise InvalidDiff("malformed metadata-only diff")
        is_added = patch.is_added_file
        is_deleted = patch.is_removed_file
        old_path = None if is_added else _path(patch.source_file, "a/", aliases)
        new_path = None if is_deleted else _path(patch.target_file, "b/", aliases)
        old_blob_sha, new_blob_sha = _blob_shas(patch.patch_info)
        path = new_path or old_path
        hunks: list[dict[str, Any]] = []
        for hunk_index, hunk in enumerate(patch, 1):
            lines = []
            for line in hunk:
                if line.is_added:
                    kind = "added"
                elif line.is_removed:
                    kind = "deleted"
                elif line.is_context:
                    kind = "context"
                else:
                    continue
                lines.append(
                    {
                        "kind": kind,
                        "text": line.value.removesuffix("\n").removesuffix("\r"),
                        "old_line": line.source_line_no,
                        "new_line": line.target_line_no,
                    }
                )
            hunks.append(
                {
                    "hunk_id": f"file_{file_index}_hunk_{hunk_index}",
                    "old_start": hunk.source_start,
                    "old_count": hunk.source_length,
                    "new_start": hunk.target_start,
                    "new_count": hunk.target_length,
                    "lines": lines,
                }
            )
        try:
            validate_hunks(hunks)
        except ValueError as exc:
            raise InvalidDiff("invalid hunk coordinates") from exc
        if any(
            hunk[f"{side}_count"] and hunk[f"{side}_start"] < 1
            for hunk in hunks
            for side in ("old", "new")
        ):
            raise InvalidDiff("invalid hunk start")
        if path is not None and (
            patch.is_binary_file
            or _default_policy(path)
            or _generated(hunks)
            or _minified_js(path, hunks)
            or any(PurePosixPath(path).full_match(glob) for glob in ignore_globs)
        ):
            skipped.append(
                {
                    "path": path,
                    "ranges": [
                        {
                            "start_line": line["old_line"]
                            if is_deleted
                            else line["new_line"],
                            "end_line": line["old_line"]
                            if is_deleted
                            else line["new_line"],
                        }
                        for hunk in hunks
                        for line in hunk["lines"]
                        if line["kind"] == ("deleted" if is_deleted else "added")
                    ],
                    "reason": "policy",
                }
            )
            continue
        files.append(
            {
                "diff": {
                    "file_id": f"file_{file_index}",
                    "old_path": old_path,
                    "new_path": new_path,
                    "status": (
                        "added"
                        if is_added
                        else "deleted"
                        if is_deleted
                        else "renamed"
                        if old_path != new_path
                        else "modified"
                    ),
                    "old_blob_sha": old_blob_sha,
                    "new_blob_sha": new_blob_sha,
                    "language": None,
                    "hunks": hunks,
                },
                "source_windows": [],
                "whole_file": None,
            }
        )
    return ParsedDiff(files=files, skipped=skipped, raw_file_count=len(patches))
