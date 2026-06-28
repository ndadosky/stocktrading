"""Persistent stage telemetry and dependency checks for the daily pipeline."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

import pandas as pd

from market_calendar import is_market_session
from scanner_config import PIPELINE_STATE_FILE, ensure_directories
from stock_storage import read_snapshot, snapshot_count


def _load() -> dict:
    if not PIPELINE_STATE_FILE.exists():
        return {"stages": {}}
    try:
        return json.loads(PIPELINE_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"stages": {}}


def record_stage(stage: str, status: str, rows: Optional[int] = None, message: str = "") -> None:
    ensure_directories()
    state = _load()
    state.setdefault("stages", {})[stage] = {
        "status": status,
        "rows": rows,
        "message": message,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "date": datetime.now().astimezone().date().isoformat(),
    }
    PIPELINE_STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def market_gate(stage: str) -> bool:
    today = datetime.now().astimezone().date()
    if is_market_session(today):
        return True
    message = f"{today} is not a regular NYSE session"
    record_stage(stage, "SKIPPED", 0, message)
    print(message)
    return False


def require_today_snapshot(table: str, date_column: str, stage: str) -> Optional[pd.DataFrame]:
    today = datetime.now().astimezone().date().isoformat()
    frame = read_snapshot(table, date_column, today)
    if frame.empty:
        record_stage(stage, "BLOCKED", 0, f"Missing upstream PostgreSQL snapshot: {table} ({today})")
        return None
    return frame


def health_snapshot() -> dict:
    state = _load()
    today = datetime.now().astimezone().date().isoformat()
    expected = {
        "morning": ("morning_candidates", "scan_date"),
        "confirmation": ("confirmations", "confirm_date"),
        "report": ("paper_performance", "report_date"),
    }
    snapshots = {}
    for name, (table, date_column) in expected.items():
        rows = snapshot_count(table, date_column, today)
        snapshots[name] = {
            "exists": rows > 0,
            "source": "postgresql",
            "table": table,
            "date_column": date_column,
            "date": today,
            "rows": rows,
        }
    return {
        "date": today,
        "market_session": is_market_session(datetime.now().astimezone().date()),
        "stages": state.get("stages", {}),
        "snapshots": snapshots,
        "files": snapshots,
    }


def main() -> int:
    ensure_directories()
    snapshot = health_snapshot()
    from scanner_config import WATCHLIST_EXPORT_DIR

    path = WATCHLIST_EXPORT_DIR / f"system_health_{snapshot['date']}.json"
    path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    print(json.dumps(snapshot, indent=2))
    blocked = any(
        stage.get("status") in {"FAILED", "BLOCKED"}
        for stage in snapshot["stages"].values()
        if stage.get("date") == snapshot["date"]
    )
    return 1 if blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
