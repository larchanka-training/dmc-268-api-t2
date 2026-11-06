"""Public CLI checks for the reviewable specification artifacts (#22)."""

import subprocess
import sys
from pathlib import Path

EXISTING_SELFCHECK_COUNT = 28


def test_selfcheck_rejects_invalid_schema_from_selected_documents(
    tmp_path: Path,
) -> None:
    rules = tmp_path / "rules.md"
    context = tmp_path / "context.md"
    rules.write_text('```json\n{"type": "not-a-json-schema-type"}\n```\n')
    context.write_text('```json\n{"type": "object"}\n```\n')
    result = subprocess.run(
        [
            sys.executable,
            "spikes/spec_selfcheck.py",
            "--rules-spec",
            str(rules),
            "--context-spec",
            str(context),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "invalid schema" in result.stdout
    assert "not-a-json-schema-type" not in result.stdout


def test_checked_in_specifications_pass_all_existing_selfchecks() -> None:
    """Run the existing document examples and compatibility checks in pytest."""
    result = subprocess.run(
        [sys.executable, "spikes/spec_selfcheck.py"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 failure(s)" in result.stdout
    assert (
        len([line for line in result.stdout.splitlines() if line.startswith("PASS ")])
        == EXISTING_SELFCHECK_COUNT
    )
