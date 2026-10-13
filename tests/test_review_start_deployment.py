"""The deployed API image must boot with its canonical validation resources."""

import os
import shutil
import subprocess
from textwrap import dedent

import pytest


@pytest.mark.skipif(
    shutil.which("docker") is None or os.environ.get("DMC268_RUN_DOCKER_TESTS") != "1",
    reason="set DMC268_RUN_DOCKER_TESTS=1 with Docker running",
)
def test_api_image_serves_healthcheck_and_loads_analysis_resources() -> None:
    image = "dmc268-review-start-api-test"
    built = subprocess.run(
        ["docker", "build", "-t", image, "."],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert built.returncode == 0, built.stderr
    script = dedent(
        """
        import asyncio
        import json
        from pathlib import Path

        import httpx
        from jsonschema import Draft202012Validator
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from app.adapters.analysis_handler import PostgresAnalysisHandler
        from app.main import create_app
        from app.review.prompt import PromptBuilder

        # Construction loads canonical schemas without any provider or DB I/O.
        PostgresAnalysisHandler(
            async_sessionmaker(), None, None, schemas_dir=Path("/app/schemas")
        )
        Draft202012Validator.check_schema(
            json.loads(Path("/app/schemas/llm-output/v1.json").read_text())
        )
        prompt = PromptBuilder.load(Path("/app/.agents/reviewer"), "v1")
        assert prompt.prompt_version == "v1"

        async def check():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=create_app()),
                base_url="http://test",
            ) as client:
                response = await client.get("/healthcheck")
                assert response.status_code == 200

        asyncio.run(check())
        """
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
