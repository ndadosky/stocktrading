#!/bin/sh
set -eu

echo "Waiting for PostgreSQL..."
python3 -c "from db import wait_for_database; wait_for_database()"

CODEX_HOME="${CODEX_HOME:-/app/logs/codex}"
mkdir -p "$CODEX_HOME" "$CODEX_HOME/cache" "$CODEX_HOME/data" /app/logs/codex-tmp
if [ -d /run/codex-host ]; then
  for f in auth.json config.toml installation_id; do
    if [ -f "/run/codex-host/$f" ] && [ ! -f "$CODEX_HOME/$f" ]; then
      cp "/run/codex-host/$f" "$CODEX_HOME/$f"
    fi
  done
fi

echo "Starting Stock Strategy App on port ${PORT:-80}..."
exec python3 app_server.py
