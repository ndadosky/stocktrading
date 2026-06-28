#!/usr/bin/env bash
# Install or refresh systemd job timers on the Raspberry Pi host.
set -euo pipefail

REPO_DIR="${STOCK_REPO_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
SYSTEMD_SRC="${REPO_DIR}/deploy/systemd"

if [ ! -d "$SYSTEMD_SRC" ]; then
  echo "Missing ${SYSTEMD_SRC}" >&2
  exit 1
fi

chmod +x "${REPO_DIR}/deploy/trigger_job.sh" "${REPO_DIR}/deploy/trigger_live_update.sh"

echo "Installing systemd job units from ${SYSTEMD_SRC}..."
sudo cp "${SYSTEMD_SRC}/stocktrading-job@.service" /etc/systemd/system/
sudo cp "${SYSTEMD_SRC}/stocktrading-live-update.service" /etc/systemd/system/
sudo cp "${SYSTEMD_SRC}/"*.timer /etc/systemd/system/
sudo systemctl daemon-reload

TIMERS=(
  stocktrading-job-morning.timer
  stocktrading-job-confirmation.timer
  stocktrading-job-report.timer
  stocktrading-job-strategy_review.timer
  stocktrading-job-pnl_flashcard.timer
  stocktrading-live-update.timer
)

for timer in "${TIMERS[@]}"; do
  sudo systemctl enable --now "$timer"
  echo "  enabled ${timer}"
done

echo "Systemd job timers:"
systemctl list-timers 'stocktrading-*' --no-pager || true
