#!/usr/bin/env bash
# Pull latest main from git and rebuild the Docker app when the commit changes.
set -euo pipefail

REPO_DIR="${STOCK_REPO_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
BRANCH="${STOCK_GIT_BRANCH:-main}"
LOG_FILE="${REPO_DIR}/logs/pull_redeploy.log"

cd "$REPO_DIR"
mkdir -p logs exports

log() {
  printf '[%s] %s\n' "$(date -Iseconds)" "$*" | tee -a "$LOG_FILE"
}

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  log "ERROR: ${REPO_DIR} is not a git repository"
  exit 1
fi

git fetch origin "$BRANCH" >>"$LOG_FILE" 2>&1
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse "origin/${BRANCH}")

if [ "$LOCAL" = "$REMOTE" ]; then
  exit 0
fi

log "Updating ${LOCAL} -> ${REMOTE}"
git pull --ff-only origin "$BRANCH" >>"$LOG_FILE" 2>&1

APP_VERSION="$(tr -d '[:space:]' < VERSION 2>/dev/null || echo dev)"
APP_GIT_COMMIT="$(git rev-parse --short HEAD)"
APP_BUILT_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
export APP_VERSION APP_GIT_COMMIT APP_BUILT_AT

log "Rebuilding app v${APP_VERSION} (${APP_GIT_COMMIT})"
docker compose -f docker-compose.pi.yml build \
  --build-arg "APP_VERSION=${APP_VERSION}" \
  --build-arg "APP_GIT_COMMIT=${APP_GIT_COMMIT}" \
  --build-arg "APP_BUILT_AT=${APP_BUILT_AT}" >>"$LOG_FILE" 2>&1

docker compose -f docker-compose.pi.yml up -d >>"$LOG_FILE" 2>&1
log "Redeploy complete — v${APP_VERSION} (${APP_GIT_COMMIT})"
