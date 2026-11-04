"""Effective settings use the existing immutable authority, never current pointer."""

import hashlib
import json
from copy import deepcopy
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.context.loader import LoadedRules
from app.db.models import RepositorySettings
from app.domain.settings import compute_rules_digest

_FIELDS = (
    "repository_id",
    "rules",
    "ignores",
    "max_starts_per_hour",
    "max_active_jobs",
    "output_language",
    "surrounding_lines",
)


def capture_settings(settings: RepositorySettings) -> dict[str, Any]:
    values = {
        "id": settings.id,
        **{key: deepcopy(getattr(settings, key)) for key in _FIELDS},
    }
    if settings.review_policy is not None:
        values["review_policy"] = deepcopy(settings.review_policy)
    return values


def snapshot_id(values: dict[str, Any]) -> UUID:
    payload = {key: values[key] for key in (*_FIELDS, "rules_source")}
    if values.get("review_policy") is not None:
        payload["review_policy"] = values["review_policy"]
    canonical = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    return uuid5(NAMESPACE_URL, "dmc268-rules-settings/1:" + digest)


def valid_snapshot(settings: RepositorySettings) -> bool:
    if settings.rules_source is None:
        return True
    source = settings.rules_source
    return (
        isinstance(source, dict)
        and isinstance(source.get("source_settings_id"), str)
        and isinstance(source.get("source_settings_identity"), str)
        and settings.id
        == snapshot_id({**capture_settings(settings), "rules_source": source})
    )


async def materialize_settings(
    session: AsyncSession,
    source: dict[str, Any],
    source_identity: str,
    loaded: LoadedRules,
) -> dict[str, Any]:
    values = {
        **deepcopy(source),
        "rules": {"instructions": list(loaded.rules.instructions)},
    }
    values["rules_source"] = {
        "source_settings_id": str(source["id"]),
        "source_settings_identity": source_identity,
        "file": loaded.provenance,
    }
    values["id"] = snapshot_id(values)
    values["rules_digest"] = compute_rules_digest(values["rules"])
    await session.execute(
        insert(RepositorySettings)
        .values(**values)
        .on_conflict_do_nothing(index_elements=["id"])
    )
    await session.commit()
    stored = await session.get(RepositorySettings, values["id"], populate_existing=True)
    if stored is None or not valid_snapshot(stored):
        raise ValueError("invalid_rules_snapshot")
    return values
