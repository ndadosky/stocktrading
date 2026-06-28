#!/usr/bin/env bash
# Pull latest main from git and rebuild the Docker app when the commit changes.
set -euo pipefail

REPO_DIR="${STOCK_REPO_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
BRANCH="${STOCK_GIT_BRANCH:-main}"
LOG_FILE="${REPO_DIR}/logs/pull_redeploy.log"
DEPLOYED_COMMIT_FILE="${REPO_DIR}/logs/deployed_commit"

cd "$REPO_DIR"
mkdir -p logs exports

log() {
  printf '[%s] %s\n' "$(date -Iseconds)" "$*" | tee -a "$LOG_FILE"
}

run_logged() {
  "$@" 2>&1 | tee -a "$LOG_FILE"
}

on_error() {
  local rc=$?
  log "ERROR: deploy failed (exit ${rc}); the timer will retry this commit"
  exit "$rc"
}

trap on_error ERR

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  log "ERROR: ${REPO_DIR} is not a git repository"
  exit 1
fi

run_logged git fetch origin "$BRANCH"
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse "origin/${BRANCH}")

if [ "$LOCAL" != "$REMOTE" ]; then
  log "Updating ${LOCAL} -> ${REMOTE}"
  run_logged git pull --ff-only origin "$BRANCH"
fi

CURRENT=$(git rev-parse HEAD)
DEPLOYED=$(cat "$DEPLOYED_COMMIT_FILE" 2>/dev/null || true)
if [ "$CURRENT" = "$DEPLOYED" ]; then
  exit 0
fi

APP_VERSION="$(tr -d '[:space:]' < VERSION 2>/dev/null || echo dev)"
APP_GIT_COMMIT="$(git rev-parse --short HEAD)"
APP_BUILT_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
export APP_VERSION APP_GIT_COMMIT APP_BUILT_AT

log "Rebuilding app v${APP_VERSION} (${APP_GIT_COMMIT})"
run_logged "${REPO_DIR}/deploy/compose.sh" -f docker-compose.pi.yml build \
  --build-arg "APP_VERSION=${APP_VERSION}" \
  --build-arg "APP_GIT_COMMIT=${APP_GIT_COMMIT}" \
  --build-arg "APP_BUILT_AT=${APP_BUILT_AT}"

run_logged "${REPO_DIR}/deploy/compose.sh" -f docker-compose.pi.yml up -d
printf '%s\n' "$CURRENT" >"$DEPLOYED_COMMIT_FILE"
log "Redeploy complete — v${APP_VERSION} (${APP_GIT_COMMIT})"

if sudo docker ps --format '{{.Names}}' 2>/dev/null | grep -qx stock_app_1; then
  log "Refreshing dashboard from PostgreSQL"
  sudo docker exec stock_app_1 python3 /app/dashboard.py >>"$LOG_FILE" 2>&1 || log "WARN: dashboard refresh failed"
fi

if [ -x "${REPO_DIR}/deploy/install_systemd_jobs.sh" ]; then
  "${REPO_DIR}/deploy/install_systemd_jobs.sh" >>"$LOG_FILE" 2>&1 || log "WARN: systemd job install failed"
fi
