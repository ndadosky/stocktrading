"""Persistent stage telemetry and dependency checks for the daily pipeline."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

from market_calendar import is_market_session
from scanner_config import PIPELINE_STATE_FILE, WATCHLIST_EXPORT_DIR, ensure_directories


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
        "status": status, "rows": rows, "message": message,
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


def require_today_csv(path: Path, stage: str) -> Optional[pd.DataFrame]:
    today = datetime.now().astimezone().date().isoformat()
    if not path.exists():
        record_stage(stage, "BLOCKED", 0, f"Missing upstream file: {path.name}")
        return None
    try:
        frame = pd.read_csv(path)
    except Exception as exc:
        record_stage(stage, "BLOCKED", 0, f"Unreadable {path.name}: {exc}")
        return None
    file_date = datetime.fromtimestamp(path.stat().st_mtime).astimezone().date().isoformat()
    if file_date != today:
        record_stage(stage, "BLOCKED", len(frame), f"Stale upstream file: {path.name} ({file_date})")
        return None
    return frame


def health_snapshot() -> dict:
    state = _load()
    today = datetime.now().astimezone().date().isoformat()
    expected = {
        "morning": WATCHLIST_EXPORT_DIR / f"morning_candidates_{today}.csv",
        "confirmation": WATCHLIST_EXPORT_DIR / f"confirm_945_{today}.csv",
        "report": WATCHLIST_EXPORT_DIR / f"paper_performance_{today}.csv",
    }
    files = {}
    for name, path in expected.items():
        files[name] = {"exists": path.exists(), "path": str(path)}
        if path.exists():
            try:
                files[name]["rows"] = len(pd.read_csv(path))
            except Exception as exc:
                files[name]["error"] = str(exc)
    return {"date": today, "market_session": is_market_session(datetime.now().astimezone().date()), "stages": state.get("stages", {}), "files": files}


def main() -> int:
    ensure_directories()
    snapshot = health_snapshot()
    path = WATCHLIST_EXPORT_DIR / f"system_health_{snapshot['date']}.json"
    path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    print(json.dumps(snapshot, indent=2))
    blocked = any(stage.get("status") in {"FAILED", "BLOCKED"} for stage in snapshot["stages"].values() if stage.get("date") == snapshot["date"])
    return 1 if blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
