#!/usr/bin/env bash
# Run on the Raspberry Pi after cloning the repo.
set -euo pipefail

cd "$(dirname "$0")/.."
REPO_DIR="$(pwd)"

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env from .env.example — edit POSTGRES_PASSWORD before production use."
fi

mkdir -p exports logs
chmod +x deploy/pi_pull_redeploy.sh

if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='stock'" | grep -q 1; then
  echo "Creating PostgreSQL role and database..."
  sudo cp "${REPO_DIR}/deploy/setup_postgres.sql" /tmp/stock_setup_postgres.sql
  sudo chmod 644 /tmp/stock_setup_postgres.sql
  sudo -u postgres psql -f /tmp/stock_setup_postgres.sql
else
  echo "PostgreSQL role 'stock' already exists."
fi

export APP_VERSION="$(tr -d '[:space:]' < VERSION 2>/dev/null || echo dev)"
export APP_GIT_COMMIT="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
export APP_BUILT_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

COMPOSE="${REPO_DIR}/deploy/compose.sh"

echo "Building and starting the app container (host network, port 80)..."
"$COMPOSE" -f docker-compose.pi.yml up -d --build

echo "Installing 5-minute git pull + redeploy timer..."
sudo cp deploy/stocktrading-pull.service /etc/systemd/system/
sudo cp deploy/stocktrading-pull.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now stocktrading-pull.timer

echo "App should be available at http://$(hostname -I | awk '{print $1}')/"
echo "Auto-deploy: git push to main → Pi pulls and rebuilds within 5 minutes."
