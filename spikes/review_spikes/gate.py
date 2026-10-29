"""Spike 4b: deterministic output gate (Pass 1-2 of the filtering pipeline, control M5)."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator

from review_spikes.difflabels import Anchor, FileTable, Unresolvable, resolve

_LABEL = r"^(?:R[0-9]+|L[0-9]+|C[0-9]+/[0-9]+)$"
_TEXT = {"type": "string", "minLength": 1, "maxLength": 2000}
FINDING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "path", "start_label", "end_label", "category", "severity",
        "title", "explanation", "evidence", "recommendation", "rule_ref",
    ],
    "properties": {
        "path": {"type": "string", "minLength": 1, "maxLength": 512},
        "start_label": {"type": "string", "pattern": _LABEL},
        "end_label": {"type": "string", "pattern": _LABEL},
        "category": {"enum": ["security", "correctness", "performance", "maintainability"]},
        "severity": {"enum": ["critical", "high", "medium", "low"]},
        "title": _TEXT,
        "explanation": _TEXT,
        "evidence": _TEXT,
        "recommendation": _TEXT,
        "rule_ref": {"type": ["string", "null"], "pattern": "^[A-Z][A-Z0-9-]{1,15}$"},
    },
}
_OUTPUT = Draft202012Validator(
    {
        "type": "object",
        "additionalProperties": False,
        "required": ["summary", "findings", "limitations"],
        "properties": {
            "summary": {"type": "string", "maxLength": 2000},
            "findings": {"type": "array", "maxItems": 50},
            "limitations": {
                "type": "array",
                "maxItems": 20,
                "items": {"type": "string", "maxLength": 500},
            },
        },
    }
)
_FINDING = Draft202012Validator(FINDING_SCHEMA)


@dataclass(frozen=True)
class Accepted:
    finding: dict[str, Any]
    anchor: Anchor


@dataclass
class GateResult:
    accepted: list[Accepted] = field(default_factory=list)
    rejected: list[tuple[int, str]] = field(default_factory=list)


def _norm(text: str) -> str:
    return " ".join(text.split())


def evidence_matches(evidence: str, anchor_lines: list[str]) -> bool:
    needle = _norm(evidence)
    return len(needle) >= 3 and needle in _norm("\n".join(anchor_lines))  # noqa: PLR2004


def _anchor_texts(tables: list[FileTable], anchor: Anchor) -> list[str]:
    table = next(t for t in tables if anchor.file_path == t.path)
    return [
        line.text
        for line in table.lines.values()
        if anchor.start.seq <= line.seq <= anchor.end.seq
    ]


def validate_output(
    raw: Any, tables: list[FileTable], known_rule_refs: Collection[str]
) -> GateResult:
    result = GateResult()
    errors = sorted(_OUTPUT.iter_errors(raw), key=lambda e: e.path)
    if errors:
        result.rejected.append((-1, "schema"))
        return result
    for index, finding in enumerate(raw["findings"]):
        if next(_FINDING.iter_errors(finding), None) is not None:
            result.rejected.append((index, "schema"))
            continue
        if finding["rule_ref"] is not None and finding["rule_ref"] not in known_rule_refs:
            result.rejected.append((index, "unknown_rule_ref"))
            continue
        try:
            anchor = resolve(tables, finding["path"], finding["start_label"], finding["end_label"])
        except Unresolvable as exc:
            result.rejected.append((index, f"unresolvable:{exc.reason}"))
            continue
        if not evidence_matches(finding["evidence"], _anchor_texts(tables, anchor)):
            result.rejected.append((index, "evidence_mismatch"))
            continue
        result.accepted.append(Accepted(finding, anchor))
    return result
