#!/bin/bash
PUID=${PUID:-1000}
PGID=${PGID:-1000}
set -e

if ! getent group runner >/dev/null; then
    groupadd -g "$PGID" runner
fi

if ! id -u runner >/dev/null 2>&1; then
    useradd -u "$PUID" -g "$PGID" -m -s /bin/bash runner
fi

if [ -S /var/run/docker.sock ]; then
    DOCKER_GID=$(stat -c '%g' /var/run/docker.sock)
    if ! getent group "$DOCKER_GID" >/dev/null; then
        groupmod -g "$DOCKER_GID" docker 2>/dev/null || true
        usermod -aG docker runner
    else
        EXISTING_GROUP=$(getent group "$DOCKER_GID" | cut -d: -f1)
        usermod -aG "$EXISTING_GROUP" runner
    fi
fi

chown -R runner:runner /app
exec gosu runner "$@"
