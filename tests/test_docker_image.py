"""The production image carries everything the app loads from disk at runtime."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
_IMAGE = "dmc268-api-test:image-contents"


@pytest.mark.skipif(
    shutil.which("docker") is None or os.environ.get("DMC268_RUN_DOCKER_TESTS") != "1",
    reason="set DMC268_RUN_DOCKER_TESTS=1 with Docker running",
)
def test_image_loads_reviewer_prompts() -> None:
    subprocess.run(
        ["docker", "build", "-q", "-t", _IMAGE, str(ROOT)],
        capture_output=True,
        check=True,
    )
    load = (
        "from pathlib import Path; from app.review.prompt import PromptBuilder; "
        "print(PromptBuilder.load(Path('.agents/reviewer'), 'v1').prompt_version)"
    )

    result = subprocess.run(
        ["docker", "run", "--rm", _IMAGE, "python", "-c", load],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "v1"
