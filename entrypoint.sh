#!/bin/bash
PUID=${PUID:-1000}
PGID=${PGID:-1000}
set -e

echo "Adding runner user.."
if ! getent group runner >/dev/null; then
    groupadd -g "$PGID" runner
fi

if ! id -u runner >/dev/null 2>&1; then
    useradd -u "$PUID" -g "$PGID" -m -s /bin/bash runner
fi

if ! getent group docker >/dev/null; then
    groupadd docker
fi

usermod -aG docker runner
echo "Starting Docker daemon..."
dockerd > /var/log/dockerd.log 2>&1 &

timeout 30 sh -c 'until docker info >/dev/null 2>&1; do sleep 1; done'

if ! docker info >/dev/null 2>&1; then
    echo "Error while starting the docker daemon!"
    cat /var/log/dockerd.log
    exit 1
fi

echo "Docker daemon is running"

chown -R runner:runner /app
exec gosu runner "$@"
