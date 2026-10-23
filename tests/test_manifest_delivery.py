"""Execute manifest delivery at the workflow and filesystem boundaries."""

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from test_analysis_deployment import ROOT, workflow_step


def test_manifest_source_follows_selected_api_image_not_checkout(
    tmp_path: Path,
) -> None:
    def git(*args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()

    git("init", "-q")
    (tmp_path / "deploy").mkdir()
    source = tmp_path / "deploy/analysis-runtime.staging.json"
    installer = tmp_path / "deploy/install-analysis-manifest.py"
    installer.write_text("# trusted workflow checkout installer\n")
    original = (ROOT / "deploy/analysis-runtime.staging.json").read_bytes()
    source.write_bytes(original)
    git("add", ".")
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-qm",
        "API",
    )
    selected = git("rev-parse", "HEAD")
    source.write_text('{"wrong": "checkout"}')
    git("add", ".")
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-qm",
        "UI",
    )
    git("remote", "add", "origin", str(tmp_path))
    output = tmp_path / "env"
    result = subprocess.run(
        ["bash", "-euc", workflow_step("Prepare staging analysis manifest")["run"]],
        cwd=tmp_path,
        env=os.environ
        | {
            "API_IMAGE": f"ghcr.io/larchanka-training/dmc-268-api-t2:sha-{selected}",
            "GITHUB_ENV": str(output),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "analysis-runtime.selected.json").read_bytes() == original
    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert (
        values["INSTALLER_SHA256"] == hashlib.sha256(installer.read_bytes()).hexdigest()
    )
    assert values["MANIFEST_SOURCE_SHA"] == selected
    assert len(values["MANIFEST_SHA256"]) == hashlib.sha256().digest_size * 2
    assert json.loads(original)["schema_version"] == 1
    for selected_sha in ("invalid-sha", "0" * 40):
        before = output.read_bytes()
        rejected = subprocess.run(
            ["bash", "-euc", workflow_step("Prepare staging analysis manifest")["run"]],
            cwd=tmp_path,
            env=os.environ
            | {
                "API_IMAGE": f"api:sha-{selected_sha}",
                "GITHUB_ENV": str(output),
            },
            capture_output=True,
            text=True,
            check=False,
        )
        assert rejected.returncode != 0
        assert output.read_bytes() == before


@pytest.mark.skipif(
    shutil.which("docker") is None or os.environ.get("DMC268_RUN_DOCKER_TESTS") != "1",
    reason="set DMC268_RUN_DOCKER_TESTS=1 with Docker running",
)
@pytest.mark.parametrize("deployment_uid", [65534, 0], ids=["ssh-user", "root"])
def test_installer_atomically_installs_trusted_manifest_and_rejects_tampering(
    tmp_path: Path,
    deployment_uid: int,
) -> None:
    staged = tmp_path / "incoming.json"
    source = (ROOT / "deploy/analysis-runtime.staging.json").read_bytes()
    staged.write_bytes(source)
    digest = hashlib.sha256(source).hexdigest()
    script = ROOT / "deploy/install-analysis-manifest.py"
    assert script.is_file(), "deployment needs a trusted atomic installer"
    probe = r"""import os, pathlib, resource, signal, subprocess, sys
uid = int(sys.argv[2])
deployment = pathlib.Path("/opt/dmc-268-t2")
deployment.mkdir(mode=0o755)
os.chown(deployment, uid, uid)
source = pathlib.Path("/source.json").read_bytes()
staged = deployment / "incoming.json"
staged.write_bytes(source)
os.chown(staged, uid, uid)
command = [sys.executable, "/installer.py", str(staged), sys.argv[1]]
subprocess.run(command, check=True)
target = (pathlib.Path("/opt/dmc-268-t2-runtime/analysis-runtime")
          / "analysis-runtime.v1.json")
assert target.read_bytes() == source
assert target.stat().st_uid == target.stat().st_gid == 0
assert target.stat().st_mode & 0o777 == 0o644
assert deployment.stat().st_uid == uid
assert not (deployment / "analysis-runtime").exists()
def as_deployer():
    os.setgroups([])
    os.setgid(uid)
    os.setuid(uid)
subprocess.run([sys.executable, "-c",
    "from pathlib import Path; Path('upload').write_text('allowed')"],
    cwd=deployment, preexec_fn=as_deployer, check=True)
if uid != 0:
    assert subprocess.run([sys.executable, "-c",
        "from pathlib import Path; Path(__import__('sys').argv[1]).write_text('bad')",
        str(target)], preexec_fn=as_deployer).returncode != 0
# An OS write error must keep the old complete document and remove temp files.
def fail_write():
    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    resource.setrlimit(resource.RLIMIT_FSIZE, (1, 1))
assert subprocess.run(command, preexec_fn=fail_write).returncode != 0
assert target.read_bytes() == source
assert list(target.parent.glob(".manifest-*")) == []
staged.write_text('{"tampered": true}')
assert subprocess.run(command).returncode != 0
assert target.read_bytes() == source
staged.write_bytes(source)
assert subprocess.run(command, preexec_fn=lambda: os.setuid(65534)).returncode != 0
runtime = target.parent
runtime.chmod(0o775)
assert subprocess.run(command).returncode != 0
runtime.chmod(0o755)
os.chown(runtime, 65534, 65534)
assert subprocess.run(command).returncode != 0
os.chown(runtime, 0, 0)
base = runtime.parent
os.chown(base, 65534, 65534)
assert subprocess.run(command).returncode != 0
os.chown(base, 0, 0)
base.chmod(0o777)
assert subprocess.run(command).returncode != 0
base.chmod(0o755)
target.unlink()
target.symlink_to(staged)
assert subprocess.run(command).returncode != 0
target.unlink()
runtime.rename(runtime.with_name("held-runtime"))
runtime.symlink_to(runtime.with_name("held-runtime"))
assert subprocess.run(command).returncode != 0
runtime.unlink()
runtime.with_name("held-runtime").rename(runtime)
staged.rename(deployment / "held.json")
staged.symlink_to(deployment / "held.json")
assert subprocess.run(command).returncode != 0
staged.unlink()
(deployment / "held.json").rename(staged)
staged.write_text("not json")
import hashlib
command[-1] = hashlib.sha256(staged.read_bytes()).hexdigest()
assert subprocess.run(command).returncode != 0
"""
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network=none",
            "-v",
            f"{staged}:/source.json:ro",
            "-v",
            f"{script}:/installer.py:ro",
            "python:3.14-slim",
            "python",
            "-c",
            probe,
            digest,
            str(deployment_uid),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def deployment_environment(tmp_path: Path) -> tuple[str, dict[str, str], Path]:
    binary = tmp_path / "bin"
    binary.mkdir()
    (binary / "docker").write_text(
        '#!/bin/bash\necho "docker $*" >> "$TRACE"\n'
        'if [[ "$1" == inspect ]]; then\n'
        '  if [[ "$*" == *Health* ]]; then echo healthy; else echo running; fi\n'
        "fi\n"
        'if [[ "$*" == *"worker python -c"* ]]; then\n'
        '  exit "${VALIDATION_FAILURE:-0}"; fi\n'
        'if [[ "$*" == *"worker python -" ]]; then exit "${PREFLIGHT_FAILURE:-0}"; fi\n'
    )
    (binary / "sudo").write_text(
        '#!/bin/bash\necho "install $*" >> "$TRACE"\nexit "${INSTALL_FAILURE:-0}"\n'
    )
    (binary / "curl").write_text("#!/bin/sh\nexit 0\n")
    for path in binary.iterdir():
        path.chmod(0o755)
    (tmp_path / ".env.test").write_text("COMPOSE_PROFILES=analysis\n")
    trace = tmp_path / "trace"
    remote = workflow_step("Deploy application")["run"].split("<<'REMOTE'\n", 1)[1]
    remote = remote.split("\nREMOTE", 1)[0]
    environment = os.environ | {
        "PATH": f"{binary}:{os.environ['PATH']}",
        "TRACE": str(trace),
        "DEPLOY_PATH": str(tmp_path),
        "ENV_FILE": ".env.test",
        "ENV_NAME": "staging",
        "WEB_PORT": "8080",
        "COMPOSE_PROJECT": "test",
        "MANIFEST_SHA256": "a" * 64,
        "INSTALLER_SHA256": "b" * 64,
    }
    return remote, environment, trace


def test_deployment_installs_manifest_after_stop_and_before_preflight(
    tmp_path: Path,
) -> None:
    remote, environment, trace = deployment_environment(tmp_path)
    result = subprocess.run(
        ["bash", "-euc", remote],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    calls = trace.read_text()
    assert "install -n /usr/bin/python3 -I" in calls, (
        "root installation must be mandatory"
    )
    assert calls.index("stop worker outbox-dispatcher") < calls.index("install -n")
    assert calls.index("install -n") < calls.index("worker python -")
    for failure in ("INSTALL_FAILURE", "VALIDATION_FAILURE", "PREFLIGHT_FAILURE"):
        trace.write_text("")
        result = subprocess.run(
            ["bash", "-euc", remote],
            env=environment | {failure: "1"},
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode != 0
        assert " up -d webhook-worker" not in trace.read_text()
        assert " up -d api" not in trace.read_text()
        if failure == "INSTALL_FAILURE":
            assert "worker python -" not in trace.read_text()
            assert " up -d " not in trace.read_text()


@pytest.mark.parametrize("identity", ["root", "ssh-user"])
@pytest.mark.parametrize("defect", ["none", "changed-code", "symlink", "import-shadow"])
def test_deployment_executes_only_installer_bytes_verified_against_runner_digest(
    tmp_path: Path, identity: str, defect: str
) -> None:
    remote, environment, trace = deployment_environment(tmp_path)
    binary = tmp_path / "bin"
    (binary / "sudo").write_text(
        '#!/bin/bash\necho "install $*" >> "$TRACE"\nshift\nexec "$@"\n'
    )
    (binary / "id").write_text(
        "#!/bin/sh\necho " + ("0" if identity == "root" else "1000") + "\n"
    )
    (binary / "id").chmod(0o755)
    script = tmp_path / "install-analysis-manifest.py"
    trusted = b"from pathlib import Path; Path('executed').write_text('verified')"
    script.write_bytes(trusted)
    environment["INSTALLER_SHA256"] = hashlib.sha256(trusted).hexdigest()
    if defect == "changed-code":
        script.write_text(
            "from pathlib import Path; Path('executed').write_text('bad')"
        )
    elif defect == "symlink":
        held = tmp_path / "held.py"
        script.rename(held)
        script.symlink_to(held)
    elif defect == "import-shadow":
        # Isolated Python must ignore cwd and PYTHONPATH before importing hashlib.
        for name in ("hashlib.py", "sitecustomize.py"):
            (tmp_path / name).write_text(
                "from pathlib import Path; Path('injected').write_text('bad')"
            )
        environment["PYTHONPATH"] = str(tmp_path)
    result = subprocess.run(
        ["bash", "-euc", remote],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if defect in ("changed-code", "symlink"):
        assert result.returncode != 0, "unverified installer must be rejected"
        assert not (tmp_path / "executed").exists()
        assert "worker python -" not in trace.read_text()
        assert " up -d " not in trace.read_text()
    else:
        assert result.returncode == 0, result.stderr
        assert (tmp_path / "executed").read_text() == "verified"
    assert not (tmp_path / "injected").exists()
