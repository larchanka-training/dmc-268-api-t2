"""Prepare dedicated staging Redis via SSH stdin, one approved operation at a time."""

import hashlib
import ipaddress
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

ROOT = Path("/opt/dmc-268-t2")
PROJECT = "dmc-268-t2-staging"
VOLUME = PROJECT + "_simulation-redis-data"
ENV_FILE = ROOT / ".env.staging"
COMPOSE_FILE = ROOT / "docker-compose.staging.yml"
SERVICE = "simulation-redis"
NETWORK = PROJECT + "_backend"
IMAGE = "redis:8-alpine"
GNU_TIMEOUT_EXIT = 124
DOCKER_SOCKET = "/var/run/docker.sock"

# An external network is lookup-only for Compose, even if its configuration has drifted.
NETWORK_OVERRIDE = f"networks: {{backend: {{external: true, name: {NETWORK}}}}}\n"


class BlockedError(Exception):
    """An allowlisted reason safe to publish in the workflow log."""


def run_docker_command(
    *args: str, mutation: bool = False, timeout: int = 8, stdin: str | None = None
) -> str:
    result = subprocess.run(
        # Compose is a Docker CLI child process. Bound the entire process group.
        ["timeout", "--kill-after=3s", f"{timeout}s", *args],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout + 5,
        env=os.environ
        | {
            "COMPOSE_REMOVE_ORPHANS": "false",
            "COMPOSE_IGNORE_ORPHANS": "true",
        },
    )
    if result.returncode == GNU_TIMEOUT_EXIT:
        raise BlockedError("docker_command_timed_out")
    if result.returncode or (not mutation and result.stderr):
        raise BlockedError("docker_command_failed")
    return result.stdout


def docker(
    *args: str, mutation: bool = False, timeout: int = 8, stdin: str | None = None
) -> str:
    return run_docker_command(
        "docker",
        "--host",
        "unix://" + DOCKER_SOCKET,
        *args,
        mutation=mutation,
        timeout=timeout,
        stdin=stdin,
    )


def container_ids(*filters: str) -> list[str]:
    ids = docker(
        "ps",
        "--all",
        "--no-trunc",
        "--format",
        "{{.ID}}",
        *(arg for value in filters for arg in ("--filter", value)),
    ).splitlines()
    if any(not re.fullmatch(r"[0-9a-f]{64}", container_id) for container_id in ids):
        raise BlockedError("invalid_container_listing")
    return ids


def container(service: str) -> dict[str, Any] | None:
    ids = container_ids(
        "label=com.docker.compose.project=" + PROJECT,
        "label=com.docker.compose.service=" + service,
    )
    if not ids:
        return None
    if len(ids) != 1:
        raise BlockedError("ambiguous_container")
    [entry] = json.loads(docker("inspect", "--type", "container", ids[0]))
    labels = entry["Config"]["Labels"]
    if (
        entry["Id"] != ids[0]
        or labels["com.docker.compose.project"] != PROJECT
        or labels["com.docker.compose.service"] != service
    ):
        raise BlockedError("wrong_container_identity")
    return dict(entry)


def healthy(entry: dict[str, Any]) -> bool:
    state = entry["State"]
    return (
        state["Running"] is True and state.get("Health", {}).get("Status") == "healthy"
    )


def require_disabled(api: dict[str, Any]) -> None:
    flag = "SIMULATION__ENABLED"
    runtime = [item for item in api["Config"]["Env"] if item.partition("=")[0] == flag]
    on_disk = [
        line
        for line in ENV_FILE.read_text().splitlines()
        if re.match(rf"\s*(?:export\s+)?{flag}(?:\s*[=:]|\s*$)", line)
    ]
    if runtime != [flag + "=false"] or on_disk != runtime:
        raise BlockedError("playground_not_disabled")


def require_rollback_clear() -> None:
    try:
        ENV_FILE.with_name(".env.staging.simulation-rollback").lstat()
    except FileNotFoundError:
        return
    raise BlockedError("rollback_pending")


def require_no_redis_container() -> None:
    if (
        container("simulation-redis") is not None
        or container_ids("name=^/?" + PROJECT + "[-_]simulation-redis[-_][0-9]+$")
        or container_ids("volume=" + VOLUME)
    ):
        raise BlockedError("redis_container_exists")


def require_volume_precondition(operation: str) -> None:
    # The Docker CLI drops the Engine warning array, including partial listings.
    inventory = json.loads(
        run_docker_command(
            "curl",
            "-q",
            "-fsS",
            "--noproxy",
            "*",
            "--unix-socket",
            DOCKER_SOCKET,
            "http://localhost/volumes",
        )
    )
    if inventory["Warnings"] not in (None, []):
        raise BlockedError("volume_inventory_warning")
    entries = inventory["Volumes"]
    if entries is not None and not isinstance(entries, list):
        raise BlockedError("invalid_volume_listing")
    names = [entry["Name"] for entry in entries or []]
    if len(names) != len(set(names)) or any(
        not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]+", name) for name in names
    ):
        raise BlockedError("invalid_volume_listing")
    if operation == "create-volume" and VOLUME in names:
        raise BlockedError("volume_exists")
    if operation == "start-redis" and VOLUME not in names:
        raise BlockedError("volume_missing")


def inspect_volume() -> dict[str, Any]:
    [volume] = json.loads(docker("volume", "inspect", VOLUME))
    if (
        volume["Name"] != VOLUME
        or volume["Driver"] != "local"
        or volume["Scope"] != "local"
        or volume["Options"] not in (None, {})
    ):
        raise BlockedError("wrong_volume_metadata")
    return dict(volume)


def create_volume() -> None:
    # Docker create may return an existing volume without changing its labels.
    label = "org.dmc268.simulation.volume-creation"
    token = uuid4().hex
    docker(
        "volume",
        "create",
        "--driver",
        "local",
        "--label",
        f"{label}={token}",
        VOLUME,
        mutation=True,
    )
    if (inspect_volume()["Labels"] or {}).get(label) != token:
        raise BlockedError("volume_creation_unverified")


def compose(*args: str, mutation: bool = False, timeout: int = 8) -> str:
    return docker(
        "compose",
        "-p",
        PROJECT,
        "--env-file",
        str(ENV_FILE),
        "-f",
        str(COMPOSE_FILE),
        "-f",
        "-",
        "--profile",
        "simulation",
        *args,
        mutation=mutation,
        timeout=timeout,
        stdin=NETWORK_OVERRIDE,
    )


def require_compose_configuration(expected_hash: str) -> None:
    if hashlib.sha256(COMPOSE_FILE.read_bytes()).hexdigest() != expected_hash:
        raise BlockedError("compose_digest_mismatch")
    config = json.loads(compose("config", "--format", "json", SERVICE))
    if config["name"] != PROJECT or set(config["services"]) != {SERVICE}:
        raise BlockedError("compose_configuration_mismatch")
    service = config["services"][SERVICE]
    network = config["networks"]["backend"]
    volume = config["volumes"]["simulation-redis-data"]
    if (
        service["image"] != IMAGE
        or service["profiles"] != ["simulation"]
        or service.get("depends_on")
        or service.get("ports")
        or set(service["networks"]) != {"backend"}
        or set(config["networks"]) != {"backend"}
        or network["name"] != NETWORK
        or network["external"] is not True
        or set(config["volumes"]) != {"simulation-redis-data"}
        or volume["name"] != VOLUME
        or volume["external"] is not True
    ):
        raise BlockedError("compose_configuration_mismatch")


def local_image_id() -> str:
    try:
        image_id = docker("image", "inspect", "--format", "{{.Id}}", IMAGE).strip()
    except BlockedError:
        raise BlockedError("local_redis_image_unavailable") from None
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise BlockedError("local_redis_image_unavailable")
    return image_id


def require_redis_mount(redis: dict[str, Any]) -> None:
    mounts = [mount for mount in redis["Mounts"] if mount["Destination"] == "/data"]
    if (
        len(mounts) != 1
        or mounts[0]["Type"] != "volume"
        or mounts[0]["Name"] != VOLUME
        or mounts[0]["RW"] is not True
    ):
        raise BlockedError("wrong_redis_mount")


def shared_redis() -> dict[str, Any]:
    shared = container("redis")
    if (
        shared is None
        or shared["State"]["Running"] is not True
        or NETWORK not in shared["NetworkSettings"]["Networks"]
        or any(mount.get("Name") == VOLUME for mount in shared["Mounts"])
    ):
        raise BlockedError("shared_redis_not_isolated")
    return shared


def require_distinct_redis(redis: dict[str, Any], shared: dict[str, Any]) -> None:
    networks = redis["NetworkSettings"]["Networks"]
    shared_network = shared["NetworkSettings"]["Networks"][NETWORK]
    if (
        redis["Id"] == shared["Id"]
        or set(networks) != {NETWORK}
        or networks[NETWORK]["NetworkID"] != shared_network["NetworkID"]
        or ipaddress.IPv4Address(networks[NETWORK]["IPAddress"])
        == ipaddress.IPv4Address(shared_network["IPAddress"])
    ):
        raise BlockedError("redis_instance_not_dedicated")


def require_redis_policy(container_id: str) -> None:
    expected = {
        "appendonly": "yes",
        "appendfsync": "always",
        "no-appendfsync-on-rewrite": "no",
        "aof-load-truncated": "no",
        "maxmemory-policy": "noeviction",
    }
    cli = ("exec", container_id, "redis-cli", "--raw")
    policy = docker(*cli, "CONFIG", "GET", *expected).splitlines()
    if (
        len(policy) != 2 * len(expected)
        or dict(zip(policy[::2], policy[1::2], strict=True)) != expected
    ):
        raise BlockedError("redis_policy_mismatch")
    persistence = docker(*cli, "INFO", "persistence").splitlines()
    if not {"aof_enabled:1", "aof_last_write_status:ok"} <= set(persistence):
        raise BlockedError("redis_aof_unhealthy")


def main() -> int:
    report: dict[str, str | bool] = {
        "operation": "unknown",
        "status": "BLOCKED",
        "mutation_attempted": False,
    }
    try:
        operation, approved, first_storage, compose_hash = sys.argv[1:]
        if operation not in {"create-volume", "start-redis"}:
            raise BlockedError("invalid_operation")
        report["operation"] = operation
        if approved != "true" or (
            operation == "create-volume" and first_storage != "true"
        ):
            raise BlockedError("confirmation_required")
        if not re.fullmatch(r"[0-9a-f]{64}", compose_hash):
            raise BlockedError("invalid_compose_digest")
        require_rollback_clear()
        api = container("api")
        if api is None or not healthy(api):
            raise BlockedError("api_unavailable")
        require_disabled(api)
        require_no_redis_container()
        require_volume_precondition(operation)
        if operation == "create-volume":
            report["mutation_attempted"] = True
            create_volume()
            report.update(status="CREATED", volume=VOLUME, driver="local")
        else:
            require_compose_configuration(compose_hash)
            inspect_volume()
            image_id = local_image_id()
            shared = shared_redis()
            require_no_redis_container()
            report["mutation_attempted"] = True
            compose(
                "up",
                "-d",
                "--no-deps",
                "--no-recreate",
                "--pull",
                "never",
                "--no-build",
                "--wait",
                "--wait-timeout",
                "60",
                SERVICE,
                mutation=True,
                timeout=70,
            )
            redis = container(SERVICE)
            if redis is None or not healthy(redis):
                raise BlockedError("redis_unhealthy")
            if redis["Image"] != image_id:
                raise BlockedError("redis_image_mismatch")
            require_redis_mount(redis)
            if redis["HostConfig"]["PortBindings"] or any(
                (redis["NetworkSettings"]["Ports"] or {}).values()
            ):
                raise BlockedError("redis_port_published")
            require_distinct_redis(redis, shared)
            require_redis_policy(redis["Id"])
            report.update(status="STARTED_VERIFIED", volume=VOLUME)
    except BlockedError as exc:
        report["reason"] = str(exc)
    except Exception:
        report["reason"] = "inspection_failed"
    print(json.dumps(report))
    return 20 if report["status"] == "BLOCKED" else 0


if __name__ == "__main__":
    sys.exit(main())
