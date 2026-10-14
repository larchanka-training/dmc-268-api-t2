from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence


def rule_instructions(rules: Mapping[str, Sequence[str]] | None) -> Sequence[str]:
    """The instruction strings of the canonical rules document."""
    return (rules or {}).get("instructions", [])


def render_rules(rules: Mapping[str, Sequence[str]] | None) -> str:
    """Canonical rules document → bullet lines for the isolated <repo_rules> block."""
    bullets = (f"- {instruction}\n" for instruction in rule_instructions(rules))
    return "".join(bullets).strip()


def compute_rules_digest(rules: Mapping[str, object]) -> str:
    """Hash the validated canonical rules document without reordering instructions."""

    canonical = json.dumps(
        rules,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
