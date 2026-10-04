#!/bin/ash
export PUID=${PUID:-1000}
export PGID=${PGID:-1000}
export TOKEN=${TOKEN:-False}

export TZ=${TZ:-Europe/Budapest}
if [ -f "/usr/share/zoneinfo/$TZ" ]; then
    ln -snf "/usr/share/zoneinfo/$TZ" /etc/localtime
fi

if ! id -u controller >/dev/null 2>&1; then
    addgroup -g "$PGID" controller 2>/dev/null || true
    adduser -u "$PUID" -G controller -D -h /home/controller -s /bin/ash controller

    if [ -S /var/run/docker.sock ]; then
        DOCKER_GID=$(stat -c '%g' /var/run/docker.sock)
        DOCKER_GROUP=$(getent group "$DOCKER_GID" | cut -d: -f1)

        if [ -z "$DOCKER_GROUP" ]; then
            DOCKER_GROUP="dockersock"
            addgroup -g "$DOCKER_GID" "$DOCKER_GROUP" 2>/dev/null || true
        fi
        addgroup controller "$DOCKER_GROUP" 2>/dev/null || true
    fi
fi


chown controller:controller /app

cat << EOF
##############################
#  Github-Runner-Controller  #            
##############################
EOF
exec gosu controller "$@"