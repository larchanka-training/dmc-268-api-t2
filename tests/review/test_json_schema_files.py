"""Committed JSON Schemas must match the Pydantic models (spec §3.4)."""

import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from app.review.schemas.export import SCHEMA_FILES

_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(("relative_path", "model"), SCHEMA_FILES.items())
def test_committed_schema_matches_model(
    relative_path: str, model: type[BaseModel]
) -> None:
    committed = json.loads((_ROOT / relative_path).read_text(encoding="utf-8"))

    assert committed == model.model_json_schema(), (
        f"{relative_path} is stale: run `uv run python -m app.review.schemas.export`"
    )
