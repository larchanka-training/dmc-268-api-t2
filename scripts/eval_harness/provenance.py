"""Content identities for reproducible replay and provider measurements."""

import hashlib
import json
from pathlib import Path
from typing import Any

from app.review.prompt import PromptBuilder
from scripts.eval_harness.dataset import dataset_file, load_manifest

MATCHING_POLICY = "path-side-category-overlap-greedy-one-to-one-production-anchor/v2"


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


def dataset_digest(directory: Path) -> str:
    paths = [directory / "manifest.json"]
    for entry in load_manifest(directory):
        paths.extend(
            directory / "fixtures" / entry["id"] / name
            for name in (
                "meta.json",
                "pr.diff",
                "expected_findings.json",
                "golden_response.json",
            )
        )
    paths.extend((directory / "licenses").glob("*"))
    contents = {
        path.relative_to(directory).as_posix(): hashlib.sha256(
            dataset_file(directory, path).read_bytes()
        ).hexdigest()
        for path in sorted(paths)
    }
    return digest(contents)


def replay_provenance(
    directory: Path, builder: PromptBuilder, category: str | None
) -> dict[str, Any]:
    config = {
        "mode": "offline",
        "category_filter": category,
        "prompt_version": builder.prompt_version,
        "prompt_digest": builder.prompt_digest,
        "schema_sha256": digest(json.loads(builder.output_schema_json)),
        "matching_policy": MATCHING_POLICY,
    }
    return {
        **config,
        "config_sha256": digest(config),
        "dataset_sha256": dataset_digest(directory),
        "quality_measurement": False,
        "model": None,
        "provider": None,
        "scope": "handwritten-response-replay; no model executed",
    }
