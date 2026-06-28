#!/usr/bin/env bash
# Run on the Raspberry Pi after cloning the repo.
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env from .env.example — edit POSTGRES_PASSWORD before production use."
fi

mkdir -p exports logs
chmod +x deploy/pi_pull_redeploy.sh

if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='stock'" | grep -q 1; then
  echo "Creating PostgreSQL role and database..."
  sudo -u postgres psql -f deploy/setup_postgres.sql
else
  echo "PostgreSQL role 'stock' already exists."
fi

export APP_VERSION="$(tr -d '[:space:]' < VERSION 2>/dev/null || echo dev)"
export APP_GIT_COMMIT="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
export APP_BUILT_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

echo "Building and starting the app container (host network, port 80)..."
docker compose -f docker-compose.pi.yml up -d --build

echo "Installing 5-minute git pull + redeploy timer..."
sudo cp deploy/stocktrading-pull.service /etc/systemd/system/
sudo cp deploy/stocktrading-pull.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now stocktrading-pull.timer

echo "App should be available at http://$(hostname -I | awk '{print $1}')/"
echo "Auto-deploy: git push to main → Pi pulls and rebuilds within 5 minutes."
