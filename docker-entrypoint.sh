#!/bin/sh
set -e

# If the Docker socket is mounted, ensure appuser can access it by joining
# whatever group owns it on the host — the GID varies per machine.
if [ -S /var/run/docker.sock ]; then
    SOCK_GID=$(stat -c '%g' /var/run/docker.sock)
    if ! getent group "$SOCK_GID" > /dev/null 2>&1; then
        groupadd --gid "$SOCK_GID" dockersock
    fi
    SOCK_GROUP=$(getent group "$SOCK_GID" | cut -d: -f1)
    usermod -aG "$SOCK_GROUP" appuser
fi

# Ensure the data directory exists and is writable by appuser.
# The ./app:/app bind mount shadows the image-layer mkdir+chown, so we
# re-apply ownership here while we still have root.
mkdir -p /app/data
chown appuser:appuser /app/data

# Drop privileges and exec the application as appuser
exec gosu appuser "$@"
