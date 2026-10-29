"""Spike 3: strict ``.review/rules.md`` parser, precedence merge, inline ignores.

Draft grammar (becomes RULES_FORMAT_SPEC section 3):

    file        = [frontmatter] { section }
    frontmatter = "---" NL { key ": " value NL } "---" NL     ; version|extends|scope
    section     = "# " ("Security"|"Performance"|"Style") NL { rule }
                | "# Ignore" NL { "- " glob NL }
    rule        = "- [" ID "] " { key "=" value } NL { "  " text NL }
    key         = severity | paths | allow_inline_ignore | enabled

Anything that is not one of those productions is *dropped* (never forwarded to the
model) and reported as a ``dropped-free-text`` warning.
"""

from __future__ import annotations

import dataclasses
import json
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

SEVERITIES = ("low", "medium", "high", "critical")
RULE_SECTIONS = {"Security": "security", "Performance": "performance", "Style": "style"}
MAX_BYTES = 64 * 1024
MAX_RULES = 100
MAX_TEXT = 1000
MAX_IGNORES = 100
MAX_EXTENDS = 3
RULE_ID = re.compile(r"^(?:SYS-)?[A-Z][A-Z0-9]{1,7}-[0-9]{1,4}$")
_RULE_HEAD = re.compile(r"^[-*]\s+\[([^\]]*)\](.*)$")
_SECTION = re.compile(r"^#\s+(\S.*?)\s*$")
_EXTENDS_PATH = re.compile(r"^\.review/[A-Za-z0-9._/-]+\.md$")
_MARKUP = re.compile(r"<\s*[/!|]?\s*[A-Za-z]|<\|")
_YAML_SPECIAL = "&*!|>{%@`"
_ATTRS = ("severity", "paths", "allow_inline_ignore", "enabled")

Level = Literal["error", "warning"]


@dataclass(frozen=True)
class Diagnostic:
    code: str
    level: Level
    line: int
    message: str


@dataclass(frozen=True)
class Rule:
    id: str
    section: str
    severity: str | None
    paths: tuple[str, ...] = ()
    text: str = ""
    allow_inline_ignore: bool = False
    enabled: bool = True
    line: int = 0
    declared: frozenset[str] = frozenset()
    source: str = "repo"


@dataclass
class ParsedRules:
    version: int | None = None
    extends: tuple[str, ...] = ()
    scope: tuple[str, ...] = ()
    rules: list[Rule] = field(default_factory=list)
    ignore_globs: list[str] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    rejected: bool = False
    dropped_chars: int = 0

    @property
    def ok(self) -> bool:
        return not any(d.level == "error" for d in self.diagnostics)

    def add(self, code: str, level: Level, line: int, message: str) -> None:
        self.diagnostics.append(Diagnostic(code, level, line, message))


# --- globs ---------------------------------------------------------------------


def validate_glob(glob: str) -> str | None:
    """Reason a glob is unacceptable, or None."""
    if not glob or glob != glob.strip():
        return "empty-or-padded"
    if glob.startswith("/"):
        return "absolute"
    if glob.startswith("!"):
        return "negation-unsupported"
    if "\\" in glob or "\x00" in glob:
        return "forbidden-character"
    if any(part == ".." for part in glob.split("/")):
        return "parent-traversal"
    if glob.endswith("/") or "//" in glob:
        return "malformed"
    if not glob.replace("**", "").replace("*", "").replace("/", "").strip():
        return "matches-everything"
    return None


def glob_to_regex(glob: str) -> re.Pattern[str]:
    """``**`` crosses '/', ``*`` and ``?`` do not; a slash-less glob matches basenames."""
    prefix = "" if "/" in glob else "(?:.*/)?"
    out, i = [], 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif glob[i] == "*":
            out.append("[^/]*")
            i += 1
        elif glob[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(glob[i]))
            i += 1
    return re.compile(f"^{prefix}{''.join(out)}$")


def matches(glob: str, path: str) -> bool:
    return glob_to_regex(glob).fullmatch(path) is not None


# --- parsing ---------------------------------------------------------------------


def _hard_reject(result: ParsedRules, code: str, message: str) -> ParsedRules:
    result.rejected = True
    result.rules = []
    result.ignore_globs = []
    result.add(code, "error", 0, message)
    return result


def _forbidden_char(text: str) -> str | None:
    for char in text:
        if char in "\n\t":
            continue
        category = unicodedata.category(char)
        if category in ("Cc", "Cf", "Co", "Cs"):
            return f"U+{ord(char):04X}"
    return None


def parse_rules(data: bytes | str) -> ParsedRules:
    result = ParsedRules()
    raw = data.encode("utf-8") if isinstance(data, str) else data
    if len(raw) > MAX_BYTES:
        return _hard_reject(result, "file-too-large", f"more than {MAX_BYTES} bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return _hard_reject(result, "invalid-encoding", "file is not valid UTF-8")
    text = text.removeprefix("﻿").replace("\r\n", "\n")
    bad = _forbidden_char(text)
    if bad:
        return _hard_reject(result, "forbidden-character", f"control/format char {bad}")
    lines = text.split("\n")
    start = _parse_frontmatter(lines, result)
    if result.rejected:
        return result
    _parse_body(lines, start, result)
    return result


def _parse_frontmatter(lines: list[str], result: ParsedRules) -> int:
    if not lines or lines[0].strip() != "---":
        return 0
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        _hard_reject(result, "unterminated-frontmatter", "missing closing '---'")
        return 0
    seen: set[str] = set()
    for no, line in enumerate(lines[1:end], 2):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if not sep or key not in ("version", "extends", "scope") or key in seen:
            result.add("invalid-frontmatter-key", "error", no, f"bad key {key!r}")
            continue
        seen.add(key)
        if not value or value[0] in _YAML_SPECIAL:
            result.add("unsupported-yaml", "error", no, f"unsupported value for {key}")
            continue
        _frontmatter_value(key, value, no, result)
    return end + 1


def _frontmatter_value(key: str, value: str, no: int, result: ParsedRules) -> None:
    try:
        parsed = json.loads(value)
    except ValueError:
        parsed = value.strip("\"'")
    items = parsed if isinstance(parsed, list) else [parsed]
    if key == "version":
        if parsed != 1 or isinstance(parsed, bool):
            result.add("unsupported-version", "error", no, "version must be 1")
        else:
            result.version = 1
        return
    if not all(isinstance(i, str) for i in items):
        result.add("invalid-frontmatter-value", "error", no, f"{key} must be strings")
        return
    if key == "extends":
        if len(items) > MAX_EXTENDS:
            result.add("too-many-extends", "error", no, f"max {MAX_EXTENDS}")
        elif not all(_EXTENDS_PATH.fullmatch(i) and ".." not in i for i in items):
            result.add("invalid-extends", "error", no, "must be .review/*.md, no '..'")
        else:
            result.extends = tuple(items)
    else:
        reasons = [validate_glob(i) for i in items]
        if any(reasons):
            result.add("invalid-scope", "error", no, f"scope glob: {reasons}")
        else:
            result.scope = tuple(items)


def _parse_body(lines: list[str], start: int, result: ParsedRules) -> None:
    section: str | None = None
    current: dict | None = None
    seen_ids: set[str] = set()

    def flush() -> None:
        nonlocal current
        if current is not None:
            _finish_rule(current, result, seen_ids)
        current = None

    for no, line in enumerate(lines[start:], start + 1):
        if not line.strip():
            continue
        head = _SECTION.match(line) if line.startswith("# ") else None
        if head:
            flush()
            name = head.group(1)
            if name in RULE_SECTIONS or name == "Ignore":
                section = name
            else:
                section = None
                result.add("unknown-section", "error", no, f"unknown section {name!r}")
            continue
        if section in RULE_SECTIONS and _RULE_HEAD.match(line):
            flush()
            current = {"no": no, "section": RULE_SECTIONS[section], "head": line, "text": []}
        elif section in RULE_SECTIONS and current is not None and line.startswith("  "):
            current["text"].append(line.strip())
        elif section == "Ignore" and line.startswith(("- ", "* ")):
            _add_ignore(line[2:].strip(), no, result)
        else:
            result.dropped_chars += len(line)
            result.add("dropped-free-text", "warning", no, "line outside the grammar")
    flush()
    if len(result.rules) > MAX_RULES:
        result.add("too-many-rules", "error", 0, f"more than {MAX_RULES} rules")
        result.rules = result.rules[:MAX_RULES]


def _add_ignore(glob: str, no: int, result: ParsedRules) -> None:
    reason = validate_glob(glob)
    if reason:
        result.add("invalid-ignore-glob", "error", no, f"{glob!r}: {reason}")
    elif len(result.ignore_globs) >= MAX_IGNORES:
        result.add("too-many-ignores", "error", no, f"max {MAX_IGNORES}")
    else:
        result.ignore_globs.append(glob)


def _finish_rule(raw: dict, result: ParsedRules, seen_ids: set[str]) -> None:
    no = raw["no"]
    match = _RULE_HEAD.match(raw["head"])
    assert match is not None
    rule_id, tail = match.group(1), match.group(2)
    errors_before = sum(d.level == "error" for d in result.diagnostics)
    if not RULE_ID.fullmatch(rule_id):
        result.add("invalid-rule-id", "error", no, f"bad id {rule_id!r}")
    if rule_id in seen_ids:
        result.add("duplicate-rule-id", "error", no, f"{rule_id} defined twice")
    seen_ids.add(rule_id)
    attrs: dict[str, str] = {}
    for token in tail.split():
        key, sep, value = token.partition("=")
        if not sep or key not in _ATTRS or key in attrs:
            result.add("invalid-attribute", "error", no, f"bad attribute {token!r}")
        else:
            attrs[key] = value
    override = rule_id.startswith("SYS-")
    severity = attrs.get("severity")
    if severity is None and not override:
        result.add("missing-severity", "error", no, f"{rule_id} needs severity=")
    elif severity is not None and severity not in SEVERITIES:
        result.add("invalid-severity", "error", no, f"{severity!r} not in {SEVERITIES}")
    paths = tuple(p for p in attrs.get("paths", "").split(",") if p)
    for glob in paths:
        reason = validate_glob(glob)
        if reason:
            result.add("invalid-paths-glob", "error", no, f"{glob!r}: {reason}")
    flags: dict[str, bool] = {}
    for name in ("allow_inline_ignore", "enabled"):
        if name in attrs:
            if attrs[name] not in ("true", "false"):
                result.add("invalid-attribute", "error", no, f"{name} must be true|false")
            else:
                flags[name] = attrs[name] == "true"
    body = unicodedata.normalize("NFKC", " ".join(raw["text"]))
    if len(body) > MAX_TEXT:
        result.add("text-too-long", "error", no, f"{len(body)} > {MAX_TEXT} chars")
    if not body and not override and flags.get("enabled", True):
        result.add("empty-rule-text", "error", no, f"{rule_id} has no text")
    if _MARKUP.search(body):
        result.add("markup-in-text", "warning", no, "tag-like text is data, not markup")
    if sum(d.level == "error" for d in result.diagnostics) > errors_before:
        return
    result.rules.append(
        Rule(
            id=rule_id,
            section=raw["section"],
            severity=severity,
            paths=paths,
            text=body,
            allow_inline_ignore=flags.get("allow_inline_ignore", False),
            enabled=flags.get("enabled", True),
            line=no,
            declared=frozenset(attrs) | (frozenset({"text"}) if body else frozenset()),
        )
    )


# --- precedence ----------------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    rule_id: str
    applied: bool
    reason: str


@dataclass
class Merged:
    rules: dict[str, Rule]
    decisions: list[Decision]


def _rank(severity: str | None) -> int:
    return SEVERITIES.index(severity) if severity in SEVERITIES else -1


def merge_rules(
    system_hard: Sequence[Rule], system_soft: Sequence[Rule], repo: Sequence[Rule]
) -> Merged:
    """System Hard > Repo > System Soft (ADR-3)."""
    effective = {r.id: dataclasses.replace(r, source="hard") for r in system_hard}
    effective.update({r.id: dataclasses.replace(r, source="soft") for r in system_soft})
    hard_ids = {r.id for r in system_hard}
    decisions: list[Decision] = []
    for rule in repo:
        if not rule.id.startswith("SYS-"):
            effective[rule.id] = rule
            decisions.append(Decision(rule.id, True, "added"))
            continue
        target = effective.get(rule.id)
        if target is None:
            decisions.append(Decision(rule.id, False, "unknown-system-rule"))
        elif rule.id in hard_ids:
            decisions.append(_override_hard(effective, target, rule))
        else:
            changes = _soft_changes(rule)
            effective[rule.id] = dataclasses.replace(target, **changes)
            decisions.append(Decision(rule.id, True, "soft-overridden"))
    return Merged(effective, decisions)


def _override_hard(effective: dict[str, Rule], target: Rule, rule: Rule) -> Decision:
    forbidden = rule.declared - {"severity"}
    if forbidden:
        return Decision(rule.id, False, f"hard-immutable:{sorted(forbidden)}")
    if rule.severity is None or _rank(rule.severity) < _rank(target.severity):
        return Decision(rule.id, False, "hard-severity-lower")
    effective[rule.id] = dataclasses.replace(target, severity=rule.severity)
    return Decision(rule.id, True, "hard-severity-raised")


def _soft_changes(rule: Rule) -> dict[str, object]:
    changes: dict[str, object] = {}
    if "severity" in rule.declared:
        changes["severity"] = rule.severity
    if "paths" in rule.declared:
        changes["paths"] = rule.paths
    if "enabled" in rule.declared:
        changes["enabled"] = rule.enabled
    if "allow_inline_ignore" in rule.declared:
        changes["allow_inline_ignore"] = rule.allow_inline_ignore
    return changes


def rule_applies(rule: Rule, path: str, ignore_globs: Sequence[str]) -> bool:
    """# Ignore globs silence Repo/Soft rules; Hard rules still run (open question)."""
    if not rule.enabled:
        return False
    if rule.paths and not any(matches(g, path) for g in rule.paths):
        return False
    return rule.source == "hard" or not any(matches(g, path) for g in ignore_globs)


def rules_for_path(path: str, chain: Sequence[ParsedRules]) -> dict[str, Rule]:
    """Nested rules.md: ``chain`` is ordered root -> nearest; nearest id wins."""
    out: dict[str, Rule] = {}
    for parsed in chain:
        if parsed.scope and not any(matches(g, path) for g in parsed.scope):
            continue
        out.update({r.id: r for r in parsed.rules})
    return out


# --- inline ignore directives ----------------------------------------------------------

_DIRECTIVE = re.compile(
    r"\breview-ignore-(next-line|file)\b(?:[ \t]+([A-Za-z0-9_-]+))?"
)


@dataclass(frozen=True)
class Directive:
    kind: Literal["next-line", "file"]
    rule_id: str | None
    path: str
    line: int


def find_directives(path: str, line_no: int, text: str) -> Directive | None:
    match = _DIRECTIVE.search(text)
    if not match:
        return None
    return Directive(match.group(1), match.group(2), path, line_no)  # type: ignore[arg-type]


@dataclass(frozen=True)
class LocatedFinding:
    path: str
    line: int
    rule_ref: str | None


@dataclass
class IgnoreReport:
    kept: list[LocatedFinding] = field(default_factory=list)
    suppressed: list[tuple[LocatedFinding, Directive]] = field(default_factory=list)
    rejected: list[tuple[Directive, str]] = field(default_factory=list)


def apply_inline_ignores(
    findings: Sequence[LocatedFinding],
    directives: Sequence[Directive],
    rules: dict[str, Rule],
) -> IgnoreReport:
    """ADR-4: directives live in attacker-controlled head code; honour sparingly."""
    report = IgnoreReport()
    honoured: list[Directive] = []
    for directive in directives:
        reason = _reject_reason(directive, rules)
        if reason:
            report.rejected.append((directive, reason))
        else:
            honoured.append(directive)
    for finding in findings:
        hit = next((d for d in honoured if _covers(d, finding)), None)
        if hit:
            report.suppressed.append((finding, hit))
        else:
            report.kept.append(finding)
    return report


def _reject_reason(directive: Directive, rules: dict[str, Rule]) -> str | None:
    if directive.rule_id is None:
        return "bare-ignore"
    rule = rules.get(directive.rule_id)
    if rule is None:
        return "unknown-rule"
    if rule.source == "hard":
        return "hard-rule"
    if not rule.allow_inline_ignore:
        return "not-allowed-by-base-rules"
    return None


def _covers(directive: Directive, finding: LocatedFinding) -> bool:
    if finding.rule_ref != directive.rule_id or finding.path != directive.path:
        return False
    if directive.kind == "file":
        return True
    return finding.line == directive.line + 1
