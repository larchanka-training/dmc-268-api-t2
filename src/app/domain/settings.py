from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any, Literal


def project_historical_empty_settings(
    value: dict[str, Any], key: Literal["instructions", "globs"]
) -> dict[str, Any]:
    """Project exact historical {} for use only; never replace stored identity."""
    if isinstance(value, dict) and value == {}:
        return {key: []}
    return value


def compute_rules_digest(rules: Mapping[str, object]) -> str:
    """Hash the validated canonical rules document without reordering instructions."""

    canonical = json.dumps(
        rules,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
