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
    DOCKER_GROUP=$(getent group "$DOCKER_GID" | cut -d: -f1)
    
    if [ -z "$DOCKER_GROUP" ]; then
        if getent group docker >/dev/null; then
            groupmod -g "$DOCKER_GID" docker
            DOCKER_GROUP="docker"
        else
            groupadd -g "$DOCKER_GID" dockersock
            DOCKER_GROUP="dockersock"
        fi
    fi
    
    usermod -aG "$DOCKER_GROUP" runner
fi

chown -R runner:runner /app
exec gosu runner "$@"
