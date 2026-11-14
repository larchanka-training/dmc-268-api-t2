"""Opt-in real Redis persistence drills; only UUID-owned Compose projects.

DMC268_TEST_SIMULATION_PERSISTENCE=1 enables local Docker. No staging service,
network or volume is reused. Published ephemeral loopback ports are test-only.
"""

import copy
import json
import os
import shutil
import statistics
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from redis.exceptions import RedisError

from app.adapters.redis_queue import RedisQueue
from app.adapters.simulation_admission import STATE_KEY, RedisSimulationAdmission
from app.application.ports import QueueTaskEnvelope
from app.application.simulation_execution import SimulationExecutionCoordinator

# ruff: noqa: PLR2004 -- exact synthetic history size and Redis TTL values

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class RedisProject:
    project: str
    compose_file: Path

    def compose(
        self, *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "docker",
                "compose",
                "-p",
                self.project,
                "-f",
                str(self.compose_file),
                *args,
            ],
            env={"PATH": os.environ["PATH"]},
            capture_output=True,
            text=True,
            check=check,
            timeout=60,
        )

    def url(self, service: str = "simulation-redis") -> str:
        address = self.compose(
            "--profile", "simulation", "port", service, "6379"
        ).stdout.strip()
        return f"redis://{address}/0"


@pytest.fixture
def redis_project(tmp_path: Path) -> Iterator[RedisProject]:
    if (
        os.environ.get("DMC268_TEST_SIMULATION_PERSISTENCE") != "1"
        and os.environ.get("DMC268_RUN_DOCKER_TESTS") != "1"
    ):
        pytest.skip("requires opt-in isolated Docker persistence drill")
    if not shutil.which("docker"):
        pytest.skip("Docker unavailable")
    ready = subprocess.run(
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        capture_output=True,
        timeout=10,
        check=False,
    )
    if ready.returncode:
        pytest.skip("Docker daemon unavailable")
    raw = yaml.safe_load((ROOT / "docker-compose.staging.yml").read_text())
    dedicated = copy.deepcopy(raw["services"]["simulation-redis"])
    dedicated["ports"] = ["127.0.0.1::6379"]
    shared = copy.deepcopy(raw["services"]["redis"])
    shared["volumes"] = ["shared-test-data:/data"]
    shared["ports"] = ["127.0.0.1::6379"]
    project = RedisProject(
        f"dmc268-e1b2a-drill-{uuid4().hex}", tmp_path / "compose.json"
    )
    project.compose_file.write_text(
        json.dumps(
            {
                "services": {"redis": shared, "simulation-redis": dedicated},
                "volumes": {"shared-test-data": {}, "simulation-redis-data": {}},
                "networks": {"backend": {}},
            }
        )
    )
    try:
        project.compose("up", "-d", "--wait", "--wait-timeout", "40")
        assert not project.compose(
            "--profile", "simulation", "ps", "-q", "simulation-redis"
        ).stdout.strip()
        project.compose(
            "--profile",
            "simulation",
            "up",
            "-d",
            "--wait",
            "--wait-timeout",
            "40",
            "simulation-redis",
        )
        yield project
    finally:
        # Only volumes owned by this exact random project can be removed.
        project.compose("--profile", "simulation", "down", "-v", "--remove-orphans")


async def test_effective_dedicated_persistence_policy(
    redis_project: RedisProject,
) -> None:
    async with Redis.from_url(redis_project.url(), decode_responses=True) as client:
        actual = await client.config_get(
            "appendonly",
            "appendfsync",
            "maxmemory-policy",
            "aof-load-truncated",
            "no-appendfsync-on-rewrite",
            "dir",
        )
        assert actual == {
            "appendonly": "yes",
            "appendfsync": "always",
            "maxmemory-policy": "noeviction",
            "aof-load-truncated": "no",
            "no-appendfsync-on-rewrite": "no",
            "dir": "/data",
        }
        assert (await client.info("persistence"))["aof_enabled"] == 1


async def test_admission_history_survives_restart_and_repeated_recreation(
    redis_project: RedisProject,
) -> None:

    secret = b"synthetic-persistence-hmac-key-32!"
    user, repository = uuid4(), uuid4()
    admission = RedisSimulationAdmission(redis_project.url(), hmac_secret=secret)
    async with Redis.from_url(redis_project.url(), decode_responses=True) as client:
        await client.set(
            STATE_KEY,
            json.dumps(
                {
                    "schema_version": 3,
                    "hmac_config_id": admission.hmac_config_id,
                    "lease": None,
                    "admissions": [],
                    "consumed_keys": [],
                }
            ),
        )
        latencies = []
        for index in range(10):
            started = time.perf_counter()
            result = await admission.acquire(
                uuid4(), uuid4(), f"synthetic-{index}".encode()
            )
            assert result.status == "ACQUIRED" and result.lease is not None
            assert await admission.release(result.lease.owner_token) == "RELEASED"
            latencies.append((time.perf_counter() - started) * 1000)
        result = await admission.acquire(user, repository, b"retained-key")
        assert result.status == "ACQUIRED" and result.lease is not None
        # Retain the active lease as well as both histories.
        before = await client.get(STATE_KEY)
        assert before is not None
        state = json.loads(before)
        assert len(state["consumed_keys"]) == len(state["admissions"]) == 11
        assert state["hmac_config_id"] == admission.hmac_config_id
        assert await client.pttl(STATE_KEY) == -1
        assert (await client.info("persistence"))["aof_last_write_status"] == "ok"
        mount_before = redis_project.compose(
            "--profile", "simulation", "ps", "-q", "simulation-redis"
        ).stdout.strip()
        print(
            f"synthetic acquire+release latency ms: "
            f"median={statistics.median(latencies):.3f}, max={max(latencies):.3f}"
        )
    await admission.aclose()
    for operation in ["restart", "recreate", "recreate"]:
        if operation == "restart":
            redis_project.compose(
                "--profile", "simulation", "restart", "simulation-redis"
            )
        else:
            redis_project.compose(
                "--profile",
                "simulation",
                "up",
                "-d",
                "--no-deps",
                "--force-recreate",
                "--wait",
                "--wait-timeout",
                "40",
                "simulation-redis",
            )
            new_id = redis_project.compose(
                "--profile", "simulation", "ps", "-q", "simulation-redis"
            ).stdout.strip()
            assert new_id != mount_before
            mount_before = new_id
        async with Redis.from_url(redis_project.url(), decode_responses=True) as client:
            assert await client.get(STATE_KEY) == before
            assert await client.pttl(STATE_KEY) == -1
        admission = RedisSimulationAdmission(redis_project.url(), hmac_secret=secret)
        try:
            assert (
                await admission.acquire(user, repository, b"retained-key")
            ).status == "DUPLICATE"
        finally:
            await admission.aclose()
        async with Redis.from_url(redis_project.url(), decode_responses=True) as client:
            assert await client.get(STATE_KEY) == before


async def test_missing_invalid_and_mismatched_state_are_never_replaced(
    redis_project: RedisProject,
) -> None:

    admission = RedisSimulationAdmission(
        redis_project.url(), hmac_secret=b"synthetic-persistence-hmac-key-32!"
    )
    calls = 0

    async def inference(deadline: float) -> str:
        nonlocal calls
        calls += 1
        return "forbidden"

    coordinator = SimulationExecutionCoordinator[str](admission)
    try:
        async with Redis.from_url(redis_project.url(), decode_responses=True) as client:
            for raw in [
                None,
                "{broken",
                json.dumps(
                    {
                        "schema_version": 3,
                        "hmac_config_id": "0" * 64,
                        "lease": None,
                        "admissions": [],
                        "consumed_keys": [],
                    }
                ),
            ]:
                if raw is None:
                    await client.delete(STATE_KEY)
                else:
                    await client.set(STATE_KEY, raw)
                assert (
                    await admission.acquire(uuid4(), uuid4(), b"synthetic-negative")
                ).status == "INVALID_STATE"
                result = await coordinator.execute(
                    uuid4(), uuid4(), b"synthetic-negative", inference
                )
                assert result.status == "INVALID_STATE"
                assert await client.get(STATE_KEY) == raw
                assert await client.pttl(STATE_KEY) == (-2 if raw is None else -1)
            assert calls == 0
    finally:
        await coordinator.aclose()
        await admission.aclose()


async def test_dedicated_failure_leaves_review_queue_operational(
    redis_project: RedisProject,
) -> None:

    dedicated_url = redis_project.url()
    async with Redis.from_url(
        redis_project.url("redis"), decode_responses=True
    ) as shared:
        queue = RedisQueue(shared, stream_prefix="synthetic:review-isolation")
        envelope = QueueTaskEnvelope(
            schema_version=1,
            event_id="synthetic-event",
            review_id="synthetic-review",
            task_kind="analyze",
            attempt=1,
            trace_id="synthetic-trace",
        )
        message_id = await queue.enqueue(envelope)
        before = await shared.config_get("appendonly", "maxmemory-policy")
        redis_project.compose("--profile", "simulation", "stop", "simulation-redis")
        admission = RedisSimulationAdmission(
            dedicated_url, hmac_secret=b"synthetic-persistence-hmac-key-32!"
        )
        coordinator = SimulationExecutionCoordinator[str](admission)
        calls = 0

        async def inference(deadline: float) -> str:
            nonlocal calls
            calls += 1
            return "forbidden"

        try:
            assert (
                await coordinator.execute(uuid4(), uuid4(), b"outage", inference)
            ).status == "UNAVAILABLE"
            assert calls == 0
            received = await queue.receive(
                task_kind="analyze", consumer_name="synthetic-worker"
            )
            assert received is not None and received.message_id == message_id
            assert received.envelope == envelope
            await queue.ack(received)
            assert (
                await shared.xpending(
                    "synthetic:review-isolation:analyze", "review-workers"
                )
            )["pending"] == 0
            assert await shared.config_get("appendonly", "maxmemory-policy") == before
        finally:
            await coordinator.aclose()
            await admission.aclose()


async def test_truncated_aof_refuses_startup(redis_project: RedisProject) -> None:
    async with Redis.from_url(redis_project.url(), decode_responses=True) as client:
        await client.set("synthetic-sentinel", "retained")
    redis_project.compose("--profile", "simulation", "stop", "simulation-redis")
    # Corrupt only this UUID project's stopped dedicated volume; never repair it.
    redis_project.compose(
        "--profile",
        "simulation",
        "run",
        "--rm",
        "--no-deps",
        "--entrypoint",
        "sh",
        "simulation-redis",
        "-c",
        "for f in /data/appendonlydir/*.incr.aof; do "
        'printf "*3\\r\\n\\0443\\r\\nSET\\r\\n\\0445\\r\\n" >> "$f"; done',
    )
    result = redis_project.compose(
        "--profile",
        "simulation",
        "up",
        "-d",
        "--no-deps",
        "--wait",
        "--wait-timeout",
        "10",
        "simulation-redis",
        check=False,
    )
    assert result.returncode != 0
    logs = redis_project.compose(
        "--profile", "simulation", "logs", "simulation-redis"
    ).stdout
    assert "AOF" in logs and "truncated" in logs.lower()


async def test_persistence_write_failure_never_acknowledges_unsafe_write(
    redis_project: RedisProject,
) -> None:

    # A per-process file-size limit injects a real AOF I/O failure without filling
    # the host disk or touching any other Docker volume.
    raw = json.loads(redis_project.compose_file.read_text())
    raw["services"]["simulation-redis"]["ulimits"] = {
        "fsize": {"soft": 65536, "hard": 65536}
    }
    redis_project.compose_file.write_text(json.dumps(raw))
    redis_project.compose(
        "--profile",
        "simulation",
        "up",
        "-d",
        "--no-deps",
        "--force-recreate",
        "--wait",
        "--wait-timeout",
        "40",
        "simulation-redis",
    )
    url = redis_project.url()
    async with Redis.from_url(
        url, socket_timeout=2, retry=Retry(NoBackoff(), 0)
    ) as client:
        with pytest.raises(RedisError):
            await client.set("synthetic-file-limit", b"x" * 131072)
    admission = RedisSimulationAdmission(
        url, hmac_secret=b"synthetic-persistence-hmac-key-32!"
    )
    coordinator = SimulationExecutionCoordinator[str](admission)
    calls = 0

    async def inference(deadline: float) -> str:
        nonlocal calls
        calls += 1
        return "forbidden"

    try:
        assert (
            await coordinator.execute(
                uuid4(), uuid4(), b"persistence-failure", inference
            )
        ).status == "UNAVAILABLE"
        assert calls == 0
    finally:
        await coordinator.aclose()
        await admission.aclose()
