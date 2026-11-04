"""Metrics as structured log events; this deployment has no metrics backend.

Each call emits one ``metric`` event with a name, a value and labels. Labels
are low-cardinality codes: never source, paths, prompts or secrets.
"""

from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any
from uuid import UUID

import structlog


def count(name: str, value: int = 1, **labels: str) -> None:
    structlog.get_logger("metrics").info("metric", metric=name, value=value, **labels)


def record_skipped_files(review_id: UUID, skipped: Iterable[Mapping[str, Any]]) -> None:
    """``files_skipped_total{reason}``: the guard detail, else the wire reason."""
    reasons = Counter(str(item.get("detail") or item["reason"]) for item in skipped)
    for reason, total in sorted(reasons.items()):
        count("files_skipped_total", total, reason=reason, review_id=str(review_id))
