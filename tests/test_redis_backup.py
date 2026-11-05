"""Public command contract for the Redis snapshot backup."""

import json
import os
import shutil
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/backup-redis.sh"
PAYLOAD = b"REDIS0012synthetic-fixture"
PRIVATE_FILE_MODE = 0o600
PRIVATE_DIR_MODE = 0o700
VALIDATION_CALLS = 2
EXISTING_BACKUPS = 9


def prepare(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    script = deploy / SCRIPT.name
    if SCRIPT.exists():
        shutil.copy2(SCRIPT, script)
    tools = tmp_path / "bin"
    tools.mkdir()
    docker = tools / "docker"
    docker.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.environ['CALLS'], 'a') as out:\n"
        " out.write(json.dumps(sys.argv[1:])+'\\n')\n"
        "if 'redis-check-rdb' in sys.argv[-1]:\n"
        " if os.environ.get('REJECT_RDB'):\n"
        "  sys.stderr.write('SYNTHETIC_SECRET_DO_NOT_LOG'); sys.exit(1)\n"
        " valid = sys.stdin.buffer.read() == b'REDIS0012synthetic-fixture'\n"
        " sys.exit(0 if valid else 1)\n"
        "sys.stdout.buffer.write(b'REDIS0012synthetic-fixture')\n"
    )
    docker.chmod(0o755)
    date = tools / "date"
    date.write_text(
        '#!/bin/sh\ncase "$*" in\n'
        "*+%F) echo 2026-11-05;;\n"
        "*) echo 2026-11-05T12:00:00Z;;\nesac\n"
    )
    date.chmod(0o755)
    flock = tools / "flock"
    flock.write_text("#!/bin/sh\nexit 0\n")
    flock.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tools}:{os.environ['PATH']}",
        "CALLS": str(tmp_path / "calls"),
    }
    return script, env


def run(script: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), "synthetic", "fixture.env"],
        env=env,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )


def test_snapshot_is_validated_and_published_privately(tmp_path: Path) -> None:
    script, env = prepare(tmp_path)
    result = run(script, env)
    assert result.returncode == 0, result.stderr
    dest = script.parent / "backups/redis"
    snapshot = dest / "redis-2026-11-05.rdb"
    assert snapshot.read_bytes() == PAYLOAD
    assert snapshot.stat().st_mode & 0o777 == PRIVATE_FILE_MODE
    assert dest.stat().st_mode & 0o777 == PRIVATE_DIR_MODE
    assert (dest.parent.stat().st_mode & 0o777) == PRIVATE_DIR_MODE
    assert not list(dest.glob("*.partial"))
    calls = [json.loads(line) for line in Path(env["CALLS"]).read_text().splitlines()]
    assert len(calls) == VALIDATION_CALLS
    for call in calls:
        assert call[:9] == [
            "compose",
            "-p",
            "synthetic",
            "--env-file",
            "fixture.env",
            "-f",
            "docker-compose.staging.yml",
            "exec",
            "-T",
        ]
        assert call[9:12] == ["redis", "sh", "-c"]
    assert "redis-cli --rdb" in calls[0][-1]
    assert "redis-check-rdb" in calls[1][-1]


def test_invalid_snapshot_preserves_previous_backup_and_removes_partial(
    tmp_path: Path,
) -> None:
    script, env = prepare(tmp_path)
    assert run(script, env).returncode == 0
    final = script.parent / "backups/redis/redis-2026-11-05.rdb"
    final.write_bytes(b"previous-validated-backup")
    result = run(script, {**env, "REJECT_RDB": "1"})
    assert result.returncode != 0
    assert "SYNTHETIC_SECRET_DO_NOT_LOG" not in result.stdout + result.stderr
    assert final.read_bytes() == b"previous-validated-backup"
    assert not list(final.parent.glob("*.partial"))
    assert "fail" in (final.parent / "backup.log").read_text()
    assert (
        "SYNTHETIC_SECRET_DO_NOT_LOG" not in (final.parent / "backup.log").read_text()
    )


def test_success_retains_seven_daily_snapshots_only(tmp_path: Path) -> None:
    script, env = prepare(tmp_path)
    dest = script.parent / "backups/redis"
    dest.mkdir(parents=True)
    for day in range(1, 10):
        (dest / f"redis-2026-10-{day:02d}.rdb").write_bytes(b"old")
    unrelated = dest / "operator-copy.rdb"
    unrelated.write_bytes(b"keep")
    rejected = run(script, {**env, "REJECT_RDB": "1"})
    assert rejected.returncode != 0
    assert len(list(dest.glob("redis-*.rdb"))) == EXISTING_BACKUPS
    assert run(script, env).returncode == 0
    assert sorted(path.name for path in dest.glob("redis-*.rdb")) == [
        *(f"redis-2026-10-{day:02d}.rdb" for day in range(4, 10)),
        "redis-2026-11-05.rdb",
    ]
    assert unrelated.read_bytes() == b"keep"


def test_busy_lock_skips_without_contacting_redis(tmp_path: Path) -> None:
    script, env = prepare(tmp_path)
    flock = tmp_path / "bin/flock"
    flock.write_text("#!/bin/sh\nexit 1\n")
    result = run(script, env)
    assert result.returncode == 0
    assert not Path(env["CALLS"]).exists()
    assert "skip-lock" in (script.parent / "backups/redis/backup.log").read_text()
