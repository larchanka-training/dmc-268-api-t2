#!/usr/bin/env bash
set -euo pipefail

DEPLOY_PATH="${1:?deploy path is required}"

if [[ "$(id -u)" -ne 0 ]]; then
    echo "ERROR: bootstrap must run as root"
    exit 1
fi

if [[ ! -r /etc/os-release ]]; then
    echo "ERROR: /etc/os-release is unavailable"
    exit 1
fi

# shellcheck disable=SC1091
. /etc/os-release

if [[ "${ID:-}" != "debian" ]]; then
    echo "ERROR: unsupported OS: ${PRETTY_NAME:-unknown}"
    exit 1
fi

echo "Provisioning ${PRETTY_NAME:-Debian}"

if command -v docker >/dev/null 2>&1 &&
   docker compose version >/dev/null 2>&1; then
    echo "Docker Engine and Docker Compose are already installed"
else
    conflicts=()

    for package in \
        docker.io \
        docker-compose \
        docker-doc \
        docker-buildx \
        podman-docker \
        containerd \
        runc
    do
        if dpkg-query -W -f='${db:Status-Abbrev}' "${package}" 2>/dev/null \
            | grep -q '^ii'; then
            conflicts+=("${package}")
        fi
    done

    if (( ${#conflicts[@]} > 0 )); then
        echo "ERROR: conflicting Docker packages are installed:"
        printf '  %s\n' "${conflicts[@]}"
        echo "Refusing to remove packages automatically."
        exit 1
    fi

    apt-get update

    DEBIAN_FRONTEND=noninteractive apt-get install -y \
        ca-certificates \
        curl

    install -m 0755 -d /etc/apt/keyrings

    curl -fsSL \
        https://download.docker.com/linux/debian/gpg \
        -o /etc/apt/keyrings/docker.asc

    chmod a+r /etc/apt/keyrings/docker.asc

    cat > /etc/apt/sources.list.d/docker.sources <<DOCKER_REPO
Types: deb
URIs: https://download.docker.com/linux/debian
Suites: ${VERSION_CODENAME}
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
DOCKER_REPO

    apt-get update

    DEBIAN_FRONTEND=noninteractive apt-get install -y \
        docker-ce \
        docker-ce-cli \
        containerd.io \
        docker-buildx-plugin \
        docker-compose-plugin
fi

systemctl enable --now docker

install -d -m 0755 "${DEPLOY_PATH}"

echo
echo "Bootstrap complete"
echo "Deployment directory: ${DEPLOY_PATH}"
docker --version
docker compose version
