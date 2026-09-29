"""Write normative JSON Schemas: `uv run python -m app.review.schemas.export`."""

import json
from pathlib import Path

from pydantic import BaseModel

from app.review.schemas.context import ContextPayload
from app.review.schemas.result import LLMReviewOutput

SCHEMA_FILES: dict[str, type[BaseModel]] = {
    "schemas/context/v1.json": ContextPayload,
    "schemas/review-result/v1.json": LLMReviewOutput,
}


def write_schemas(root: Path) -> None:
    """Regenerate every schema file under ``root`` (the repository root)."""
    for relative_path, model in SCHEMA_FILES.items():
        target = root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(model.model_json_schema(), ensure_ascii=False, indent=2)
        target.write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    write_schemas(Path.cwd())
