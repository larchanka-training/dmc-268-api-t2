"""Stub docker/curl binaries for Deploy-application REMOTE script unit tests."""

from __future__ import annotations

from pathlib import Path

_DOCKER_STUB = r"""#!/bin/bash
trace_file="${DEPLOY_TRACE:-${TRACE:-/dev/null}}"
printf "%s\n" "$*" >> "$trace_file"

# docker compose ... ps -q <service>
if [[ "$1" == compose && " $* " == *" ps -q "* ]]; then
  echo "cid-${@: -1}"
  exit 0
fi

if [[ "$1" == inspect ]]; then
  if [[ "$*" == *Health* ]]; then
    echo healthy
  elif [[ "$*" == *Config.Image* ]]; then
    cid="${@: -1}"
    env_file="${ENV_FILE:-.env.test}"
    if [[ "$cid" == *web* ]]; then
      grep -E '^UI_IMAGE=' "${env_file}" | tail -n1 | cut -d= -f2-
    else
      grep -E '^API_IMAGE=' "${env_file}" | tail -n1 | cut -d= -f2-
    fi
  else
    echo running
  fi
  exit 0
fi

if [[ "$*" == *"publication-worker python -c"* ]]; then
  exit "${PUBLICATION_CHECK_FAILURE:-0}"
fi
if [[ "$*" == *" worker python -c"* ]]; then
  exit "${VALIDATION_FAILURE:-0}"
fi
if [[ "$*" == *" worker python -"* ]]; then
  exit "${PREFLIGHT_FAILURE:-0}"
fi
exit 0
"""

_CURL_STUB = r"""#!/bin/bash
header_out=""
url=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -D)
      header_out="$2"
      shift 2
      ;;
    -o|--output|--max-redirs)
      shift 2
      ;;
    -sS|-s|-S|-f|-L)
      shift
      ;;
    *)
      url="$1"
      shift
      ;;
  esac
done
if [[ -n "${header_out}" && "${url}" == *auth/github/start* ]]; then
  {
    printf 'HTTP/1.1 302 Found\r\n'
    printf 'Location: https://github.com/login/oauth/authorize'
    printf '?client_id=x&scope=repo&state=y\r\n\r\n'
  } > "${header_out}"
fi
exit 0
"""


def install_deploy_remote_fakes(binary: Path) -> None:
    """Install executable docker/curl stubs under ``binary`` (on PATH)."""
    binary.mkdir(parents=True, exist_ok=True)
    docker = binary / "docker"
    docker.write_text(_DOCKER_STUB, encoding="utf-8", newline="\n")
    docker.chmod(0o755)
    curl = binary / "curl"
    curl.write_text(_CURL_STUB, encoding="utf-8", newline="\n")
    curl.chmod(0o755)
