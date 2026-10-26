"""Deploy keeps a pre-migration Postgres dump and a separate daily cron dump."""

import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]
KEPT_DUMPS = 7
DEPLOY_TIMEOUT_MINUTES = 30

_DOCKER_OK = """#!/bin/bash
printf '%s\\n' "$*" > "${DOCKER_ARGS}"
partial="$(find . -name 'dmc-*.sql.gz.partial' -print -quit)"
if [[ -n "${partial}" ]]; then
  stat -c '%a' "${partial}" > "${PARTIAL_MODE}"
fi
printf 'CREATE TABLE kept (id int);\\n'
"""

_DOCKER_FAIL = """#!/bin/bash
printf 'ran\\n' > "${DOCKER_ARGS}"
exit 1
"""

_FLOCK_OK = """#!/bin/bash
printf '%s\\n' "$*" >> "${FLOCK_ARGS}"
exit 0
"""

_FLOCK_BUSY = """#!/bin/bash
printf '%s\\n' "$*" >> "${FLOCK_ARGS}"
exit 1
"""

_DATE_TWO_STAMPS = """#!/bin/bash
if [[ "$1" == "-u" && "$2" == "+%F" ]]; then
  printf '2026-10-25\\n'
  exit 0
fi
if [[ "$1" == "-u" && "$2" == "+%Y%m%dT%H%M%SZ" ]]; then
  n=0
  if [[ -f "${DATE_SEQ}" ]]; then
    n="$(cat "${DATE_SEQ}")"
  fi
  printf '20261025T12000%sZ\\n' "${n}"
  echo $((n + 1)) > "${DATE_SEQ}"
  exit 0
fi
exec /usr/bin/date "$@"
"""


def test_deploy_snapshots_before_migrate_and_caddy_before_crontab() -> None:
    """Deploy dumps before alembic. Caddy runs before the crontab install."""
    text = (ROOT / ".github/workflows/deploy-staging.yml").read_text(encoding="utf-8")
    assert "crontab -l 2>/dev/null || true" not in text

    workflow = yaml.safe_load(text)
    deploy_job = workflow["jobs"]["deploy"]
    assert deploy_job["timeout-minutes"] == DEPLOY_TIMEOUT_MINUTES
    steps = [step for job in workflow["jobs"].values() for step in job.get("steps", [])]
    names = [step.get("name") for step in steps]
    deploy_at = names.index("Deploy application")
    backup_at = names.index("Install Postgres backup")
    caddy_at = names.index("Ensure Caddy HTTPS")
    assert deploy_at < caddy_at < backup_at

    deploy_run = steps[deploy_at]["run"]
    alembic_at = deploy_run.index("alembic upgrade head")
    assert deploy_run.index("scripts/backup-postgres.sh") < alembic_at
    remote = deploy_run.split("<<'REMOTE'", 1)[1]
    assert remote.lstrip().startswith("set -euo pipefail")
    predeploy_line = next(
        line
        for line in remote.splitlines()
        if "backup-postgres.sh" in line and "predeploy" in line
    )
    assert "||" not in predeploy_line
    assert remote.index(predeploy_line) < remote.index("alembic upgrade head")

    run = steps[backup_at]["run"]
    assert "scripts/backup-postgres.sh" in run
    assert "install -d -m 700" in run
    assert "# dmc-postgres-backup" in run
    assert "15 3 * * *" in run
    assert "45 3 * * *" in run
    assert "crontab is not installed" in run
    assert "/etc/cron.d" not in run
    assert "sudo" not in run
    line_eq = next(
        line for line in run.splitlines() if line.strip().startswith("line=")
    )
    assert ">>" in line_eq
    assert "backups/cron.log" in line_eq
    assert "2>&1" in line_eq
    install_remote = run.split("<<'REMOTE'", 1)[1]
    executed = '"${DEPLOY_PATH}/backup-postgres.sh" "${COMPOSE_PROJECT}"'
    assert executed not in install_remote
    assert "no crontab" in install_remote
    assert "crontab -l 2>&1" in install_remote

    staging = (ROOT / "docs/staging-deployment.md").read_text(encoding="utf-8")
    postgres = staging.split("### PostgreSQL", 1)[1].split("### Redis", 1)[0]
    assert "/opt/dmc-268-t2/backups/dmc-YYYY-MM-DD.sql.gz" in postgres
    assert "/opt/dmc-268-t2/backups/predeploy/" in postgres
    assert "proposals/postgres-backup.md" in postgres
    assert "postgres-backup-review-fixes.md" not in postgres
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


def _mode(bash: str, path: Path) -> str:
    completed = subprocess.run(
        [bash, "-c", 'stat -c %a "$1"', "stat", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _prepare(
    tmp_path: Path,
    bash: str,
    docker: str,
    flock: str,
    *,
    date: str | None = None,
) -> tuple[Path, dict[str, str]]:
    work = tmp_path / "deploy"
    work.mkdir()
    shutil.copy(ROOT / "scripts" / "backup-postgres.sh", work / "backup-postgres.sh")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_exe(bin_dir / "docker", docker)
    _write_exe(bin_dir / "flock", flock)
    if date is not None:
        _write_exe(bin_dir / "date", date)
    env = os.environ.copy()
    env["PATH"] = os.pathsep.join([str(bin_dir), _tool_dir(bash), env.get("PATH", "")])
    env["DOCKER_ARGS"] = str(tmp_path / "docker-args.txt")
    env["PARTIAL_MODE"] = str(tmp_path / "partial-mode.txt")
    env["FLOCK_ARGS"] = str(tmp_path / "flock-args.txt")
    env["DATE_SEQ"] = str(tmp_path / "date-seq.txt")
    return work, env


def _run(
    bash: str, work: Path, env: dict[str, str], *args: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [bash, str(work / "backup-postgres.sh"), *args],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_backup_script_keeps_seven_daily_dumps(tmp_path: Path) -> None:
    """A successful run stores one gzip per UTC day and drops older dumps."""
    bash = _bash()
    work, env = _prepare(tmp_path, bash, _DOCKER_OK, _FLOCK_OK)

    today = datetime.now(UTC).date()
    for age in range(KEPT_DUMPS):
        name = f"dmc-{(today - timedelta(days=age + 1)).isoformat()}.sql.gz"
        old = work / "backups" / name
        old.parent.mkdir(parents=True, exist_ok=True)
        old.write_bytes(b"old")
        stamp = (datetime.now(UTC) - timedelta(days=age + 1)).timestamp()
        os.utime(old, (stamp, stamp))

    completed = _run(bash, work, env, "dmc-268-t2-staging", ".env.staging")
    assert completed.returncode == 0, completed.stderr

    dumps = sorted((work / "backups").glob("dmc-*.sql.gz"))
    assert len(dumps) == KEPT_DUMPS
    current = work / "backups" / f"dmc-{today.isoformat()}.sql.gz"
    assert current.is_file()
    assert not (work / "backups" / f"dmc-{today.isoformat()}.sql.gz.partial").exists()
    assert _mode(bash, work / "backups") == "700"
    assert _mode(bash, current) == "600"
    assert (tmp_path / "partial-mode.txt").read_text(encoding="utf-8").strip() == "600"
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

    again = _run(bash, work, env, "dmc-268-t2-staging", ".env.staging")
    assert again.returncode == 0, again.stderr
    assert len(list((work / "backups").glob("dmc-*.sql.gz"))) == KEPT_DUMPS


def test_daily_rotation_uses_names_and_ignores_predeploy(tmp_path: Path) -> None:
    """An older daily name is deleted even when its mtime is newest."""
    bash = _bash()
    work, env = _prepare(tmp_path, bash, _DOCKER_OK, _FLOCK_OK)
    today = datetime.now(UTC).date()
    backups = work / "backups"
    backups.mkdir()
    oldest = backups / f"dmc-{(today - timedelta(days=8)).isoformat()}.sql.gz"
    oldest.write_bytes(b"oldest-name")
    os.utime(oldest, (datetime.now(UTC).timestamp(), datetime.now(UTC).timestamp()))
    for age in range(1, 8):
        path = backups / f"dmc-{(today - timedelta(days=age)).isoformat()}.sql.gz"
        path.write_bytes(b"kept-name")
        stamp = (datetime.now(UTC) - timedelta(days=age + 3)).timestamp()
        os.utime(path, (stamp, stamp))
    predeploy = backups / "predeploy" / "dmc-1999-01-01.sql.gz"
    predeploy.parent.mkdir()
    predeploy.write_bytes(b"predeploy-daily-name")

    completed = _run(bash, work, env, "dmc-268-t2-staging", ".env.staging")
    assert completed.returncode == 0, completed.stderr

    names = sorted(path.name for path in backups.glob("dmc-*.sql.gz"))
    assert oldest.name not in names
    assert f"dmc-{today.isoformat()}.sql.gz" in names
    assert len(names) == KEPT_DUMPS
    assert predeploy.is_file()
    assert predeploy.read_bytes() == b"predeploy-daily-name"


def test_failed_dump_is_not_kept(tmp_path: Path) -> None:
    """A failed pg_dump removes the partial file and leaves older dumps in place."""
    bash = _bash()
    work, env = _prepare(tmp_path, bash, _DOCKER_FAIL, _FLOCK_OK)
    kept = work / "backups" / "dmc-2000-01-01.sql.gz"
    kept.parent.mkdir()
    kept.write_bytes(b"old")

    completed = _run(bash, work, env, "dmc-268-t2-staging", ".env.staging")

    assert completed.returncode != 0
    assert (tmp_path / "docker-args.txt").is_file()
    assert kept.is_file()
    assert list((work / "backups").glob("dmc-*.sql.gz.partial")) == []
    assert list((work / "backups").glob("dmc-*.sql.gz")) == [kept]
    assert "fail" in (work / "backups" / "backup.log").read_text(encoding="utf-8")


def test_busy_lock_skips_daily_run_and_fails_predeploy(tmp_path: Path) -> None:
    """Cron skips a busy lock. A predeploy wait timeout fails the deploy."""
    bash = _bash()
    work, env = _prepare(tmp_path, bash, _DOCKER_OK, _FLOCK_BUSY)

    daily = _run(bash, work, env, "dmc-268-t2-staging", ".env.staging")
    assert daily.returncode == 0, daily.stderr
    flock_args = (tmp_path / "flock-args.txt").read_text(encoding="utf-8").split()
    assert "-n" in flock_args
    assert "-w" not in flock_args
    assert "skip-lock" in (work / "backups" / "backup.log").read_text(encoding="utf-8")

    (tmp_path / "flock-args.txt").write_text("", encoding="utf-8")
    predeploy = _run(bash, work, env, "dmc-268-t2-staging", ".env.staging", "predeploy")
    assert predeploy.returncode != 0
    predeploy_args = (tmp_path / "flock-args.txt").read_text(encoding="utf-8").split()
    assert "-w" in predeploy_args
    assert "600" in predeploy_args


def test_two_predeploy_runs_keep_both_snapshots(tmp_path: Path) -> None:
    """A second predeploy writes a new file and leaves the first in place."""
    bash = _bash()
    work, env = _prepare(tmp_path, bash, _DOCKER_OK, _FLOCK_OK, date=_DATE_TWO_STAMPS)

    first = _run(bash, work, env, "dmc-268-t2-staging", ".env.staging", "predeploy")
    assert first.returncode == 0, first.stderr
    second = _run(bash, work, env, "dmc-268-t2-staging", ".env.staging", "predeploy")
    assert second.returncode == 0, second.stderr

    predeploy = work / "backups" / "predeploy"
    names = sorted(path.name for path in predeploy.glob("dmc-*.sql.gz"))
    assert names == [
        "dmc-20261025T120000Z.sql.gz",
        "dmc-20261025T120001Z.sql.gz",
    ]
    assert _mode(bash, predeploy) == "700"
    assert _mode(bash, predeploy / names[0]) == "600"
    assert (tmp_path / "partial-mode.txt").read_text(encoding="utf-8").strip() == "600"


def test_predeploy_copies_missing_daily_and_leaves_existing_daily(
    tmp_path: Path,
) -> None:
    """A missing daily file is a copy. An existing daily file is not replaced."""
    bash = _bash()
    work, env = _prepare(tmp_path, bash, _DOCKER_OK, _FLOCK_OK, date=_DATE_TWO_STAMPS)

    created = _run(bash, work, env, "dmc-268-t2-staging", ".env.staging", "predeploy")
    assert created.returncode == 0, created.stderr
    predeploy = work / "backups" / "predeploy" / "dmc-20261025T120000Z.sql.gz"
    daily = work / "backups" / "dmc-2026-10-25.sql.gz"
    assert predeploy.is_file()
    assert daily.read_bytes() == predeploy.read_bytes()
    assert _mode(bash, daily) == "600"

    daily.write_bytes(b"already-there")
    again = _run(bash, work, env, "dmc-268-t2-staging", ".env.staging", "predeploy")
    assert again.returncode == 0, again.stderr
    assert daily.read_bytes() == b"already-there"
    assert (work / "backups" / "predeploy" / "dmc-20261025T120001Z.sql.gz").is_file()
    assert predeploy.is_file()


def test_predeploy_keeps_seven_utc_dates_without_a_per_day_cap(tmp_path: Path) -> None:
    """Today's predeploy files all stay. A file eight UTC days old is removed."""
    bash = _bash()
    work, env = _prepare(tmp_path, bash, _DOCKER_OK, _FLOCK_OK)
    today = datetime.now(UTC)
    predeploy = work / "backups" / "predeploy"
    predeploy.mkdir(parents=True)
    planted = []
    for index in range(8):
        path = predeploy / f"dmc-{today.strftime('%Y%m%d')}T01000{index}Z.sql.gz"
        path.write_bytes(b"today")
        planted.append(path)
    six_days = predeploy / (
        f"dmc-{(today - timedelta(days=6)).strftime('%Y%m%d')}T010000Z.sql.gz"
    )
    six_days.write_bytes(b"six")
    eight_days = predeploy / (
        f"dmc-{(today - timedelta(days=8)).strftime('%Y%m%d')}T010000Z.sql.gz"
    )
    eight_days.write_bytes(b"eight")
    other = predeploy / "notes.txt"
    other.write_bytes(b"keep")

    completed = _run(bash, work, env, "dmc-268-t2-staging", ".env.staging", "predeploy")
    assert completed.returncode == 0, completed.stderr

    for path in planted:
        assert path.is_file()
    assert six_days.is_file()
    assert not eight_days.exists()
    assert other.is_file()
    same_day = list(predeploy.glob(f"dmc-{today.strftime('%Y%m%d')}T*.sql.gz"))
    assert len(same_day) == len(planted) + 1
