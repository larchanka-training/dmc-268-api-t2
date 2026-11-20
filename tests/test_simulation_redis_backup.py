"""Command contract for dedicated Redis RDB capture."""

import fcntl
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/backup-simulation-redis.sh"
SNAPSHOT = "simulation-redis-20261120T201530Z.rdb"
PAYLOAD = b"REDIS0012synthetic-dedicated-rdb"
SECRET = "SYNTHETIC_ADMISSION_STATE_DO_NOT_LOG"
CONTAINER_ID = "a" * 64
PRIVATE_FILE_MODE = 0o600
PRIVATE_DIR_MODE = 0o700


@pytest.fixture
def deployment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    deployment = tmp_path / "deployment"
    deployment.mkdir()
    if SCRIPT.exists():
        shutil.copy2(SCRIPT, deployment / SCRIPT.name)
    (deployment / "docker-compose.staging.yml").write_text("services: {}\n")
    (deployment / "fixture.env").touch()
    tools = tmp_path / "bin"
    tools.mkdir()
    docker = tools / "docker"
    docker.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "args = sys.argv[1:]\n"
        f"payload = {PAYLOAD!r}\n"
        f"container = {CONTAINER_ID!r}\n"
        "if args[-3:] == ['ps', '-q', 'simulation-redis']:\n"
        "    print(container)\n"
        "elif args[:2] == ['container', 'inspect'] and args[-1] == container:\n"
        "    volume = os.getenv('DATA_VOLUME', 'synthetic_simulation-redis-data')\n"
        "    print(f'synthetic simulation-redis true volume {volume}')\n"
        "elif args[:3] == ['exec', '-i', container] and "
        "'redis-check-rdb' in args[-1]:\n"
        "    if sys.stdin.buffer.read() != payload:\n"
        f"        print({SECRET!r})\n"
        f"        print({SECRET!r}, file=sys.stderr)\n"
        "        sys.exit(1)\n"
        "elif args[0] == 'exec' and container in args and '--rdb' in args[-1]:\n"
        "    sys.stdout.buffer.write(payload if not os.getenv('BAD_RDB') else b'bad')\n"
        "else:\n"
        "    sys.exit(1)\n"
    )
    docker.chmod(0o755)
    date = tools / "date"
    date.write_text("#!/bin/sh\necho 20261120T201530Z\n")
    date.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tools}:{os.environ['PATH']}")
    return deployment


def backup(deployment: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(deployment / SCRIPT.name), "synthetic", "fixture.env"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_captures_dedicated_redis_in_its_own_private_directory(
    deployment: Path,
) -> None:
    result = backup(deployment)

    assert result.returncode == 0, result.stderr
    dest = deployment / "backups/simulation-redis"
    final = dest / SNAPSHOT
    assert final.read_bytes() == PAYLOAD
    assert stat.S_IMODE(final.stat().st_mode) == PRIVATE_FILE_MODE
    assert stat.S_IMODE(dest.stat().st_mode) == PRIVATE_DIR_MODE
    assert stat.S_IMODE(dest.parent.stat().st_mode) == PRIVATE_DIR_MODE
    assert not (dest.parent / "redis").exists()
    assert not list(dest.glob("*.partial"))


def test_invalid_rdb_fails_without_publishing_or_logging_data(
    deployment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BAD_RDB", "1")

    result = backup(deployment)

    assert result.returncode != 0
    assert "validate-rdb" in result.stderr
    assert SECRET not in result.stdout + result.stderr
    assert not list((deployment / "backups/simulation-redis").glob("*"))


def test_same_timestamp_preserves_the_published_snapshot(deployment: Path) -> None:
    assert backup(deployment).returncode == 0
    dest = deployment / "backups/simulation-redis"
    final = dest / SNAPSHOT
    final.write_bytes(b"previous validated snapshot")

    result = backup(deployment)

    assert result.returncode != 0
    assert "publish" in result.stderr
    assert final.read_bytes() == b"previous validated snapshot"
    assert not list(dest.glob("*.partial"))


def test_refuses_a_container_mounting_shared_redis_data(
    deployment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATA_VOLUME", "synthetic_redis-data")

    result = backup(deployment)

    assert result.returncode != 0
    assert "source" in result.stderr
    assert not list((deployment / "backups/simulation-redis").glob("*"))


@pytest.mark.parametrize("unsafe_parent", ["symlink", "public_directory"])
def test_refuses_an_unsafe_backup_parent(deployment: Path, unsafe_parent: str) -> None:
    parent = deployment / "backups"
    if unsafe_parent == "symlink":
        actual_dir = deployment / "unrelated"
        actual_dir.mkdir(mode=0o700)
        parent.symlink_to(actual_dir, target_is_directory=True)
    else:
        actual_dir = parent
        actual_dir.mkdir(mode=0o755)
        actual_dir.chmod(0o755)

    result = backup(deployment)

    assert result.returncode != 0
    assert "prepare" in result.stderr
    assert not list(actual_dir.iterdir())


def test_busy_backup_fails_while_the_existing_run_keeps_its_partial(
    deployment: Path,
) -> None:
    dest = deployment / "backups/simulation-redis"
    dest.parent.mkdir(mode=0o700)
    dest.mkdir(mode=0o700)
    partial = dest / ".in-progress.partial"
    partial.write_bytes(b"owned by the other run")

    lock = os.open(dest, os.O_RDONLY)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = backup(deployment)
    finally:
        os.close(lock)

    assert result.returncode != 0
    assert "lock" in result.stderr
    assert list(dest.iterdir()) == [partial]
    assert partial.read_bytes() == b"owned by the other run"


def test_refuses_a_git_checkout_as_the_backup_directory(deployment: Path) -> None:
    (deployment / ".git").write_text("gitdir: /fixture/worktree\n")

    result = backup(deployment)

    assert result.returncode != 0
    assert "prepare" in result.stderr
    assert not (deployment / "backups").exists()


def test_writeback_failure_leaves_no_published_file(deployment: Path) -> None:
    sync = deployment.parent / "bin/sync"
    sync.write_text("#!/bin/sh\nexit 1\n")
    sync.chmod(0o755)

    result = backup(deployment)

    assert result.returncode != 0
    assert "sync-rdb" in result.stderr
    assert not list((deployment / "backups/simulation-redis").iterdir())
