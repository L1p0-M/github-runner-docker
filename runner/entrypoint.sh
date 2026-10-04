#!/bin/bash
PUID=${PUID:-1000}
PGID=${PGID:-1000}
PACKAGES=${PACKAGES:-}
set -e

echo "[INFO] Setting IDs for Runner user.."
if [ "$(id -g runner)" -ne "$PGID" ]; then
    groupmod -o -g "$PGID" runner
fi

if [ "$(id -u runner)" -ne "$PUID" ]; then
    usermod -o -u "$PUID" runner
fi

if ! getent group docker >/dev/null; then
    groupadd docker
fi

usermod -aG docker runner

echo "[INFO] Starting Docker daemon..."
dockerd --storage-driver=fuse-overlayfs > /var/log/docker.log 2>&1 &

timeout 30 sh -c 'until docker info >/dev/null 2>&1; do sleep 1; done'

if ! docker info >/dev/null 2>&1; then
    echo "Error while starting the docker daemon!"
    cat /var/log/docker.log
    exit 1
fi

# Fix permission for docker group... Just to be sure
if [ -S /var/run/docker.sock ]; then
    chown root:docker /var/run/docker.sock
fi

echo "[INFO] Docker daemon is running"

if [ -n "$PACKAGES" ]; then
    echo "[INFO] Installing user packages: $PACKAGES"
    if apt-get update && sudo apt-get install -y --no-install-recommends $(echo "$PACKAGES" | tr ',' ' '); then
        echo "Packages installed successfully."
        rm -rf /var/lib/apt/lists/*
    else
        echo "Error while trying to install user packages!!"
    fi
else
    echo "[INFO] No need to install any extra package, continue"
fi

chown -R runner:runner /app

mkdir -p /home/runner/.local/bin
export PATH="/home/runner/.local/bin:$PATH"
chown -R runner:runner /home/runner/.local

exec gosu runner "$@"
