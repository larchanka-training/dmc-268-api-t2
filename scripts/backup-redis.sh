#!/bin/bash
# Daily RDB snapshot of the Compose Redis service; files stay on this host.
set -euo pipefail
umask 077
export PATH="${PATH:+${PATH}:}/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

if [[ $# -ne 2 ]]; then
  echo "usage: backup-redis.sh COMPOSE_PROJECT ENV_FILE" >&2
  exit 2
fi
compose_project="$1"
env_file="$2"
root="$(cd "$(dirname "$0")" && pwd)"
if [[ ! -f "${root}/docker-compose.staging.yml" && -f "${root}/../docker-compose.staging.yml" ]]; then
  root="$(cd "${root}/.." && pwd)"
fi
cd "${root}"
install -d -m 700 "${root}/backups" "${root}/backups/redis"
chmod 700 "${root}/backups" "${root}/backups/redis"
dest="${root}/backups/redis"
stamp="$(date -u +%F)"
final="${dest}/redis-${stamp}.rdb"
partial="${final}.partial"
log_status() {
  printf '%s %s\n' "$(date -u +%FT%TZ)" "$1" >> "${dest}/backup.log"
  tail -n 30 "${dest}/backup.log" > "${dest}/backup.log.tmp"
  mv "${dest}/backup.log.tmp" "${dest}/backup.log"
}
finish() {
  local status=$?
  rm -f "${partial}"
  if ((status != 0)); then
    log_status "fail ${stage} exit=${status}"
  fi
}
exec 9>"${dest}/backup.lock"
flock -n 9 2>/dev/null || {
  status=$?
  if ((status == 1)); then
    log_status skip-lock
    exit 0
  fi
  log_status "fail flock exit=${status}"
  exit "${status}"
}
stage=snapshot
trap finish EXIT

docker compose -p "${compose_project}" --env-file "${env_file}" \
  -f docker-compose.staging.yml exec -T redis sh -c '
    set -eu
    umask 077
    snapshot="$(mktemp /tmp/dmc268-redis.XXXXXX)"
    trap '\''rm -f "$snapshot"'\'' EXIT
    redis-cli --rdb "$snapshot" >/dev/null
    cat "$snapshot"
  ' > "${partial}" 2>/dev/null

stage=validate-rdb
docker compose -p "${compose_project}" --env-file "${env_file}" \
  -f docker-compose.staging.yml exec -T redis sh -c '
    set -eu
    umask 077
    snapshot="$(mktemp /tmp/dmc268-redis-check.XXXXXX)"
    trap '\''rm -f "$snapshot"'\'' EXIT
    cat > "$snapshot"
    redis-check-rdb "$snapshot" >/dev/null
  ' < "${partial}" >/dev/null 2>&1
chmod 600 "${partial}"
mv -f "${partial}" "${final}"

shopt -s nullglob
snapshots=("${dest}"/redis-????-??-??.rdb)
shopt -u nullglob
extra=$((${#snapshots[@]} - 7))
for ((i = 0; i < extra; i++)); do
  rm -f "${snapshots[i]}"
done
log_status ok
