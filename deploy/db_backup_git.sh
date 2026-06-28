#!/usr/bin/env bash
# Nightly PostgreSQL dump committed and pushed to git (backups/).
set -euo pipefail

REPO_DIR="${STOCK_REPO_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
BACKUP_DIR="${REPO_DIR}/backups"
LOG_FILE="${REPO_DIR}/logs/db_backup.log"
KEEP_DAYS="${STOCK_BACKUP_KEEP_DAYS:-14}"

POSTGRES_USER="${POSTGRES_USER:-stock}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-stockpass}"
POSTGRES_HOST="${POSTGRES_HOST:-127.0.0.1}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"
POSTGRES_DB="${POSTGRES_DB:-stocktrading}"

mkdir -p "$BACKUP_DIR" "$(dirname "$LOG_FILE")"
DATE_UTC="$(date -u +%Y-%m-%d)"
STAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
FILE="${BACKUP_DIR}/stocktrading_${DATE_UTC}.sql.gz"

log() {
  echo "[${STAMP}] $*" | tee -a "$LOG_FILE"
}

if ! command -v pg_dump >/dev/null 2>&1; then
  log "ERROR: pg_dump not found on PATH"
  exit 1
fi

export PGPASSWORD="$POSTGRES_PASSWORD"
log "Starting pg_dump → ${FILE}"
pg_dump \
  -h "$POSTGRES_HOST" \
  -p "$POSTGRES_PORT" \
  -U "$POSTGRES_USER" \
  -d "$POSTGRES_DB" \
  --no-owner \
  --no-privileges \
  | gzip -9 > "$FILE"
unset PGPASSWORD

log "Dump size: $(du -h "$FILE" | awk '{print $1}')"

find "$BACKUP_DIR" -name 'stocktrading_*.sql.gz' -mtime +"$KEEP_DAYS" -delete 2>/dev/null || true

cd "$REPO_DIR"
git add "$FILE"
if git diff --staged --quiet; then
  log "No backup changes to commit"
  exit 0
fi

git -c user.name="stock backup" -c user.email="backup@local" commit -m "Nightly PostgreSQL backup ${DATE_UTC}"
if git push origin HEAD; then
  log "Backup committed and pushed"
else
  log "WARNING: commit succeeded but git push failed — check Pi credentials"
  exit 1
fi
