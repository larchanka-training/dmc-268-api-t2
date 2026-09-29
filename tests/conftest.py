"""Shared fixtures: a minimal L1-only ContextPayload (SD §6 example)."""

from collections.abc import Callable
from typing import Any

import pytest

from app.review.schemas.context import FileDiff, FileStatus, Hunk, HunkLine, LineKind


def _hunk() -> dict[str, Any]:
    return {
        "hunk_id": "hunk_1",
        "old_start": 38,
        "old_count": 2,
        "new_start": 38,
        "new_count": 2,
        "lines": [
            {
                "kind": "context",
                "text": "def validate(data):",
                "old_line": 38,
                "new_line": 38,
            },
            {
                "kind": "deleted",
                "text": "    return data",
                "old_line": 39,
                "new_line": None,
            },
            {
                "kind": "added",
                "text": "    return Schema.model_validate(data)",
                "old_line": None,
                "new_line": 39,
            },
        ],
    }


@pytest.fixture
def payload_data() -> dict[str, Any]:
    """Raw JSON-like ContextPayload with one modified file and one hunk."""
    return {
        "schema_version": 1,
        "review_id": "rev_01",
        "chunk_id": "chunk_001",
        "snapshot": {
            "provider": "github",
            "base_repository_id": "100",
            "head_repository_id": "101",
            "base_sha": "base-commit-sha",
            "merge_base_sha": "merge-base-sha",
            "head_sha": "head-commit-sha",
            "captured_at": "2026-09-13T18:00:00Z",
            "provider_metadata": {"pull_number": 42},
        },
        "metadata": {
            "title": "Validate review payload",
            "body": None,
            "base_ref": "main",
            "head_ref": "feature/review",
            "commit_messages": ["Add payload validation"],
            "languages": ["Python"],
            "discussions": [],
            "rules_version": "rules-v3",
            "output_language": "ru",
        },
        "files": [
            {
                "diff": {
                    "file_id": "file_1",
                    "old_path": "src/review.py",
                    "new_path": "src/review.py",
                    "status": "modified",
                    "old_blob_sha": "old-blob-sha",
                    "new_blob_sha": "new-blob-sha",
                    "language": "Python",
                    "hunks": [_hunk()],
                },
                "source_windows": [],
                "whole_file": None,
            }
        ],
        "related_symbols": [],
        "coverage": {
            "included": [
                {
                    "path": "src/review.py",
                    "ranges": [{"start_line": 39, "end_line": 39}],
                }
            ],
            "skipped": [],
            "context_quality": "diff_only",
        },
        "budget": {
            "estimated_input_tokens": 4200,
            "reserved_output_tokens": 2000,
            "limit_input_tokens": 10000,
        },
    }


def _make_diff(  # noqa: PLR0913
    path: str,
    *,
    status: str = "modified",
    added: int = 1,
    start: int = 1,
    hunks: int = 1,
    text: str = "x = 1",
) -> FileDiff:
    """FileDiff with ``hunks`` hunks of ``added`` added lines each."""
    built = []
    for index in range(hunks):
        first = start + index * (added + 10)
        lines = [
            HunkLine(kind=LineKind.ADDED, text=text, old_line=None, new_line=first + i)
            for i in range(added)
        ]
        built.append(
            Hunk(
                hunk_id=f"{path}#h{index}",
                old_start=first - 1,
                old_count=0,
                new_start=first,
                new_count=added,
                lines=lines,
            )
        )
    deleted = status == "deleted"
    return FileDiff(
        file_id=f"file:{path}",
        old_path=None if status == "added" else path,
        new_path=None if deleted else path,
        status=FileStatus(status),
        old_blob_sha=None if status == "added" else "old",
        new_blob_sha=None if deleted else "new",
        language=None,
        hunks=built,
    )


MakeDiff = Callable[..., FileDiff]


@pytest.fixture
def make_diff() -> MakeDiff:
    """Factory for FileDiff test data (see ``_make_diff``)."""
    return _make_diff
