"""Local web app and scheduler for the stock paper-trading workflow."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, time as day_time, timedelta
from html import escape
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, unquote, urlparse

from market_calendar import is_market_session
from db import database_url, init_schema, wait_for_database
from job_storage import (
    job_history,
    job_rows_for_date,
    last_run,
    last_runs,
    record_job_run,
    scheduled_already_ran,
)
from scanner_config import (
    CANDIDATES_FILE,
    DASHBOARD_FILE,
    FIRST_TARGET_GAIN_PCT,
    PIPELINE_STATE_FILE,
    RUNNER_EXIT_SESSIONS,
    SECOND_TARGET_GAIN_PCT,
    WATCHLIST_EXPORT_DIR,
    ensure_directories,
)
from stock_storage import add_bankroll_deposit, bankroll_base, query_rows, snapshot_count, table_count, total_bankroll_deposits
from platform_health import codex_chat, platform_health_payload
from nav_html import finalize_page_html
from strategy_review import current_data_suggestions, load_latest_review_rows, render_strategy_review_html
from best_case_analysis import compute_best_case
from strategy_optimizer import load_active_settings
from morning_candidates import build_morning_candidates, preview_rows
from home_controls import (
    home_assistant_door_control,
    home_assistant_pool_control,
    home_assistant_switch,
    home_controls_payload,
    poolsync_control,
)
from enphase import begin_authorization, complete_authorization
from version import version_label


PROJECT_DIR = Path(__file__).resolve().parent
LOGS_DIR = PROJECT_DIR / "logs"
SERVER_LOG = LOGS_DIR / "app_server.log"
PYTHON = os.getenv("PYTHON", "python3")
IMAGE_PYTHON = os.getenv("STOCK_IMAGE_PYTHON", PYTHON)
LIVE_UPDATE_SECONDS = int(os.getenv("STOCK_LIVE_UPDATE_SECONDS", "300"))
POLL_SECONDS = int(os.getenv("STOCK_CRON_POLL_SECONDS", "30"))
JOB_TIMEOUT_SECONDS = int(os.getenv("STOCK_JOB_TIMEOUT_SECONDS", "1800"))
SCHEDULER_MODE = os.getenv("STOCK_SCHEDULER", "internal").strip().lower()
AUTOMATED_DEDUP_REASONS = frozenset({"scheduled", "systemd"})


@dataclass(frozen=True)
class Job:
    name: str
    command: list[str]
    schedule_time: Optional[day_time] = None
    description: str = ""


JOBS: dict[str, Job] = {
    "morning": Job("morning", [PYTHON, "morning_candidates.py"], day_time(8, 45), "Fetch and score the Finviz universe."),
    "confirmation": Job("confirmation", [PYTHON, "confirm_945.py"], day_time(9, 50), "Run 9:45 confirmation and live quote checks."),
    "report": Job("report", [PYTHON, "daily_report.py"], day_time(10, 7), "Update paper trades, analytics, and dashboard."),
    "strategy_review": Job("strategy_review", [PYTHON, "strategy_review.py"], day_time(10, 30), "Review database and strategy evidence for safe improvements."),
    "pnl_flashcard": Job("pnl_flashcard", [IMAGE_PYTHON, "pnl_flashcard.py"], day_time(16, 15), "Generate the post-market P/L flash-card infographic."),
    "health": Job("health", [PYTHON, "system_health.py"], None, "Write the latest pipeline health snapshot."),
    "dashboard": Job("dashboard", [PYTHON, "dashboard.py"], None, "Rebuild the dashboard from local outputs."),
}


class AppState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running: Optional[str] = None
        self.scheduler_started_at = datetime.now().astimezone().isoformat(timespec="seconds")
        self.live_updates = os.getenv("STOCK_DISABLE_LIVE_UPDATES", "").lower() not in {"1", "true", "yes"}
        self.scanner_preview_running = False
        self.scanner_preview_result: Optional[dict] = None
        self.scanner_preview_error: Optional[str] = None


STATE = AppState()


def append_log(message: str) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    with SERVER_LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"[{stamp}] {message}\n")


def today_key() -> str:
    return datetime.now().astimezone().date().isoformat()


def market_open_now() -> bool:
    now = datetime.now().astimezone()
    return is_market_session(now.date()) and day_time(9, 30) <= now.time() <= day_time(16, 5)


def confirmation_ready() -> bool:
    from stock_storage import snapshot_count

    return snapshot_count("confirmations", "confirm_date", today_key()) > 0


def run_job(name: str, reason: str = "manual") -> dict:
    if name not in JOBS:
        return {"ok": False, "error": f"Unknown job: {name}"}
    job = JOBS[name]
    if job.schedule_time and reason in AUTOMATED_DEDUP_REASONS:
        today = today_key()
        if not is_market_session(datetime.now().astimezone().date()):
            return {"ok": True, "skipped": True, "reason": reason, "detail": "not a market session"}
        if scheduled_already_ran(name, today):
            return {"ok": True, "skipped": True, "reason": reason, "detail": "already ran today"}
    with STATE.lock:
        if STATE.running:
            return {"ok": False, "error": f"Job already running: {STATE.running}"}
        STATE.running = name
    started = datetime.now().astimezone()
    append_log(f"START {name} ({reason}): {' '.join(job.command)}")
    try:
        completed = subprocess.run(
            job.command,
            cwd=PROJECT_DIR,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=JOB_TIMEOUT_SECONDS,
        )
        output = completed.stdout[-12000:] if completed.stdout else ""
        result = {
            "ok": completed.returncode == 0,
            "returncode": completed.returncode,
            "started_at": started.isoformat(timespec="seconds"),
            "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "reason": reason,
            "run_date": today_key(),
            "output_tail": output,
        }
        if reason in AUTOMATED_DEDUP_REASONS:
            result["scheduled_for"] = today_key()
        append_log(f"END {name}: rc={completed.returncode}\n{output}")
    except subprocess.TimeoutExpired as exc:
        result = {
            "ok": False,
            "returncode": None,
            "started_at": started.isoformat(timespec="seconds"),
            "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "reason": reason,
            "run_date": today_key(),
            "output_tail": f"Timed out after {JOB_TIMEOUT_SECONDS}s\n{exc.stdout or ''}",
        }
        if reason in AUTOMATED_DEDUP_REASONS:
            result["scheduled_for"] = today_key()
        append_log(f"TIMEOUT {name} after {JOB_TIMEOUT_SECONDS}s")
    except Exception as exc:
        result = {
            "ok": False,
            "returncode": None,
            "started_at": started.isoformat(timespec="seconds"),
            "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "reason": reason,
            "run_date": today_key(),
            "output_tail": str(exc),
        }
        if reason in AUTOMATED_DEDUP_REASONS:
            result["scheduled_for"] = today_key()
        append_log(f"ERROR {name}: {exc}")
    with STATE.lock:
        result = record_job_run(name, result)
        STATE.running = None
    return result


def run_live_update(reason: str = "live-update") -> dict:
    if not STATE.live_updates:
        return {"ok": True, "skipped": True, "reason": reason, "detail": "live updates disabled"}
    if not is_market_session(datetime.now().astimezone().date()):
        return {"ok": True, "skipped": True, "reason": reason, "detail": "not a market session"}
    if not market_open_now():
        return {"ok": True, "skipped": True, "reason": reason, "detail": "outside market hours"}
    with STATE.lock:
        if STATE.running:
            return {"ok": False, "error": f"Job already running: {STATE.running}"}
    if confirmation_ready():
        return run_job("report", reason)
    return run_job("dashboard", reason)


def next_run_for(job: Job) -> Optional[str]:
    if job.schedule_time is None:
        return None
    now = datetime.now().astimezone()
    candidate = datetime.combine(now.date(), job.schedule_time, tzinfo=now.tzinfo)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate.isoformat(timespec="seconds")


def scheduler_loop() -> None:
    """In-process scheduler (used when STOCK_SCHEDULER=internal, e.g. local dev)."""
    append_log("Scheduler loop started (internal mode)")
    last_live = 0.0
    while True:
        now = datetime.now().astimezone()
        run_date = now.date().isoformat()
        if is_market_session(now.date()):
            for name, job in JOBS.items():
                if job.schedule_time is None:
                    continue
                due = now.time() >= job.schedule_time
                if due and not scheduled_already_ran(name, run_date):
                    run_job(name, "scheduled")
        if STATE.live_updates and market_open_now() and time.time() - last_live >= LIVE_UPDATE_SECONDS:
            last_live = time.time()
            if confirmation_ready():
                run_job("report", "live-update")
            else:
                run_job("dashboard", "live-dashboard-refresh")
        time.sleep(POLL_SECONDS)


def newest_path(pattern: str) -> Optional[Path]:
    files = sorted(WATCHLIST_EXPORT_DIR.glob(pattern))
    return files[-1] if files else None


def file_info(path: Path) -> dict:
    exists = path.exists()
    info = {"exists": exists, "path": str(path)}
    if exists:
        stat = path.stat()
        info.update({"bytes": stat.st_size, "modified_at": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds")})
    return info


def pipeline_state() -> dict:
    try:
        return json.loads(PIPELINE_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"stages": {}}


def status_payload() -> dict:
    today = today_key()
    files = {
        "dashboard": file_info(DASHBOARD_FILE),
        "database_url": database_url().split("@")[-1],
        "morning_candidates": {
            "exists": snapshot_count("morning_candidates", "scan_date", today) > 0,
            "rows": snapshot_count("morning_candidates", "scan_date", today),
            "source": "postgresql",
        },
        "confirmation": {
            "exists": snapshot_count("confirmations", "confirm_date", today) > 0,
            "rows": snapshot_count("confirmations", "confirm_date", today),
            "source": "postgresql",
        },
        "paper_performance": {
            "exists": snapshot_count("paper_performance", "report_date", today) > 0,
            "rows": snapshot_count("paper_performance", "report_date", today),
            "source": "postgresql",
        },
        "latest_optimizer_review": file_info(newest_path("strategy_optimizer_review_*.html") or WATCHLIST_EXPORT_DIR / "strategy_optimizer_review_missing.html"),
        "latest_strategy_review": file_info(newest_path("strategy_review_*.html") or WATCHLIST_EXPORT_DIR / "strategy_review_missing.html"),
    }
    runs = last_runs(list(JOBS.keys()))
    with STATE.lock:
        running = STATE.running
    return {
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "market_session": is_market_session(datetime.now().astimezone().date()),
        "market_open_now": market_open_now(),
        "live_updates": STATE.live_updates,
        "scheduler_mode": SCHEDULER_MODE,
        "bankroll": {
            "base": bankroll_base(),
            "deposits": total_bankroll_deposits(),
            "injection_amount": 25000.0,
        },
        "running": running,
        "jobs": {
            name: {
                "description": job.description,
                "schedule_time": job.schedule_time.isoformat(timespec="minutes") if job.schedule_time else None,
                "next_run": next_run_for(job),
                "last_run": runs.get(name),
            }
            for name, job in JOBS.items()
        },
        "pipeline": pipeline_state(),
        "files": files,
    }


def jobs_payload() -> dict:
    status = status_payload()
    return {
        "server_time": status["server_time"],
        "running": status["running"],
        "market_session": status["market_session"],
        "market_open_now": status["market_open_now"],
        "live_updates": status["live_updates"],
        "database_url": database_url().split("@")[-1],
        "jobs": status["jobs"],
        "history": job_history(80),
    }


def health_payload() -> dict:
    status = status_payload()
    app_state = {
        "scheduler_started_at": STATE.scheduler_started_at,
        "live_updates": STATE.live_updates,
        "running": status["running"],
    }
    return platform_health_payload(app_state, status["jobs"])


def strategy_review_page_html() -> bytes:
    rows, review_date, csv_name = load_latest_review_rows()
    return finalize_page_html(render_strategy_review_html(rows, review_date, csv_name), "/strategy-review")


def architecture_svg(payload: dict) -> str:
    """Simple topology diagram for the health page."""
    healthy = payload.get("ok", False)
    accent = "#16803c" if healthy else "#b42318"
    return f"""
<svg viewBox="0 0 920 320" xmlns="http://www.w3.org/2000/svg" style="width:100%;max-width:920px;display:block">
  <defs>
    <marker id="arrow" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto">
      <path d="M0,0 L6,3 L0,6 Z" fill="#94a3b8"/>
    </marker>
  </defs>
  <rect x="20" y="120" width="130" height="56" rx="10" fill="#eff6ff" stroke="#93c5fd"/>
  <text x="85" y="145" text-anchor="middle" font-size="12" font-weight="700" fill="#1e3a8a">Your PC</text>
  <text x="85" y="162" text-anchor="middle" font-size="10" fill="#64748b">Browser</text>

  <rect x="190" y="120" width="150" height="56" rx="10" fill="#f8fafc" stroke="#cbd5e1"/>
  <text x="265" y="145" text-anchor="middle" font-size="12" font-weight="700" fill="#0f172a">Raspberry Pi</text>
  <text x="265" y="162" text-anchor="middle" font-size="10" fill="#64748b">:80 host network</text>

  <rect x="380" y="70" width="160" height="56" rx="10" fill="#ecfdf5" stroke="#86efac"/>
  <text x="460" y="95" text-anchor="middle" font-size="12" font-weight="700" fill="#14532d">Docker app</text>
  <text x="460" y="112" text-anchor="middle" font-size="10" fill="#64748b">app_server.py</text>

  <rect x="380" y="170" width="160" height="56" rx="10" fill="#fff7ed" stroke="#fdba74"/>
  <text x="460" y="195" text-anchor="middle" font-size="12" font-weight="700" fill="#9a3412">PostgreSQL</text>
  <text x="460" y="212" text-anchor="middle" font-size="10" fill="#64748b">127.0.0.1</text>

  <rect x="590" y="20" width="140" height="52" rx="10" fill="#fdf4ff" stroke="#e9d5ff"/>
  <text x="660" y="42" text-anchor="middle" font-size="12" font-weight="700" fill="#6b21a8">Codex CLI</text>
  <text x="660" y="58" text-anchor="middle" font-size="10" fill="#64748b">analysis · chat</text>

  <rect x="590" y="90" width="140" height="52" rx="10" fill="#f0fdf4" stroke="#bbf7d0"/>
  <text x="660" y="112" text-anchor="middle" font-size="12" font-weight="700" fill="#166534">GitHub</text>
  <text x="660" y="128" text-anchor="middle" font-size="10" fill="#64748b">main branch</text>

  <rect x="590" y="160" width="140" height="52" rx="10" fill="#fef2f2" stroke="#fecaca"/>
  <text x="660" y="182" text-anchor="middle" font-size="12" font-weight="700" fill="#991b1b">Finviz/Yahoo</text>
  <text x="660" y="198" text-anchor="middle" font-size="10" fill="#64748b">market data</text>

  <rect x="590" y="230" width="140" height="52" rx="10" fill="#f8fafc" stroke="#cbd5e1"/>
  <text x="660" y="252" text-anchor="middle" font-size="12" font-weight="700" fill="#0f172a">exports/</text>
  <text x="660" y="268" text-anchor="middle" font-size="10" fill="#64748b">CSV · HTML</text>

  <rect x="770" y="120" width="130" height="56" rx="10" fill="#eff6ff" stroke="#93c5fd"/>
  <text x="835" y="145" text-anchor="middle" font-size="12" font-weight="700" fill="#1e3a8a">systemd</text>
  <text x="835" y="162" text-anchor="middle" font-size="10" fill="#64748b">5m git pull</text>

  <line x1="150" y1="148" x2="190" y2="148" stroke="#94a3b8" marker-end="url(#arrow)"/>
  <line x1="340" y1="148" x2="380" y2="98" stroke="#94a3b8" marker-end="url(#arrow)"/>
  <line x1="460" y1="126" x2="460" y2="170" stroke="#94a3b8" marker-end="url(#arrow)"/>
  <line x1="540" y1="98" x2="590" y2="46" stroke="#94a3b8" marker-end="url(#arrow)"/>
  <line x1="540" y1="110" x2="590" y2="116" stroke="#94a3b8" marker-end="url(#arrow)"/>
  <line x1="540" y1="120" x2="590" y2="186" stroke="#94a3b8" marker-end="url(#arrow)"/>
  <line x1="460" y1="226" x2="590" y2="256" stroke="#94a3b8" marker-end="url(#arrow)"/>
  <line x1="730" y1="116" x2="770" y2="148" stroke="#94a3b8" marker-end="url(#arrow)"/>
  <line x1="835" y1="176" x2="835" y2="220" stroke="#94a3b8" marker-end="url(#arrow)"/>
  <line x1="835" y1="220" x2="730" y2="130" stroke="#94a3b8" marker-end="url(#arrow)" stroke-dasharray="4,4"/>

  <circle cx="460" cy="98" r="6" fill="{accent}"/>
  <text x="20" y="24" font-size="11" fill="#64748b">Overall: {'healthy' if healthy else 'degraded'} · {escape(payload.get('version', ''))}</text>
</svg>"""


def component_cards(components: dict) -> str:
    rows = []
    for name, info in components.items():
        ok = info.get("ok", False)
        cls = "ok" if ok else "bad"
        badge = "HEALTHY" if ok else "UNHEALTHY"
        detail = escape(str(info.get("detail", "")))
        rows.append(
            f"<div class='card {cls}'><div class='card-top'><strong>{escape(name.replace('_', ' '))}</strong>"
            f"<span class='badge {cls}'>{badge}</span></div><p>{detail}</p></div>"
        )
    return "".join(rows)


def scheduled_jobs_table(jobs: dict, running: Optional[str]) -> str:
    rows = []
    for name, job in jobs.items():
        schedule = job.get("schedule_time") or "manual"
        last = job.get("last_run") or {}
        last_status = "OK" if last.get("ok") else ("Failed" if last else "Not run")
        status_cls = "ok" if last.get("ok") else ("bad" if last else "muted")
        next_run = job.get("next_run") or "—"
        is_running = running == name
        rows.append(
            f"<tr><td><b>{escape(name.replace('_', ' '))}</b>"
            f"{' <span class=run-pill>RUNNING</span>' if is_running else ''}"
            f"<div class='sub'>{escape(job.get('description', ''))}</div></td>"
            f"<td>{escape(str(schedule))}</td>"
            f"<td>{escape(str(next_run))}</td>"
            f"<td class='{status_cls}'>{escape(last_status)}</td></tr>"
        )
    return (
        "<table><thead><tr><th>Job</th><th>Schedule (ET)</th><th>Next run</th><th>Last status</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def postgres_data_panel(payload: dict) -> str:
    data = payload.get("components", {}).get("postgres_data", {})
    db = payload.get("database", {})
    ledger = payload.get("ledger", {})
    warnings = data.get("warnings") or []
    source = data.get("active_source", "unknown")
    source_ok = source == "postgresql"
    cls = "ok" if data.get("ok") else "bad"
    rows = [
        ("Database engine", db.get("engine", "postgresql")),
        ("Connection", escape(str(db.get("target", "—")))),
        ("Active data source", escape(source.replace("_", " "))),
        ("Paper trades (PostgreSQL)", str(data.get("postgres_trades", ledger.get("paper_trades", 0)))),
        ("Job runs (PostgreSQL)", str(data.get("postgres_job_runs", ledger.get("job_runs", 0)))),
        ("Account events", str(data.get("postgres_account_events", 0))),
        ("Strategy reviews", str(data.get("postgres_strategy_reviews", 0))),
        ("Morning candidates (PG)", str(data.get("postgres_morning_candidates", 0))),
        ("Confirmations (PG)", str(data.get("postgres_confirmations", 0))),
        ("Bankroll base", f"${data.get('bankroll_base', 0):,.0f}"),
        ("Deposits", f"${data.get('bankroll_deposits', 0):,.0f}"),
    ]
    table_rows = "".join(f"<tr><th>{escape(str(label))}</th><td>{value}</td></tr>" for label, value in rows)
    warn_html = ""
    if warnings:
        items = "".join(f"<li>{escape(str(w))}</li>" for w in warnings)
        warn_html = f"<ul class='warn-list'>{items}</ul>"
    return f"""
<section class="panel">
  <h2>PostgreSQL data source</h2>
  <p class="sub">All live trading data lives in PostgreSQL. HTML files under exports/ are read-only views.</p>
  <div class="source-banner {cls}"><strong>{'Using PostgreSQL' if source_ok else 'PostgreSQL check failed'}</strong> — {escape(str(data.get('detail', '')))}</div>
  <table class="ledger-table">{table_rows}</table>
  {warn_html}
</section>"""


def healthcheck_html() -> bytes:
    payload = health_payload()
    overall = "healthy" if payload["ok"] else "degraded"
    overall_cls = "ok" if payload["ok"] else "bad"
    html = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Health check</title><style>
:root{{--bg:#f4f6f8;--panel:#fff;--text:#17202a;--muted:#687386;--line:#dce3ec;--blue:#1d4ed8;--green:#16803c;--red:#b42318}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:14px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
header{{padding:0 20px;background:var(--panel);border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;gap:16px;position:sticky;top:0;z-index:3;height:52px}}
.hdr-left h1{{font-size:15px;font-weight:700;margin:0}}
.hdr-nav{{display:flex;align-items:center;gap:2px;flex:1;padding:0 8px}}
.hdr-nav a{{padding:6px 13px;border-radius:7px;font-size:13px;font-weight:600;color:var(--muted);text-decoration:none}}
.hdr-nav a:hover{{background:#f1f3f5;color:var(--text)}}.hdr-nav a.active{{background:#eff6ff;color:var(--blue)}}
.hdr-right{{display:flex;gap:10px;align-items:center;color:var(--muted);font-size:12px}}
.badge-top{{border-radius:999px;padding:5px 12px;font-size:12px;font-weight:700}}
.badge-top.ok{{background:#f0fdf4;color:var(--green);border:1px solid #86efac}}
.badge-top.bad{{background:#fef2f2;color:var(--red);border:1px solid #fca5a5}}
main{{max-width:1200px;margin:0 auto;padding:20px 20px 64px;display:grid;gap:16px}}
.panel{{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px 20px;box-shadow:0 1px 6px rgba(15,23,42,.04)}}
.panel h2{{margin:0 0 6px;font-size:16px}}.sub{{color:var(--muted);font-size:12px;margin:0 0 14px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}}
.card{{border:1px solid var(--line);border-radius:10px;padding:14px;background:#fbfcfe}}
.card.ok{{border-color:#bbf7d0;background:#f8fff9}}.card.bad{{border-color:#fecaca;background:#fffafa}}
.card-top{{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:8px}}
.card p{{margin:0;color:var(--muted);font-size:12px;line-height:1.45;word-break:break-word}}
.badge{{font-size:10px;font-weight:800;letter-spacing:.04em;padding:3px 8px;border-radius:999px}}
.badge.ok{{background:#dcfce7;color:var(--green)}}.badge.bad{{background:#fee2e2;color:var(--red)}}
.source-banner{{border-radius:10px;padding:12px 14px;margin-bottom:14px;font-size:13px;border:1px solid var(--line)}}
.source-banner.ok{{background:#f0fdf4;border-color:#86efac;color:#14532d}}
.source-banner.bad{{background:#fef2f2;border-color:#fca5a5;color:#991b1b}}
.ledger-table{{width:100%;border-collapse:collapse;font-size:13px;margin-top:4px}}
.ledger-table th{{width:220px;color:var(--muted);font-weight:600;text-transform:none;letter-spacing:0}}
.warn-list{{margin:12px 0 0;padding-left:18px;color:#92400e;font-size:13px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th,td{{padding:10px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}}
th{{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.04em}}
.ok{{color:var(--green);font-weight:700}}.bad{{color:var(--red);font-weight:700}}.muted{{color:var(--muted)}}
.sub{{font-size:12px;color:var(--muted);margin-top:4px}}
.run-pill{{display:inline-block;background:#eff6ff;color:#1d4ed8;border:1px solid #93c5fd;border-radius:999px;padding:2px 8px;font-size:10px;font-weight:700;margin-left:6px}}
.codex-box{{display:grid;gap:10px}}
textarea{{width:100%;min-height:96px;border:1px solid var(--line);border-radius:10px;padding:12px;font:13px inherit;resize:vertical}}
button{{border:1px solid #bfcee3;background:#f8fbff;color:#183b75;border-radius:8px;padding:10px 14px;font-weight:700;cursor:pointer;width:fit-content}}
button:disabled{{opacity:.55;cursor:not-allowed}}
#codex-output{{white-space:pre-wrap;background:#0f172a;color:#e2e8f0;border-radius:10px;padding:14px;font:12px ui-monospace,Menlo,monospace;min-height:80px}}
.legend{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:8px;margin-top:12px;font-size:12px;color:var(--muted)}}
.legend div{{border:1px dashed var(--line);border-radius:8px;padding:8px 10px;background:#fff}}
a{{color:var(--blue)}}
</style></head><body>
<header>
  <div class="hdr-left"><h1>Health check</h1></div>
  <nav class="hdr-nav">__HEADER_NAV__</nav>
  <div class="hdr-right">
    <span class="badge-top {overall_cls}">{overall.upper()}</span>
    <span id="clock">{escape(payload['generated_at'])}</span>
  </div>
</header>
<main>
  <section class="panel">
    <h2>Network architecture</h2>
    <p class="sub">Pi deployment: Docker app on port 80, host PostgreSQL, systemd jobs, nightly DB backup to git.</p>
    {architecture_svg(payload)}
    <div class="legend">
      <div><b>Deploy loop</b> — systemd timer → git pull → docker rebuild</div>
      <div><b>Trading loop</b> — systemd timers on the Pi host trigger jobs via the app API</div>
      <div><b>Codex</b> — P/L flashcards and optional chat probe below</div>
      <div><b>Backups</b> — nightly pg_dump committed to backups/ in git</div>
      <div><b>JSON API</b> — <a href="/api/healthcheck">/api/healthcheck</a></div>
    </div>
  </section>
  {postgres_data_panel(payload)}
  <section class="panel">
    <h2>Component health</h2>
    <p class="sub">Live probes from this app instance.</p>
    <div class="grid">{component_cards(payload['components'])}</div>
  </section>
  <section class="panel">
    <h2>Scheduled tasks</h2>
    <p class="sub">Market-day ET schedule via systemd timers on the Pi (POST /api/run). Manual runs available from the Jobs page.</p>
    {scheduled_jobs_table(payload['scheduled_jobs'], payload['components']['scheduler'].get('running_job'))}
  </section>
  <section class="panel">
    <h2>Codex probe</h2>
    <p class="sub">Ask about trades or performance — each prompt includes live PostgreSQL ledger and report snapshots. Uses <code>codex exec --sandbox read-only</code>.</p>
    <div class="codex-box">
      <textarea id="codex-input" placeholder="Example: Summarize yesterday's trades and performance."></textarea>
      <button id="codex-send">Send to Codex</button>
      <div id="codex-output">Enter a prompt above and click Send to Codex.</div>
    </div>
  </section>
</main>
<script>
document.getElementById('codex-send').onclick=async()=>{{
  const btn=document.getElementById('codex-send');
  const out=document.getElementById('codex-output');
  const input=document.getElementById('codex-input');
  const message=input.value.trim();
  if(!message){{out.textContent='Enter a message first.';return;}}
  btn.disabled=true;input.disabled=true;out.textContent='Running Codex… (may take up to 2 minutes)';
  try{{
    const res=await fetch('/api/codex/chat',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{message}})}});
    const data=await res.json();
    if(data.ok){{
      out.textContent=(data.response||'(empty response)')+(data.warnings&&data.warnings.length?'\\n\\nWarnings:\\n'+data.warnings.join('\\n'):'')+'\\n\\n['+data.duration_ms+' ms'+(data.context_chars?', '+data.context_chars+' chars DB context':'')+']';
    }}else{{
      out.textContent='Error: '+(data.error||'unknown')+(data.warnings&&data.warnings.length?'\\n\\nWarnings:\\n'+data.warnings.join('\\n'):'');
    }}
  }}catch(err){{out.textContent='Request failed: '+err;}}
  btn.disabled=false;input.disabled=false;
}};
</script>
</body></html>"""
    return finalize_page_html(html, "/healthcheck")


def normalized_date(value: str | None) -> str:
    if not value:
        return today_key()
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    except ValueError:
        return today_key()


def trade_exit_activity(rows: list[dict]) -> list[dict]:
    """Expand recorded trade sale buckets into readable Day-page activity rows."""
    buckets = (
        ("shares_sold_10", f"Scale +{FIRST_TARGET_GAIN_PCT:g}%", "target_10", "target_10_hit_at"),
        ("shares_sold_20", f"Scale +{SECOND_TARGET_GAIN_PCT:g}%", "target_20", "target_20_hit_at"),
        ("shares_sold_30", "Legacy final target", "target_30", "target_30_hit_at"),
        ("shares_sold_protect", "Protective exit", "active_stop", "exit_datetime"),
        ("shares_sold_stop", "Stop exit", "stop_8", "exit_datetime"),
        ("shares_sold_time", f"Runner/time exit ({RUNNER_EXIT_SESSIONS} sessions)", "exit_price", "exit_datetime"),
    )
    activity: list[dict] = []
    for row in rows:
        for shares_column, event, reference_column, time_column in buckets:
            try:
                shares_sold = float(row.get(shares_column) or 0)
            except (TypeError, ValueError):
                shares_sold = 0
            if shares_sold <= 0:
                continue
            activity.append(
                {
                    "ticker": row.get("ticker"),
                    "event": event,
                    "shares sold": int(shares_sold) if shares_sold.is_integer() else shares_sold,
                    "reference price": row.get(reference_column),
                    "remaining shares": row.get("remaining_shares"),
                    "total realized P/L": row.get("realized_p_l"),
                    "recorded at": row.get(time_column) or row.get("exit_datetime"),
                    "position": "Open remainder" if float(row.get("remaining_shares") or 0) > 0 else "Resolved",
                }
            )
    return sorted(activity, key=lambda row: (str(row.get("recorded at") or ""), str(row.get("ticker") or "")), reverse=True)


def morning_purchase_summary(rows: list[dict], target_date: str) -> dict:
    """Summarize new paper positions entered on the selected trading date."""
    purchases = [
        row for row in rows
        if str(row.get("trade_date") or "")[:10] == target_date
    ]
    tickers = [str(row.get("ticker") or "").upper() for row in purchases if row.get("ticker")]
    count = len(purchases)
    noun = "stock" if count == 1 else "stocks"
    message = f"{count} {noun} bought this morning"
    if tickers:
        message += f": {', '.join(tickers)}"
    return {"count": count, "tickers": tickers, "message": message}


def day_payload(target_date: str) -> dict:
    target_date = normalized_date(target_date)
    paper_performance = query_rows("paper_performance", "WHERE report_date = ?", (target_date,), 500)
    score_band = query_rows("score_band_performance", "WHERE report_date = ?", (target_date,), 100)
    components = query_rows("component_performance", "WHERE report_date = ?", (target_date,), 100)
    strategy_reviews = query_rows(
        "strategy_reviews",
        "WHERE stored_review_date = ? OR review_date = ?",
        (target_date, target_date),
        200,
    )
    paper_trades = query_rows("paper_trades", "WHERE trade_date <= ?", (target_date,), 500)
    morning_purchases = morning_purchase_summary(paper_trades, target_date)
    jobs = job_rows_for_date(target_date)
    pipeline = pipeline_state()
    stages = {
        name: value for name, value in pipeline.get("stages", {}).items()
        if value.get("date") == target_date
    }
    open_positions = sum(1 for row in paper_performance if float(row.get("remaining_shares") or 0) > 0)
    resolved = sum(1 for row in paper_performance if float(row.get("remaining_shares") or 0) <= 0)
    exit_activity = trade_exit_activity(paper_performance)
    partial_exits = len({
        str(row.get("ticker")) for row in exit_activity if row.get("position") == "Open remainder"
    })
    failed_jobs = sum(1 for row in jobs if not bool(row.get("ok")))
    decision_rows = [row for row in strategy_reviews if row.get("section") == "decision"]
    decision = decision_rows[-1].get("value") if decision_rows else "no strategy review"
    return {
        "date": target_date,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "bankroll": {"base": bankroll_base(), "deposits": total_bankroll_deposits()},
        "summary": {
            "paper_rows": len(paper_performance),
            "current_trade_rows": len(paper_trades),
            "stocks_bought": morning_purchases["count"],
            "open_positions": open_positions,
            "resolved_trades": resolved,
            "partial_exits": partial_exits,
            "job_runs": len(jobs),
            "failed_jobs": failed_jobs,
            "strategy_decision": decision,
        },
        "pipeline_stages": stages,
        "jobs": jobs,
        "strategy_reviews": strategy_reviews,
        "paper_performance": paper_performance,
        "exit_activity": exit_activity,
        "score_band": score_band,
        "components": components,
        "paper_trades": paper_trades,
        "morning_purchases": morning_purchases,
    }


def latest_report_date() -> str:
    rows = query_rows("paper_performance", "ORDER BY report_date DESC", (), 1)
    if rows and rows[0].get("report_date"):
        return str(rows[0]["report_date"])
    return today_key()


def visible_live_infographic_rows(rows: list[dict], target_date: str) -> list[dict]:
    """Keep active positions and trades that closed on the selected date."""
    visible = []
    for row in rows:
        try:
            is_open = float(row.get("remaining_shares") or 0) > 0
        except (TypeError, ValueError):
            is_open = str(row.get("status") or "").upper() in {"OPEN", "PARTIAL"}
        closed_on_target = str(row.get("exit_datetime") or "")[:10] == target_date
        if is_open or closed_on_target:
            visible.append(row)
    return visible


def live_infographic_payload(target_date: str | None = None) -> dict:
    target_date = normalized_date(target_date) if target_date else latest_report_date()
    account_rows = query_rows("paper_performance", "WHERE report_date = ?", (target_date,), 500)
    if not account_rows and target_date != today_key():
        target_date = today_key()
        account_rows = query_rows("paper_performance", "WHERE report_date = ?", (target_date,), 500)
    rows = visible_live_infographic_rows(account_rows, target_date)

    deposits = total_bankroll_deposits()
    base = bankroll_base()
    total_cost = sum(float(row.get("cost") or 0) for row in account_rows)
    total_market_value = sum(float(row.get("market_value") or 0) for row in account_rows)
    realized_proceeds = sum(float(row.get("realized_proceeds") or 0) for row in account_rows)
    total_pl = sum(float(row.get("p_l") or 0) for row in account_rows)
    cash = base - total_cost + realized_proceeds
    equity = cash + total_market_value
    open_rows = [row for row in rows if float(row.get("remaining_shares") or 0) > 0]
    closed_rows = [row for row in rows if float(row.get("remaining_shares") or 0) <= 0]
    heat = (
        sum(
            float(row.get("initial_risk") or 0)
            * (float(row.get("remaining_shares") or 0) / max(float(row.get("shares") or 1), 1.0))
            for row in open_rows
        )
        / base
        * 100
        if base else 0.0
    )
    return_on_bankroll = total_pl / base * 100 if base else 0.0
    return_on_deployed = total_pl / total_cost * 100 if total_cost else 0.0
    winners = sum(1 for row in rows if float(row.get("p_l") or 0) > 0)
    losers = sum(1 for row in rows if float(row.get("p_l") or 0) < 0)
    flat = len(rows) - winners - losers
    best = max(rows, key=lambda row: float(row.get("p_l_%") or 0), default=None)
    worst = min(rows, key=lambda row: float(row.get("p_l_%") or 0), default=None)
    pipeline = pipeline_state().get("stages", {})
    report_stage = pipeline.get("report", {})
    return {
        "date": target_date,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "report_updated_at": report_stage.get("updated_at"),
        "report_message": report_stage.get("message"),
        "refresh_seconds": 60,
        "rows": rows,
        "open_rows": open_rows,
        "closed_rows": closed_rows,
        "bankroll": {"base": base, "deposits": deposits},
        "summary": {
            "cash": cash,
            "equity": equity,
            "deployed": total_cost,
            "market_value": total_market_value,
            "p_l": total_pl,
            "return_on_bankroll": return_on_bankroll,
            "return_on_deployed": return_on_deployed,
            "heat": heat,
            "winners": winners,
            "losers": losers,
            "flat": flat,
            "open_count": len(open_rows),
            "closed_count": len(closed_rows),
            "total_count": len(rows),
        },
        "best": best,
        "worst": worst,
    }


def html_table(rows: list[dict], empty: str, max_columns: int | None = None) -> str:
    if not rows:
        return f"<div class='empty'>{escape(empty)}</div>"
    columns = list(rows[0].keys())
    if max_columns is not None:
        columns = columns[:max_columns]
    head = "".join(f"<th>{escape(str(column))}</th>" for column in columns)
    body = []
    for row in rows:
        cells = "".join(f"<td>{escape(str(row.get(column, '')))}</td>" for column in columns)
        body.append(f"<tr>{cells}</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def money(value: float) -> str:
    return f"-${abs(value):,.2f}" if value < 0 else f"${value:,.2f}"


def pct(value: float) -> str:
    return f"{value:.2f}%"


def live_infographic_html(target_date: str | None = None) -> bytes:
    payload = live_infographic_payload(target_date)
    summary = payload["summary"]
    is_up = summary["p_l"] >= 0
    accent_class = "positive" if is_up else "negative"
    headline = "Up on the day" if is_up else "Down on the day"
    best = payload["best"]
    worst = payload["worst"]
    report_updated = payload.get("report_updated_at") or "No report stage timestamp"
    report_message = payload.get("report_message") or "Awaiting report stage"

    def trade_card(title: str, row: dict | None, cls: str) -> str:
        if row is None:
            return (
                f"<section class='side-card'><small>{escape(title)}</small>"
                "<div class='empty-mini'>No positions yet</div></section>"
            )
        ticker = escape(str(row.get("ticker") or ""))
        sector = escape(str(row.get("sector") or ""))
        pl = float(row.get("p_l") or 0)
        pl_pct = float(row.get("p_l_%") or 0)
        return (
            f"<section class='side-card'><small>{escape(title)}</small>"
            f"<div class='ticker-row'><strong>{ticker}</strong><span>{sector}</span></div>"
            f"<div class='trade-pnl {cls}'>{money(pl)} <span>{pl_pct:+.2f}%</span></div></section>"
        )

    rows = sorted(payload["rows"], key=lambda row: float(row.get("p_l_%") or 0))

    # --- Diverging bar chart SVG (poster fill) ---
    def bar_chart_svg(chart_rows: list) -> str:
        if not chart_rows:
            return ""
        row_h, pad_l, pad_r, total_w = 27, 54, 64, 500
        bar_area = total_w - pad_l - pad_r
        svg_h = len(chart_rows) * row_h + 2
        pct_vals = [float(r.get("p_l_%") or 0) for r in chart_rows]
        max_pos = max((v for v in pct_vals if v > 0), default=0.5)
        max_neg = abs(min((v for v in pct_vals if v < 0), default=-0.5))
        total_range = max(max_pos + max_neg, 0.1)
        zero_x = pad_l + bar_area * (max_neg / total_range)
        parts = [
            f'<svg viewBox="0 0 {total_w} {svg_h}" xmlns="http://www.w3.org/2000/svg" style="width:100%;display:block">',
            f'<line x1="{zero_x:.1f}" y1="0" x2="{zero_x:.1f}" y2="{svg_h}" stroke="#dce5ef" stroke-width="1.5"/>',
        ]
        for i, r in enumerate(chart_rows):
            pv = float(r.get("p_l_%") or 0)
            status = str(r.get("status") or "").upper()
            tick = escape(str(r.get("ticker") or ""))
            y0 = i * row_h + 2
            bh = row_h - 6
            ym = y0 + bh / 2 + 4.5
            col = "#16803c" if pv >= 0 else "#c43d3d"
            op = "0.85" if float(r.get("remaining_shares") or 0) > 0 else "0.42"
            bw = (abs(pv) / total_range) * bar_area
            bx = zero_x if pv >= 0 else zero_x - bw
            if bw > 0.5:
                parts.append(
                    f'<rect x="{bx:.1f}" y="{y0}" width="{bw:.1f}" height="{bh}"'
                    f' rx="3" fill="{col}" opacity="{op}"/>'
                )
            parts.append(
                f'<text x="{pad_l - 6}" y="{ym:.1f}" text-anchor="end"'
                f' font-size="11" font-weight="700"'
                f' font-family="-apple-system,BlinkMacSystemFont,sans-serif"'
                f' fill="#17202a">{tick}</text>'
            )
            parts.append(
                f'<text x="{pad_l + bar_area + 6}" y="{ym:.1f}" text-anchor="start"'
                f' font-size="10.5" font-weight="700"'
                f' font-family="-apple-system,BlinkMacSystemFont,sans-serif"'
                f' fill="{col}">{pv:+.1f}%</text>'
            )
        parts.append('</svg>')
        return ''.join(parts)

    chart_svg = bar_chart_svg(rows)

    # --- Donut ---
    total = max(int(summary["total_count"]), 1)
    green_deg = summary["winners"] / total * 360
    red_deg = (summary["winners"] + summary["losers"]) / total * 360
    if summary["total_count"]:
        donut = (
            f"conic-gradient(var(--green) 0deg {green_deg:.1f}deg,"
            f"var(--red) {green_deg:.1f}deg {red_deg:.1f}deg,"
            f"var(--amber) {red_deg:.1f}deg 360deg)"
        )
    else:
        donut = "#e5e7eb"

    # P&L totals per win/loss bucket
    green_pl = sum(float(r.get("p_l") or 0) for r in rows if float(r.get("p_l") or 0) > 0)
    red_pl = sum(float(r.get("p_l") or 0) for r in rows if float(r.get("p_l") or 0) < 0)

    # --- Colored positions table ---
    def positions_table_html(table_rows: list) -> str:
        if not table_rows:
            return "<div class='empty'>No paper performance rows available yet.</div>"
        max_abs_pct = max((abs(float(r.get("p_l_%") or 0)) for r in table_rows), default=1) or 1
        cols = ["Ticker", "Sector", "Status", "P/L", "P/L %", "Band"]
        thead = "".join(f"<th>{c}</th>" for c in cols)
        tbody = []
        for r in table_rows:
            pl = float(r.get("p_l") or 0)
            pv = float(r.get("p_l_%") or 0)
            pos = pl >= 0
            bw = int(abs(pv) / max_abs_pct * 52)
            bc = "#16803c" if pos else "#c43d3d"
            st = str(r.get("status") or "")
            pct_td = (
                f"<td><div class='pct-cell'>"
                f"<span class='pbar' style='width:{bw}px;background:{bc}'></span>"
                f"<span class='{'ok' if pos else 'bad'}'>{pv:+.2f}%</span>"
                f"</div></td>"
            )
            pl_td = f"<td class='{'ok' if pos else 'bad'}'>{money(pl)}</td>"
            badge = f"<span class='sb {st.lower()}'>{escape(st)}</span>"
            tbody.append(
                f"<tr class='{'rpos' if pos else 'rneg'}'>"
                f"<td class='tkr'>{escape(str(r.get('ticker') or ''))}</td>"
                f"<td class='sec'>{escape(str(r.get('sector') or ''))}</td>"
                f"<td>{badge}</td>"
                f"{pl_td}"
                f"{pct_td}"
                f"<td class='bnd'>{escape(str(r.get('confirmation_band') or ''))}</td>"
                f"</tr>"
            )
        return f"<table><thead><tr>{thead}</tr></thead><tbody>{''.join(tbody)}</tbody></table>"

    pos_table = positions_table_html(rows)

    html = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="{payload['refresh_seconds']}">
<title>Live P/L Infographic</title><style>
:root{{--bg:#f4f7fb;--panel:#fff;--text:#17202a;--muted:#64748b;--line:#dce5ef;--blue:#1d4ed8;--green:#16803c;--red:#c43d3d;--amber:#b7791f;--soft:#f8fafc}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:14px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;-webkit-font-smoothing:antialiased}}
header{{padding:0 20px;background:var(--panel);border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;gap:16px;position:sticky;top:0;z-index:3;height:52px}}
.hdr-left{{display:flex;align-items:center;gap:10px;flex-shrink:0}}.hdr-left h1{{font-size:15px;font-weight:700;margin:0;letter-spacing:-.01em}}
.hdr-nav{{display:flex;align-items:center;gap:2px;flex:1;padding:0 8px}}
.hdr-nav a{{padding:6px 13px;border-radius:7px;font-size:13px;font-weight:600;color:var(--muted);text-decoration:none;transition:background .15s,color .15s;white-space:nowrap}}
.hdr-nav a:hover{{background:#f1f3f5;color:var(--text)}}.hdr-nav a.active{{background:#eff6ff;color:var(--blue)}}
.hdr-right{{display:flex;gap:10px;align-items:center;flex-shrink:0;color:var(--muted);font-size:12px}}
main{{max-width:1320px;margin:0 auto;padding:24px 20px 64px}}
.watch-shell{{display:grid;grid-template-columns:minmax(360px,1.35fr) minmax(300px,.75fr);gap:20px;align-items:start}}
.poster{{background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:34px;box-shadow:0 8px 28px rgba(17,24,39,.05);display:flex;flex-direction:column;gap:0}}
.eyebrow{{font-size:12px;font-weight:800;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin:0 0 14px}}
.headline{{font-size:54px;line-height:1;letter-spacing:-.045em;margin:0 0 20px;text-transform:uppercase}}
.big-pnl{{font-size:98px;line-height:.9;letter-spacing:-.06em;font-weight:850;margin:0 0 14px}}
.positive{{color:var(--green)}}.negative{{color:var(--red)}}.muted{{color:var(--muted)}}
.subline{{font-size:18px;color:var(--muted);font-weight:650}}
.metric-row{{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-top:24px}}
.metric small{{display:block;font-size:11px;font-weight:800;letter-spacing:.05em;text-transform:uppercase;color:var(--muted);margin-bottom:8px}}
.metric strong{{font-size:22px;letter-spacing:-.02em}}
.chart-area{{border-top:1px solid var(--line);margin-top:26px;padding-top:18px}}
.chart-label{{font-size:11px;font-weight:800;letter-spacing:.07em;text-transform:uppercase;color:var(--muted);margin-bottom:10px}}
.note{{border-top:1px solid var(--line);margin-top:20px;padding-top:14px;color:var(--muted);font-size:12px;line-height:1.5}}
.side{{display:grid;gap:16px}}
.side-card{{background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:26px;box-shadow:0 4px 18px rgba(17,24,39,.04)}}
.side-card small{{display:block;color:var(--muted);font-size:12px;font-weight:800;text-transform:uppercase;letter-spacing:.06em;margin-bottom:20px}}
.mix{{display:grid;grid-template-columns:148px 1fr;gap:22px;align-items:center}}
.donut{{width:142px;height:142px;border-radius:50%;background:{donut};position:relative}}
.donut::after{{content:"";position:absolute;inset:32px;background:var(--panel);border:1px solid var(--line);border-radius:50%}}
.donut-center{{position:absolute;inset:0;display:grid;place-items:center;text-align:center;z-index:1;font-weight:800;font-size:30px}}
.donut-center span{{display:block;font-size:12px;color:var(--muted);font-weight:800;text-transform:uppercase}}
.legend div{{font-size:19px;font-weight:800;margin:7px 0 2px}}.legend .lsub{{display:block;font-size:12px;font-weight:600;color:var(--muted);margin-bottom:2px}}
.legend .g{{color:var(--green)}}.legend .r{{color:var(--red)}}.legend .f{{color:var(--amber)}}
.ticker-row{{display:flex;align-items:baseline;gap:12px;min-width:0}}.ticker-row strong{{font-size:58px;letter-spacing:-.06em;line-height:.95}}.ticker-row span{{font-size:20px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.trade-pnl{{font-size:36px;font-weight:850;letter-spacing:-.03em;margin-top:8px}}.trade-pnl span{{margin-left:10px}}
.status-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}}
.status-tile{{background:#fbfdff;border:1px solid var(--line);border-radius:12px;padding:14px}}.status-tile small{{display:block;margin:0 0 8px}}.status-tile strong{{font-size:17px}}
.table-panel{{margin-top:18px;background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:20px;overflow:auto}}
.panel-head{{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:14px}}.panel-head h2{{font-size:15px;margin:0}}.panel-head span{{color:var(--muted);font-size:12px}}
table{{width:100%;border-collapse:collapse;white-space:nowrap}}
th{{padding:9px 10px;border-bottom:2px solid var(--line);text-align:left;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.04em}}
td{{padding:7px 10px;border-bottom:1px solid var(--line)}}
tbody tr:last-child td{{border-bottom:0}}
tbody tr:hover td{{filter:brightness(.97)}}
.rpos td{{background:#f5fcf7}}.rneg td{{background:#fdf6f6}}
.tkr{{font-weight:700;font-size:13px}}.sec{{color:var(--muted);font-size:12px}}.bnd{{color:var(--muted);font-size:12px;text-align:center}}
.ok{{color:var(--green);font-weight:700}}.bad{{color:var(--red);font-weight:700}}
.sb{{display:inline-block;font-size:11px;font-weight:700;padding:2px 7px;border-radius:5px;letter-spacing:.02em}}
.sb.open{{background:#f0fdf4;color:var(--green);border:1px solid #86efac}}
.sb.partial{{background:#eff6ff;color:var(--blue);border:1px solid #93c5fd}}
.sb.closed{{background:#f1f5f9;color:var(--muted);border:1px solid var(--line)}}
.pct-cell{{display:flex;align-items:center;gap:7px}}
.pbar{{display:inline-block;height:8px;border-radius:3px;flex-shrink:0;opacity:.75}}
.empty{{padding:18px;border-radius:10px;background:var(--soft);color:var(--muted)}}.empty-mini{{color:var(--muted);font-size:20px;font-weight:700}}
@media(max-width:1000px){{.watch-shell{{grid-template-columns:1fr}}.headline{{font-size:42px}}.big-pnl{{font-size:72px}}}}
@media(max-width:640px){{main{{padding:16px}}header{{overflow:auto}}.metric-row,.status-grid{{grid-template-columns:1fr 1fr}}.mix{{grid-template-columns:1fr}}}}
</style></head><body>
<header>
  <div class="hdr-left"><h1>Live Infographic</h1></div>
  <nav class="hdr-nav">__HEADER_NAV__</nav>
  <div class="hdr-right"><span>Auto-refresh {payload['refresh_seconds']}s</span><span>{escape(payload['generated_at'])}</span></div>
</header>
<main>
  <div class="watch-shell">
    <section class="poster">
      <div>
        <p class="eyebrow">Realtime P/L flash card · {escape(payload['date'])}</p>
        <h2 class="headline">{escape(headline)}</h2>
        <div class="big-pnl {accent_class}">{money(summary['p_l'])}</div>
        <div class="subline">{pct(summary['return_on_bankroll'])} of bankroll&nbsp;&nbsp;|&nbsp;&nbsp;{pct(summary['return_on_deployed'])} on deployed capital</div>
        <div class="metric-row">
          <div class="metric"><small>Equity</small><strong>{money(summary['equity'])}</strong></div>
          <div class="metric"><small>Deployed</small><strong class="muted">{money(summary['deployed'])}</strong></div>
          <div class="metric"><small>Cash</small><strong>{money(summary['cash'])}</strong></div>
          <div class="metric"><small>Heat</small><strong class="positive">{pct(summary['heat'])}</strong></div>
        </div>
      </div>
      <div class="chart-area">
        <div class="chart-label">Position P/L %</div>
        {chart_svg}
      </div>
      <div class="note">Open positions plus trades closed on {escape(payload['date'])} &middot; faded bars = closed today &middot; auto-refreshes every {payload['refresh_seconds']}s</div>
    </section>
    <aside class="side">
      <section class="side-card">
        <small>Position mix</small>
        <div class="mix">
          <div class="donut"><div class="donut-center"><div>{summary['total_count']}<span>Rows</span></div></div></div>
          <div class="legend">
            <div class="g">{summary['winners']} green<span class="lsub">{money(green_pl)}</span></div>
            <div class="r">{summary['losers']} red<span class="lsub">{money(red_pl)}</span></div>
            <div class="f">{summary['flat']} flat</div>
          </div>
        </div>
      </section>
      {trade_card("Best card", best, "positive")}
      {trade_card("Worst card", worst, "negative")}
      <section class="side-card">
        <small>Live status</small>
        <div class="status-grid">
          <div class="status-tile"><small>Open</small><strong>{summary['open_count']}</strong></div>
          <div class="status-tile"><small>Resolved today</small><strong>{summary['closed_count']}</strong></div>
          <div class="status-tile"><small>Report</small><strong>{escape(str(report_message))}</strong></div>
        </div>
        <p class="muted" style="margin:12px 0 0;font-size:12px">Last report: {escape(str(report_updated))}</p>
      </section>
    </aside>
  </div>
  <section class="table-panel">
    <div class="panel-head"><h2>Positions, worst first</h2><span>{summary['total_count']} rows</span></div>
    {pos_table}
  </section>
</main>
<script>
setTimeout(function(){{ window.location.reload(); }}, {payload['refresh_seconds'] * 1000});
</script>
</body></html>"""
    return finalize_page_html(html, "/live-infographic")


def day_html(target_date: str) -> bytes:
    payload = day_payload(target_date)
    summary = payload["summary"]
    failed = summary["failed_jobs"]
    status_cls = "bad" if failed else "ok"
    status_label = f"{failed} failed job{'s' if failed != 1 else ''}" if failed else "Pipeline clear"
    morning_purchases = payload["morning_purchases"]
    purchase_cls = "has-buys" if morning_purchases["count"] else "no-buys"
    cards = {
        "Bankroll": f"${payload['bankroll']['base']:,.2f}",
        "Deposits": f"${payload['bankroll']['deposits']:,.2f}",
        "Paper rows": summary["paper_rows"],
        "Open positions": summary["open_positions"],
        "Resolved trades": summary["resolved_trades"],
        "Partial exits": summary["partial_exits"],
        "Job runs": summary["job_runs"],
        "Failed jobs": summary["failed_jobs"],
        "Decision": summary["strategy_decision"],
    }
    card_html = "".join(
        f"<div class='card'><small>{escape(str(label))}</small><strong class='{'bad' if label == 'Failed jobs' and value else ''}'>{escape(str(value))}</strong></div>"
        for label, value in cards.items()
    )
    stage_rows = [{"stage": name, **value} for name, value in payload["pipeline_stages"].items()]
    html = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Day status · {escape(payload['date'])}</title><style>
:root{{--bg:#f4f6f8;--panel:#fff;--text:#17202a;--muted:#687386;--line:#dce3ec;--blue:#1d4ed8;--green:#16803c;--red:#b42318}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:14px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;-webkit-font-smoothing:antialiased}}
header{{padding:0 20px;background:var(--panel);border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;gap:16px;position:sticky;top:0;z-index:3;height:52px}}
.hdr-left{{display:flex;align-items:center;gap:10px;flex-shrink:0}}
.hdr-left h1{{font-size:15px;font-weight:700;margin:0;letter-spacing:-.01em}}
.hdr-nav{{display:flex;align-items:center;gap:2px;flex:1;padding:0 8px}}
.hdr-nav a{{padding:6px 13px;border-radius:7px;font-size:13px;font-weight:600;color:var(--muted);text-decoration:none;transition:background .15s,color .15s;white-space:nowrap}}
.hdr-nav a:hover{{background:#f1f3f5;color:var(--text)}}
.hdr-nav a.active{{background:#eff6ff;color:var(--blue)}}
.hdr-right{{display:flex;gap:8px;align-items:center;flex-shrink:0}}
.status-badge{{border-radius:999px;padding:5px 12px;font-size:12px;font-weight:600;white-space:nowrap}}
.status-badge.ok{{background:#f0fdf4;border:1px solid #86efac;color:var(--green)}}
.status-badge.bad{{background:#fef2f2;border:1px solid #fca5a5;color:var(--red)}}
form{{display:flex;gap:6px;align-items:center}}
input[type=date]{{border:1px solid var(--line);border-radius:7px;padding:5px 9px;font:13px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:var(--text);background:#fff}}
button{{border:1px solid #bfcee3;background:#f8fbff;color:#183b75;border-radius:7px;padding:5px 12px;font-size:13px;font-weight:600;cursor:pointer}}
button:hover{{border-color:#7da2da}}
main{{max-width:1400px;margin:0 auto;padding:24px 20px 64px}}
.page-title{{margin:0 0 20px}}
.page-title h2{{font-size:22px;font-weight:700;letter-spacing:-.02em;margin:0 0 3px}}
.page-title .sub{{font-size:12px;color:var(--muted)}}
.purchase-status{{display:flex;align-items:center;gap:14px;background:var(--panel);border:1px solid var(--line);border-left:5px solid #94a3b8;border-radius:12px;padding:17px 20px;margin-bottom:16px;box-shadow:0 1px 6px rgba(17,24,39,.03)}}
.purchase-status.has-buys{{border-left-color:var(--green);background:#f7fdf9}}
.purchase-count{{font-size:32px;line-height:1;font-weight:800;letter-spacing:-.04em}}
.purchase-copy strong{{display:block;font-size:17px;letter-spacing:-.01em}}.purchase-copy span{{display:block;color:var(--muted);font-size:12px;margin-top:3px}}
.cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:20px}}
.card{{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px 18px;box-shadow:0 1px 6px rgba(17,24,39,.03)}}
.card small{{display:block;color:var(--muted);font-size:11px;font-weight:700;letter-spacing:.04em;text-transform:uppercase;margin-bottom:8px}}
.card strong{{display:block;font-size:20px;font-weight:700;letter-spacing:-.02em}}
.card strong.bad{{color:var(--red)}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}
.panel{{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px 20px;overflow:auto;box-shadow:0 1px 6px rgba(17,24,39,.03)}}
.panel.full{{grid-column:1/-1}}
.panel-head{{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:12px}}
.panel-head h3{{font-size:14px;font-weight:700;margin:0}}
.panel-head .sub{{font-size:12px;color:var(--muted)}}
table{{width:100%;border-collapse:collapse;font-size:13px;white-space:nowrap}}
th,td{{padding:9px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}}
th{{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.04em;font-weight:600}}
tbody tr:last-child td{{border-bottom:0}}
.empty{{color:var(--muted);padding:18px;background:#f9fafb;border-radius:8px;font-size:13px}}
a{{color:var(--blue)}}
.ok{{color:var(--green);font-weight:700}}.bad{{color:var(--red);font-weight:700}}
@media(max-width:1000px){{.cards{{grid-template-columns:repeat(2,1fr)}}.grid{{grid-template-columns:1fr}}}}
@media(max-width:560px){{.cards{{grid-template-columns:1fr 1fr}}main{{padding:16px}}}}
</style></head><body>
<header>
  <div class="hdr-left"><h1>Day Status</h1></div>
  <nav class="hdr-nav">__HEADER_NAV__</nav>
  <div class="hdr-right">
    <span class="status-badge {status_cls}">{escape(status_label)}</span>
    <form action="/day" method="get">
      <input type="date" name="date" value="{escape(payload['date'])}">
      <button>Go</button>
    </form>
  </div>
</header>
<main>
  <div class="page-title">
    <h2>{escape(payload['date'])}</h2>
    <div class="sub">Generated {escape(payload['generated_at'])}</div>
  </div>
  <section class="purchase-status {purchase_cls}" aria-label="Morning purchases">
    <div class="purchase-count">{morning_purchases['count']}</div>
    <div class="purchase-copy"><strong>{escape(morning_purchases['message'])}</strong><span>Based on paper-trade entries dated {escape(payload['date'])}</span></div>
  </section>
  <section class="cards">{card_html}</section>
  <div class="grid">
    <section class="panel full">
      <div class="panel-head"><h3>Pipeline stages</h3><span class="sub">Dependency gates</span></div>
      {html_table(stage_rows, "No pipeline stages recorded for this date.")}
    </section>
    <section class="panel full">
      <div class="panel-head"><h3>Job runs</h3><span class="sub">Scheduler activity</span></div>
      {html_table(payload['jobs'], "No job runs recorded for this date.", 10)}
    </section>
    <section class="panel full">
      <div class="panel-head"><h3>Scale-outs &amp; exits</h3><span class="sub">Partial profits, stops, and final exits recorded in the paper ledger</span></div>
      {html_table(payload['exit_activity'], "No shares were sold in this snapshot.")}
    </section>
    <section class="panel full">
      <div class="panel-head"><h3>Strategy review</h3><span class="sub">Improvement and tuning gates</span></div>
      {html_table(payload['strategy_reviews'], "No strategy review rows recorded for this date.")}
    </section>
    <section class="panel full">
      <div class="panel-head"><h3>Paper performance snapshot</h3><span class="sub">Trade state captured for this date</span></div>
      {html_table(payload['paper_performance'], "No paper performance snapshot recorded for this date.", 24)}
    </section>
    <section class="panel">
      <div class="panel-head"><h3>Score-band analytics</h3><span class="sub">Resolved outcomes by band</span></div>
      {html_table(payload['score_band'], "No score-band analytics recorded for this date.")}
    </section>
    <section class="panel">
      <div class="panel-head"><h3>Signal component analytics</h3><span class="sub">Resolved outcomes by signal</span></div>
      {html_table(payload['components'], "No component analytics recorded for this date.")}
    </section>
    <section class="panel full">
      <div class="panel-head"><h3>Paper ledger through date</h3><span class="sub">PostgreSQL paper_trades rows</span></div>
      {html_table(payload['paper_trades'], "No paper trades recorded through this date.", 24)}
    </section>
  </div>
</main></body></html>"""
    return finalize_page_html(html, "/day")


def settings_payload() -> dict:
    status = status_payload()
    status["version"] = version_label()
    return status


def settings_html() -> bytes:
    html = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Settings</title><style>
:root{--bg:#f4f6f8;--panel:#fff;--text:#17202a;--muted:#687386;--line:#dce3ec;--blue:#1d4ed8;--green:#16803c;--red:#b42318;--amber:#92400e;--amber-bg:#fffbeb;--amber-border:#fcd34d}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
header{padding:0 20px;background:var(--panel);border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;gap:16px;position:sticky;top:0;z-index:2;height:52px}
.hdr-left h1{font-size:15px;font-weight:700;margin:0}
.hdr-nav{display:flex;align-items:center;gap:2px;flex:1;padding:0 8px}
.hdr-nav a{padding:6px 13px;border-radius:7px;font-size:13px;font-weight:600;color:var(--muted);text-decoration:none}
.hdr-nav a:hover{background:#f1f3f5;color:var(--text)}.hdr-nav a.active{background:#eff6ff;color:var(--blue)}
.hdr-right{font-size:12px;color:var(--muted)}
main{max-width:960px;margin:0 auto;padding:24px 20px 64px;display:grid;gap:16px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:20px;box-shadow:0 1px 6px rgba(15,23,42,.04)}
.page-title h2{margin:0 0 4px;font-size:22px}.sub{color:var(--muted);font-size:13px;margin:0 0 14px}
.sec{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:0 0 10px}
.kv{display:grid;grid-template-columns:1fr auto;gap:6px;padding:8px 0;border-bottom:1px solid #f1f3f6;align-items:baseline}
.kv:last-child{border-bottom:0}.kv .k{color:var(--muted);font-size:13px}.kv .v{font-size:13px;text-align:right}
.ok{color:var(--green);font-weight:700}.bad{color:var(--red);font-weight:700}.muted{color:var(--muted)}
button{border:1px solid #bfcee3;background:#f8fbff;color:#183b75;border-radius:8px;padding:10px 16px;font-weight:700;cursor:pointer;font-size:13px}
button:disabled{opacity:.55;cursor:not-allowed}
.btn-inject{background:var(--amber-bg);border-color:var(--amber-border);color:var(--amber)}
.job-row{display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid #f1f3f6;gap:8px}
.job-row:last-child{border-bottom:0}
.jb{font-size:11px;font-weight:700;padding:2px 7px;border-radius:999px}
.jb.ok{background:#f0fdf4;color:var(--green);border:1px solid #86efac}
.jb.bad{background:#fef2f2;color:var(--red);border:1px solid #fca5a5}
.jb.none{background:#f9fafb;color:var(--muted);border:1px solid var(--line)}
.log{white-space:pre-wrap;max-height:240px;overflow:auto;font:12px ui-monospace,Menlo,monospace;background:#0f172a;color:#e2e8f0;border-radius:10px;padding:12px;line-height:1.5}
.run-bar{display:flex;align-items:center;gap:8px;padding:10px 12px;background:#eff6ff;border:1px solid #93c5fd;border-radius:8px;font-size:13px;font-weight:600;color:#1e40af;margin-bottom:12px}
.run-bar.hidden{display:none}
@keyframes spin{to{transform:rotate(360deg)}}.spinner{width:13px;height:13px;border:2px solid #93c5fd;border-top-color:#1d4ed8;border-radius:50%;animation:spin .7s linear infinite}
a{color:var(--blue)}
</style></head><body>
<header>
  <div class="hdr-left"><h1>Settings</h1></div>
  <nav class="hdr-nav">__HEADER_NAV__</nav>
  <div class="hdr-right" id="server-time">—</div>
</header>
<main>
  <section class="panel">
    <div class="page-title">
      <h2>Bankroll</h2>
      <p class="sub">Paper account capital. Scheduled jobs and the dashboard use these totals.</p>
    </div>
    <div id="bankroll"></div>
    <div style="margin-top:14px">
      <button class="btn-inject" id="inject-bankroll">Inject $25,000 bankroll</button>
    </div>
  </section>
  <section class="panel">
    <div class="page-title">
      <h2>System</h2>
      <p class="sub">Runtime configuration and backend status. Manual job runs live on the <a href="/jobs">Jobs</a> page.</p>
    </div>
    <div id="run-bar" class="run-bar hidden"><span class="spinner"></span><span id="run-label">Running…</span></div>
    <div id="system"></div>
  </section>
  <section class="panel">
    <div class="page-title">
      <h2>Today's pipeline files</h2>
      <p class="sub">Export artifacts on disk for the current session.</p>
    </div>
    <div id="files"></div>
  </section>
  <section class="panel">
    <div class="page-title">
      <h2>Job status</h2>
      <p class="sub">Last result per scheduled job. Use <a href="/jobs">Jobs</a> to run manually or view history.</p>
    </div>
    <div id="jobs"></div>
  </section>
  <section class="panel">
    <div class="page-title">
      <h2>Last job output</h2>
      <p class="sub" id="log-src"></p>
    </div>
    <div class="log" id="log">No job output yet.</div>
  </section>
</main>
<script>
function fmtTime(iso){if(!iso)return '—';const d=new Date(iso);const t=d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});return d.toDateString()===new Date().toDateString()?t:d.toLocaleDateString([],{month:'short',day:'numeric'})+' '+t}
function kv(k,v,cls=''){return `<div class="kv"><span class="k">${k}</span><span class="v ${cls}">${v}</span></div>`}
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
async function loadSettings(){const r=await fetch('/api/settings');return r.json()}
function render(s){
  document.getElementById('server-time').textContent=fmtTime(s.server_time);
  const rb=document.getElementById('run-bar');
  if(s.running){rb.className='run-bar';document.getElementById('run-label').textContent='Running: '+s.running}
  else{rb.className='run-bar hidden'}
  document.getElementById('bankroll').innerHTML=
    kv('Base capital','$'+Number(s.bankroll.base).toLocaleString(),'ok')+
    kv('Deposits','$'+Number(s.bankroll.deposits).toLocaleString(),'muted')+
    kv('Injection amount','$'+Number(s.bankroll.injection_amount).toLocaleString(),'muted');
  let sys='';
  sys+=kv('Version',esc(s.version||'—'));
  sys+=kv('Scheduler',esc(s.scheduler_mode||'—'));
  sys+=kv('Backend',s.running?'Running: '+esc(s.running):'Idle',s.running?'bad':'ok');
  sys+=kv('Live updates',s.live_updates?'On':'Off',s.live_updates?'ok':'muted');
  sys+=kv('Database',esc(s.files?.database_url||'—'));
  document.getElementById('system').innerHTML=sys;
  let files='';
  const fileLabels={morning_candidates:'Morning candidates (PG)',confirmation:'9:45 confirmation (PG)',paper_performance:'Paper performance (PG)',dashboard:'Dashboard HTML'};
  for(const [key,label] of Object.entries(fileLabels)){
    const f=s.files?.[key]||{};
    const val=f.rows!=null?`${f.rows} rows in PostgreSQL`:(f.exists?'Available':'Missing');
    files+=kv(label,val,f.rows>0?'ok':'muted');
  }
  document.getElementById('files').innerHTML=files;
  let jobs='';
  for(const [name,job] of Object.entries(s.jobs||{})){
    const last=job.last_run;
    const badge=last?(last.ok?'<span class="jb ok">OK</span>':'<span class="jb bad">Failed</span>'):'<span class="jb none">not run</span>';
    jobs+=`<div class="job-row"><span><b>${esc(name.replace(/_/g,' '))}</b><br><span class="sub">${esc(job.description||'')}</span></span><span>${badge}</span></div>`;
  }
  document.getElementById('jobs').innerHTML=jobs;
  document.getElementById('inject-bankroll').disabled=!!s.running;
  const allRuns=[];
  for(const [name,job] of Object.entries(s.jobs||{})){if(job.last_run)allRuns.push({...job.last_run,_name:name})}
  const latest=allRuns.sort((a,b)=>(b.finished_at||'').localeCompare(a.finished_at||''))[0];
  if(latest){
    document.getElementById('log').textContent=latest.output_tail||'(no output)';
    document.getElementById('log-src').textContent=latest._name.replace(/_/g,' ')+' · '+fmtTime(latest.finished_at);
  }
}
document.getElementById('inject-bankroll').onclick=async()=>{
  if(!confirm('Inject $25,000 into the paper bankroll?'))return;
  const btn=document.getElementById('inject-bankroll');btn.disabled=true;
  try{await fetch('/api/bankroll/inject',{method:'POST'});render(await loadSettings())}catch(e){alert('Request failed: '+e)}
  btn.disabled=false;
};
async function refresh(){try{render(await loadSettings())}catch(e){document.getElementById('system').innerHTML=kv('Backend','unreachable','bad')}}
refresh();setInterval(refresh,30000);
</script></body></html>"""
    return finalize_page_html(html, "/settings")


def home_controls_html() -> bytes:
    html = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Home Controls</title><style>
:root{--bg:#f4f6f8;--panel:#fff;--text:#17202a;--muted:#687386;--line:#dce3ec;--blue:#1d4ed8;--green:#16803c;--amber:#b7791f;--red:#b42318;--soft:#f8fafc}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;-webkit-font-smoothing:antialiased}
header{padding:0 20px;background:var(--panel);border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;gap:16px;position:sticky;top:0;z-index:3;height:52px}
.hdr-left{display:flex;align-items:center;flex-shrink:0}.hdr-left h1{font-size:15px;font-weight:700;margin:0;letter-spacing:-.01em}
.hdr-nav{display:flex;align-items:center;gap:2px;flex:1;padding:0 8px}.hdr-nav a{padding:6px 13px;border-radius:7px;font-size:13px;font-weight:600;color:var(--muted);text-decoration:none;white-space:nowrap}.hdr-nav a:hover{background:#f1f3f5;color:var(--text)}.hdr-nav a.active{background:#eff6ff;color:var(--blue)}
.hdr-right{font-size:12px;color:var(--muted);white-space:nowrap}
.page-title{display:flex;justify-content:space-between;align-items:flex-end;gap:16px;margin-bottom:20px}.page-title h2{margin:0 0 5px;font-size:25px;letter-spacing:-.03em}.sub{font-size:12px;color:var(--muted)}
.home-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}.home-card{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:22px;box-shadow:0 2px 12px rgba(15,23,42,.035);min-width:0}.cars-card{grid-column:1/-1}
.card-head{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;margin-bottom:22px}.card-title{display:flex;align-items:center;gap:11px}.card-title h3{font-size:16px;margin:0 0 3px}.icon{width:38px;height:38px;border-radius:11px;display:grid;place-items:center;font-size:19px;background:#eff6ff}.pool .icon{background:#ecfeff}.door .icon{background:#f3f4f6}.solar .icon{background:#fffbeb}.cars-card .icon{background:#eef2ff}
.pill{font-size:11px;font-weight:700;padding:4px 9px;border-radius:999px;background:#f1f5f9;color:var(--muted);white-space:nowrap}.pill.ok{background:#f0fdf4;color:var(--green)}.pill.warn{background:#fffbeb;color:var(--amber)}.pill.bad{background:#fef2f2;color:var(--red)}
.hero-value{font-size:46px;line-height:1;font-weight:800;letter-spacing:-.055em;margin:5px 0 8px}.hero-value small{font-size:19px;color:var(--muted);letter-spacing:0}.detail-row{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin-top:20px}.detail{background:var(--soft);border:1px solid #edf1f5;border-radius:10px;padding:12px}.detail small{display:block;color:var(--muted);font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;margin-bottom:6px}.detail strong{font-size:14px}
.door-state{display:flex;align-items:center;gap:16px;padding:5px 0 15px}.door-state .lock{font-size:42px}.door-state strong{display:block;font-size:25px;letter-spacing:-.03em;text-transform:capitalize}.door-state span{color:var(--muted);font-size:12px}.door-actions{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.door-actions .control-btn{width:100%;min-height:54px;font-size:14px}.door-actions .control-btn.active{background:#e8f5ed;border-color:#73bd8d;color:#116530}
.pool-mode-card .icon{background:#eefbf3}.mode-current{font-size:30px;font-weight:800;letter-spacing:-.04em;margin:4px 0 16px}.mode-actions{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.mode-choice{min-height:54px!important;width:100%;font-size:14px}.mode-choice.active{background:#e8f5ed;border-color:#73bd8d;color:#116530}.pool-accessories{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.pool-accessory{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-top:14px;padding:10px 12px;border:1px solid var(--line);border-radius:11px;background:var(--soft)}.pool-accessory strong{display:block}.pool-accessory small{display:block;margin-top:3px;color:var(--muted);text-transform:capitalize}.pool-accessory .control-btn{min-width:100px;width:auto;max-width:none;flex:0 0 auto}
.cars-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.car{border:1px solid var(--line);border-radius:12px;padding:14px;background:var(--soft);overflow:hidden}.car-top{display:flex;justify-content:space-between;gap:10px;align-items:center}.car h4{font-size:14px;margin:0}.vehicle-badges{display:flex;align-items:center;gap:7px}.vehicle-lock{width:27px;height:27px;border-radius:999px;display:grid;place-items:center;background:#eef2f7;border:1px solid #d8e0ea;font-size:14px;line-height:1}.vehicle-lock.locked{background:#f0fdf4;border-color:#bbdfc6}.vehicle-lock.unlocked{background:#fffbeb;border-color:#ead6aa}.car-image{display:block;width:100%;height:108px;object-fit:contain;mix-blend-mode:multiply;margin:3px 0}.car-metrics{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin:2px 0 10px}.car-metric small{display:block;color:var(--muted);font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;margin-bottom:2px}.car-metric strong{font-size:23px;letter-spacing:-.04em}.car-metric strong span{font-size:11px;color:var(--muted);letter-spacing:0}.battery{height:7px;background:#e5e7eb;border-radius:999px;overflow:hidden}.battery span{display:block;height:100%;background:var(--green);border-radius:999px;transition:width .3s ease}
.control-row{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:16px}.control-btn{min-height:38px;border:1px solid #cbd5e1;background:#fff;color:var(--text);border-radius:9px;padding:8px 13px;font-weight:700;cursor:pointer}.control-btn:hover{border-color:#7da2da;background:#f8fbff}.control-btn:disabled{opacity:.5;cursor:wait}.control-btn.primary{background:#eff6ff;border-color:#93c5fd;color:#1e40af}.control-btn.danger{color:var(--red)}.stepper{display:flex;align-items:center;gap:7px;margin-right:auto}.stepper .control-btn{width:40px;padding:8px}.stepper-value{min-width:72px;text-align:center;font-weight:750}
.action-message{font-size:12px;color:var(--muted);min-height:18px;margin-top:10px}.action-message.bad{color:var(--red)}
.empty{color:var(--muted);font-size:13px;line-height:1.5;padding:8px 0}.loading{position:relative;color:transparent!important;border-radius:7px;background:linear-gradient(90deg,#e7ebf0 25%,#f6f7f9 50%,#e7ebf0 75%);background-size:200% 100%;animation:shimmer 1.3s infinite}.loading *{visibility:hidden}@keyframes shimmer{to{background-position:-200% 0}}
.solar{grid-column:1/-1}.solar-auth{display:none;margin-top:14px;text-decoration:none;width:max-content}.solar-layout{display:grid;grid-template-columns:minmax(220px,.7fr) minmax(0,1.3fr);gap:28px;align-items:end}.solar-chart-wrap{min-width:0}.solar-chart-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:10px}.solar-legend{display:flex;gap:12px;color:var(--muted);font-size:10px;font-weight:700}.solar-legend span{display:flex;align-items:center;gap:5px}.solar-legend i{width:8px;height:8px;border-radius:2px;background:#f2b84b}.solar-legend .use i{background:#7da2da}.solar-chart{height:150px;display:flex;align-items:stretch;gap:3px;border-bottom:1px solid var(--line);background:linear-gradient(to top,transparent 32%,#eef2f6 33%,transparent 34%,transparent 65%,#eef2f6 66%,transparent 67%);padding:4px 2px 0}.solar-hour{flex:1;min-width:0;display:flex;align-items:flex-end;justify-content:center;gap:1px;position:relative}.solar-bar{width:min(7px,42%);min-height:0;border-radius:3px 3px 0 0;background:#f2b84b;transition:height .25s ease}.solar-bar.use{background:#7da2da}.solar-hour small{position:absolute;top:100%;margin-top:5px;font-size:9px;color:var(--muted);transform:translateX(-50%);left:50%;white-space:nowrap}.solar-chart-empty{height:150px;display:grid;place-items:center;border:1px dashed var(--line);border-radius:10px;color:var(--muted);font-size:12px}
.deck-meta{display:none}.deck-dots{display:flex;align-items:center;justify-content:center;gap:1px}.deck-dot{border:0;background:transparent;padding:0;display:grid;place-items:center}.deck-dot::after{content:"";width:7px;height:7px;border-radius:999px;background:#cbd5e1;transition:width .2s ease,background .2s ease}.deck-dot.active::after{width:20px;background:var(--blue)}
@media(max-width:720px){.home-grid{grid-template-columns:1fr}.cars-card,.solar{grid-column:auto}.cars-grid,.pool-accessories{grid-template-columns:1fr}.solar-layout{grid-template-columns:1fr;gap:22px}.page-title{align-items:flex-start;flex-direction:column}.home-card{padding:18px}.hero-value{font-size:40px}.stepper{width:100%;margin-right:0}.control-row>.control-btn{flex:1}}
@media(max-width:1100px) and (max-height:650px){
  body.home-controls-page{overflow-x:hidden}body.home-controls-page header{height:52px;min-height:52px;padding:4px 10px;gap:6px;flex-wrap:nowrap}body.home-controls-page .hdr-left{display:none}body.home-controls-page .hdr-nav{order:initial;width:auto;flex:1 1 auto;padding:0;mask-image:linear-gradient(to right,#000 94%,transparent);overflow-x:auto}body.home-controls-page .hdr-nav a{min-height:44px;width:auto;padding:8px 12px}body.home-controls-page .hdr-right{order:initial;margin-left:0;font-size:10px}body.home-controls-page main{padding:8px 12px 18px;max-width:none}
  body.home-controls-page .page-title{margin:0 0 2px;min-height:34px;display:flex;flex-direction:row;align-items:center}body.home-controls-page .page-title h2{font-size:19px;margin:0}body.home-controls-page .page-title .sub{display:none}body.home-controls-page .page-title>.pill{margin-left:auto}
  .deck-meta{height:44px;display:flex;align-items:center;justify-content:space-between;color:var(--muted);font-size:11px;padding:0 2px}.deck-meta>span{display:flex;align-items:center;gap:6px}.deck-meta>span::before{content:"↔";font-size:16px;color:var(--blue)}.deck-dot{width:44px!important;min-height:44px!important}
  .home-grid{display:grid;grid-template-columns:none;grid-auto-flow:column;grid-auto-columns:calc(100vw - 28px);gap:10px;overflow-x:auto;overflow-y:hidden;scroll-snap-type:x mandatory;scroll-padding-inline:2px;overscroll-behavior-x:contain;touch-action:pan-y pinch-zoom;user-select:none;-webkit-user-select:none;scrollbar-width:none;padding:1px 2px 6px;cursor:grab}.home-grid.dragging{cursor:grabbing;scroll-snap-type:none}.home-grid::-webkit-scrollbar{display:none}.home-card,.cars-card,.solar{grid-column:auto;scroll-snap-align:center;scroll-snap-stop:always;min-height:304px;padding:16px 18px;border-radius:14px}.card-head{margin-bottom:14px}.hero-value{font-size:40px}.detail-row{margin-top:13px}.control-btn{min-height:46px;padding:10px 15px}.control-row>.control-btn{width:auto;max-width:none;flex:1}.stepper .control-btn{width:46px}.solar-layout{grid-template-columns:minmax(190px,.72fr) minmax(0,1.28fr);gap:22px}.solar-chart,.solar-chart-empty{height:122px}.cars-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.cars-card .card-head{margin-bottom:8px}.car{padding:10px 12px}.car-image{height:105px}.car-metrics{margin:0 0 8px}.car-metric strong{font-size:21px}
}
@media(prefers-reduced-motion:reduce){.loading{animation:none}}
</style></head><body class="home-controls-page">
<header><div class="hdr-left"><h1>Stock Strategy App</h1></div><nav class="hdr-nav">__HEADER_NAV__</nav><div class="hdr-right" id="home-updated">Loading…</div></header>
<main>
  <div class="page-title"><div><h2>Home Controls</h2><div class="sub">A calm glance at the systems keeping home moving.</div></div><span class="pill" id="ha-status">Connecting</span></div>
  <div class="deck-meta"><span>Swipe to move between controls</span><div class="deck-dots" aria-label="Home control pages"><button class="deck-dot active" type="button" aria-label="Show pool"></button><button class="deck-dot" type="button" aria-label="Show pool mode"></button><button class="deck-dot" type="button" aria-label="Show front door"></button><button class="deck-dot" type="button" aria-label="Show solar"></button><button class="deck-dot" type="button" aria-label="Show cars"></button></div></div>
  <div class="home-grid">
    <section class="home-card pool" id="pool-card">
      <div class="card-head"><div class="card-title"><span class="icon">≈</span><div><h3>Pool</h3><div class="sub">PoolSync heat pump</div></div></div><span class="pill" id="pool-status">Loading</span></div>
      <div class="hero-value" id="pool-temperature">—<small> °F</small></div><div class="sub" id="pool-message">Reading water temperature…</div>
      <div class="detail-row"><div class="detail"><small>Mode</small><strong id="pool-mode">—</strong></div><div class="detail"><small>Setpoint</small><strong id="pool-setpoint">—</strong></div></div>
      <div class="control-row"><div class="stepper"><button class="control-btn" id="pool-down" type="button" aria-label="Lower pool setpoint">−</button><span class="stepper-value" id="pool-target">— °F</span><button class="control-btn" id="pool-up" type="button" aria-label="Raise pool setpoint">+</button></div><button class="control-btn primary" id="pool-heat" type="button">Heat pool</button><button class="control-btn danger" id="pool-off" type="button">Turn off</button></div>
      <div class="action-message" id="pool-action-message"></div>
    </section>
    <section class="home-card pool-mode-card">
      <div class="card-head"><div class="card-title"><span class="icon">⇄</span><div><h3>Pool Mode</h3><div class="sub">Sonoff valve controls</div></div></div><span class="pill" id="pool-mode-status">Loading</span></div>
      <div class="mode-current" id="pool-mode-current">Checking valves…</div>
      <div class="mode-actions"><button class="control-btn mode-choice" id="pool-mode-pool" type="button">Pool Mode</button><button class="control-btn mode-choice" id="pool-mode-spa" type="button">Spa Mode</button></div>
      <div class="pool-accessories"><div class="pool-accessory"><div><strong>Deck Jets</strong><small id="deck-jets-state">Checking state…</small></div><button class="control-btn primary" id="deck-jets-toggle" type="button">Turn on</button></div><div class="pool-accessory"><div><strong>Pool Lights</strong><small id="pool-lights-state">Checking state…</small></div><button class="control-btn primary" id="pool-lights-toggle" type="button">Turn on</button></div></div>
      <div class="action-message" id="pool-mode-action-message"></div>
    </section>
    <section class="home-card door">
      <div class="card-head"><div class="card-title"><span class="icon">⌂</span><div><h3>Front door</h3><div class="sub">Yale lock</div></div></div><span class="pill" id="door-status">Loading</span></div>
      <div class="door-state"><div class="lock" id="door-icon">◌</div><div><strong id="door-state">—</strong><span id="door-message">Waiting for Home Assistant</span></div></div>
      <div class="door-actions"><button class="control-btn" id="door-lock" type="button">Lock</button><button class="control-btn" id="door-unlock" type="button">Unlock</button></div>
      <div class="action-message" id="door-action-message"></div>
    </section>
    <section class="home-card solar">
      <div class="card-head"><div class="card-title"><span class="icon">☀</span><div><h3>Solar</h3><div class="sub">Enphase energy</div></div></div><span class="pill" id="solar-status">Loading</span></div>
      <div class="solar-layout"><div><div class="hero-value" id="solar-power">—<small> kW</small></div><div class="sub" id="solar-message">Current production</div>
        <a class="control-btn primary solar-auth" id="solar-auth" href="/api/home/enphase/authorize">Connect Enphase</a>
        <div class="detail-row"><div class="detail"><small>Produced today</small><strong id="solar-today">—</strong></div><div class="detail"><small>Source</small><strong id="solar-source">—</strong></div></div></div>
        <div class="solar-chart-wrap"><div class="solar-chart-head"><strong>Today by hour</strong><div class="solar-legend"><span><i></i>Produced</span><span class="use"><i></i>Used</span></div></div><div id="solar-chart" class="solar-chart-empty">Loading interval history…</div></div>
      </div>
    </section>
    <section class="home-card cars-card">
      <div class="card-head"><div class="card-title"><span class="icon">◇</span><div><h3>Cars</h3><div class="sub">Charge and connection status</div></div></div></div>
      <div class="cars-grid" id="cars-grid"><div class="car loading">Loading vehicle status</div><div class="car loading">Loading vehicle status</div></div>
    </section>
  </div>
</main>
<script>
const esc=v=>String(v??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
const setPill=(id,text,kind='')=>{const el=document.getElementById(id);el.textContent=text;el.className='pill '+kind};
const value=(v,fallback='—')=>v===null||v===undefined||v===''?fallback:v;
let latestHome=null;
const controlDeck=document.querySelector('.home-grid');const controlCards=[...controlDeck.querySelectorAll('.home-card')];const deckDots=[...document.querySelectorAll('.deck-dot')];
function setDeckPage(index){deckDots.forEach((dot,i)=>dot.classList.toggle('active',i===index))}
deckDots.forEach((dot,index)=>dot.onclick=()=>{const card=controlCards[index];controlDeck.scrollTo({left:card.offsetLeft-controlDeck.offsetLeft-2,behavior:'smooth'});setDeckPage(index)});
let deckTick=0;controlDeck.addEventListener('scroll',()=>{cancelAnimationFrame(deckTick);deckTick=requestAnimationFrame(()=>{const center=controlDeck.scrollLeft+controlDeck.clientWidth/2;let closest=0,distance=Infinity;controlCards.forEach((card,index)=>{const delta=Math.abs(card.offsetLeft+card.offsetWidth/2-center);if(delta<distance){distance=delta;closest=index}});setDeckPage(closest)})},{passive:true});
let deckDrag=null,suppressDeckClick=false;
controlDeck.addEventListener('pointerdown',event=>{if(event.button!==0)return;deckDrag={id:event.pointerId,x:event.clientX,y:event.clientY,left:controlDeck.scrollLeft,start:Math.max(0,deckDots.findIndex(dot=>dot.classList.contains('active'))),moved:false}});
controlDeck.addEventListener('pointermove',event=>{if(!deckDrag||event.pointerId!==deckDrag.id)return;const dx=event.clientX-deckDrag.x,dy=event.clientY-deckDrag.y;if(!deckDrag.moved){if(Math.max(Math.abs(dx),Math.abs(dy))<7)return;if(Math.abs(dy)>=Math.abs(dx)){deckDrag=null;return}deckDrag.moved=true;controlDeck.classList.add('dragging');controlDeck.setPointerCapture(event.pointerId)}event.preventDefault();controlDeck.scrollLeft=deckDrag.left-dx});
function finishDeckDrag(event){if(!deckDrag||event.pointerId!==deckDrag.id)return;const drag=deckDrag,moved=drag.moved,dx=event.clientX-drag.x;deckDrag=null;controlDeck.classList.remove('dragging');if(moved){suppressDeckClick=true;setTimeout(()=>{suppressDeckClick=false},0);let closest=drag.start;if(Math.abs(dx)>55)closest=Math.max(0,Math.min(controlCards.length-1,drag.start+(dx<0?1:-1)));else{const center=controlDeck.scrollLeft+controlDeck.clientWidth/2,distanceByCard=controlCards.map(card=>Math.abs(card.offsetLeft+card.offsetWidth/2-center));closest=distanceByCard.indexOf(Math.min(...distanceByCard))}const card=controlCards[closest];controlDeck.scrollTo({left:card.offsetLeft-controlDeck.offsetLeft-2,behavior:'smooth'});setDeckPage(closest)}}
controlDeck.addEventListener('pointerup',finishDeckDrag);controlDeck.addEventListener('pointercancel',finishDeckDrag);controlDeck.addEventListener('click',event=>{if(!suppressDeckClick)return;suppressDeckClick=false;event.preventDefault();event.stopPropagation()},{capture:true});
function renderSolarChart(intervals,message){
  const chart=document.getElementById('solar-chart');const today=new Date();const hours=Array.from({length:24},(_,hour)=>({hour,production:0,consumption:0,seen:false}));
  (intervals||[]).forEach(row=>{const date=new Date(Number(row.end_at)*1000);if(date.toDateString()!==today.toDateString())return;const item=hours[date.getHours()];if(row.production_wh!=null){item.production+=Number(row.production_wh)||0;item.seen=true}if(row.consumption_wh!=null){item.consumption+=Number(row.consumption_wh)||0;item.seen=true}});
  if(!hours.some(item=>item.seen)){chart.className='solar-chart-empty';chart.textContent=message||'Interval history will appear after the next Enphase refresh.';return}
  const max=Math.max(1,...hours.flatMap(item=>[item.production,item.consumption]));chart.className='solar-chart';chart.innerHTML=hours.map(item=>{const p=Math.max(0,item.production/max*100),c=Math.max(0,item.consumption/max*100),label=item.hour%4===0?String(item.hour).padStart(2,'0')+':00':'';return '<div class="solar-hour" title="'+String(item.hour).padStart(2,'0')+':00 · Produced '+(item.production/1000).toFixed(2)+' kWh · Used '+(item.consumption/1000).toFixed(2)+' kWh"><span class="solar-bar" style="height:'+p+'%"></span><span class="solar-bar use" style="height:'+c+'%"></span>'+(label?'<small>'+label+'</small>':'')+'</div>'}).join('');
}
function renderDoor(door){
  const state=String(door.state||'unavailable').toLowerCase(),lockButton=document.getElementById('door-lock'),unlockButton=document.getElementById('door-unlock');lockButton.classList.toggle('active',state==='locked');unlockButton.classList.toggle('active',state==='unlocked');
  if(door.connected){const locked=state==='locked',unlocked=state==='unlocked',transitioning=state==='locking'||state==='unlocking';setPill('door-status',state,locked?'ok':transitioning?'warn':unlocked?'warn':'bad');document.getElementById('door-icon').textContent=locked?'●':unlocked?'○':'◌';document.getElementById('door-state').textContent=state;document.getElementById('door-message').textContent=locked?'Secure':unlocked?'Unlocked':transitioning?'Lock is moving':'Check the front door'}
  else if(door.configured){setPill('door-status','Unavailable','bad');document.getElementById('door-icon').textContent='◌';document.getElementById('door-state').textContent='Unavailable';document.getElementById('door-message').textContent=(door.entity_id||'Front-door entity')+' was not returned by Home Assistant'}
  else{setPill('door-status','Setup needed','warn');document.getElementById('door-icon').textContent='◌';document.getElementById('door-state').textContent='Not connected';document.getElementById('door-message').textContent='Choose the Yale lock entity in .env'}
}
function renderHome(data){
  latestHome=data;
  document.getElementById('home-updated').textContent='Updated '+new Date(data.generated_at).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});
  const ha=data.home_assistant||{};setPill('ha-status',ha.connected?'Home Assistant online':'Home Assistant setup needed',ha.connected?'ok':'warn');
  const pool=data.pool||{};
  if(pool.connected){
    setPill('pool-status',pool.online?'Online':'Offline',pool.online?'ok':'bad');
    document.getElementById('pool-temperature').innerHTML=esc(value(pool.temperature))+'<small> '+esc(pool.unit||'°F')+'</small>';
    document.getElementById('pool-message').textContent=pool.name||'PoolSync heater';
    document.getElementById('pool-mode').textContent=pool.mode||'—';
    document.getElementById('pool-setpoint').textContent=pool.setpoint==null?'—':pool.setpoint+' '+(pool.unit||'°F');
    document.getElementById('pool-target').textContent=pool.setpoint==null?'— °F':pool.setpoint+' '+(pool.unit||'°F');
  }else{
    setPill('pool-status',pool.configured?'Unavailable':'Setup needed',pool.configured?'bad':'warn');
    document.getElementById('pool-message').textContent=pool.message||'PoolSync is not configured';
  }
  const poolMode=data.pool_mode||{};const poolButton=document.getElementById('pool-mode-pool');const spaButton=document.getElementById('pool-mode-spa');const deckButton=document.getElementById('deck-jets-toggle'),lightsButton=document.getElementById('pool-lights-toggle');
  poolButton.classList.toggle('active',poolMode.mode==='pool');spaButton.classList.toggle('active',poolMode.mode==='spa');
  if(poolMode.configured){const modeLabel=poolMode.mode==='pool'?'Pool Mode':poolMode.mode==='spa'?'Spa Mode':poolMode.mode==='mixed'?'Custom valve state':'Unavailable';document.getElementById('pool-mode-current').textContent=modeLabel;setPill('pool-mode-status',poolMode.connected?'Connected':'Unavailable',poolMode.connected?'ok':'bad')}
  else{document.getElementById('pool-mode-current').textContent='Setup needed';setPill('pool-mode-status','Setup needed','warn')}
  const deckOn=poolMode.deck_jets==='on';document.getElementById('deck-jets-state').textContent=deckOn?'On':poolMode.deck_jets==='off'?'Off':'Unavailable';deckButton.textContent=deckOn?'Turn off':'Turn on';deckButton.className='control-btn '+(deckOn?'danger':'primary');
  const lightsOn=poolMode.pool_lights==='on';document.getElementById('pool-lights-state').textContent=lightsOn?'On':poolMode.pool_lights==='off'?'Off':'Unavailable';lightsButton.textContent=lightsOn?'Turn off':'Turn on';lightsButton.className='control-btn '+(lightsOn?'danger':'primary');
  renderDoor(data.front_door||{});
  const solar=data.solar||{};
  const solarAuth=document.getElementById('solar-auth');solarAuth.style.display=solar.authorization_required?'inline-flex':'none';
  document.getElementById('solar-source').textContent=solar.source||'—';
  document.getElementById('solar-message').textContent=solar.message||(solar.cached?'Updated every 30 minutes':'Current production');
  renderSolarChart(solar.intervals,solar.chart_message);
  if(solar.configured){setPill('solar-status',solar.connected?'Online':(solar.authorization_required?'Authorize':'Unavailable'),solar.connected?'ok':'warn');document.getElementById('solar-power').innerHTML=esc(value(solar.power))+'<small> '+esc(solar.power_unit||'kW')+'</small>';document.getElementById('solar-today').textContent=value(solar.today)+' '+(solar.today_unit||'kWh')}
  else{setPill('solar-status','Setup needed','warn');document.getElementById('solar-power').innerHTML='—<small> kW</small>';document.getElementById('solar-today').textContent='Add Enphase credentials in .env'}
  document.getElementById('cars-grid').innerHTML=(data.cars||[]).map(car=>{const charge=car.charge==null?0:Math.max(0,Math.min(100,car.charge));const charging=['on','charging','true'].includes(String(car.charging||'').toLowerCase());const status=car.configured?(charging?'Charging':'Connected'):'Setup needed';const range=car.range==null?'—':esc(car.range),lockState=car.lock_state==='locked'?'locked':car.lock_state==='unlocked'?'unlocked':'unavailable',lockIcon=lockState==='locked'?'🔒':lockState==='unlocked'?'🔓':'–',lockLabel=lockState==='locked'?'Locked':lockState==='unlocked'?'Unlocked':'Lock unavailable';return '<article class="car"><div class="car-top"><h4>'+esc(car.name)+'</h4><div class="vehicle-badges"><span class="pill '+(car.configured?'ok':'warn')+'">'+esc(status)+'</span><span class="vehicle-lock '+lockState+'" role="img" aria-label="'+lockLabel+'" title="'+lockLabel+'">'+lockIcon+'</span></div></div><img class="car-image" src="'+esc(car.image)+'" alt="'+esc(car.name)+'"><div class="car-metrics"><div class="car-metric"><small>Charge</small><strong>'+(car.charge==null?'—':esc(car.charge)+'%')+'</strong></div><div class="car-metric"><small>Range</small><strong>'+range+' <span>'+esc(car.range_unit||'mi')+'</span></strong></div></div><div class="battery"><span style="width:'+charge+'%"></span></div></article>'}).join('');
}
async function refreshHome(){try{const response=await fetch('/api/home/status',{cache:'no-store'});const data=await response.json();renderHome(data);return data}catch(error){setPill('ha-status','Home status unavailable','bad');return null}}
async function sendControl(url,payload,messageId){
  const message=document.getElementById(messageId);message.className='action-message';message.textContent='Applying…';document.querySelectorAll('.control-btn').forEach(button=>button.disabled=true);
  try{const response=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});const result=await response.json();if(!result.ok)throw new Error(result.error||'Control request failed');message.textContent='Updated';await refreshHome()}
  catch(error){message.className='action-message bad';message.textContent=error.message}
  finally{document.querySelectorAll('.control-btn').forEach(button=>button.disabled=false)}
}
async function sendDoorControl(action){
  const message=document.getElementById('door-action-message'),previous={...(latestHome?.front_door||{})},target=action==='lock'?'locked':'unlocked';message.className='action-message';message.textContent='Updating…';setPill('door-status','Updating','warn');document.querySelectorAll('.control-btn').forEach(button=>button.disabled=true);
  try{const response=await fetch('/api/home/front-door',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action})});const result=await response.json();if(!result.ok)throw new Error(result.error||'Door control request failed');for(let attempt=0;attempt<20;attempt++){await new Promise(resolve=>setTimeout(resolve,750));const data=await refreshHome();if(data?.front_door?.state===target){message.textContent='Updated';return}}message.className='action-message bad';message.textContent='Command sent, but Home Assistant has not confirmed '+target}
  catch(error){message.className='action-message bad';message.textContent=error.message;renderDoor(previous)}
  finally{document.querySelectorAll('.control-btn').forEach(button=>button.disabled=false)}
}
document.getElementById('pool-down').onclick=()=>sendControl('/api/home/pool',{action:'adjust_temperature',value:-1},'pool-action-message');
document.getElementById('pool-up').onclick=()=>sendControl('/api/home/pool',{action:'adjust_temperature',value:1},'pool-action-message');
document.getElementById('pool-heat').onclick=()=>sendControl('/api/home/pool',{action:'set_mode',value:'heat'},'pool-action-message');
document.getElementById('pool-off').onclick=()=>sendControl('/api/home/pool',{action:'set_mode',value:'off'},'pool-action-message');
document.getElementById('pool-mode-pool').onclick=()=>sendControl('/api/home/pool-mode',{action:'set_mode',value:'pool'},'pool-mode-action-message');
document.getElementById('pool-mode-spa').onclick=()=>sendControl('/api/home/pool-mode',{action:'set_mode',value:'spa'},'pool-mode-action-message');
document.getElementById('deck-jets-toggle').onclick=()=>{const state=latestHome?.pool_mode?.deck_jets==='on'?'off':'on';sendControl('/api/home/pool-mode',{action:'set_deck_jets',value:state},'pool-mode-action-message')};
document.getElementById('pool-lights-toggle').onclick=()=>{const state=latestHome?.pool_mode?.pool_lights==='on'?'off':'on';sendControl('/api/home/pool-mode',{action:'set_pool_lights',value:state},'pool-mode-action-message')};
document.getElementById('door-lock').onclick=()=>sendDoorControl('lock');
document.getElementById('door-unlock').onclick=()=>sendDoorControl('unlock');
refreshHome();setInterval(refreshHome,30000);
</script></body></html>"""
    return finalize_page_html(html, "/home")


def app_html() -> bytes:
    html = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Stock Strategy App</title><style>
:root{--bg:#f4f6f8;--panel:#fff;--text:#17202a;--muted:#687386;--line:#dce3ec;--green:#16803c;--blue:#1d4ed8}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
header{padding:0 20px;background:var(--panel);border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;gap:16px;position:sticky;top:0;z-index:3;height:52px}
.hdr-left h1{font-size:15px;font-weight:700;margin:0}
.hdr-nav{display:flex;align-items:center;gap:2px;flex:1;padding:0 8px}
.hdr-nav a{padding:6px 13px;border-radius:7px;font-size:13px;font-weight:600;color:var(--muted);text-decoration:none}
.hdr-nav a:hover{background:#f1f3f5;color:var(--text)}.hdr-nav a.active{background:#eff6ff;color:var(--blue)}
.hdr-right{display:flex;gap:8px;align-items:center}
.badge{border:1px solid var(--line);border-radius:999px;padding:5px 11px;font-size:12px;font-weight:600;background:#fff;color:var(--muted)}
.badge.open{background:#f0fdf4;border-color:#86efac;color:var(--green)}.badge.session{background:#eff6ff;border-color:#93c5fd;color:#1e40af}
.clock-txt{font-size:12px;color:var(--muted);font-variant-numeric:tabular-nums}
main.dash-frame-main{position:relative;padding:0;height:auto}
main.dash-frame-main #dash-frame{position:absolute;inset:0;opacity:0;pointer-events:none;transition:opacity .2s ease}
main.dash-frame-main.dashboard-ready #dash-frame{position:static;opacity:1;pointer-events:auto}
.dashboard-skeleton{padding:38px 28px 64px;display:grid;gap:18px;opacity:1;transition:opacity .2s ease;background:var(--bg)}
.dashboard-ready .dashboard-skeleton{position:absolute;inset:0;opacity:0;visibility:hidden;pointer-events:none}
.sk-row{display:flex;justify-content:space-between;align-items:center;gap:18px}.sk-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.sk-panels{display:grid;grid-template-columns:1.65fr .75fr;gap:16px}
.sk{display:block;border-radius:8px;background:linear-gradient(90deg,#e5e9ef 25%,#f5f7fa 50%,#e5e9ef 75%);background-size:200% 100%;animation:skeleton-shimmer 1.4s ease-in-out infinite}
.sk-brand{width:240px;height:38px}.sk-status{width:170px;height:34px;border-radius:999px}.sk-hero{height:170px;border-radius:20px}.sk-card{height:92px;border-radius:14px}.sk-panel{height:300px;border-radius:18px}.sk-panel.short{height:300px}
@keyframes skeleton-shimmer{0%{background-position:200% 0}100%{background-position:-200% 0}}
@media(max-width:900px){.dashboard-skeleton{padding:24px 12px 48px}.sk-grid{grid-template-columns:repeat(2,1fr)}.sk-panels{grid-template-columns:1fr}.sk-status{display:none}}
@media(prefers-reduced-motion:reduce){.sk{animation:none}.dashboard-skeleton,#dash-frame{transition:none}}
</style></head><body>
<header>
  <div class="hdr-left"><h1>Stock Strategy App</h1></div>
  <nav class="hdr-nav">__HEADER_NAV__</nav>
  <div class="hdr-right"><div id="market-badge" class="badge">Market —</div><span id="clock" class="clock-txt">—</span></div>
</header>
<main class="dash-frame-main" id="dashboard-shell" aria-busy="true">
  <div class="dashboard-skeleton" id="dashboard-skeleton" aria-hidden="true">
    <div class="sk-row"><span class="sk sk-brand"></span><span class="sk sk-status"></span></div>
    <span class="sk sk-hero"></span>
    <div class="sk-grid"><span class="sk sk-card"></span><span class="sk sk-card"></span><span class="sk sk-card"></span><span class="sk sk-card"></span></div>
    <div class="sk-panels"><span class="sk sk-panel"></span><span class="sk sk-panel short"></span></div>
  </div>
  <iframe id="dash-frame" src="/dashboard" title="Strategy dashboard" scrolling="no"></iframe>
</main>
<script>
function fmtTime(iso){if(!iso)return '—';return new Date(iso).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})}
const dashFrame=document.getElementById('dash-frame');
const dashboardShell=document.getElementById('dashboard-shell');
function showDashboardSkeleton(){
  dashboardShell.classList.remove('dashboard-ready');
  dashboardShell.setAttribute('aria-busy','true');
}
function sizeDashboardFrame(){
  try{
    const doc=dashFrame.contentDocument;
    if(!doc?.body)return;
    doc.documentElement.style.overflow='hidden';
    doc.body.style.overflow='hidden';
    dashFrame.style.height='1px';
    const height=Math.max(doc.documentElement.scrollHeight,doc.body.scrollHeight);
    dashFrame.style.height=`${height}px`;
    dashFrame.contentWindow.addEventListener('beforeunload',showDashboardSkeleton,{once:true});
    requestAnimationFrame(()=>{
      dashboardShell.classList.add('dashboard-ready');
      dashboardShell.setAttribute('aria-busy','false');
    });
  }catch(e){}
}
dashFrame.addEventListener('load',sizeDashboardFrame);
window.addEventListener('resize',()=>requestAnimationFrame(sizeDashboardFrame));
async function refresh(){
  try{
    const s=await fetch('/api/status').then(r=>r.json());
    document.getElementById('clock').textContent=fmtTime(s.server_time);
    const mb=document.getElementById('market-badge');
    if(s.market_open_now){mb.textContent='Market open';mb.className='badge open'}
    else if(s.market_session){mb.textContent='Market session';mb.className='badge session'}
    else{mb.textContent='Market closed';mb.className='badge'}
  }catch(e){}
}
refresh();setInterval(refresh,30000);
</script></body></html>"""
    return finalize_page_html(html, "/")


def jobs_html() -> bytes:
    html = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Scheduled Jobs</title><style>
:root{--bg:#f4f6f8;--panel:#fff;--text:#17202a;--muted:#687386;--line:#dce3ec;--blue:#1d4ed8;--green:#16803c;--red:#b42318}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
header{padding:0 20px;background:var(--panel);border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;gap:16px;position:sticky;top:0;z-index:2;height:52px}
.hdr-left{display:flex;align-items:center;flex-shrink:0}.hdr-left h1{font-size:15px;font-weight:700;margin:0;letter-spacing:-.01em}
.hdr-nav{display:flex;align-items:center;gap:2px;flex:1;padding:0 8px}
.hdr-nav a{padding:6px 13px;border-radius:7px;font-size:13px;font-weight:600;color:var(--muted);text-decoration:none;transition:background .15s,color .15s;white-space:nowrap}
.hdr-nav a:hover{background:#f1f3f5;color:var(--text)}
.hdr-nav a.active{background:#eff6ff;color:var(--blue)}
.hdr-right{display:flex;gap:12px;align-items:center}
.badge{border:1px solid var(--line);border-radius:999px;padding:5px 11px;font-size:12px;font-weight:600;background:#fff;color:var(--muted)}
.badge.open{background:#f0fdf4;border-color:#86efac;color:var(--green)}.badge.session{background:#eff6ff;border-color:#93c5fd;color:#1e40af}
main{padding:16px;display:grid;gap:14px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px;overflow:auto}
.panel-hdr{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:14px}
.panel-hdr h2{font-size:15px;font-weight:700;margin:0}.panel-hdr .sub{font-size:12px;color:var(--muted)}
table{width:100%;border-collapse:collapse}
th,td{padding:10px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:middle}
th{font-size:11px;text-transform:uppercase;color:var(--muted);letter-spacing:.04em;font-weight:600}
tbody tr:last-child td{border-bottom:0}
button{border:1px solid #bfcee3;background:#f8fbff;color:#183b75;border-radius:7px;padding:7px 11px;font-weight:600;cursor:pointer;white-space:nowrap;font-size:13px;transition:border-color .15s}
button:hover:not(:disabled){border-color:#7da2da}button:disabled{opacity:.5;cursor:not-allowed}
.jb{display:inline-block;font-size:11px;font-weight:700;padding:2px 8px;border-radius:999px;white-space:nowrap}
.jb.ok{background:#f0fdf4;color:var(--green);border:1px solid #86efac}
.jb.bad{background:#fef2f2;color:var(--red);border:1px solid #fca5a5}
.jb.none{background:#f9fafb;color:var(--muted);border:1px solid var(--line)}
.ts{color:var(--muted);font-size:12px;font-variant-numeric:tabular-nums}
.job-desc{font-size:12px;color:var(--muted);margin-top:2px}
.run-indicator{display:inline-flex;align-items:center;gap:6px;padding:4px 10px;background:#eff6ff;border:1px solid #93c5fd;border-radius:999px;font-size:12px;font-weight:600;color:#1e40af}
.run-indicator.hidden{display:none}
@keyframes spin{to{transform:rotate(360deg)}}.spinner{width:11px;height:11px;border:2px solid #93c5fd;border-top-color:#1d4ed8;border-radius:50%;animation:spin .7s linear infinite}
.log{white-space:pre-wrap;max-height:240px;overflow:auto;font:12px ui-monospace,SFMono-Regular,Menlo,monospace;background:#0f172a;color:#e2e8f0;border-radius:7px;padding:10px;line-height:1.5}
a{color:var(--blue)}
</style></head><body>
<header>
  <div class="hdr-left"><h1>Stock Strategy App</h1></div>
  <nav class="hdr-nav">__HEADER_NAV__</nav>
  <div class="hdr-right">
    <div id="market-badge" class="badge">Market —</div>
    <div id="run-indicator" class="run-indicator hidden"><span class="spinner"></span><span id="run-label">Running…</span></div>
  </div>
</header>
<main>
<section class="panel">
  <div class="panel-hdr"><h2>Jobs</h2><span class="sub" id="server-time"></span></div>
  <table><thead><tr><th>Job</th><th>Schedule</th><th>Next run</th><th>Last status</th><th>Last run</th><th></th></tr></thead><tbody id="jobs"></tbody></table>
</section>
<section class="panel">
  <div class="panel-hdr"><h2>Run history</h2><span class="sub">Most recent 80 runs</span></div>
  <table><thead><tr><th>Finished</th><th>Job</th><th>Reason</th><th>Status</th><th>Return code</th></tr></thead><tbody id="history"></tbody></table>
</section>
<section class="panel">
  <div class="panel-hdr"><h2>Last output</h2><span class="sub" id="log-src"></span></div>
  <div class="log" id="log">No output yet.</div>
</section>
</main>
<script>
function fmtTime(iso){if(!iso)return '—';const d=new Date(iso);const t=d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});return d.toDateString()===new Date().toDateString()?t:d.toLocaleDateString([],{month:'short',day:'numeric'})+' '+t}
function fmtNext(iso){if(!iso)return '—';const d=new Date(iso);const diff=d-Date.now();if(diff<0)return 'overdue';if(diff<60000)return 'in <1 min';if(diff<3600000)return 'in '+Math.round(diff/60000)+'m';return fmtTime(iso)}
async function loadJobs(){const r=await fetch('/api/jobs');return r.json()}
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function jb(run){if(!run)return '<span class="jb none">not run</span>';return run.ok?'<span class="jb ok">OK</span>':'<span class="jb bad">Failed</span>'}
function render(data){
  document.getElementById('server-time').textContent=fmtTime(data.server_time);
  const mb=document.getElementById('market-badge');
  if(data.market_open_now){mb.textContent='Market open';mb.className='badge open'}
  else if(data.market_session){mb.textContent='Market session';mb.className='badge session'}
  else{mb.textContent='Market closed';mb.className='badge'}
  const ri=document.getElementById('run-indicator');
  if(data.running){ri.className='run-indicator';document.getElementById('run-label').textContent='Running: '+data.running}
  else{ri.className='run-indicator hidden'}
  const dis=data.running?'disabled':'';
  const MANUAL_SKIP=new Set(['dashboard']);
  document.getElementById('jobs').innerHTML=Object.entries(data.jobs).map(([name,job])=>`<tr>
    <td><b>${esc(name.replace(/_/g,' '))}</b><div class="job-desc">${esc(job.description)}</div></td>
    <td class="ts">${esc(job.schedule_time||'manual')}</td>
    <td class="ts" title="${esc(job.next_run||'')}">${fmtNext(job.next_run)}</td>
    <td>${jb(job.last_run)}</td>
    <td class="ts" title="${esc(job.last_run?.finished_at||'')}">${fmtTime(job.last_run?.finished_at)}</td>
    <td>${MANUAL_SKIP.has(name)?'<span class="ts">auto</span>':`<button data-job="${esc(name)}" ${dis}>Run now</button>`}</td>
  </tr>`).join('');
  document.getElementById('history').innerHTML=data.history.map(run=>`<tr>
    <td class="ts" title="${esc(run.finished_at)}">${fmtTime(run.finished_at)}</td>
    <td>${esc(run.job_name.replace(/_/g,' '))}</td>
    <td class="ts">${esc(run.reason)}</td>
    <td>${jb(run)}</td>
    <td class="ts">${esc(run.returncode??'')}</td>
  </tr>`).join('');
  const latest=data.history[0];
  document.getElementById('log').textContent=latest?.output_tail||'No output yet.';
  document.getElementById('log-src').textContent=latest?latest.job_name.replace(/_/g,' ')+' \xb7 '+fmtTime(latest.finished_at):'';
  document.querySelectorAll('button[data-job]').forEach(btn=>btn.onclick=async()=>{btn.disabled=true;await fetch('/api/run/'+btn.dataset.job,{method:'POST'});setTimeout(refresh,500)});
}
async function refresh(){render(await loadJobs())}
refresh();setInterval(refresh,30000);
</script></body></html>"""
    return finalize_page_html(html, "/jobs")


def _saved_scanner_preview() -> tuple[list[str], list[dict], str]:
    from stock_storage import read_latest_snapshot, read_snapshot

    today = today_key()
    frame = read_snapshot("morning_candidates", "scan_date", today)
    source = f"postgresql:morning_candidates/{today}"
    if frame.empty:
        saved_date, frame = read_latest_snapshot("morning_candidates", "scan_date")
        if frame.empty:
            return [], [], "none"
        source = f"postgresql:morning_candidates/{saved_date}"
    try:
        columns, rows = preview_rows(frame, limit=50)
        return columns, rows, source
    except Exception as exc:
        return [], [{"error": f"Could not read morning candidates: {exc}"}], "saved"


def _run_scanner_preview_worker() -> None:
    try:
        _, candidates, stats = build_morning_candidates(include_earnings=True)
        columns, rows = preview_rows(candidates, limit=50)
        result = {
            "ok": True,
            "read_only": True,
            "preview_source": "live",
            "preview_columns": columns,
            "preview": rows,
            "stats": stats,
        }
        with STATE.lock:
            STATE.scanner_preview_result = result
            STATE.scanner_preview_error = None
        append_log(f"Scanner preview complete — {stats['scored_count']} candidates ({stats['coverage_pct']}% coverage)")
    except Exception as exc:
        with STATE.lock:
            STATE.scanner_preview_result = None
            STATE.scanner_preview_error = str(exc)
        append_log(f"Scanner preview failed: {exc}")
    finally:
        with STATE.lock:
            STATE.scanner_preview_running = False


def start_scanner_preview() -> dict:
    with STATE.lock:
        if STATE.scanner_preview_running:
            return {"ok": False, "error": "Preview scan already running"}
        if STATE.running:
            return {"ok": False, "error": f"Scheduled job already running: {STATE.running}"}
        STATE.scanner_preview_running = True
        STATE.scanner_preview_error = None
    thread = threading.Thread(target=_run_scanner_preview_worker, daemon=True)
    thread.start()
    append_log("Scanner preview started (read-only)")
    return {"ok": True, "started": True, "read_only": True}


def scanner_confirmation_status(
    columns: list[str],
    rows: list[dict],
    confirmations,
) -> tuple[list[str], list[dict]]:
    """Join morning scanner rows to their same-day 9:45 confirmation result."""
    status_column = "Confirmation status"
    output_columns = list(columns)
    if status_column not in output_columns:
        output_columns.append(status_column)

    confirmation_by_ticker: dict[str, dict] = {}
    if confirmations is not None and not confirmations.empty and "ticker" in confirmations.columns:
        for record in confirmations.fillna("").to_dict(orient="records"):
            ticker = str(record.get("ticker") or "").strip().upper()
            if ticker:
                confirmation_by_ticker[ticker] = record
    confirmation_finished = bool(confirmation_by_ticker)

    confirmation_threshold = int(
        load_active_settings().get("selection", {}).get("confirmation_buy_min_score", 40)
    )
    output_rows = []
    for row in rows:
        output = dict(row)
        ticker = str(row.get("Ticker") or row.get("ticker") or "").strip().upper()
        confirmation = confirmation_by_ticker.get(ticker)
        if confirmation is None:
            output[status_column] = "Not checked" if confirmation_finished else "Pending"
        else:
            try:
                score = int(float(confirmation.get("score") or 0))
            except (TypeError, ValueError):
                score = 0
            band = str(confirmation.get("confirmation_band") or "").strip()
            detail = " · ".join(value for value in (band, str(score)) if value)
            label = "Confirmed" if score >= confirmation_threshold else "Below threshold"
            output[status_column] = f"{label} · {detail}" if detail else label
        output_rows.append(output)
    return output_columns, output_rows


def scanner_payload() -> dict:
    with STATE.lock:
        preview_running = STATE.scanner_preview_running
        preview_result = STATE.scanner_preview_result
        preview_error = STATE.scanner_preview_error

    if preview_result:
        preview_columns = preview_result.get("preview_columns") or []
        preview = preview_result.get("preview") or []
        preview_source = preview_result.get("preview_source", "live")
        stats = preview_result.get("stats")
    else:
        preview_columns, preview, preview_source = _saved_scanner_preview()
        stats = None

    today = today_key()
    preview_date = (
        str(preview_source).rsplit("/", 1)[-1]
        if str(preview_source).startswith("postgresql:") and "/" in str(preview_source)
        else today
    )
    from stock_storage import read_snapshot
    confirmations = read_snapshot("confirmations", "confirm_date", preview_date)
    preview_columns, preview = scanner_confirmation_status(preview_columns, preview, confirmations)
    morning_html = WATCHLIST_EXPORT_DIR / f"morning_candidates_{today}.html"
    latest_html = CANDIDATES_FILE.with_suffix(".html")
    last = last_run("morning")
    return {
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "read_only": True,
        "preview_running": preview_running,
        "preview_error": preview_error if not preview_running else None,
        "preview_source": preview_source,
        "preview_date": preview_date,
        "preview_columns": preview_columns,
        "preview": preview,
        "stats": stats,
        "files": {
            "morning_html": file_info(morning_html if morning_html.exists() else latest_html),
            "today_html": file_info(morning_html),
        },
        "last_scheduled_run": last,
    }


def _render_preview_table(columns: list[str], rows: list[dict]) -> str:
    if columns and rows and "error" not in rows[0]:
        head = "".join(f"<th>{escape(str(c))}</th>" for c in columns)

        def cell(column: str, value: object) -> str:
            text = str(value)
            if column != "Confirmation status":
                return f"<td>{escape(text)}</td>"
            cls = (
                "confirmed" if text.startswith("Confirmed")
                else "below" if text.startswith("Below threshold")
                else "unchecked" if text.startswith("Not checked")
                else "pending"
            )
            return f"<td><span class='confirmation {cls}'>{escape(text)}</span></td>"

        body = "".join(
            "<tr>" + "".join(cell(c, row.get(c, "")) for c in columns) + "</tr>"
            for row in rows
        )
        return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
    if rows:
        return f"<p class='empty'>{escape(str(rows[0].get('error', 'No candidates yet.')))}</p>"
    return "<p class='empty'>Click <strong>Run preview scan</strong> for a live read-only view. Nothing is saved to the database or export files.</p>"


def scanner_html() -> bytes:
    payload = scanner_payload()
    files = payload["files"]
    html_link = files.get("morning_html") or {}
    last = payload.get("last_scheduled_run") or {}
    last_label = "OK" if last.get("ok") else ("Failed" if last else "Never")
    last_cls = "ok" if last.get("ok") else ("bad" if last else "muted")
    exports_html = "—"
    if html_link.get("exists"):
        name = html_link.get("path", "").split("/")[-1]
        exports_html = f'<a href="/exports/{escape(name)}">HTML report</a>'
    preview_cols = payload.get("preview_columns") or []
    preview_rows_data = payload.get("preview") or []
    preview_table = _render_preview_table(preview_cols, preview_rows_data)
    stats = payload.get("stats") or {}
    stats_text = ""
    if stats:
        stats_text = (
            f"Live preview · {stats.get('scored_count', 0)} scored from {stats.get('raw_count', 0)} Finviz rows "
            f"({stats.get('coverage_pct', 0)}% coverage)"
        )
    source_label = "Live preview" if payload.get("preview_source") == "live" else (
        "PostgreSQL" if str(payload.get("preview_source", "")).startswith("postgresql:") else "No data"
    )
    running_style = "" if payload.get("preview_running") else "display:none"

    html = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Scanner</title><style>
:root{{--bg:#f4f6f8;--panel:#fff;--text:#17202a;--muted:#687386;--line:#dce3ec;--blue:#1d4ed8;--green:#16803c;--red:#b42318;--amber:#92400e}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:14px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
header{{padding:0 20px;background:var(--panel);border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;gap:16px;position:sticky;top:0;z-index:2;height:52px}}
.hdr-left h1{{font-size:15px;font-weight:700;margin:0}}
.hdr-nav{{display:flex;align-items:center;gap:2px;flex:1;padding:0 8px}}
.hdr-nav a{{padding:6px 13px;border-radius:7px;font-size:13px;font-weight:600;color:var(--muted);text-decoration:none}}
.hdr-nav a:hover{{background:#f1f3f5;color:var(--text)}}.hdr-nav a.active{{background:#eff6ff;color:var(--blue)}}
.hdr-right{{font-size:12px;color:var(--muted)}}
main{{max-width:1200px;margin:0 auto;padding:24px 20px 64px;display:grid;gap:16px}}
.panel{{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:20px;box-shadow:0 1px 6px rgba(15,23,42,.04)}}
.page-title{{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;flex-wrap:wrap;margin-bottom:4px}}
.page-title h2{{margin:0 0 4px;font-size:22px}}
.sub{{color:var(--muted);font-size:13px;margin:0 0 14px}}
.btn-run{{border:1px solid #bfcee3;background:#f8fbff;color:#183b75;border-radius:8px;padding:10px 16px;font-weight:700;cursor:pointer;font-size:13px}}
.btn-run:disabled{{opacity:.55;cursor:not-allowed}}
.meta{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px;margin-bottom:14px}}
.meta div{{border:1px solid var(--line);border-radius:10px;padding:12px;background:#fbfcfe;font-size:13px}}
.meta small{{display:block;color:var(--muted);font-size:10px;font-weight:700;text-transform:uppercase;margin-bottom:6px}}
.ok{{color:var(--green);font-weight:700}}.bad{{color:var(--red);font-weight:700}}.muted{{color:var(--muted)}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th,td{{padding:9px 8px;border-bottom:1px solid var(--line);text-align:left}}
th{{color:var(--muted);font-size:11px;text-transform:uppercase}}
.empty{{color:var(--muted);padding:8px 0}}
.run-pill{{display:inline-block;background:#eff6ff;color:#1d4ed8;border:1px solid #93c5fd;border-radius:999px;padding:2px 8px;font-size:10px;font-weight:700;margin-left:8px}}
.note{{background:#f8fafc;border:1px solid var(--line);border-radius:10px;padding:12px 14px;font-size:13px;color:var(--muted)}}
.confirmation{{display:inline-block;border-radius:999px;padding:3px 8px;font-size:11px;font-weight:750;white-space:nowrap}}
.confirmation.confirmed{{background:#dcfce7;color:var(--green)}}.confirmation.below{{background:#fffbeb;color:var(--amber)}}
.confirmation.pending{{background:#eff6ff;color:var(--blue)}}.confirmation.unchecked{{background:#f1f5f9;color:var(--muted)}}
a{{color:var(--blue)}}
</style></head><body>
<header>
  <div class="hdr-left"><h1>Scanner</h1></div>
  <nav class="hdr-nav">__HEADER_NAV__</nav>
  <div class="hdr-right" id="server-time">{escape(payload['server_time'])}</div>
</header>
<main>
  <section class="panel">
    <div class="page-title">
      <div>
        <h2>Morning scanner + confirmation</h2>
        <p class="sub">Shows the saved 8:45 ET morning candidates alongside each ticker's 9:45 ET confirmation result. The preview button runs a fresh read-only scan without changing saved trading data.</p>
      </div>
      <button class="btn-run" id="run-scanner">Run preview scan</button>
    </div>
    <div class="meta">
      <div><small>Preview source</small><span id="preview-source">{escape(source_label)}</span></div>
      <div><small>Rows shown</small><span id="row-count">{len(preview_rows_data)}</span></div>
      <div><small>Confirmation</small><span>9:45 status for {escape(payload.get('preview_date') or today_key())}</span></div>
      <div><small>Last scheduled save</small><span class="{last_cls}">{escape(last_label)}</span></div>
      <div><small>Saved exports</small>{exports_html}</div>
    </div>
    <div id="run-indicator" class="sub" style="{running_style}"><span class="run-pill">RUNNING</span> Fetching Finviz and scoring candidates… (may take a few minutes)</div>
    <div id="preview-stats" class="note" style="{'display:block' if stats_text else 'display:none'}">{escape(stats_text)}</div>
    <div id="preview">{preview_table}</div>
  </section>
</main>
<script>
function esc(v){{return String(v??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}}
function previewCell(column,value){{
  const text=String(value??'');
  if(column!=='Confirmation status')return '<td>'+esc(text)+'</td>';
  const cls=text.startsWith('Confirmed')?'confirmed':text.startsWith('Below threshold')?'below':text.startsWith('Not checked')?'unchecked':'pending';
  return '<td><span class="confirmation '+cls+'">'+esc(text)+'</span></td>';
}}
function renderPreview(data){{
  const cols=data.preview_columns||[];
  const rows=data.preview||[];
  const target=document.getElementById('preview');
  document.getElementById('row-count').textContent=rows.length;
    document.getElementById('preview-source').textContent=
    data.preview_source==='live'?'Live preview':(String(data.preview_source||'').startsWith('postgresql:')?'PostgreSQL':'No data');
  const statsEl=document.getElementById('preview-stats');
  if(data.stats){{
    const s=data.stats;
    statsEl.style.display='block';
    statsEl.textContent=`Live preview · ${{s.scored_count||0}} scored from ${{s.raw_count||0}} Finviz rows (${{s.coverage_pct||0}}% coverage)`;
  }}else if(!data.preview_running){{statsEl.style.display='none';}}
  if(!cols.length||!rows.length){{target.innerHTML='<p class="empty">Click <strong>Run preview scan</strong> for a live read-only view.</p>';return;}}
  if(rows[0].error){{target.innerHTML='<p class="empty">'+rows[0].error+'</p>';return;}}
  const head=cols.map(c=>'<th>'+esc(c)+'</th>').join('');
  const body=rows.map(r=>'<tr>'+cols.map(c=>previewCell(c,r[c])).join('')+'</tr>').join('');
  target.innerHTML='<table><thead><tr>'+head+'</tr></thead><tbody>'+body+'</tbody></table>';
}}
async function loadScanner(){{
  const res=await fetch('/api/scanner',{{cache:'no-store'}});
  return res.json();
}}
let previewErrorShown=false;
async function refreshScanner(){{
  try{{
    const data=await loadScanner();
    document.getElementById('server-time').textContent=data.server_time;
    const indicator=document.getElementById('run-indicator');
    const btn=document.getElementById('run-scanner');
    if(data.preview_running){{indicator.style.display='block';btn.disabled=true;btn.textContent='Scanning…';}}
    else{{indicator.style.display='none';btn.disabled=false;btn.textContent='Run preview scan';}}
    if(data.preview_error&&!data.preview_running&&!previewErrorShown){{previewErrorShown=true;alert(data.preview_error);}}
    if(data.preview_source==='live'&&!data.preview_error){{previewErrorShown=false;}}
    renderPreview(data);
  }}catch(e){{}}
}}
document.getElementById('run-scanner').onclick=async()=>{{
  const btn=document.getElementById('run-scanner');
  btn.disabled=true;btn.textContent='Starting…';
  document.getElementById('run-indicator').style.display='block';
  try{{
    const res=await fetch('/api/scanner/preview',{{method:'POST'}});
    const data=await res.json();
    if(!data.ok){{alert(data.error||'Could not start preview scan');btn.disabled=false;btn.textContent='Run preview scan';document.getElementById('run-indicator').style.display='none';return;}}
    refreshScanner();
  }}catch(err){{alert('Request failed: '+err);btn.disabled=false;btn.textContent='Run preview scan';document.getElementById('run-indicator').style.display='none';}}
}};
refreshScanner();
setInterval(refreshScanner,3000);
</script></body></html>"""
    return finalize_page_html(html, "/scanner")


class Handler(SimpleHTTPRequestHandler):
    server_version = "StockStrategyApp/1.0"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PROJECT_DIR), **kwargs)

    def log_message(self, format: str, *args) -> None:
        append_log(f"HTTP {self.address_string()} {format % args}")

    def send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def enphase_redirect_uri(self) -> str:
        configured = os.getenv("ENPHASE_REDIRECT_URI", "").strip()
        if configured:
            return configured
        scheme = self.headers.get("X-Forwarded-Proto", "http").split(",", 1)[0].strip()
        host = self.headers.get("Host", "raspberrypi.local")
        return f"{scheme}://{host}/api/home/enphase/callback"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)
        if path == "/":
            body = app_html()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/home":
            body = home_controls_html()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/day":
            body = day_html(query.get("date", [today_key()])[0])
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/live-infographic":
            body = live_infographic_html(query.get("date", [None])[0])
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/jobs":
            body = jobs_html()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/scanner":
            body = scanner_html()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/settings":
            body = settings_html()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path in {"/healthcheck", "/health"}:
            body = healthcheck_html()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/strategy-review":
            body = strategy_review_page_html()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        elif path == "/dashboard":
            from dashboard import dashboard_http_html

            body = dashboard_http_html()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        elif path == "/api/status":
            self.send_json(status_payload())
            return
        elif path == "/api/home/status":
            self.send_json(home_controls_payload())
            return
        elif path == "/api/home/enphase/authorize":
            try:
                self.redirect(begin_authorization(self.enphase_redirect_uri()))
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.CONFLICT)
            return
        elif path == "/api/home/enphase/callback":
            if query.get("error"):
                self.redirect("/home?enphase=denied")
                return
            code = query.get("code", [""])[0]
            state = query.get("state", [""])[0]
            if not code or not state:
                self.send_json({"ok": False, "error": "Missing Enphase OAuth code or state"}, HTTPStatus.BAD_REQUEST)
                return
            try:
                complete_authorization(code, state)
                self.redirect("/home?enphase=connected")
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.CONFLICT)
            return
        elif path == "/api/jobs":
            self.send_json(jobs_payload())
            return
        elif path == "/api/day":
            self.send_json(day_payload(query.get("date", [today_key()])[0]))
            return
        elif path == "/api/healthcheck":
            self.send_json(health_payload())
            return
        elif path == "/api/scanner":
            self.send_json(scanner_payload())
            return
        elif path == "/api/settings":
            self.send_json(settings_payload())
            return
        elif path == "/api/strategy/best-case":
            result = compute_best_case()
            result.update(current_data_suggestions())
            self.send_json(result)
            return
        super().do_GET()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if path in {"/api/home/pool", "/api/home/pool-mode", "/api/home/front-door", "/api/home/ewelink"}:
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else {}
            except json.JSONDecodeError:
                self.send_json({"ok": False, "error": "Invalid JSON body"}, HTTPStatus.BAD_REQUEST)
                return
            if path == "/api/home/pool":
                result = poolsync_control(str(payload.get("action") or ""), payload.get("value"))
            elif path == "/api/home/pool-mode":
                result = home_assistant_pool_control(
                    str(payload.get("action") or ""), payload.get("value")
                )
            elif path == "/api/home/front-door":
                result = home_assistant_door_control(str(payload.get("action") or ""))
            else:
                try:
                    channel = int(payload.get("channel"))
                except (TypeError, ValueError):
                    self.send_json({"ok": False, "error": "Invalid eWeLink channel"}, HTTPStatus.BAD_REQUEST)
                    return
                state = str(payload.get("state") or "").lower()
                if state not in {"on", "off"}:
                    self.send_json({"ok": False, "error": "eWeLink state must be on or off"}, HTTPStatus.BAD_REQUEST)
                    return
                result = home_assistant_switch(channel, state == "on")
            status = HTTPStatus.OK if result.get("ok") else HTTPStatus.CONFLICT
            self.send_json(result, status)
            return
        if path.startswith("/api/run/"):
            name = path.rsplit("/", 1)[-1]
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except json.JSONDecodeError:
                self.send_json({"ok": False, "error": "Invalid JSON body"}, HTTPStatus.BAD_REQUEST)
                return
            reason = body.get("reason", "manual")
            if name == "live":
                result = run_live_update("live-update")
            else:
                result = run_job(name, reason)
                if name == "strategy_review" and result.get("ok"):
                    result["dashboard_refresh"] = run_job("dashboard", "strategy-review-refresh")
            status = HTTPStatus.OK if result.get("ok") or result.get("skipped") else HTTPStatus.CONFLICT
            self.send_json(result, status)
            return
        if path == "/api/bankroll/inject":
            event = add_bankroll_deposit(25000.0, "UI bankroll injection")
            dashboard_result = run_job("dashboard", "bankroll-injection")
            self.send_json({"ok": True, "event": event, "bankroll": {"base": bankroll_base(), "deposits": total_bankroll_deposits()}, "dashboard": dashboard_result})
            return
        if path == "/api/codex/chat":
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                self.send_json({"ok": False, "error": "Invalid JSON body"}, HTTPStatus.BAD_REQUEST)
                return
            message = payload.get("message", "")
            self.send_json(codex_chat(message))
            return
        if path == "/api/scanner/preview":
            self.send_json(start_scanner_preview())
            return
        self.send_json({"ok": False, "error": "Not found"}, HTTPStatus.NOT_FOUND)


def main() -> int:
    ensure_directories()
    wait_for_database()
    init_schema()
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    append_log(f"PostgreSQL ledger: {table_count('paper_trades')} trades @ {database_url().split('@')[-1]}")
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "80"))
    server = ThreadingHTTPServer((host, port), Handler)
    run_job("dashboard", "startup")
    if SCHEDULER_MODE == "internal":
        thread = threading.Thread(target=scheduler_loop, daemon=True)
        thread.start()
        append_log("Internal scheduler thread started")
    else:
        append_log(f"Scheduler mode={SCHEDULER_MODE} — job timing handled outside the app")
    append_log(f"Serving on http://{host}:{port}")
    print(f"Stock Strategy App running at http://{host}:{port}")
    print(f"Dashboard: http://{host}:{port}/dashboard")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
