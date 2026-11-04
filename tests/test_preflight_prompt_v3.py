"""Preflight verifies the supported prompt inventory even without queued jobs."""

import shutil
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from test_runtime_manifest import PROMPTS, manifest_values, write_manifest

from app.review.configuration import load_runtime_configuration
from app.review.prompt import PromptBuilder, ReflectionBundle


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


async def test_policy_two_preflight_rejects_changed_verifier_before_http(
    tmp_path: Path,
) -> None:

    values = manifest_values()
    admission = values["admission"]
    admission["prompt_version"] = "v4"
    admission["prompt_digest"] = PromptBuilder.load(PROMPTS, "v4").prompt_digest
    admission["policy"]["schema_version"] = 2
    admission["policy"]["reflection"] = {
        "max_reflections_per_run": 1,
        "max_reflections_per_chunk": 1,
        "failure_mode": "fail_open",
        "verifier_version": "v1",
        "verifier_digest": ReflectionBundle.load(PROMPTS / "reflection", "v1").digest,
    }
    runtime = load_runtime_configuration(write_manifest(tmp_path, values))
    prompt_root = tmp_path / "reviewer"
    shutil.copytree(PROMPTS, prompt_root)
    (prompt_root / "reflection/v1/system.md").write_text("tampered")
    factory = MagicMock()
    session = AsyncMock()
    factory.return_value.__aenter__.return_value = session
    rows = MagicMock()
    rows.all.return_value = []
    session.execute.return_value = rows
    report = await runtime.offline_preflight(factory, prompt_root)
    assert not report.ready
    assert [issue.code for issue in report.issues] == ["unsupported_prompt_bundle"]


async def test_policy_two_rejects_historical_initial_prompt(tmp_path: Path) -> None:

    values = manifest_values()
    values["admission"]["policy"] |= {
        "schema_version": 2,
        "reflection": {
            "max_reflections_per_run": 1,
            "max_reflections_per_chunk": 1,
            "failure_mode": "fail_open",
            "verifier_version": "v1",
            "verifier_digest": ReflectionBundle.load(
                PROMPTS / "reflection", "v1"
            ).digest,
        },
    }
    runtime = load_runtime_configuration(write_manifest(tmp_path, values))
    factory = MagicMock()
    session = AsyncMock()
    factory.return_value.__aenter__.return_value = session
    rows = MagicMock()
    rows.all.return_value = []
    session.execute.return_value = rows
    report = await runtime.offline_preflight(factory, PROMPTS)
    assert not report.ready
