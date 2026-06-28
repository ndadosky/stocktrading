#!/usr/bin/env bash
# Refresh report or dashboard during market hours (systemd timer, every 5 minutes).
set -euo pipefail

REPO_DIR="${STOCK_REPO_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
COMPOSE="${REPO_DIR}/deploy/compose.sh"
COMPOSE_FILE="${STOCK_COMPOSE_FILE:-docker-compose.pi.yml}"
PORT="${STOCK_APP_PORT:-80}"
BASE="http://127.0.0.1:${PORT}"
LOG_FILE="${REPO_DIR}/logs/systemd_jobs.log"

log() {
  printf '[%s] %s\n' "$(date -Iseconds)" "$*" | tee -a "$LOG_FILE"
}

mkdir -p "${REPO_DIR}/logs"

if ! "$COMPOSE" -f "$COMPOSE_FILE" ps -q app 2>/dev/null | grep -q .; then
  log "SKIP live-update — app container is not running"
  exit 0
fi

if ! curl -sf "${BASE}/api/status" >/dev/null 2>&1; then
  log "SKIP live-update — app API not reachable"
  exit 0
fi

response="$(curl -sf -X POST "${BASE}/api/run/live" \
  -H "Content-Type: application/json" \
  -d '{}' 2>&1)" || {
  log "FAIL live-update: ${response:-curl error}"
  exit 1
}

log "RUN live-update: ${response}"
