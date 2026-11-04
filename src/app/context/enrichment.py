"""Bounded snapshot-side source enrichment, persisted before any model call."""

import asyncio
from typing import Any

from app.application.github_ports import GithubFailure, SourceFileReader
from app.context.ast import extract_context

MAX_SOURCE_REQUESTS = 16


async def enrich_context(body: dict[str, Any], reader: SourceFileReader) -> None:
    snapshot = body["snapshot"]
    degraded = False
    languages: set[str] = set()
    calls = 0
    try:
        async with asyncio.timeout(30):
            for file in body["files"]:
                diff = file["diff"]
                for side in ("OLD", "NEW"):
                    key = side.lower()
                    path = diff[f"{key}_path"]
                    changed = {
                        line[f"{key}_line"]
                        for hunk in diff["hunks"]
                        for line in hunk["lines"]
                        if line[f"{key}_line"] is not None
                    }
                    if not path or not changed:
                        continue
                    if calls >= MAX_SOURCE_REQUESTS:
                        degraded = True
                        continue
                    calls += 1
                    revision = (
                        snapshot["head_sha"]
                        if side == "NEW"
                        else snapshot["merge_base_sha"]
                    )
                    repository = (
                        snapshot["head_repository_id"]
                        if side == "NEW"
                        else snapshot["base_repository_id"]
                    )
                    try:
                        source = await reader.read_file_by_id(
                            repository, revision, path, max_bytes=65536
                        )
                    except GithubFailure:
                        degraded = True
                        continue
                    if source is None:
                        degraded = True
                        continue
                    expected_blob = diff[f"{key}_blob_sha"]
                    if expected_blob and not source.blob_sha.startswith(expected_blob):
                        degraded = True
                        continue
                    extracted = await asyncio.to_thread(
                        extract_context, path, source.text, revision, changed
                    )
                    degraded |= extracted.degraded
                    if extracted.language:
                        languages.add(extracted.language)
                        diff["language"] = extracted.language
                    for window in extracted.windows:
                        window["side"] = side
                        window["covered_hunk_ids"] = [
                            hunk["hunk_id"]
                            for hunk in diff["hunks"]
                            if any(
                                window["start_line"]
                                <= line[f"{key}_line"]
                                <= window["end_line"]
                                for line in hunk["lines"]
                                if line[f"{key}_line"] is not None
                            )
                        ]
                    file["source_windows"].extend(extracted.windows)
                    body["related_symbols"].extend(extracted.symbols)
    except TimeoutError:
        degraded = True
    body["metadata"]["languages"] = sorted(languages)
    body["coverage"]["context_quality"] = "degraded" if degraded else "full"
