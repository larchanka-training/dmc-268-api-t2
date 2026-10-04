from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping


def compute_rules_digest(rules: Mapping[str, object]) -> str:
    """Hash the validated canonical rules document without reordering instructions."""

    canonical = json.dumps(
        rules,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
