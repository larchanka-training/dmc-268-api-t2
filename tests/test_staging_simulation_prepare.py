import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/staging-simulation-prepare.yml"
JOB_TIMEOUT = 5
BLOCKED_EXIT = 20


def test_prepare_is_a_main_only_manual_workflow() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    assert list(workflow[True]) == ["workflow_dispatch"]
    inputs = workflow[True]["workflow_dispatch"]["inputs"]
    assert inputs["operation"]["options"] == [
        "create-volume",
        "start-redis",
        "provision-admission",
    ]
    for name in (
        "first_storage",
        "approve_create_volume",
        "approve_start_redis",
        "approve_provision_admission",
        "confirm_new_instance_no_history",
        "confirm_exclusive_dedicated_instance",
    ):
        assert inputs[name]["type"] == "boolean"
        assert inputs[name]["default"] is False
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"] == {
        "group": "deploy-staging",
        "cancel-in-progress": False,
    }
    job = workflow["jobs"]["prepare"]
    assert job["if"] == (
        "github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main'"
    )
    deploy = yaml.safe_load((ROOT / ".github/workflows/deploy-staging.yml").read_text())
    for key in ("SSH_HOST", "SSH_HOST_KEY"):
        assert job["env"][key] == deploy["jobs"]["deploy"]["env"][key]
    assert job["env"]["SSH_USER"] == "${{ secrets.VPS_DMC268_U }}"
    assert job["env"]["SSHPASS"] == "${{ secrets.VPS_DMC268_P }}"
    assert job["timeout-minutes"] <= JOB_TIMEOUT
    for step in job["steps"]:
        if "run" in step:
            subprocess.run(["bash", "-n"], input=step["run"], text=True, check=True)


PROJECT = "dmc-268-t2-staging"
VOLUME = PROJECT + "_simulation-redis-data"
API_IMAGE = "ghcr.io/larchanka-training/dmc-268-api-t2:sha-" + "1" * 40
HMAC = "c3ludGhldGljLXRlc3Qta2V5LWZvci1wcm92aXNpb25pbmc="
CLI = ROOT / "scripts/provision_simulation_admission.py"
SECRET = "synthetic-secret"
SCRIPT = ROOT / ".github/scripts/staging_simulation_prepare.py"


@pytest.fixture
def prepare(tmp_path: Path) -> Any:
    compose_file = tmp_path / "docker-compose.staging.yml"
    compose_file.write_text("compose-fixture")
    docker = tmp_path / "docker"
    docker.write_text("""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path

args = sys.argv[1:]
if Path(sys.argv[0]).name == "curl":
    assert args[0] == "-q"
    assert args[args.index("--unix-socket") + 1] == "/var/run/docker.sock"
    assert args[-1] == "http://localhost/volumes"
    args = ["engine", "volumes"]
else:
    assert args[:2] == ["--host", "unix:///var/run/docker.sock"]
    args = args[2:]
calls = Path(os.environ["DOCKER_CALLS"])
previous = [json.loads(line) for line in calls.read_text().splitlines()]
with calls.open("a") as out:
    out.write(json.dumps(args) + "\\n")
data = json.loads(Path(os.environ["DOCKER_DATA"]).read_text())
project = "dmc-268-t2-staging"
api_image = "ghcr.io/larchanka-training/dmc-268-api-t2:sha-" + "1" * 40
hmac = data.get("hmac", "c3ludGhldGljLXRlc3Qta2V5LWZvci1wcm92aXNpb25pbmc=")
redis_url = "redis://simulation-redis:6379/0"
volume = project + "_simulation-redis-data"
image_id = "sha256:" + "d" * 64
created = next((c for c in previous if c[:2] == ["volume", "create"]), None)
started = any("up" in c for c in previous if c[0] == "compose")
if args[0] == "compose":
    if "run" in args:
        config = json.loads(sys.stdin.read())
        assert config["networks"]["backend"] == {
            "external": True, "name": project + "_backend",
        }
        assert set(config["services"]) == {"api"}
        svc = config["services"]["api"]
        assert svc["image"] == image_id
        assert svc["networks"] == ["backend"]
        assert svc["extra_hosts"] == {"simulation-redis": "172.18.0.5"}
        assert "depends_on" not in svc and "ports" not in svc and "build" not in svc
        assert {"--rm", "--no-deps", "--interactive=false", "-T"} <= set(args)
        assert args[args.index("--pull") + 1] == "never"
        assert "uvicorn" not in args and hmac not in args
    else:
        assert args.count("-f") == 2 and "-" in args
        assert f"external: true, name: {project}_backend" in sys.stdin.read()
    assert os.environ["COMPOSE_REMOVE_ORPHANS"] == "false"
    assert os.environ["COMPOSE_IGNORE_ORPHANS"] == "true"
    if "up" in args and data.get("compose_timeout"):
        print("synthetic-secret", file=sys.stderr)
        sys.exit(124)
if args[:2] == data.get("error_command"):
    print("synthetic-secret", file=sys.stderr)
    sys.exit(1)
if args == ["engine", "volumes"]:
    listing = [volume] if data.get("volume") or created else []
    print(json.dumps({
        "Warnings": [data["volume_warning"]] if data.get("volume_warning") else None,
        "Volumes": [{"Name": name} for name in data.get("volume_listing", listing)],
    }))
elif args[:2] == ["volume", "inspect"]:
    labels = {}
    if created and not data.get("volume_race"):
        key, value = created[created.index("--label") + 1].split("=", 1)
        labels[key] = value
    print(json.dumps([{"Name": volume, "Driver": "local", "Scope": "local",
                       "Options": None, "Labels": labels}]))
elif args[:2] == ["volume", "create"]:
    print(volume)
elif args[0] == "ps":
    services = {"api": "a", "redis": "b", "simulation-redis": "c"}
    service = next((arg.rsplit("=", 1)[1] for arg in args
                    if arg.startswith("label=com.docker.compose.service=")), None)
    appeared = data.get("container_arrived") and any(
        call[:2] == ["image", "inspect"] for call in previous
    )
    if service in ("api", "redis") or service == "simulation-redis" and (
        started or data.get("existing_container") or appeared
    ):
        print(services[service] * 64)
    elif service is None and data.get("provision") and "volume=" + volume in args:
        print("c" * 64)
    elif service is None and data.get("orphan_container"):
        print("e" * 64)
elif args[0] == "inspect":
    service = {"a": "api", "b": "redis", "c": "simulation-redis"}[args[-1][0]]
    live_redis = started or data.get("redis_running", False)
    entry = {
        "Id": args[-1],
        "Image": data.get("running_image_id", image_id),
        "Config": {"Image": api_image, "Labels": {"com.docker.compose.project": project,
                              "com.docker.compose.service": service},
                   "Env": ["SIMULATION__ENABLED=" + data.get("live_flag", "false"),
                           "SIMULATION__HMAC_SECRET_B64=" + hmac,
                           "SIMULATION__REDIS_URL=" + redis_url,
                           "REDIS__URL=redis://redis:6379/0", "GIT_SHA=" + "1" * 40]},
        "State": {"Running": service != "simulation-redis" or live_redis,
                  "Health": {"Status": data.get("health", "healthy")}},
        "Mounts": [],
        "HostConfig": {"PortBindings": None},
        "NetworkSettings": {"Ports": {"6379/tcp": None}, "Networks": {
            project + "_backend": {"IPAddress": "172.18.0." + (
                "4" if service == "redis" or data.get("shared_ip") else "5"
            ), "NetworkID": "f" * 64}
        }},
    }
    if service == "api":
        labels = entry["Config"]["Labels"]
        labels["com.docker.compose.project"] = data.get("api_project", project)
    if service == "simulation-redis":
        entry["State"]["Health"]["Status"] = data.get("redis_health", "healthy")
        entry["Mounts"] = [{"Type": "volume", "Name": data.get("mount", volume),
                           "Destination": "/data", "RW": True}]
        if data.get("published"):
            entry["NetworkSettings"]["Ports"] = {"6379/tcp": [{"HostPort": "6379"}]}
    print(json.dumps([entry]))
elif args[0] == "compose" and "config" in args and args[-1] == "api":
    compose_hmac = data.get("compose_hmac", hmac)
    print(json.dumps({
        "name": project, "services": {"api": {
            "image": api_image, "networks": {"backend": None},
            "environment": {"SIMULATION__ENABLED": "false",
                            "SIMULATION__HMAC_SECRET_B64": compose_hmac,
                            "SIMULATION__REDIS_URL": redis_url,
                            "REDIS__URL": "redis://redis:6379/0"},
        }},
    }))
elif args[0] == "compose" and "run" in args:
    create = "--create" in args
    attempted = any("--create" in call for call in previous)
    if create:
        assert args[-3:] == ["--create", "--confirm-new-instance-no-history",
                            "--confirm-exclusive-dedicated-instance"]
    history = calls.with_name("redis-history")
    if create and data.get("write_failure") != "concurrent_creation_refused":
        assert not history.exists()
        history.write_text("retained")
    if create and data.get("write_failure") == "lost_response":
        print("synthetic-secret")
        print("synthetic-secret", file=sys.stderr)
        sys.exit(124)
    exists = history.exists() or create or attempted or data.get("existing_state", True)
    report = {
        "mode": "create" if create else "check",
        "redis_available": True, "activation_authorized": False,
        "history_continuity": "unproven", "eviction_policy": "noeviction",
        "persistence": {"appendonly": "yes", "appendfsync": "always",
                        "no-appendfsync-on-rewrite": "no", "aof_enabled": 1,
                        "aof_last_write_status": "ok"},
        "state_exists": exists, "state_type": "string" if exists else "none",
        "structure": "valid" if exists else "absent",
        "hmac_identity": "match" if exists else "unknown",
        "pttl_ms": -1 if exists else -2,
    }
    if create:
        report["status"] = "created"
        if failure := data.get("write_failure"):
            print(json.dumps({"error": failure, "mode": "check"}))
            sys.exit(1)
    elif not attempted and (failure := data.get("check_failure")):
        if failure == "invalid_simulation_secret":
            print(json.dumps({"error": failure}))
            sys.exit(1)
        if failure in {"invalid_state", "hmac_mismatch"}:
            report.update(structure="invalid", error="invalid_state")
            if failure == "hmac_mismatch":
                report["hmac_identity"] = "mismatch"
            print(json.dumps(report))
            sys.exit(1)
        if failure == "unsafe_policy":
            report["persistence"]["appendfsync"] = "everysec"
        if failure == "inconsistent_absence":
            report["pttl_ms"] = -1
    elif attempted and data.get("postflight_mismatch"):
        report["pttl_ms"] = 5000
    if data.get("noisy_output"):
        report["unsafe_field"] = "synthetic-secret"
        print("synthetic-secret", file=sys.stderr)
    print(json.dumps(report))
elif args[:2] == ["network", "inspect"]:
    print("f" * 64)
elif args[0] == "compose" and "config" in args:
    print(json.dumps({"name": data.get("compose_project", project), "services": {
        "simulation-redis": {"image": "redis:8-alpine", "profiles": ["simulation"],
            "networks": {"backend": None}, "volumes": [
                {"type": "volume", "source": "simulation-redis-data", "target": "/data"}
            ]},
    }, "volumes": {"simulation-redis-data": {"name": volume, "external": True}},
       "networks": {"backend": {"name": project + "_backend", "external": True}}}))
elif args[0] == "compose" and "up" in args:
    pass
elif args[:2] == ["image", "inspect"]:
    print(image_id)
elif args[0] == "exec" and args[4:6] == ["CONFIG", "GET"]:
    print("\\n".join(("appendonly", "yes", "appendfsync", data.get("fsync", "always"),
                    "no-appendfsync-on-rewrite", "no", "aof-load-truncated", "no",
                    "maxmemory-policy", "noeviction")))
elif args[0] == "exec" and args[4:] == ["INFO", "persistence"]:
    print("aof_enabled:1\\naof_last_write_status:ok")
else:
    raise AssertionError(args)
""")
    docker.chmod(0o755)
    (tmp_path / "curl").symlink_to(docker)

    def run(
        operation: str, approval: str = "true", first_storage: str = "true", **data: Any
    ) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
        script = tmp_path / "prepare.py"
        script.write_text(SCRIPT.read_text().replace("/opt/dmc-268-t2", str(tmp_path)))
        if operation == "provision-admission":
            data = {
                "volume": True,
                "existing_container": True,
                "redis_running": True,
                "provision": True,
            } | data
        (tmp_path / "data.json").write_text(json.dumps(data))
        ops = tmp_path / "simulation-ops"
        ops.mkdir(exist_ok=True)
        (ops / CLI.name).write_bytes(data.get("remote_cli", CLI.read_text()).encode())
        calls = tmp_path / "docker-calls"
        calls.write_text("")
        deployed_hmac = json.dumps(data.get("compose_hmac", data.get("hmac", HMAC)))
        (tmp_path / ".env.staging").write_text(
            f"SIMULATION__ENABLED={data.get('file_flag', 'false')}\n"
            f"API_IMAGE={API_IMAGE}\n"
            f"SIMULATION__REDIS_URL=redis://simulation-redis:6379/0\n"
            f"SIMULATION__HMAC_SECRET_B64={deployed_hmac}\n"
        )
        if data.get("rollback"):
            (tmp_path / ".env.staging.simulation-rollback").symlink_to("missing")
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                str(script),
                operation,
                approval,
                first_storage,
                hashlib.sha256(compose_file.read_bytes()).hexdigest(),
                *(
                    [
                        data.get("new_instance", "true"),
                        data.get("exclusive", "true"),
                        hashlib.sha256(CLI.read_bytes()).hexdigest(),
                    ]
                    if operation == "provision-admission"
                    else []
                ),
            ],
            env=os.environ
            | {
                "PATH": f"{tmp_path}:{os.environ['PATH']}",
                "DOCKER_DATA": str(tmp_path / "data.json"),
                "DOCKER_CALLS": str(calls),
            },
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert SECRET not in result.stdout + result.stderr
        assert HMAC not in result.stdout + result.stderr + calls.read_text()
        return result, [json.loads(line) for line in calls.read_text().splitlines()]

    return run


@pytest.mark.parametrize(
    ("operation", "approval", "first_storage"),
    [
        ("create-volume", "false", "true"),
        ("create-volume", "true", "false"),
        ("start-redis", "false", "true"),
        ("other", "true", "true"),
    ],
)
def test_operator_confirmation_is_required(
    prepare: Any, operation: str, approval: str, first_storage: str
) -> None:
    result, calls = prepare(operation, approval, first_storage)
    assert result.returncode == BLOCKED_EXIT
    assert json.loads(result.stdout)["status"] == "BLOCKED"
    assert calls == []


def test_wrong_staging_api_project_blocks_before_mutation(prepare: Any) -> None:
    result, _ = prepare("create-volume", api_project="dmc-268-t2-develop")
    assert json.loads(result.stdout)["reason"] == "wrong_container_identity"


def test_working_staging_api_is_required(prepare: Any) -> None:
    result, _ = prepare("create-volume", health="unhealthy")
    assert json.loads(result.stdout)["reason"] == "api_unavailable"


@pytest.mark.parametrize("flag", ["live_flag", "file_flag"])
def test_playground_enabled_blocks_preparation(prepare: Any, flag: str) -> None:
    result, _ = prepare("create-volume", **{flag: "true"})
    assert json.loads(result.stdout)["reason"] == "playground_not_disabled"


def test_unresolved_rollback_blocks_preparation(prepare: Any) -> None:
    result, calls = prepare("start-redis", volume=True, rollback=True)
    assert json.loads(result.stdout)["reason"] == "rollback_pending"
    assert calls == []


@pytest.mark.parametrize(
    "extra", [{"existing_container": True}, {"orphan_container": True}]
)
def test_existing_container_blocks_before_mutation(
    prepare: Any, extra: dict[str, bool]
) -> None:
    result, calls = prepare("start-redis", volume=True, **extra)
    assert json.loads(result.stdout)["reason"] == "redis_container_exists"
    assert all("up" not in c for c in calls)


@pytest.mark.parametrize(
    ("operation", "exists", "reason"),
    [
        ("create-volume", True, "volume_exists"),
        ("start-redis", False, "volume_missing"),
    ],
)
def test_exact_external_volume_precondition(
    prepare: Any, operation: str, exists: bool, reason: str
) -> None:
    result, calls = prepare(operation, volume=exists)
    assert json.loads(result.stdout)["reason"] == reason
    assert all(c[:2] != ["volume", "create"] and "up" not in c for c in calls)


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        ({"volume_listing": ["Error: synthetic-secret"]}, "invalid_volume_listing"),
        ({"error_command": ["engine", "volumes"]}, "docker_command_failed"),
    ],
)
def test_uncertain_volume_inspection_fails_closed(
    prepare: Any, data: dict[str, Any], reason: str
) -> None:
    result, calls = prepare("create-volume", **data)
    assert json.loads(result.stdout)["reason"] == reason
    assert all(c[:2] != ["volume", "create"] for c in calls)


def test_create_volume_verifies_new_local_storage(prepare: Any) -> None:
    result, calls = prepare("create-volume", volume=False)
    assert result.returncode == 0
    assert json.loads(result.stdout) == {
        "operation": "create-volume",
        "status": "CREATED",
        "volume": VOLUME,
        "driver": "local",
        "mutation_attempted": True,
    }
    writes = [c for c in calls if c[:2] == ["volume", "create"] or "up" in c]
    assert len(writes) == 1 and writes[0][-1] == VOLUME
    assert calls[-1] == ["volume", "inspect", VOLUME]


def test_create_does_not_claim_volume_created_by_another_actor(prepare: Any) -> None:
    result, calls = prepare("create-volume", volume_race=True)
    report = json.loads(result.stdout)
    assert result.returncode == BLOCKED_EXIT
    assert report["reason"] == "volume_creation_unverified"
    assert report["mutation_attempted"] is True
    assert sum(c[:2] == ["volume", "create"] for c in calls) == 1


def test_wrong_rendered_compose_project_blocks_start(prepare: Any) -> None:
    result, calls = prepare(
        "start-redis", volume=True, compose_project="dmc-268-t2-develop"
    )
    assert json.loads(result.stdout)["reason"] == "compose_configuration_mismatch"
    assert all("up" not in c for c in calls)


def test_missing_local_redis_image_blocks_without_pull(prepare: Any) -> None:
    result, calls = prepare(
        "start-redis", volume=True, error_command=["image", "inspect"]
    )
    assert json.loads(result.stdout)["reason"] == "local_redis_image_unavailable"
    assert all("up" not in c and "pull" not in c for c in calls)


def test_start_redis_targets_only_the_initial_dedicated_service(prepare: Any) -> None:
    result, calls = prepare("start-redis", volume=True, first_storage="false")
    assert result.returncode == 0
    assert json.loads(result.stdout) == {
        "operation": "start-redis",
        "status": "STARTED_VERIFIED",
        "volume": VOLUME,
        "mutation_attempted": True,
    }
    [up] = [c for c in calls if c[0] == "compose" and "up" in c]
    assert up[-1] == "simulation-redis"
    assert up[up.index("--profile") + 1] == "simulation"
    assert up[up.index("-p") + 1] == PROJECT
    assert {"--no-deps", "--no-recreate", "--no-build", "--wait"} <= set(up)
    assert up[up.index("--pull") + 1] == "never"
    assert all(
        c[0] in {"engine", "volume", "ps", "inspect", "image", "compose", "exec"}
        for c in calls
    )
    assert all(c[1] != "create" for c in calls if c[0] == "volume")
    assert all(
        c[1] == "c" * 64
        and c[4:]
        in (
            ["INFO", "persistence"],
            [
                "CONFIG",
                "GET",
                "appendonly",
                "appendfsync",
                "no-appendfsync-on-rewrite",
                "aof-load-truncated",
                "maxmemory-policy",
            ],
        )
        for c in calls
        if c[0] == "exec"
    )


def test_start_requires_a_running_healthy_dedicated_container(prepare: Any) -> None:
    result, _ = prepare("start-redis", volume=True, redis_health="unhealthy")
    assert result.returncode == BLOCKED_EXIT
    assert json.loads(result.stdout)["reason"] == "redis_unhealthy"
    assert json.loads(result.stdout)["mutation_attempted"] is True


def test_wrong_mount_after_start_blocks_and_preserves_evidence(prepare: Any) -> None:
    result, calls = prepare("start-redis", volume=True, mount="wrong-volume")
    report = json.loads(result.stdout)
    assert report["reason"] == "wrong_redis_mount"
    assert report["status"] == "BLOCKED" and report["mutation_attempted"] is True
    assert sum("up" in c for c in calls) == 1
    assert all(set(c).isdisjoint({"rm", "down", "prune", "exec"}) for c in calls)


def test_host_published_port_blocks_after_start(prepare: Any) -> None:
    result, _ = prepare("start-redis", volume=True, published=True)
    assert json.loads(result.stdout)["reason"] == "redis_port_published"


def test_dedicated_redis_must_be_distinct_before_probing_policy(prepare: Any) -> None:
    result, calls = prepare("start-redis", volume=True, shared_ip=True)
    assert json.loads(result.stdout)["reason"] == "redis_instance_not_dedicated"
    assert all(c[0] != "exec" for c in calls)


def test_unsafe_effective_redis_persistence_blocks_after_start(prepare: Any) -> None:
    result, calls = prepare("start-redis", volume=True, fsync="everysec")
    assert json.loads(result.stdout)["reason"] == "redis_policy_mismatch"
    assert any(c[4:6] == ["CONFIG", "GET"] for c in calls)


def test_start_checks_the_running_image_against_the_preflight_image(
    prepare: Any,
) -> None:
    result, calls = prepare(
        "start-redis", volume=True, running_image_id="sha256:" + "e" * 64
    )
    assert json.loads(result.stdout)["reason"] == "redis_image_mismatch"
    assert all(c[0] != "exec" for c in calls)


@pytest.mark.parametrize(
    ("operation", "approvals", "reason", "uses_ssh"),
    [
        ("create-volume", ("true", "false", "false"), "confirmation_required", False),
        ("start-redis", ("true", "false", "true"), "confirmation_required", False),
        (
            "other; echo synthetic-secret",
            ("true", "true", "true"),
            "invalid_operation",
            False,
        ),
        ("start-redis", ("false", "true", "false"), "remote_result_unknown", True),
    ],
)
def test_dispatch_filters_inputs_and_ssh_output(
    tmp_path: Path,
    operation: str,
    approvals: tuple[str, str, str],
    reason: str,
    uses_ssh: bool,
) -> None:
    create, start, first = approvals
    sshpass = tmp_path / "sshpass"
    marker = tmp_path / "ssh-invoked"
    sshpass.write_text(
        '#!/bin/sh\ntouch "$SSH_MARKER"\necho synthetic-secret\n'
        "echo synthetic-secret >&2\nexit 255\n"
    )
    sshpass.chmod(0o755)
    step = yaml.safe_load(WORKFLOW.read_text())["jobs"]["prepare"]["steps"][-1]
    result = subprocess.run(
        ["bash", "-c", step["run"]],
        cwd=ROOT,
        env=os.environ
        | {
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "SSH_MARKER": str(marker),
            "SSH_USER": "fixture",
            "SSH_HOST": "fixture",
            "OPERATION": operation,
            "APPROVE_CREATE_VOLUME": create,
            "APPROVE_START_REDIS": start,
            "FIRST_STORAGE": first,
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode != 0 and marker.exists() is uses_ssh
    assert SECRET not in result.stdout + result.stderr
    assert json.loads(result.stdout) == {"status": "BLOCKED", "reason": reason}


def test_container_appearing_during_preflight_blocks_start(prepare: Any) -> None:
    result, calls = prepare("start-redis", volume=True, container_arrived=True)
    assert json.loads(result.stdout)["reason"] == "redis_container_exists"
    assert all("up" not in call for call in calls)


@pytest.mark.parametrize(
    ("reported_operation", "expected_status"),
    [("start-redis", "STARTED_VERIFIED"), ("create-volume", "BLOCKED")],
)
def test_dispatch_requires_the_report_to_match_the_selected_operation(
    tmp_path: Path, reported_operation: str, expected_status: str
) -> None:
    sshpass = tmp_path / "sshpass"
    sshpass.write_text('#!/bin/sh\nprintf "%s\\n" "$SSH_REPORT"\n')
    sshpass.chmod(0o755)
    step = yaml.safe_load(WORKFLOW.read_text())["jobs"]["prepare"]["steps"][-1]
    report = {
        "operation": reported_operation,
        "status": "STARTED_VERIFIED",
        "mutation_attempted": True,
        "volume": VOLUME,
    }
    result = subprocess.run(
        ["bash", "-c", step["run"]],
        cwd=ROOT,
        env=os.environ
        | {
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "SSH_REPORT": json.dumps(report),
            "SSH_USER": "fixture",
            "SSH_HOST": "fixture",
            "OPERATION": "start-redis",
            "APPROVE_CREATE_VOLUME": "false",
            "APPROVE_START_REDIS": "true",
            "FIRST_STORAGE": "false",
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert json.loads(result.stdout)["status"] == expected_status
    assert (result.returncode == 0) is (expected_status == "STARTED_VERIFIED")


def test_compose_timeout_preserves_uncertain_mutation_evidence(prepare: Any) -> None:
    result, calls = prepare("start-redis", volume=True, compose_timeout=True)
    assert json.loads(result.stdout) == {
        "operation": "start-redis",
        "status": "BLOCKED",
        "mutation_attempted": True,
        "reason": "docker_command_timed_out",
    }
    assert sum("up" in call for call in calls) == 1


def test_partial_engine_volume_listing_blocks_creation(prepare: Any) -> None:
    result, calls = prepare("create-volume", volume_warning=SECRET)
    report = json.loads(result.stdout)
    assert report["reason"] == "volume_inventory_warning"
    assert report["mutation_attempted"] is False
    assert all(call[:2] != ["volume", "create"] for call in calls)


@pytest.mark.parametrize("missing", ["approval", "new_instance", "exclusive"])
def test_provision_requires_each_independent_confirmation(
    prepare: Any, missing: str
) -> None:
    confirmations = {"approval": "true", "new_instance": "true", "exclusive": "true"}
    confirmations[missing] = "false"
    result, calls = prepare("provision-admission", **confirmations)
    assert result.returncode == BLOCKED_EXIT
    assert json.loads(result.stdout)["reason"] == "confirmation_required"
    assert calls == []


def test_provision_blocks_existing_state_after_read_only_inspection(
    prepare: Any,
) -> None:
    result, calls = prepare("provision-admission")
    assert result.returncode == BLOCKED_EXIT
    assert json.loads(result.stdout)["reason"] == "state_already_exists"
    assert [call[-1] for call in calls if "run" in call] == ["--check"]


def test_provision_creates_once_between_read_only_checks(prepare: Any) -> None:
    result, calls = prepare(
        "provision-admission", existing_state=False, noisy_output=True
    )
    assert result.returncode == 0
    assert json.loads(result.stdout) == {
        "operation": "provision-admission",
        "status": "PROVISIONED_VERIFIED",
        "mutation_attempted": True,
        "activation_authorized": False,
        "history_continuity": "unproven",
        "state_exists": True,
        "state_type": "string",
        "structure": "valid",
        "hmac_identity": "match",
        "pttl_ms": -1,
    }
    assert [
        "--create" if "--create" in call else call[-1]
        for call in calls
        if "run" in call
    ] == ["--check", "--create", "--check"]
    assert all(call[:2] != ["volume", "create"] and "up" not in call for call in calls)
    assert all(call[1] == "c" * 64 for call in calls if call[0] == "exec")


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        ("lost_response", "creation_unconfirmed"),
        ("creation_unconfirmed", "creation_unconfirmed"),
        ("redis_operation_failed", "creation_unconfirmed"),
        ("concurrent_creation_refused", "concurrent_creation_refused"),
    ],
)
def test_provision_stops_after_failed_or_uncertain_write(
    prepare: Any, failure: str, reason: str
) -> None:
    result, calls = prepare(
        "provision-admission", existing_state=False, write_failure=failure
    )
    assert result.returncode == BLOCKED_EXIT
    assert json.loads(result.stdout) == {
        "operation": "provision-admission",
        "status": "BLOCKED",
        "mutation_attempted": True,
        "reason": reason,
    }
    assert ["--create" in call for call in calls if "run" in call] == [False, True]


def test_provision_requires_postflight_to_verify_the_created_state(
    prepare: Any,
) -> None:
    result, calls = prepare(
        "provision-admission", existing_state=False, postflight_mismatch=True
    )
    assert result.returncode == BLOCKED_EXIT
    assert json.loads(result.stdout)["reason"] == "postflight_unverified"
    assert ["--create" in call for call in calls if "run" in call] == [
        False,
        True,
        False,
    ]


@pytest.mark.parametrize(
    ("settings", "reason"),
    [
        ({"hmac": ""}, "invalid_simulation_secret"),
        ({"hmac": "invalid"}, "invalid_simulation_secret"),
        ({"check_failure": "invalid_simulation_secret"}, "invalid_simulation_secret"),
        ({"compose_hmac": "different"}, "api_environment_mismatch"),
        ({"existing_container": False}, "redis_unhealthy"),
        ({"redis_running": False}, "redis_unhealthy"),
        ({"volume": False}, "volume_missing"),
        ({"mount": "shared-volume"}, "wrong_redis_mount"),
        ({"shared_ip": True}, "redis_instance_not_dedicated"),
        ({"fsync": "everysec"}, "redis_policy_mismatch"),
        ({"check_failure": "unsafe_policy"}, "redis_policy_mismatch"),
        ({"check_failure": "invalid_state"}, "invalid_state"),
        ({"check_failure": "hmac_mismatch"}, "hmac_identity_mismatch"),
        ({"remote_cli": "synthetic-secret"}, "cli_digest_mismatch"),
        ({"running_image_id": "sha256:" + "e" * 64}, "api_image_mismatch"),
        (
            {"check_failure": "inconsistent_absence", "existing_state": False},
            "invalid_admission_inspection",
        ),
    ],
)
def test_provision_blocks_on_unsafe_or_inconsistent_preflight(
    prepare: Any, settings: dict[str, Any], reason: str
) -> None:
    result, calls = prepare("provision-admission", **settings)
    assert result.returncode == BLOCKED_EXIT
    assert json.loads(result.stdout)["reason"] == reason
    assert json.loads(result.stdout)["mutation_attempted"] is False
    assert all("--create" not in call for call in calls)


def test_provision_repeat_preserves_the_created_history(
    prepare: Any, tmp_path: Path
) -> None:
    first, _ = prepare("provision-admission", existing_state=False)
    assert first.returncode == 0
    before = (tmp_path / "redis-history").stat()
    repeat, calls = prepare("provision-admission", existing_state=False)
    assert repeat.returncode == BLOCKED_EXIT
    assert json.loads(repeat.stdout)["reason"] == "state_already_exists"
    assert [call[-1] for call in calls if "run" in call] == ["--check"]
    assert (tmp_path / "redis-history").stat().st_mtime_ns == before.st_mtime_ns


@pytest.mark.parametrize(
    ("missing", "ssh_exit", "override", "expected"),
    [
        ("APPROVE_PROVISION_ADMISSION", 0, {}, ("BLOCKED", "confirmation_required")),
        (
            "CONFIRM_NEW_INSTANCE_NO_HISTORY",
            0,
            {},
            ("BLOCKED", "confirmation_required"),
        ),
        (
            "CONFIRM_EXCLUSIVE_DEDICATED_INSTANCE",
            0,
            {},
            ("BLOCKED", "confirmation_required"),
        ),
        ("", 0, {}, ("PROVISIONED_VERIFIED", None)),
        ("", 255, {}, ("UNKNOWN", "creation_unconfirmed")),
        ("", 0, {"activation_authorized": True}, ("UNKNOWN", "creation_unconfirmed")),
        (
            "",
            BLOCKED_EXIT,
            {"reason": "synthetic_secret"},
            ("UNKNOWN", "creation_unconfirmed"),
        ),
        (
            "",
            BLOCKED_EXIT,
            {"reason": "creation_unconfirmed"},
            ("BLOCKED", "creation_unconfirmed"),
        ),
    ],
)
def test_provision_dispatch_checks_approvals_and_accepts_only_safe_remote_reports(
    tmp_path: Path,
    missing: str,
    ssh_exit: int,
    override: dict[str, str | bool],
    expected: tuple[str, str | None],
) -> None:
    expected_status, reason = expected
    sshpass = tmp_path / "sshpass"
    marker = tmp_path / "ssh-invoked"
    sshpass.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$@" >> "$SSH_MARKER"\n'
        'printf "%s\\n" "$SSH_REPORT"\n'
        "echo synthetic-secret >&2\n"
        'exit "$SSH_EXIT"\n'
    )
    sshpass.chmod(0o755)
    step = yaml.safe_load(WORKFLOW.read_text())["jobs"]["prepare"]["steps"][-1]
    report: dict[str, str | bool | int] = {
        "operation": "provision-admission",
        "status": "BLOCKED" if ssh_exit == BLOCKED_EXIT else "PROVISIONED_VERIFIED",
        "mutation_attempted": True,
    }
    if ssh_exit != BLOCKED_EXIT:
        report.update(
            activation_authorized=False,
            history_continuity="unproven",
            state_exists=True,
            state_type="string",
            structure="valid",
            hmac_identity="match",
            pttl_ms=-1,
        )
    env = os.environ | {
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "SSH_MARKER": str(marker),
        "SSH_REPORT": json.dumps(report | override),
        "SSH_EXIT": str(ssh_exit),
        "SSH_USER": "fixture",
        "SSH_HOST": "fixture",
        "OPERATION": "provision-admission",
        "APPROVE_CREATE_VOLUME": "false",
        "APPROVE_START_REDIS": "false",
        "FIRST_STORAGE": "false",
        "APPROVE_PROVISION_ADMISSION": "true",
        "CONFIRM_NEW_INSTANCE_NO_HISTORY": "true",
        "CONFIRM_EXCLUSIVE_DEDICATED_INSTANCE": "true",
    }
    if missing:
        env[missing] = "false"
        env["APPROVE_CREATE_VOLUME"] = "true"
        env["APPROVE_START_REDIS"] = "true"
        env["FIRST_STORAGE"] = "true"
    result = subprocess.run(
        ["bash", "-c", step["run"]],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert "synthetic" not in result.stdout + result.stderr
    assert json.loads(result.stdout)["status"] == expected_status
    assert json.loads(result.stdout).get("reason") == reason
    assert (result.returncode == 0) is (expected_status == "PROVISIONED_VERIFIED")
    assert marker.exists() is (not missing)
    if not missing:
        command = marker.read_text()
        assert command.count("exec python3") == 1
        assert hashlib.sha256(CLI.read_bytes()).hexdigest() in command
