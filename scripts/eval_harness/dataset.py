"""Loading fixtures from a test-prs-dataset directory."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class Fixture:
    id: str
    category: str
    title: str
    description: str
    language: str
    diff_text: str
    expected_findings: list[dict[str, Any]]
    golden_response: dict[str, Any]


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_manifest(dataset_dir: Path) -> list[dict[str, str]]:
    manifest: list[dict[str, str]] = _read_json(dataset_dir / "manifest.json")
    return manifest


def load_fixture(dataset_dir: Path, fixture_id: str) -> Fixture:
    fixture_dir = dataset_dir / "fixtures" / fixture_id
    meta = _read_json(fixture_dir / "meta.json")
    diff_text = (fixture_dir / "pr.diff").read_text(encoding="utf-8")
    expected_findings = _read_json(fixture_dir / "expected_findings.json")
    golden_response = _read_json(fixture_dir / "golden_response.json")
    return Fixture(
        id=fixture_id,
        category=meta["category"],
        title=meta["title"],
        description=meta.get("description", ""),
        language=meta.get("language", ""),
        diff_text=diff_text,
        expected_findings=expected_findings,
        golden_response=golden_response,
    )


def load_dataset(
    dataset_dir: Path, *, category_filter: str | None = None
) -> list[Fixture]:
    """Load every fixture listed in ``manifest.json``, in manifest order."""

    fixtures = []
    for entry in load_manifest(dataset_dir):
        if category_filter is not None and entry["category"] != category_filter:
            continue
        fixtures.append(load_fixture(dataset_dir, entry["id"]))
    return fixtures
