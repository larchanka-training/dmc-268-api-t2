"""Spike 3b: ``python -m review_spikes.lint [--strict] FILE`` (CI linter prototype).

Exit codes: 0 clean (warnings allowed unless --strict), 1 rule errors,
2 file rejected as a whole (size/encoding/frontmatter), 64 usage.
"""

from __future__ import annotations

import sys
from pathlib import Path

from review_spikes.rules import ParsedRules, parse_rules


def format_report(name: str, parsed: ParsedRules) -> str:
    out = [
        f"{name}:{d.line}: {d.level}: {d.code}: {d.message}"
        for d in sorted(parsed.diagnostics, key=lambda d: (d.line, d.code))
    ]
    errors = sum(d.level == "error" for d in parsed.diagnostics)
    warnings = len(parsed.diagnostics) - errors
    out.append(
        f"{name}: {len(parsed.rules)} rule(s) accepted, {len(parsed.ignore_globs)} "
        f"ignore glob(s), {errors} error(s), {warnings} warning(s)"
        + (", FILE REJECTED" if parsed.rejected else "")
    )
    return "\n".join(out)


def exit_code(parsed: ParsedRules, *, strict: bool = False) -> int:
    if parsed.rejected:
        return 2
    if not parsed.ok or (strict and parsed.diagnostics):
        return 1
    return 0


def main(argv: list[str]) -> int:
    strict = "--strict" in argv
    files = [a for a in argv if not a.startswith("--")]
    if len(files) != 1:
        print("usage: lint [--strict] FILE", file=sys.stderr)  # noqa: T201
        return 64
    parsed = parse_rules(Path(files[0]).read_bytes())
    print(format_report(files[0], parsed))  # noqa: T201
    return exit_code(parsed, strict=strict)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
