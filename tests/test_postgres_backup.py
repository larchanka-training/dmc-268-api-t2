"""Deploy installs a daily Postgres backup on the VPS."""

import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]
KEPT_DUMPS = 7


def test_deploy_installs_postgres_backup_before_caddy() -> None:
    """The existing Deploy job copies the backup script and installs crontab."""
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/deploy-staging.yml").read_text(encoding="utf-8")
    )
    steps = [step for job in workflow["jobs"].values() for step in job.get("steps", [])]
    names = [step.get("name") for step in steps]
    deploy_at = names.index("Deploy application")
    backup_at = names.index("Install Postgres backup")
    caddy_at = names.index("Ensure Caddy HTTPS")
    assert deploy_at < backup_at < caddy_at

    run = steps[backup_at]["run"]
    assert "scripts/backup-postgres.sh" in run
    assert "install -d -m 700" in run
    assert "# dmc-postgres-backup" in run
    assert "15 3 * * *" in run
    assert "45 3 * * *" in run
    assert "crontab is not installed" in run
    assert "/etc/cron.d" not in run
    assert "sudo" not in run

    staging = (ROOT / "docs/staging-deployment.md").read_text(encoding="utf-8")
    postgres = staging.split("### PostgreSQL", 1)[1].split("### Redis", 1)[0]
    assert "/opt/dmc-268-t2/backups/dmc-YYYY-MM-DD.sql.gz" in postgres
    assert "proposals/postgres-backup.md" in postgres
    assert "700" in postgres


def _bash() -> str:
    for candidate in (
        r"C:\dev\msys64\usr\bin\bash.exe",
        r"C:\Program Files\Git\bin\bash.exe",
    ):
        if Path(candidate).is_file():
            return candidate
    found = shutil.which("bash")
    if found is not None and "system32" not in found.lower():
        return found
    pytest.fail("bash is required to run scripts/backup-postgres.sh")


def _write_exe(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")
    path.chmod(0o755)


def test_backup_script_keeps_seven_daily_dumps(tmp_path: Path) -> None:
    """A successful run stores one gzip per UTC day and drops older dumps."""
    bash = _bash()

    script = ROOT / "scripts" / "backup-postgres.sh"
    work = tmp_path / "deploy"
    work.mkdir()
    shutil.copy(script, work / "backup-postgres.sh")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_exe(
        bin_dir / "docker",
        "#!/bin/bash\n"
        'printf \'%s\\n\' "$*" > "${DOCKER_ARGS}"\n'
        "printf 'CREATE TABLE kept (id int);\\n'\n",
    )
    _write_exe(bin_dir / "flock", "#!/bin/bash\nexit 0\n")
    _write_exe(bin_dir / "install", '#!/bin/bash\nmkdir -p "${@: -1}"\n')
    _write_exe(bin_dir / "chmod", "#!/bin/bash\nexit 0\n")
    tools = _tool_dir(bash)
    env = os.environ.copy()
    env["PATH"] = os.pathsep.join([str(bin_dir), tools, env.get("PATH", "")])
    env["DOCKER_ARGS"] = str(tmp_path / "docker-args.txt")

    today = datetime.now(UTC).date()
    for age in range(KEPT_DUMPS):
        old = (
            work
            / "backups"
            / f"dmc-{(today - timedelta(days=age + 1)).isoformat()}.sql.gz"
        )
        old.parent.mkdir(parents=True, exist_ok=True)
        old.write_bytes(b"old")
        stamp = (datetime.now(UTC) - timedelta(days=age + 1)).timestamp()
        os.utime(old, (stamp, stamp))

    completed = subprocess.run(
        [bash, str(work / "backup-postgres.sh"), "dmc-268-t2-staging", ".env.staging"],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr

    dumps = sorted((work / "backups").glob("dmc-*.sql.gz"))
    assert len(dumps) == KEPT_DUMPS
    current = work / "backups" / f"dmc-{today.isoformat()}.sql.gz"
    assert current.is_file()
    assert not (work / "backups" / f"dmc-{today.isoformat()}.sql.gz.partial").exists()
    payload = subprocess.run(
        [bash, "-c", 'gzip -dc "$1"', "gzip", str(current)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "CREATE TABLE kept" in payload.stdout
    args = (tmp_path / "docker-args.txt").read_text(encoding="utf-8")
    assert "compose -p dmc-268-t2-staging --env-file .env.staging" in args
    assert "exec -T db pg_dump -U dmc -d dmc --no-owner --no-acl" in args

    again = subprocess.run(
        [bash, str(work / "backup-postgres.sh"), "dmc-268-t2-staging", ".env.staging"],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert again.returncode == 0, again.stderr
    assert len(list((work / "backups").glob("dmc-*.sql.gz"))) == KEPT_DUMPS


def test_failed_dump_is_not_kept(tmp_path: Path) -> None:
    """A failed pg_dump removes the partial file and leaves older dumps in place."""
    bash = _bash()
    script = ROOT / "scripts" / "backup-postgres.sh"
    work = tmp_path / "deploy"
    work.mkdir()
    shutil.copy(script, work / "backup-postgres.sh")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_exe(
        bin_dir / "docker",
        "#!/bin/bash\nprintf 'ran\\n' > \"${DOCKER_ARGS}\"\nexit 1\n",
    )
    _write_exe(bin_dir / "flock", "#!/bin/bash\nexit 0\n")
    _write_exe(bin_dir / "install", '#!/bin/bash\nmkdir -p "${@: -1}"\n')
    _write_exe(bin_dir / "chmod", "#!/bin/bash\nexit 0\n")
    kept = work / "backups" / "dmc-2000-01-01.sql.gz"
    kept.parent.mkdir()
    kept.write_bytes(b"old")
    env = os.environ.copy()
    env["PATH"] = os.pathsep.join([str(bin_dir), _tool_dir(bash), env.get("PATH", "")])
    env["DOCKER_ARGS"] = str(tmp_path / "docker-args.txt")

    completed = subprocess.run(
        [bash, str(work / "backup-postgres.sh"), "dmc-268-t2-staging", ".env.staging"],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert (tmp_path / "docker-args.txt").is_file()
    assert kept.is_file()
    assert list((work / "backups").glob("dmc-*.sql.gz.partial")) == []
    assert list((work / "backups").glob("dmc-*.sql.gz")) == [kept]


def _tool_dir(bash: str) -> str:
    usr_bin = Path(bash).parent
    gzip = usr_bin / "gzip.exe"
    if not gzip.is_file():
        gzip = usr_bin / "gzip"
    if not gzip.is_file():
        found = shutil.which("gzip")
        if found is None:
            pytest.fail("gzip is required to run scripts/backup-postgres.sh")
        return str(Path(found).parent)
    return str(usr_bin)
