"""Opt-in real PostgreSQL/Redis restore drill, confined to owned containers.

Run on Linux with Bash, GNU date, flock, gzip and Docker Compose available:
DMC268_RUN_DOCKER_TESTS=1 uv run pytest tests/test_backup_restore.py -q
"""

import os
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "linux"
    or os.environ.get("DMC268_RUN_DOCKER_TESTS") != "1"
    or shutil.which("docker") is None,
    reason="requires Linux, Docker Compose and explicit restore-drill opt-in",
)
ROOT = Path(__file__).resolve().parents[1]


def command(
    *args: str, data: bytes | None = None, check: bool = True
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        args, input=data, capture_output=True, timeout=90, check=check
    )


def docker(
    *args: str, data: bytes | None = None, check: bool = True
) -> subprocess.CompletedProcess[bytes]:
    return command("docker", *args, data=data, check=check)


def wait_ready(container: str, *probe: str) -> None:
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        if docker("exec", container, *probe, check=False).returncode == 0:
            return
        time.sleep(0.2)
    pytest.fail(f"owned fixture {container} did not become ready")


@pytest.fixture
def deployment(tmp_path: Path) -> Iterator[tuple[Path, str, str, str]]:
    project = f"dmc268-obs26-backup-{uuid.uuid4().hex[:10]}"
    for script in ("backup-postgres.sh", "backup-redis.sh"):
        shutil.copy2(ROOT / "scripts" / script, tmp_path / script)
    (tmp_path / "fixture.env").write_text("# synthetic drill only\n")
    compose = tmp_path / "docker-compose.staging.yml"
    compose.write_text(
        "services:\n"
        "  db:\n"
        "    image: postgres:18\n"
        f"    container_name: {project}-db\n"
        "    labels: {codex.task: dmc268-obs26-backup}\n"
        "    environment:\n"
        "      POSTGRES_USER: dmc\n"
        "      POSTGRES_DB: dmc\n"
        "      POSTGRES_PASSWORD: synthetic-only\n"
        "  redis:\n"
        "    image: redis:8\n"
        f"    container_name: {project}-redis\n"
        "    labels: {codex.task: dmc268-obs26-backup}\n"
        "    command: [redis-server, --appendonly, 'no', --save, '']\n"
    )
    try:
        docker("compose", "-p", project, "-f", str(compose), "up", "-d")
        wait_ready(
            f"{project}-db", "pg_isready", "-h", "127.0.0.1", "-U", "dmc", "-d", "dmc"
        )
        wait_ready(f"{project}-redis", "redis-cli", "ping")
        yield tmp_path, project, f"{project}-db", f"{project}-redis"
    finally:
        for suffix in ("pg-restored", "redis-restored"):
            docker("rm", "-fv", f"{project}-{suffix}", check=False)
        docker(
            "compose",
            "-p",
            project,
            "-f",
            str(compose),
            "down",
            "-v",
            "--remove-orphans",
            check=False,
        )


def sql(
    container: str, statement: str, *, check: bool = True
) -> subprocess.CompletedProcess[bytes]:
    return docker(
        "exec",
        "-i",
        container,
        "psql",
        "-U",
        "dmc",
        "-d",
        "dmc",
        "-At",
        "-v",
        "ON_ERROR_STOP=1",
        data=statement.encode(),
        check=check,
    )


def test_postgres_dump_restores_foreign_keys_unicode_and_sequence(
    deployment: tuple[Path, str, str, str],
) -> None:
    directory, project, source, _ = deployment
    sql(
        source,
        "CREATE TABLE parent(id serial PRIMARY KEY, label text NOT NULL); "
        "CREATE TABLE child(id serial PRIMARY KEY, "
        "parent_id integer REFERENCES parent(id)); "
        "INSERT INTO parent(label) VALUES ('Правило 🔒'); "
        "INSERT INTO child(parent_id) VALUES (1);",
    )
    command(
        "bash",
        str(directory / "backup-postgres.sh"),
        project,
        str(directory / "fixture.env"),
    )
    snapshot = next((directory / "backups").glob("dmc-*.sql.gz"))
    command("gzip", "-t", str(snapshot))
    sql(source, "UPDATE parent SET label='changed after snapshot'; DELETE FROM child;")
    restored = f"{project}-pg-restored"
    docker(
        "run",
        "-d",
        "--name",
        restored,
        "--label",
        "codex.task=dmc268-obs26-backup",
        "-e",
        "POSTGRES_USER=dmc",
        "-e",
        "POSTGRES_DB=dmc",
        "-e",
        "POSTGRES_PASSWORD=synthetic-only",
        "postgres:18",
    )
    wait_ready(restored, "pg_isready", "-h", "127.0.0.1", "-U", "dmc", "-d", "dmc")
    dump = command("gzip", "-dc", str(snapshot)).stdout
    docker(
        "exec",
        "-i",
        restored,
        "psql",
        "-U",
        "dmc",
        "-d",
        "dmc",
        "-v",
        "ON_ERROR_STOP=1",
        "--single-transaction",
        data=dump,
    )
    assert (
        sql(restored, "SELECT p.label FROM parent p JOIN child c ON c.parent_id=p.id;")
        .stdout.decode()
        .strip()
        == "Правило 🔒"
    )
    assert (
        sql(
            restored, "INSERT INTO parent(label) VALUES ('next') RETURNING id;"
        ).stdout.splitlines()[0]
        == b"2"
    )
    assert (
        sql(
            restored, "INSERT INTO child(parent_id) VALUES (999);", check=False
        ).returncode
        != 0
    )
    corrupt = directory / "corrupt.sql.gz"
    corrupt.write_bytes(snapshot.read_bytes()[:20])
    assert command("gzip", "-t", str(corrupt), check=False).returncode != 0
    assert (
        command(
            "bash",
            "-o",
            "pipefail",
            "-c",
            'gzip -dc "$1" | docker exec -i "$2" '
            "psql -U dmc -d dmc -v ON_ERROR_STOP=1 --single-transaction",
            "restore-drill",
            str(corrupt),
            restored,
            check=False,
        ).returncode
        != 0
    )
    assert sql(restored, "SELECT count(*) FROM child;").stdout.strip() == b"1"
    assert (
        sql(source, "SELECT label FROM parent WHERE id=1;").stdout.strip()
        == b"changed after snapshot"
    )


def test_redis_rdb_restores_stream_group_pending_and_rejects_corruption(
    deployment: tuple[Path, str, str, str],
) -> None:
    directory, project, _, source = deployment

    def redis(container: str, *args: str) -> bytes:
        return docker("exec", container, "redis-cli", "--raw", *args).stdout.strip()

    redis(source, "SET", "unicode", "Правило 🔒")
    redis(source, "XADD", "reviews", "1-0", "job_id", "synthetic-one")
    redis(source, "XADD", "reviews", "2-0", "job_id", "synthetic-two")
    redis(source, "XGROUP", "CREATE", "reviews", "analysis", "0")
    redis(
        source,
        "XREADGROUP",
        "GROUP",
        "analysis",
        "worker-before",
        "COUNT",
        "1",
        "STREAMS",
        "reviews",
        ">",
    )
    command(
        "bash",
        str(directory / "backup-redis.sh"),
        project,
        str(directory / "fixture.env"),
    )
    snapshot = next((directory / "backups/redis").glob("redis-*.rdb"))
    redis(source, "XACK", "reviews", "analysis", "1-0")
    redis(source, "SET", "unicode", "changed after snapshot")
    restored = f"{project}-redis-restored"
    docker(
        "create",
        "--name",
        restored,
        "--label",
        "codex.task=dmc268-obs26-backup",
        "redis:8",
        "redis-server",
        "--appendonly",
        "no",
        "--save",
        "",
        "--dir",
        "/data",
        "--dbfilename",
        "dump.rdb",
    )
    docker("cp", str(snapshot), f"{restored}:/data/dump.rdb")
    docker("start", restored)
    wait_ready(restored, "redis-cli", "ping")
    assert redis(restored, "GET", "unicode").decode() == "Правило 🔒"
    assert redis(restored, "XRANGE", "reviews", "-", "+").splitlines() == [
        b"1-0",
        b"job_id",
        b"synthetic-one",
        b"2-0",
        b"job_id",
        b"synthetic-two",
    ]
    pending = redis(restored, "XPENDING", "reviews", "analysis").splitlines()
    assert pending[:3] == [b"1", b"1-0", b"1-0"]
    assert b"worker-before" in pending
    groups = redis(restored, "XINFO", "GROUPS", "reviews").splitlines()
    assert groups[groups.index(b"lag") + 1] == b"1"
    claimed = redis(
        restored, "XAUTOCLAIM", "reviews", "analysis", "worker-after", "0", "0-0"
    ).splitlines()
    assert b"1-0" in claimed and b"synthetic-one" in claimed
    corrupt = directory / "corrupt.rdb"
    corrupt.write_bytes(snapshot.read_bytes()[:20])
    docker("cp", str(corrupt), f"{restored}:/tmp/corrupt.rdb")
    assert (
        docker(
            "exec", restored, "redis-check-rdb", "/tmp/corrupt.rdb", check=False
        ).returncode
        != 0
    )
    assert redis(source, "GET", "unicode") == b"changed after snapshot"
