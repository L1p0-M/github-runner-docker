#!/bin/bash
PUID=${PUID:-1000}
PGID=${PGID:-1000}

if ! getent group runner >/dev/null; then
    groupadd -g "$PGID" runner
fi

if ! id -u runner >/dev/null 2>&1; then
    useradd -u "$PUID" -g "$PGID" -m -s /bin/bash runner
fi

chown -R runner:runner /app
exec gosu runner "$@"