#!/usr/bin/env bash
# Trigger a scheduled job via the app API (used by systemd timers on the Pi).
set -euo pipefail

JOB="${1:?job name required}"
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
  log "FAIL ${JOB}: app container is not running"
  exit 1
fi

if ! "$COMPOSE" -f "$COMPOSE_FILE" exec -T app python3 -c "
from datetime import datetime
from market_calendar import is_market_session
import sys
sys.exit(0 if is_market_session(datetime.now().astimezone().date()) else 1)
" >>"$LOG_FILE" 2>&1; then
  log "SKIP ${JOB} — not a market session"
  exit 0
fi

ready=0
for _ in 1 2 3 4 5 6; do
  if curl -sf "${BASE}/api/status" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 2
done
if [ "$ready" -ne 1 ]; then
  log "FAIL ${JOB}: app API not reachable on ${BASE}"
  exit 1
fi

response="$(curl -sf -X POST "${BASE}/api/run/${JOB}" \
  -H "Content-Type: application/json" \
  -d '{"reason":"systemd"}' 2>&1)" || {
  log "FAIL ${JOB}: ${response:-curl error}"
  exit 1
}

log "RUN ${JOB}: ${response}"
