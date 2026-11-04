"""Preflight verifies the supported prompt inventory even without queued jobs."""

import shutil
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from test_runtime_manifest import PROMPTS, manifest_values, write_manifest

from app.review.configuration import load_runtime_configuration


async def test_preflight_checks_v3_artifacts_before_admission_switch(
    tmp_path: Path,
) -> None:
    runtime = load_runtime_configuration(write_manifest(tmp_path, manifest_values()))
    prompt_root = tmp_path / "reviewer"
    shutil.copytree(PROMPTS, prompt_root)
    (prompt_root / "v3/system.md").write_text("tampered", encoding="utf-8")
    factory = MagicMock()
    session = AsyncMock()
    factory.return_value.__aenter__.return_value = session
    rows = MagicMock()
    rows.all.return_value = []
    session.execute.return_value = rows

    report = await runtime.offline_preflight(factory, prompt_root)
    assert not report.ready
    assert [issue.code for issue in report.issues] == ["unsupported_prompt_bundle"]
