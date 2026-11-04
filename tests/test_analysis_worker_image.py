"""Analysis CLI and its trusted artifacts are usable in the production image."""

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "dmc268-analysis-worker-test:8c0401"


@pytest.mark.skipif(
    shutil.which("docker") is None or os.environ.get("DMC268_RUN_DOCKER_TESTS") != "1",
    reason="set DMC268_RUN_DOCKER_TESTS=1 with Docker running",
)
def test_image_analysis_help_and_verified_artifacts_without_dependencies(
    tmp_path: Path,
) -> None:
    # Limit context to Dockerfile COPY inputs. In particular, .scratch can contain
    # historical checkout copies/logs and must not enter this disposable build.
    context = tmp_path / "analysis-image-context"
    context.mkdir()
    for name in ("Dockerfile", "pyproject.toml", "uv.lock", "README.md", "alembic.ini"):
        shutil.copyfile(ROOT / name, context / name)
    for name in ("src", "migrations", "schemas", ".agents/reviewer"):
        shutil.copytree(
            ROOT / name,
            context / name,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
    subprocess.run(
        ["docker", "build", "-q", "-t", IMAGE, str(context)],
        capture_output=True,
        text=True,
        check=True,
        timeout=300,
    )
    help_result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "-e",
            "ANALYSIS__CONFIG_PATH=/missing/manifest.json",
            "-e",
            "POSTGRES__PORT=invalid",
            "-e",
            "REDIS__URL=invalid",
            "-e",
            "LLM__EUROROUTER__API_KEYS=",
            "-e",
            "GITHUB__TOKEN=",
            IMAGE,
            "python",
            "-m",
            "app.analysis_worker",
            "--help",
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert help_result.returncode == 0, help_result.stderr
    assert "--once" in help_result.stdout
    assert "--poll-interval" in help_result.stdout
    assert "analysis_worker_failed" not in help_result.stdout + help_result.stderr

    digests = {}
    for version in ("v1", "v2"):
        descriptor = json.loads(
            (ROOT / f"tests/review/fixtures/{version}-descriptor.json").read_text()
        )
        canonical = json.dumps(
            descriptor,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        digests[version] = hashlib.sha256(canonical).hexdigest()
    probe = """
import json, sys
from pathlib import Path
from app.review.prompt import PromptBuilder
expected = json.load(sys.stdin)
for version in ('v1', 'v2'):
    builder = PromptBuilder.load(
        Path('.agents/reviewer'), version, expected_digest=expected[version]
    )
    builder.ensure_verified()
    assert builder.prompt_digest == expected[version]
for name in ('context', 'llm-output', 'review-result'):
    schema = json.loads(Path(f'schemas/{name}/v1.json').read_text())
    assert schema['$schema'] == 'https://json-schema.org/draft/2020-12/schema'
print('verified v1 v2; context llm-output review-result')
"""
    artifacts = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-i",
            "--network",
            "none",
            IMAGE,
            "python",
            "-c",
            probe,
        ],
        input=json.dumps(digests),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert artifacts.returncode == 0, artifacts.stderr
    assert (
        artifacts.stdout.strip() == "verified v1 v2; context llm-output review-result"
    )
