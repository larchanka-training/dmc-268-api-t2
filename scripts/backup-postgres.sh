#!/bin/bash
# Daily pg_dump of the Compose Postgres service. Dumps stay on the host.
set -euo pipefail

if ! command -v docker >/dev/null 2>&1 || ! command -v gzip >/dev/null 2>&1 || ! command -v flock >/dev/null 2>&1; then
  export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:${PATH:-}"
fi

if [[ $# -ne 2 ]]; then
  echo "usage: backup-postgres.sh COMPOSE_PROJECT ENV_FILE" >&2
  exit 2
fi

compose_project="$1"
env_file="$2"
root="$(cd "$(dirname "$0")" && pwd)"
cd "${root}"
dest="${root}/backups"
install -d -m 700 "${dest}"
chmod 700 "${dest}"

exec 9>"${dest}/backup.lock"
if ! flock -n 9; then
  echo "backup already running" >&2
  exit 0
fi

stamp="$(date -u +%F)"
partial="${dest}/dmc-${stamp}.sql.gz.partial"
final="${dest}/dmc-${stamp}.sql.gz"
rm -f "${partial}"

if ! docker compose \
  -p "${compose_project}" \
  --env-file "${env_file}" \
  -f docker-compose.staging.yml \
  exec -T db \
  pg_dump -U dmc -d dmc --no-owner --no-acl \
  | gzip -c > "${partial}"
then
  rm -f "${partial}"
  exit 1
fi

if ! gzip -t "${partial}"; then
  rm -f "${partial}"
  exit 1
fi

chmod 600 "${partial}"
mv -f "${partial}" "${final}"

ls -1t "${dest}"/dmc-*.sql.gz | tail -n +8 | while IFS= read -r old; do
  rm -f "${old}"
done

log="${dest}/backup.log"
printf '%s ok %s\n' "$(date -u +%FT%TZ)" "${final}" >> "${log}"
tail -n 30 "${log}" > "${log}.tmp"
mv "${log}.tmp" "${log}"
