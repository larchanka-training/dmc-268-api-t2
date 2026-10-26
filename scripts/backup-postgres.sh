#!/bin/bash
# pg_dump of the Compose Postgres service. Dumps stay on the host.
# Two arguments write the daily cron file. A third argument, predeploy,
# writes a timestamped snapshot and does not replace an existing daily file.
set -euo pipefail
umask 077

if ! command -v docker >/dev/null 2>&1 || ! command -v gzip >/dev/null 2>&1 || ! command -v flock >/dev/null 2>&1; then
  export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:${PATH:-}"
fi

if [[ $# -lt 2 || $# -gt 3 ]]; then
  echo "usage: backup-postgres.sh COMPOSE_PROJECT ENV_FILE [predeploy]" >&2
  exit 2
fi

compose_project="$1"
env_file="$2"
mode="${3:-}"
if [[ -n "${mode}" && "${mode}" != "predeploy" ]]; then
  echo "usage: backup-postgres.sh COMPOSE_PROJECT ENV_FILE [predeploy]" >&2
  exit 2
fi

root="$(cd "$(dirname "$0")" && pwd)"
cd "${root}"
dest="${root}/backups"

make_private_dir() {
  local dir="$1"
  install -d -m 700 "${dir}"
  chmod 700 "${dir}"
}

log_status() {
  local status="$1"
  local detail="${2-}"
  local log="${dest}/backup.log"
  local now
  now="$(date -u +%FT%TZ)"
  if [[ -n "${detail}" ]]; then
    printf '%s %s %s\n' "${now}" "${status}" "${detail}" >> "${log}"
  else
    printf '%s %s\n' "${now}" "${status}" >> "${log}"
  fi
  tail -n 30 "${log}" > "${log}.tmp"
  mv "${log}.tmp" "${log}"
}

make_private_dir "${dest}"

exec 9>"${dest}/backup.lock"
if [[ "${mode}" == "predeploy" ]]; then
  if ! flock -w 600 9; then
    log_status fail "lock"
    exit 1
  fi
elif ! flock -n 9; then
  log_status skip-lock
  echo "backup already running" >&2
  exit 0
fi

stamp_date="$(date -u +%F)"
if [[ "${mode}" == "predeploy" ]]; then
  make_private_dir "${dest}/predeploy"
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  partial="${dest}/predeploy/dmc-${stamp}.sql.gz.partial"
  final="${dest}/predeploy/dmc-${stamp}.sql.gz"
else
  partial="${dest}/dmc-${stamp_date}.sql.gz.partial"
  final="${dest}/dmc-${stamp_date}.sql.gz"
fi
rm -f "${partial}"

fail_dump() {
  rm -f "${partial}"
  log_status fail "$1"
  exit 1
}

if ! docker compose \
  -p "${compose_project}" \
  --env-file "${env_file}" \
  -f docker-compose.staging.yml \
  exec -T db \
  pg_dump -U dmc -d dmc --no-owner --no-acl \
  | gzip -c > "${partial}"
then
  fail_dump "pg_dump"
fi

if ! gzip -t "${partial}"; then
  fail_dump "gzip"
fi

chmod 600 "${partial}"
mv -f "${partial}" "${final}"

if [[ "${mode}" == "predeploy" ]]; then
  daily="${dest}/dmc-${stamp_date}.sql.gz"
  if [[ ! -e "${daily}" ]]; then
    cp "${final}" "${daily}"
    chmod 600 "${daily}"
  fi

  cutoff="$(date -u -d "${stamp_date} -6 days" +%Y%m%d)"
  shopt -s nullglob
  for old in "${dest}/predeploy"/dmc-*.sql.gz; do
    base="$(basename "${old}")"
    if [[ ! "${base}" =~ ^dmc-([0-9]{8})T[0-9]{6}Z\.sql\.gz$ ]]; then
      continue
    fi
    if [[ "${BASH_REMATCH[1]}" < "${cutoff}" ]]; then
      rm -f "${old}"
    fi
  done
  shopt -u nullglob
fi

shopt -s nullglob
daily_files=("${dest}"/dmc-????-??-??.sql.gz)
shopt -u nullglob
if ((${#daily_files[@]} > 7)); then
  mapfile -t sorted < <(printf '%s\n' "${daily_files[@]}" | sort)
  extra=$((${#sorted[@]} - 7))
  for ((i = 0; i < extra; i++)); do
    rm -f "${sorted[i]}"
  done
fi

log_status ok "${final}"
