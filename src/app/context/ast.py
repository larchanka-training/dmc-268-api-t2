"""Bounded Tree-sitter extraction around changed lines; never executes code."""

import time
from bisect import bisect_right
from dataclasses import dataclass
from typing import Any

import tree_sitter_go
import tree_sitter_javascript
import tree_sitter_python
import tree_sitter_typescript
from tree_sitter import Language, Node, Parser, Tree

MAX_SOURCE_BYTES = 65536
MAX_NODES = 2048
MAX_WINDOW_SPAN = 80
MAX_EXCERPT_CHARS = 2048
MAX_SIGNATURE_CHARS = 512
MAX_SYMBOLS = 32
MAX_WINDOW_BYTES = 8192
MAX_WINDOWS = 16
_NEWLINE_BYTE = ord("\n")


@dataclass(frozen=True, slots=True)
class ExtractedContext:
    language: str | None
    symbols: list[dict[str, Any]]
    windows: list[dict[str, Any]]
    degraded: bool = False


def extract_context(
    path: str, text: str, revision: str, changed: set[int], *, parse_budget_ms: int = 50
) -> ExtractedContext:
    source = text.encode("utf-8")
    language, grammar = _grammar(path)
    if (
        parse_budget_ms <= 0
        or len(source) > MAX_SOURCE_BYTES
        or not path.endswith(
            (".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".go")
        )
    ):
        windows, _ = _bounded_windows(
            path, text.splitlines(), revision, changed, (min(changed), max(changed))
        )
        return ExtractedContext(None, [], windows, True)
    tree = _parse(source, grammar, parse_budget_ms)
    if tree is None:
        windows, _ = _bounded_windows(
            path, text.splitlines(), revision, changed, (min(changed), max(changed))
        )
        return ExtractedContext(language, [], windows, True)
    line_starts = [
        0,
        *(i + 1 for i, byte in enumerate(source) if byte == _NEWLINE_BYTE),
    ]
    symbols: list[dict[str, Any]] = []
    lines = text.splitlines()
    start, end = min(changed), max(changed)

    def visit(node: Node) -> None:
        nonlocal start, end
        first = bisect_right(line_starts, node.start_byte)
        last = bisect_right(line_starts, max(node.start_byte, node.end_byte - 1))
        kind = {
            "import_declaration": "import",
            "import_statement": "import",
            "import_from_statement": "import",
            "class_definition": "class",
            "class_declaration": "class",
            "function_definition": "function",
            "function_declaration": "function",
            "method_declaration": "function",
            "method_definition": "function",
            "arrow_function": "function",
        }.get(node.type)
        if kind and (
            kind == "import" or any(first <= line <= last for line in changed)
        ):
            name = node.child_by_field_name("name")
            if node.type == "arrow_function" and node.parent is not None:
                name = node.parent.child_by_field_name("name")
            if name is None and node.named_children:
                name = node.named_children[0]
            title = (
                source[name.start_byte : name.end_byte].decode() if name else node.type
            )
            body = node.child_by_field_name("body")
            signature = (
                source[
                    node.start_byte : body.start_byte
                    if body is not None
                    else node.end_byte
                ]
                .decode()
                .strip()
            )
            symbols.append(
                {
                    "qualified_name": title,
                    "kind": kind,
                    "signature": signature,
                    "path": path,
                    "commit_sha": revision,
                    "range": {"start_line": first, "end_line": last},
                    "relation": "import" if kind == "import" else "enclosing",
                    "relation_to_change": "changed_file",
                    "source_excerpt": "\n".join(lines[first - 1 : last]),
                }
            )
            if kind != "import":
                start, end = min(start, first), max(end, last)

    degraded = tree.root_node.has_error or tree.root_node.descendant_count > MAX_NODES
    if not degraded:
        pending = [tree.root_node]
        while pending:
            node = pending.pop()
            visit(node)
            pending.extend(reversed(node.named_children))
    if end - start > MAX_WINDOW_SPAN:
        start, end = min(changed), min(max(changed), min(changed) + MAX_WINDOW_SPAN)
        degraded = True
    for symbol in symbols:
        if (
            len(symbol["source_excerpt"]) > MAX_EXCERPT_CHARS
            or len(symbol["signature"]) > MAX_SIGNATURE_CHARS
        ):
            degraded = True
        symbol["source_excerpt"] = symbol["source_excerpt"][:MAX_EXCERPT_CHARS]
        symbol["signature"] = symbol["signature"][:MAX_SIGNATURE_CHARS]
    if len(symbols) > MAX_SYMBOLS:
        symbols = symbols[:MAX_SYMBOLS]
        degraded = True
    windows, trimmed = _bounded_windows(path, lines, revision, changed, (start, end))
    return ExtractedContext(language, symbols, windows, degraded or trimmed)


def _parse(source: bytes, grammar: Any, parse_budget_ms: int) -> Tree | None:
    deadline = time.monotonic() + min(parse_budget_ms, 1000) / 1000
    parser = Parser(Language(grammar))
    try:
        return parser.parse(
            lambda offset, point: source[offset : offset + 4096],
            progress_callback=lambda offset, has_error: time.monotonic() >= deadline,
        )
    except ValueError:
        # Tree-sitter reports an interrupted parse as ValueError on this API.
        return None


def _grammar(path: str) -> tuple[str, Any]:
    language = "typescript" if path.endswith(".ts") else "python"
    grammar = (
        tree_sitter_typescript.language_typescript()
        if language == "typescript"
        else tree_sitter_python.language()
    )
    if path.endswith(".tsx"):
        language, grammar = "tsx", tree_sitter_typescript.language_tsx()
    if path.endswith((".js", ".jsx", ".mjs", ".cjs")):
        language, grammar = "javascript", tree_sitter_javascript.language()
    if path.endswith(".go"):
        language, grammar = "go", tree_sitter_go.language()
    return language, grammar


def _bounded_windows(
    path: str,
    lines: list[str],
    revision: str,
    changed: set[int],
    bounds: tuple[int, int],
) -> tuple[list[dict[str, Any]], bool]:
    def window(first: int, last: int) -> dict[str, Any]:
        return {
            "path": path,
            "side": "NEW",
            "commit_sha": revision,
            "start_line": first,
            "end_line": last,
            "text": "\n".join(lines[first - 1 : last]),
            "covered_hunk_ids": [],
        }

    start, end = bounds
    text = "\n".join(lines[start - 1 : end])
    if (
        1 <= start <= end <= len(lines)
        and end - start <= MAX_WINDOW_SPAN
        and len(text.encode()) <= MAX_WINDOW_BYTES
    ):
        return [window(start, end)], False
    remaining = MAX_WINDOW_BYTES
    windows: list[dict[str, Any]] = []
    for number in sorted(changed):
        if len(windows) >= MAX_WINDOWS:
            break
        if not 1 <= number <= len(lines):
            continue
        size = len(lines[number - 1].encode())
        if size <= remaining:
            windows.append(window(number, number))
            remaining -= size
    return windows, True
