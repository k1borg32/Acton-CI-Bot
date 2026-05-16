#!/bin/sh
# Bot container entrypoint.
#
# Runs as root briefly to fix ownership on bind-mounted /app/data
# (Coolify and many other orchestrators create host dirs as root, and
# our non-root botuser can't write to a root-owned SQLite path), then
# drops to UID 1000 (botuser) via `runuser` and exec's the CMD.

set -e

if [ -d /app/data ]; then
    # Only chown if not already owned by botuser — avoids touching files
    # on every restart (cheap but tidier in logs).
    owner_uid="$(stat -c '%u' /app/data)"
    if [ "$owner_uid" != "1000" ]; then
        chown -R 1000:1000 /app/data
    fi
fi

exec runuser -u botuser -- "$@"
