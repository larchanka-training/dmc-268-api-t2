from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any, Literal
from uuid import UUID


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


def compute_repository_settings_digest(  # noqa: PLR0913 -- exact raw stored fields
    *,
    id: UUID,
    repository_id: UUID,
    rules: Mapping[str, object],
    ignores: Mapping[str, object],
    output_language: str,
    surrounding_lines: int,
) -> str:
    """Hash raw stored analysis fields *before* the historical read projection."""
    values = {
        "id": str(id),
        "repository_id": str(repository_id),
        "rules": rules,
        "ignores": ignores,
        "output_language": output_language,
        "surrounding_lines": surrounding_lines,
    }
    return hashlib.sha256(
        json.dumps(
            values,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
