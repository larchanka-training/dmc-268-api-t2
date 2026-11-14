"""Explicit local fixtures; never register integration modules as plugins."""

import copy
import json
import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
import yaml


@pytest.fixture(scope="session")
def dedicated_simulation_redis_url(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[str]:
    explicit = os.environ.get("DMC268_TEST_SIMULATION_REDIS_URL")
    if explicit:
        yield explicit
        return
    if os.environ.get("DMC268_RUN_DOCKER_TESTS") != "1":
        pytest.skip("requires dedicated disposable Redis URL or Docker test opt-in")
    if not shutil.which("docker"):
        pytest.skip("Docker unavailable for dedicated Redis fixture")
    ready = subprocess.run(
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        capture_output=True,
        check=False,
        timeout=10,
    )
    if ready.returncode:
        pytest.skip("Docker daemon unavailable for dedicated Redis fixture")
    root = Path(__file__).resolve().parents[1]
    raw = yaml.safe_load((root / "docker-compose.staging.yml").read_text())
    service = copy.deepcopy(raw["services"]["simulation-redis"])
    service["ports"] = ["127.0.0.1::6379"]
    project = f"dmc268-simulation-runtime-test-{uuid4().hex}"
    compose_file = tmp_path_factory.mktemp("simulation-redis") / "compose.json"
    compose_file.write_text(
        json.dumps(
            {
                "services": {"simulation-redis": service},
                "volumes": {"simulation-redis-data": {}},
                "networks": {"backend": {}},
            }
        )
    )

    def compose(*args: str) -> str:
        result = subprocess.run(
            [
                "docker",
                "compose",
                "-p",
                project,
                "-f",
                str(compose_file),
                "--profile",
                "simulation",
                *args,
            ],
            env={"PATH": os.environ["PATH"]},
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        return result.stdout

    try:
        compose("up", "-d", "--wait", "--wait-timeout", "40")
        address = compose("port", "simulation-redis", "6379").strip()
        yield f"redis://{address}/0"
    finally:
        # The exact UUID project owns this test volume; no staging storage.
        compose("down", "-v", "--remove-orphans")
