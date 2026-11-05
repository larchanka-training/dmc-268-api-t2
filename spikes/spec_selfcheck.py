"""Spike: mechanical self-check of docs/specs (Phase 3). Not production code.

Extracts schemas and examples from the two specs, runs a minimal prototype of
the RULES_FORMAT_SPEC grammar/merge, and validates results against the specs'
own JSON Schemas. Usage: python spikes/spec_selfcheck.py
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, SchemaError, ValidationError

type Document = dict[str, Any]
type Diagnostic = tuple[str, int, str, str, str]
type Layer = tuple[str, list[Document], list[Document], str]

FLOOR_RULE_COUNT = 2
DEFAULT_RULE_COUNT = 16


def flat_yaml(text: str) -> dict[str, object]:
    """Flat `key: scalar` subset; PyYAML is not installed in the venv (see R-4.1)."""
    out: dict[str, object] = {}
    for line in text.splitlines():
        key, _, value = line.partition(":")
        value = value.strip()
        out[key.strip()] = (
            int(value)
            if value.isdigit()
            else {"true": True, "false": False}.get(value, value)
        )
    return out


ROOT = Path(__file__).resolve().parents[1]
arguments = argparse.ArgumentParser(description=__doc__)
arguments.add_argument(
    "--rules-spec", type=Path, default=ROOT / "docs/specs/RULES_FORMAT_SPEC.md"
)
arguments.add_argument(
    "--context-spec",
    type=Path,
    default=ROOT / "docs/specs/CONTEXT_AND_VERIFICATION_SPEC.md",
)
selected = arguments.parse_args()
RULES_SPEC = selected.rules_spec.read_text("utf-8")
CTX_SPEC = selected.context_spec.read_text("utf-8")
FAILURES: list[str] = []


def check(cond: bool, what: str) -> None:
    print(("PASS " if cond else "FAIL ") + what)
    if not cond:
        FAILURES.append(what)


def fences(text: str, lang: str) -> list[str]:
    return re.findall(rf"^```{lang}\n(.*?)^```$", text, re.S | re.M)


# ---------------------------------------------------------------- schemas
schemas = {}
for spec_name, spec in (("rules", RULES_SPEC), ("ctx", CTX_SPEC)):
    for i, block in enumerate(fences(spec, "json")):
        try:
            schema = json.loads(block)
            Draft202012Validator.check_schema(schema)
        except json.JSONDecodeError, SchemaError:
            print(f"FAIL invalid schema {spec_name}[{i}]")
            sys.exit(1)
        schemas[f"{spec_name}[{i}]"] = schema
        print(f"PASS schema {spec_name}[{i}] is valid JSON Schema 2020-12")
EFFECTIVE = schemas["rules[0]"]
FINDING_V3 = schemas["ctx[0]"]
VERIFIER = schemas["ctx[1]"]

# ---------------------------------------------------------------- prototype parser
RULE_ID = re.compile(r"^[A-Z][A-Z0-9]{1,9}-[0-9]{1,4}$")
SECTIONS = {
    "security": "security",
    "performance": "performance",
    "style": "maintainability",
    "correctness": "correctness",
    "maintainability": "maintainability",
}
RESERVED = ("FSEC", "BSEC", "BPERF", "BSTY", "BCOR", "BMNT")
FM_KEYS = {"version", "extends", "defaults_version", "scope", "allow_inline_ignores"}
INJECTION = re.compile(
    r"(ignore|disregard|forget)\b.*\b(previous|above|all)\b.*\b(instruction|rule)"
    r"|<\|im_start\|>|\bapprove this pull request\b",
    re.I,
)
SEVERITIES = {"blocker", "major", "minor", "info"}


def globs(value: str) -> list[str]:
    return re.findall(r"`([^`]+)`", value)


def parse_frontmatter(
    lines: list[str], source: str, diags: list[Diagnostic]
) -> tuple[dict[str, object], int]:
    if lines[0] != "---":
        return {}, 0
    end = lines.index("---", 1)
    fm = flat_yaml("\n".join(lines[1:end]))
    for n, line in enumerate(lines[1:end], start=2):
        key = line.split(":", 1)[0].strip()
        if key and key not in FM_KEYS:
            diags.append(
                (source, n, "warning", "RF014", f"unknown frontmatter key {key}")
            )
            fm.pop(key, None)
    return fm, end + 1


def close_rule(
    rule: Document | None, rules: list[Document], diags: list[Diagnostic], source: str
) -> None:
    if rule is None:
        return
    rule["text"] = "\n".join(rule.pop("_text")).strip()
    if rule["text"] and INJECTION.search(rule["text"]):
        diags.append(
            (
                source,
                rule["_line"],
                "error",
                "RF043",
                f"{rule['id']}: rule text contains reviewer-directed instructions",
            )
        )
    else:
        rules.append(rule)


def section_mode(
    line: str,
    location: tuple[str, int],
    seen: set[str],
    previous: str | None,
    diags: list[Diagnostic],
) -> tuple[str, str | None]:
    source, number = location
    name = line[2:].strip().lower()
    if name in seen:
        diags.append(
            (
                source,
                number,
                "error",
                "RF062",
                f"duplicate section {line[2:].strip()}; second occurrence discarded",
            )
        )
        return "discard", previous
    if name not in SECTIONS and name != "ignore":
        diags.append(
            (
                source,
                number,
                "warning",
                "RF061",
                f"unknown section {line[2:].strip()} discarded",
            )
        )
        return "discard", previous
    seen.add(name)
    return ("ignore" if name == "ignore" else "section"), name


def new_rule(
    line: str,
    number: int,
    source: str,
    section: str | None,
    diags: list[Diagnostic],
) -> Document | None:
    matched = re.match(r"^## (\S+): (.+)$", line)
    rid = matched[1] if matched else line[3:]
    if not matched or not RULE_ID.match(rid):
        diags.append((source, number, "error", "RF030", f"invalid rule id {rid}"))
        return None
    return {
        "id": rid,
        "section": section,
        "title": matched[2].strip(),
        "_line": number,
        "_text": [],
        "_attrs_done": False,
    }


def append_rule_line(line: str, rule: Document) -> None:
    matched = re.match(r"^- (\w+): (.+)$", line)
    if matched and not rule["_attrs_done"]:
        key, val = matched[1], matched[2].strip()
        if key == "severity":
            rule["severity"] = val
        elif key in ("paths", "exclude_paths"):
            rule[key] = globs(val)
        elif key == "disabled":
            rule["disabled"] = val == "true"
    elif line.strip():
        rule["_attrs_done"] = True
        rule["_text"].append(line)


def parse_ignore(line: str, number: int) -> Document | None:
    matched = re.match(r"^- `([^`]+)`: (.+)$", line)
    if not matched:
        return None
    target = matched[2].split("<-")[0].strip()
    return {
        "glob": matched[1],
        "target": "*" if target == "*" else [t.strip() for t in target.split(",")],
        "_line": number,
    }


def parse(
    text: str, source: str
) -> tuple[dict[str, object], list[Document], list[Document], list[Diagnostic]]:
    diags: list[Diagnostic] = []
    rules: list[Document] = []
    ignores: list[Document] = []
    lines = text.replace("\r\n", "\n").lstrip("\ufeff").split("\n")
    fm, first_line = parse_frontmatter(lines, source, diags)
    seen: set[str] = set()
    mode, section = "outside", None
    discarded = []
    rule = None
    in_fence = False
    for n, line in enumerate(lines[first_line:], start=first_line + 1):
        if line.startswith("```"):
            in_fence = not in_fence
        if not in_fence and line.startswith("# "):
            close_rule(rule, rules, diags, source)
            rule = None
            mode, section = section_mode(line, (source, n), seen, section, diags)
            continue
        if (
            not in_fence
            and line.startswith("## ")
            and mode in ("section", "rule", "skip_rule")
        ):
            close_rule(rule, rules, diags, source)
            rule = new_rule(line, n, source, section, diags)
            mode = "rule" if rule is not None else "skip_rule"
            continue
        if mode == "rule" and rule is not None:
            append_rule_line(line, rule)
            continue
        if mode == "ignore":
            entry = parse_ignore(line, n)
            if entry is not None:
                ignores.append(entry)
            elif line.strip():
                discarded.append(n)
            continue
        if mode in ("outside", "section") and line.strip():
            discarded.append(n)
    close_rule(rule, rules, diags, source)
    if discarded:
        diags.append(
            (
                source,
                discarded[0],
                "info",
                "RF060",
                f"{len(discarded)} lines outside rules were discarded",
            )
        )
    return fm, rules, ignores, diags


def merge_rule(
    base: Document | None,
    entry: Document,
    origin: str,
    source: str,
    diags: list[Diagnostic],
) -> Document | None:
    rid = entry["id"]
    if base is not None:
        if base["floor"]:
            diags.append(
                (
                    source,
                    entry["_line"],
                    "error",
                    "RF032",
                    f"{rid} is a security-floor rule and cannot be changed",
                )
            )
            return None
        if entry["section"] != base["section"]:
            diags.append(
                (
                    source,
                    entry["_line"],
                    "error",
                    "RF034",
                    f"{rid} belongs to section {base['section']}; "
                    f"cannot be redefined under {entry['section']}",
                )
            )
            return None
        updated = dict(base)
        for key in ("title", "severity", "paths", "exclude_paths", "disabled"):
            if key in entry:
                updated[key] = entry[key]
        if entry.get("text"):
            updated["text"] = entry["text"]
        updated["origin"] = origin
        return updated
    if origin not in ("floor", "defaults") and rid.split("-")[0] in RESERVED:
        diags.append(
            (
                source,
                entry["_line"],
                "error",
                "RF033",
                f"{rid} uses a reserved built-in prefix",
            )
        )
        return None
    missing = [key for key in ("severity", "text") if not entry.get(key)]
    if missing:
        diags.append(
            (
                source,
                entry["_line"],
                "error",
                "RF031",
                f"new rule {rid} requires {', '.join(missing)}",
            )
        )
        return None
    return {
        "id": rid,
        "section": entry["section"],
        "category": SECTIONS[entry["section"]],
        "severity": entry["severity"],
        "title": entry["title"],
        "text": entry["text"],
        "paths": entry.get("paths") or ["**"],
        "exclude_paths": entry.get("exclude_paths", []),
        "floor": origin == "floor",
        "origin": origin,
        "disabled": entry.get("disabled", False),
    }


def merge(
    layers: list[Layer], diags: list[Diagnostic]
) -> tuple[list[Document], list[Document]]:
    """Merge (origin, rules, ignores, source) prototype layers."""
    eff: dict[str, Document] = {}
    ign: list[Document] = []
    for origin, rules, ignores, source in layers:
        for r in rules:
            rid = r["id"]
            updated = merge_rule(eff.get(rid), r, origin, source, diags)
            if updated is not None:
                eff[rid] = updated
        for e in ignores:
            targets = [] if e["target"] == "*" else e["target"]
            if any(t.startswith("FSEC-") for t in targets):
                diags.append(
                    (
                        source,
                        e["_line"],
                        "error",
                        "RF051",
                        f"ignore entry targets security-floor rule {targets[0]}",
                    )
                )
                continue
            unknown = [t for t in targets if t not in eff]
            if unknown:
                diags.append(
                    (
                        source,
                        e["_line"],
                        "warning",
                        "RF050",
                        f"ignore entry references unknown rule {unknown[0]}",
                    )
                )
                continue
            ign.append({"glob": e["glob"], "target": e["target"]})
    rules = [
        {k: v for k, v in r.items() if k != "disabled"}
        for r in eff.values()
        if not r["disabled"]
    ]
    return rules, ign


# ---------------------------------------------------------------- defaults
md_blocks = fences(RULES_SPEC, "markdown")
defaults_md = next(
    b for b in md_blocks if "## FSEC-1: Secrets must not be committed" in b
)
_, d_rules, d_ign, d_diags = parse(defaults_md, "defaults")
check(not d_diags, f"defaults-v1 parses without diagnostics ({d_diags})")
floor = [r for r in d_rules if r["id"].startswith("FSEC-")]
rest = [r for r in d_rules if not r["id"].startswith("FSEC-")]
check(
    len(floor) == FLOOR_RULE_COUNT and len(rest) == DEFAULT_RULE_COUNT,
    f"defaults: 2 floor + 16 rules (got {len(floor)}+{len(rest)})",
)
BASE_LAYERS = [("floor", floor, [], "defaults"), ("defaults", rest, d_ign, "defaults")]


def effective(
    scopes: list[Document], diags: list[Diagnostic], layer_records: list[Document]
) -> tuple[Document, list[ValidationError]]:
    doc = {
        "schema_version": 2,
        "defaults_version": "defaults-v1",
        "layers": layer_records,
        "scopes": scopes,
        "diagnostics": [
            {
                "code": c,
                "level": lvl,
                "source": s,
                "line": n,
                "rule_id": None,
                "message": m,
            }
            for s, n, lvl, c, m in diags
        ],
    }
    errs = sorted(Draft202012Validator(EFFECTIVE).iter_errors(doc), key=str)
    return doc, errs


LAYER_BASE = [
    {
        "layer": "floor",
        "source": "defaults-v1",
        "commit_sha": None,
        "blob_sha": None,
        "status": "applied",
    },
    {
        "layer": "defaults",
        "source": "defaults-v1",
        "commit_sha": None,
        "blob_sha": None,
        "status": "applied",
    },
    {
        "layer": "legacy_db",
        "source": "RepositorySettings.rules",
        "commit_sha": None,
        "blob_sha": None,
        "status": "absent",
    },
]
ROOT_LAYER = {
    "layer": "root",
    "source": ".review/rules.md",
    "commit_sha": "a" * 40,
    "blob_sha": "b" * 40,
    "status": "applied",
}

# ---------------------------------------------------------------- 9.1 minimal
ex91 = next(b for b in md_blocks if "## ACME-1: Use the internal HTTP client" in b)
diags: list[Diagnostic] = []
fm, rules, ign, pd = parse(ex91, ".review/rules.md")
diags += pd
eff, eign = merge([*BASE_LAYERS, ("root", rules, ign, ".review/rules.md")], diags)
doc, errs = effective(
    [
        {
            "dir": "",
            "scope": ["**"],
            "allow_inline_ignores": True,
            "rules": eff,
            "ignores": eign,
        }
    ],
    diags,
    [*LAYER_BASE, ROOT_LAYER],
)
check(not diags, f"9.1 minimal: no diagnostics ({diags})")
check(
    not errs, f"9.1 minimal: effective doc validates ({[e.message for e in errs[:3]]})"
)
check(
    any(r["id"] == "ACME-1" and r["severity"] == "major" for r in eff),
    "9.1: ACME-1 major present",
)

# ---------------------------------------------------------------- 9.2 typical
ex92_root = next(b for b in md_blocks if "## ACME-10: Tenant isolation" in b)
ex92_nested = next(b for b in md_blocks if "## ACME-40: Money as integer" in b)
diags = []
fm_r, r_rules, r_ign, pd = parse(ex92_root, ".review/rules.md")
diags += pd
fm_n, n_rules, n_ign, pd = parse(ex92_nested, "services/payments/.review/rules.md")
diags += pd
root_layers = [*BASE_LAYERS, ("root", r_rules, r_ign, ".review/rules.md")]
eff_root, ign_root = merge(root_layers, diags)
eff_pay, ign_pay = merge(
    [*root_layers, ("nested", n_rules, n_ign, "services/payments/.review/rules.md")], []
)
doc, errs = effective(
    [
        {
            "dir": "",
            "scope": ["**"],
            "allow_inline_ignores": True,
            "rules": eff_root,
            "ignores": ign_root,
        },
        {
            "dir": "services/payments",
            "scope": ["**"],
            "allow_inline_ignores": True,
            "rules": eff_pay,
            "ignores": ign_pay,
        },
    ],
    diags,
    [
        *LAYER_BASE,
        ROOT_LAYER,
        {
            **ROOT_LAYER,
            "layer": "nested",
            "source": "services/payments/.review/rules.md",
        },
    ],
)
check(not diags, f"9.2 typical: no diagnostics ({diags})")
check(
    not errs, f"9.2 typical: effective doc validates ({[e.message for e in errs[:3]]})"
)
by_root = {r["id"]: r for r in eff_root}
by_pay = {r["id"]: r for r in eff_pay}
check("BSEC-6" not in by_root, "9.2: BSEC-6 disabled")
check(by_root["BPERF-4"]["severity"] == "major", "9.2: BPERF-4 raised to major")
check(
    by_root["BMNT-1"]["severity"] == "info" and by_pay["BMNT-1"]["severity"] == "major",
    "9.2: BMNT-1 info at root, major under services/payments",
)
check(
    "ACME-40" in by_pay and "ACME-40" not in by_root,
    "9.2: ACME-40 only in nested scope",
)
check(fm_r.get("defaults_version") == "defaults-v1", "9.2: frontmatter parsed")

# ---------------------------------------------------------------- 9.3 invalid
ex93 = next(b for b in md_blocks if "## ACME-3: Prompt smuggling" in b)
diags = []
fm, rules, ign, pd = parse(ex93, ".review/rules.md")
diags += pd
eff, eign = merge([*BASE_LAYERS, ("root", rules, ign, ".review/rules.md")], diags)
# inline directive scan with the spec's normative regex
directive_match = re.search(r"Regex \(normative.*?\n```\n(.*?)\n```", RULES_SPEC, re.S)
assert directive_match is not None
directive_block = directive_match[1]
STRICT = re.compile(directive_block)
src_line = "# review-ignore-next-line"
if re.search(r"\breview-ignore-(next-line|file)\b", src_line) and not STRICT.search(
    src_line
):
    diags.append(
        (
            "src/app/x.py",
            1,
            "warning",
            "RF070",
            "inline ignore without rule id at src/app/x.py:1",
        )
    )
check(
    bool(STRICT.search("// review-ignore-next-line BPERF-1,BCOR-3 -- hot path")),
    "normative directive regex accepts a valid directive",
)
actual = "\n".join(
    f"{s}:{n}: {lvl} {c}: {m}"
    for s, n, lvl, c, m in sorted(diags, key=lambda d: (d[0], d[1], d[3]))
)
expected_match = re.search(
    r"Expected `review-rules lint --format text --sources src`:\n\n```\n(.*?)\n```",
    RULES_SPEC,
    re.S,
)
assert expected_match is not None
expected = expected_match[1]
check(actual == expected, "9.3: prototype linter output == spec expected output")
if actual != expected:
    print("--- actual\n" + actual + "\n--- expected\n" + expected)
doc, errs = effective(
    [
        {
            "dir": "",
            "scope": ["**"],
            "allow_inline_ignores": True,
            "rules": eff,
            "ignores": eign,
        }
    ],
    diags,
    [*LAYER_BASE, ROOT_LAYER],
)
check(
    not errs,
    "9.3: effective doc (with diagnostics) validates "
    f"({[e.message for e in errs[:3]]})",
)
check(
    {r["id"]: r for r in eff}["BPERF-1"]["severity"] == "major",
    "9.3: BPERF-1 unchanged (RF034)",
)
check(
    len(eff) == FLOOR_RULE_COUNT + DEFAULT_RULE_COUNT,
    "9.3: effective ruleset == floor + defaults-v1",
)
check(not any(r["id"].startswith("ACME") for r in eff), "9.3: no ACME rule survives")

# ---------------------------------------------------------------- spec 2 schemas
finding = {
    "path": "src/app/pay.py",
    "start_label": "C40/40",
    "end_label": "R41",
    "rule_ref": "FSEC-2",
    "category": "security",
    "severity": "critical",
    "title": "SQL injection",
    "explanation": "user.id is interpolated into SQL.",
    "evidence_quote": 'f"SELECT * FROM t WHERE id={user.id}"',
    "recommendation": "Use a bound parameter.",
    "proposed_diff_fix": None,
}
check(
    Draft202012Validator(FINDING_V3).is_valid(finding),
    "spec2: sample finding (C.1 example) validates v3",
)
for bad in ("R0", "41", "C40", "X1"):
    check(
        not Draft202012Validator(FINDING_V3).is_valid({**finding, "end_label": bad}),
        f"spec2: label {bad!r} rejected",
    )
verdict = {
    "verdicts": [
        {
            "index": 0,
            "claim": "true",
            "claim_confidence": 0.9,
            "relevance": "direct",
            "refutation": "No parameterization present.",
            "reason_code": "confirmed",
        }
    ]
}
check(
    Draft202012Validator(VERIFIER).is_valid(verdict),
    "spec2: sample verifier output validates",
)
check(
    FINDING_V3["properties"]["rule_ref"]["pattern"].strip("^$")
    in EFFECTIVE["$defs"]["rule_id"]["pattern"],
    "rule_ref pattern compatible with rules labels across specs",
)

print(f"\n{len(FAILURES)} failure(s)")
sys.exit(1 if FAILURES else 0)
