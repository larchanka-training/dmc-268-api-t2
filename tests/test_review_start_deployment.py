"""The deployed API image must boot with its canonical validation resources."""

import os
import shutil
import subprocess

import pytest


@pytest.mark.skipif(
    shutil.which("docker") is None or os.environ.get("DMC268_RUN_DOCKER_TESTS") != "1",
    reason="set DMC268_RUN_DOCKER_TESTS=1 with Docker running",
)
def test_api_image_serves_healthcheck() -> None:
    image = "dmc268-review-start-api-test"
    built = subprocess.run(
        ["docker", "build", "-t", image, "."],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert built.returncode == 0, built.stderr
    script = (
        "import asyncio, httpx; from app.main import create_app; "
        "exec('async def check():\\n"
        " async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()),"
        ' base_url="http://test") as client:\\n'
        '  response = await client.get("/healthcheck")\\n'
        "  assert response.status_code == 200\\n'); asyncio.run(check())"
    )
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-e",
            "LOGGING__FILE_ENABLED=false",
            image,
            "python",
            "-c",
            script,
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
