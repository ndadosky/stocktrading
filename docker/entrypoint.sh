#!/bin/sh
set -eu

echo "Waiting for PostgreSQL..."
python3 -c "from db import wait_for_database; wait_for_database()"

echo "Starting Stock Strategy App on port ${PORT:-80}..."
exec python3 app_server.py
