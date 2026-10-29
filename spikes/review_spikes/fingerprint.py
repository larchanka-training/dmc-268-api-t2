"""Spike 2b: fingerprint v2 (ADR-10): sha256(rule_key | path | normalized line)."""

from __future__ import annotations

import hashlib
import re
import unicodedata

MARKER_RE = re.compile(r"<!-- dmc268-finding:v2:([0-9a-f]{64}) -->")


def normalize_line(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).split())


def rule_key(rule_ref: str | None, category: str) -> str:
    return rule_ref if rule_ref else f"cat:{category}"


def fingerprint_v2(
    key: str, path: str, lines: list[str], *, occurrence: int = 0
) -> str:
    content = "\n".join(normalize_line(line) for line in lines)
    parts = ("v2", key, path, content, str(occurrence))
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


def marker_v2(fingerprint: str) -> str:
    return f"<!-- dmc268-finding:v2:{fingerprint} -->"
