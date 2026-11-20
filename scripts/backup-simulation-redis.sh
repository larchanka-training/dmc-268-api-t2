#!/bin/bash
# Host-local RDB capture for the dedicated simulation admission Redis service.
set -euo pipefail
umask 077

if [[ $# -ne 2 ]]; then
  echo "usage: backup-simulation-redis.sh COMPOSE_PROJECT ENV_FILE" >&2
  exit 2
fi
compose_project="$1"
env_file="$2"
root="$(cd "$(dirname "$0")" && pwd -P)"
cd "${root}"
dest="${root}/backups/simulation-redis"
stage=prepare
partial=
finish() {
  local status=$?
  if [[ -n "${partial}" ]] && ! rm -f -- "${partial}" 2>/dev/null; then
    echo "dedicated Redis backup failed: cleanup" >&2
    status=1
  fi
  if ((status != 0)); then
    printf 'dedicated Redis backup failed: %s\n' "${stage}" >&2
  fi
  exit "${status}"
}
trap finish EXIT

[[ -f "${root}/docker-compose.staging.yml" ]]
# Keep sensitive archives outside Git checkouts, including linked worktrees.
parent="${root%/}"
while [[ -n "${parent}" ]]; do
  git_entry="${parent}/.git"
  [[ ! -f "${git_entry}" && ! -L "${git_entry}" && ! -f "${git_entry}/HEAD" ]]
  parent="${parent%/*}"
done

private_dir() {
  local dir="$1"
  [[ ! -L "${dir}" ]] || return 1
  mkdir -p -m 700 -- "${dir}"
  [[ -d "${dir}" && -O "${dir}" && "$(stat -c %a -- "${dir}")" == 700 ]]
}
private_dir "${root}/backups"
private_dir "${dest}"

stage=lock
exec {backup_lock}<"${dest}"
flock -n "${backup_lock}" 2>/dev/null

stage=source
container="$(docker compose -p "${compose_project}" --env-file "${env_file}" \
  -f docker-compose.staging.yml --profile simulation ps -q simulation-redis 2>/dev/null)"
[[ "${container}" =~ ^[0-9a-f]{64}$ ]]
identity="$(docker container inspect --format \
  '{{index .Config.Labels "com.docker.compose.project"}} {{index .Config.Labels "com.docker.compose.service"}} {{.State.Running}}{{range .Mounts}}{{if eq .Destination "/data"}} {{.Type}} {{.Name}}{{end}}{{end}}' \
  "${container}" 2>/dev/null)"
[[ "${identity}" == "${compose_project} simulation-redis true volume ${compose_project}_simulation-redis-data" ]]

stage=prepare
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
final="${dest}/simulation-redis-${stamp}.rdb"
partial="$(mktemp "${dest}/.simulation-redis.XXXXXX.partial")"

stage=snapshot
docker exec "${container}" sh -c '
    set -eu
    umask 077
    snapshot="$(mktemp /tmp/dmc268-simulation-redis.XXXXXX)"
    trap '\''rm -f "$snapshot"'\'' EXIT
    redis-cli -h 127.0.0.1 --rdb "$snapshot" >/dev/null
    cat "$snapshot"
  ' > "${partial}" 2>/dev/null

stage=validate-rdb
docker exec -i "${container}" sh -c '
    set -eu
    umask 077
    snapshot="$(mktemp /tmp/dmc268-simulation-redis-check.XXXXXX)"
    trap '\''rm -f "$snapshot"'\'' EXIT
    cat > "$snapshot"
    redis-check-rdb "$snapshot"
  ' < "${partial}" >/dev/null 2>&1

stage=sync-rdb
sync -d -- "${partial}" 2>/dev/null

stage=publish
# Both names are in the same directory. link(2) fails if the final name exists.
ln -T -- "${partial}" "${final}" 2>/dev/null

stage=sync-publication
# Sync the final directory entry and the parent entries created on the first run.
if ! sync -- "${dest}" "${root}/backups" "${root}" 2>/dev/null; then
  rm -f -- "${final}" 2>/dev/null
  exit 1
fi
