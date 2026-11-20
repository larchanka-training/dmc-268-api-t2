"""Synthetic dedicated Redis 8 restore drill on the local Docker daemon."""

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.adapters.simulation_admission import (
    STATE_KEY,
    RedisSimulationAdmission,
    create_admission_client,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "linux"
    or os.environ.get("DMC268_RUN_DOCKER_TESTS") != "1"
    or shutil.which("docker") is None,
    reason="requires Linux, local Docker Compose and explicit restore-drill opt-in",
)
ROOT = Path(__file__).resolve().parents[1]
IMAGE = "redis:8-alpine"
BACKUP_SCRIPT = "backup-simulation-redis.sh"
TEST_HMAC = b"test-only-secret-material-32bytes!"
USER = uuid.UUID(int=1)
REPOSITORY = uuid.UUID(int=2)
CONSUMED = b"drill-consumed-request"
EXPIRING = "drill:expiring"


@dataclass(frozen=True)
class Drill:
    root: Path
    project: str
    environment: dict[str, str]

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            args,
            cwd=self.root,
            env=self.environment,
            capture_output=True,
            timeout=60,
            check=check,
        )

    def docker(
        self, *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[bytes]:
        return self.run("docker", *args, check=check)

    def wait_ready(self, container: str) -> None:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if (
                self.docker("exec", container, "redis-cli", "PING", check=False).stdout
                == b"PONG\n"
            ):
                return
            time.sleep(0.2)
        pytest.fail("disposable Redis did not become ready")

    def capture(self) -> bytes:
        self.run("bash", str(self.root / BACKUP_SCRIPT), self.project, "fixture.env")
        (snapshot,) = (self.root / "backups/simulation-redis").glob("*.rdb")
        return snapshot.read_bytes()

    def socket_url(self, instance: str) -> str:
        return f"unix://{self.root / instance / 'redis.sock'}"

    def restore(self, rdb: bytes) -> str:
        snapshot = self.root / "dump.rdb"
        snapshot.write_bytes(rdb)
        container = f"{self.project}-target"
        self.docker(
            "run",
            "-d",
            "--name",
            container,
            "--pull=never",
            "--network=none",
            "--restart=no",
            "--tmpfs",
            "/data",
            "--mount",
            f"type=bind,src={snapshot},dst=/restore/dump.rdb,readonly",
            "--mount",
            f"type=bind,src={self.root / 'target'},dst=/drill",
            IMAGE,
            "redis-server",
            "--dir",
            "/restore",
            "--dbfilename",
            "dump.rdb",
            "--appendonly",
            "no",
            "--save",
            "",
            "--unixsocket",
            "/drill/redis.sock",
            "--unixsocketperm",
            "777",
        )
        return container


@pytest.fixture
def drill(tmp_path: Path) -> Iterator[Drill]:
    project = f"dmc268-redis-drill-{uuid.uuid4().hex[:12]}"
    config = tmp_path / "docker-config"
    config.mkdir()
    resources = Drill(
        tmp_path,
        project,
        {
            "PATH": os.environ.get("PATH", os.defpath),
            "DOCKER_HOST": "unix:///var/run/docker.sock",
            "DOCKER_CONFIG": str(config),
        },
    )
    for instance in ("source", "target"):
        sockets = tmp_path / instance
        sockets.mkdir()
        # Allow the Redis uid to create a socket inside the private fixture.
        sockets.chmod(0o777)
    shutil.copy2(ROOT / "scripts" / BACKUP_SCRIPT, tmp_path / BACKUP_SCRIPT)
    (tmp_path / "fixture.env").write_text("# synthetic restore drill only\n")
    (tmp_path / "docker-compose.staging.yml").write_text(
        "services:\n"
        "  simulation-redis:\n"
        f"    image: {IMAGE}\n"
        "    pull_policy: never\n"
        "    profiles: [simulation]\n"
        f"    container_name: {project}-source\n"
        "    network_mode: none\n"
        "    volumes:\n"
        "      - simulation-redis-data:/data\n"
        "      - ./source:/drill\n"
        "    command: [redis-server, --appendonly, 'yes', --appendfsync, always,\n"
        "      --aof-load-truncated, 'no', --save, '',\n"
        "      --repl-diskless-sync-delay, '0',\n"
        "      --unixsocket, /drill/redis.sock, --unixsocketperm, '777']\n"
        "volumes: {simulation-redis-data: {}}\n"
    )
    compose = (
        "compose",
        "-p",
        project,
        "--profile",
        "simulation",
        "-f",
        "docker-compose.staging.yml",
    )
    with ExitStack() as cleanup:
        cleanup.callback(resources.docker, *compose, "down", "-v")
        cleanup.callback(
            resources.docker, "rm", "-fv", f"{project}-target", check=False
        )
        resources.docker(*compose, "up", "-d")
        resources.wait_ready(f"{project}-source")
        yield resources


@pytest.fixture
async def source_admission(drill: Drill) -> AsyncIterator[RedisSimulationAdmission]:
    source_url = drill.socket_url("source")
    admission = RedisSimulationAdmission(source_url, hmac_secret=TEST_HMAC)
    try:
        async with create_admission_client(source_url) as client:
            await client.set(
                STATE_KEY,
                json.dumps(
                    {
                        "schema_version": 3,
                        "hmac_config_id": admission.hmac_config_id,
                        "consumed_keys": [],
                        "admissions": [],
                        "lease": None,
                    }
                ),
            )
        yield admission
    finally:
        await admission.aclose()


async def test_simulation_redis_restore_preserves_admission_and_expiry(
    drill: Drill, source_admission: RedisSimulationAdmission
) -> None:
    acquired = await source_admission.acquire(USER, REPOSITORY, CONSUMED)
    assert acquired.status == "ACQUIRED" and acquired.lease is not None
    async with create_admission_client(drill.socket_url("source")) as source:
        before = await source.get(STATE_KEY)
        assert before is not None
        await source.set(EXPIRING, "synthetic", ex=600)
        deadline = await source.pexpiretime(EXPIRING)
        rdb = drill.capture()
        assert await source.get(STATE_KEY) == before
        assert await source.pttl(STATE_KEY) == -1
        assert await source_admission.release(acquired.lease.owner_token) == "RELEASED"
        assert await source.get(STATE_KEY) != before
        await source.persist(EXPIRING)

    drill.wait_ready(drill.restore(rdb))
    restored_url = drill.socket_url("target")
    admission = RedisSimulationAdmission(restored_url, hmac_secret=TEST_HMAC)
    mismatch = RedisSimulationAdmission(
        restored_url, hmac_secret=b"different-test-material-32-bytes!"
    )
    try:
        async with create_admission_client(restored_url) as restored:
            assert await restored.type(STATE_KEY) == "string"
            assert await restored.get(STATE_KEY) == before
            assert await restored.pttl(STATE_KEY) == -1
            assert await restored.get(EXPIRING) == "synthetic"
            assert await restored.pexpiretime(EXPIRING) == deadline
            assert (
                await admission.acquire(USER, REPOSITORY, CONSUMED)
            ).status == "DUPLICATE"
            assert (
                await mismatch.acquire(USER, REPOSITORY, CONSUMED)
            ).status == "INVALID_STATE"
            assert await restored.get(STATE_KEY) == before
    finally:
        await admission.aclose()
        await mismatch.aclose()


@pytest.mark.parametrize("damage", ["checksum", "truncated"])
async def test_simulation_redis_restore_refuses_broken_rdb_without_restart(
    drill: Drill, source_admission: RedisSimulationAdmission, damage: str
) -> None:
    result = await source_admission.acquire(USER, REPOSITORY, CONSUMED)
    assert result.status == "ACQUIRED"
    rdb = drill.capture()
    if damage == "checksum":
        broken = rdb[:-1] + bytes([rdb[-1] ^ 1])
    else:
        broken = rdb[: len(rdb) // 2]

    target = drill.restore(broken)
    assert drill.docker("wait", target).stdout == b"1\n"
    assert (
        drill.docker("inspect", "--format", "{{.RestartCount}}", target).stdout
        == b"0\n"
    )
    assert (drill.root / "dump.rdb").read_bytes() == broken


@pytest.mark.parametrize("missing", ["admission-key", "consumed-history"])
async def test_simulation_redis_restore_with_incomplete_state_does_not_rearm(
    drill: Drill, source_admission: RedisSimulationAdmission, missing: str
) -> None:
    result = await source_admission.acquire(USER, REPOSITORY, CONSUMED)
    assert result.status == "ACQUIRED"
    async with create_admission_client(drill.socket_url("source")) as source:
        if missing == "admission-key":
            await source.delete(STATE_KEY)
        else:
            raw = await source.get(STATE_KEY)
            assert raw is not None
            incomplete = json.loads(raw)
            del incomplete["consumed_keys"]
            await source.set(STATE_KEY, json.dumps(incomplete))
        before = await source.get(STATE_KEY)
        ttl = await source.pttl(STATE_KEY)

    drill.wait_ready(drill.restore(drill.capture()))
    restored_url = drill.socket_url("target")
    admission = RedisSimulationAdmission(restored_url, hmac_secret=TEST_HMAC)
    try:
        async with create_admission_client(restored_url) as restored:
            assert await restored.get(STATE_KEY) == before
            assert (
                await admission.acquire(USER, REPOSITORY, CONSUMED)
            ).status == "INVALID_STATE"
            assert await restored.get(STATE_KEY) == before
            assert await restored.pttl(STATE_KEY) == ttl
    finally:
        await admission.aclose()
