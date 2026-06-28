"""Platform health, architecture metadata, and Codex probe helpers."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

from codex_context import codex_prompt_with_context, build_trading_context
from db import connect, database_url
from job_storage import job_health
from pipeline_health import health_snapshot
from scanner_config import LOGS_DIR, PROJECT_DIR, WATCHLIST_EXPORT_DIR
from stock_storage import bankroll_base, table_count, total_bankroll_deposits
from version import version_label

CODEX_BIN = os.getenv("CODEX_BIN", "codex")
CODEX_HOME = os.getenv("CODEX_HOME", str(LOGS_DIR / "codex")).strip()
CODEX_HOST_SEED = Path("/run/codex-host")
CODEX_TIMEOUT_SECONDS = int(os.getenv("CODEX_PROBE_TIMEOUT_SECONDS", "120"))
CODEX_HOST_URL = os.getenv("CODEX_HEALTH_URL", "").strip()
IMAGE_PYTHON = os.getenv("STOCK_IMAGE_PYTHON", os.getenv("PYTHON", "python3"))


def _status(ok: bool, detail: str = "", **extra) -> dict:
    return {"ok": ok, "status": "healthy" if ok else "unhealthy", "detail": detail, **extra}


def check_postgresql() -> dict:
    try:
        with connect(dict_rows=False) as (_, cursor):
            cursor.execute("SELECT 1")
            cursor.execute("SELECT version()")
            version = (cursor.fetchone()[0] or "").split(",")[0]
            cursor.execute("SELECT count(*) FROM paper_trades")
            trades = int(cursor.fetchone()[0])
        target = database_url().split("@")[-1]
        return _status(
            True,
            f"Connected · {trades} paper trades · {version}",
            trades=trades,
            target=target,
            engine="postgresql",
        )
    except Exception as exc:
        return _status(False, str(exc), target=database_url().split("@")[-1], engine="postgresql")


def check_postgres_data_source() -> dict:
    """Verify live app data is served from PostgreSQL."""
    pg_trades = table_count("paper_trades")
    pg_jobs = table_count("job_runs")
    pg_events = table_count("account_events")
    pg_reviews = table_count("strategy_reviews")
    pg_morning = table_count("morning_candidates")
    pg_confirm = table_count("confirmations")

    legacy_sqlite = (WATCHLIST_EXPORT_DIR / "stock_app.sqlite").exists() or (WATCHLIST_EXPORT_DIR / "app_server.sqlite").exists()
    legacy_csv = (PROJECT_DIR / "paper_trades.csv").exists()

    if pg_trades > 0:
        source = "postgresql"
        detail = (
            f"PostgreSQL ledger ({pg_trades} trades, {pg_jobs} job runs, "
            f"{pg_morning} morning rows, {pg_confirm} confirmations)"
        )
    else:
        source = "postgresql_empty"
        detail = "PostgreSQL connected but paper_trades is empty"

    warnings: list[str] = []
    if legacy_sqlite:
        warnings.append("Legacy SQLite files present — safe to delete after migration")
    if legacy_csv:
        warnings.append("Legacy paper_trades.csv present — no longer used at runtime")

    ok = pg_trades > 0 and source == "postgresql"
    return _status(
        ok,
        detail,
        active_source=source,
        postgres_trades=pg_trades,
        postgres_job_runs=pg_jobs,
        postgres_account_events=pg_events,
        postgres_strategy_reviews=pg_reviews,
        postgres_morning_candidates=pg_morning,
        postgres_confirmations=pg_confirm,
        legacy_sqlite=legacy_sqlite,
        legacy_csv=legacy_csv,
        bankroll_base=bankroll_base(),
        bankroll_deposits=total_bankroll_deposits(),
        warnings=warnings,
    )


def check_docker() -> dict:
    in_container = Path("/.dockerenv").exists()
    return _status(
        True,
        "Running inside Docker" if in_container else "Running on host Python",
        in_container=in_container,
    )


def check_app_process() -> dict:
    return _status(True, version_label(), port=os.getenv("PORT", "80"), host=os.getenv("HOST", "0.0.0.0"))


def check_exports_volume() -> dict:
    try:
        probe = WATCHLIST_EXPORT_DIR / ".health_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return _status(True, str(WATCHLIST_EXPORT_DIR), writable=True)
    except Exception as exc:
        return _status(False, str(exc), path=str(WATCHLIST_EXPORT_DIR))


def check_git_autodeploy() -> dict:
    log_path = LOGS_DIR / "pull_redeploy.log"
    if not log_path.exists():
        return _status(False, "No pull_redeploy.log yet — timer may not have run", path=str(log_path))
    text = log_path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
    last = text[-1] if text else ""
    try:
        mtime = datetime.fromtimestamp(log_path.stat().st_mtime).astimezone().isoformat(timespec="seconds")
    except OSError:
        mtime = ""
    ok = "Redeploy complete" in last or "Updating " not in last
    return _status(ok, last or "log empty", last_modified=mtime, path=str(log_path))


def check_scheduler(state: dict) -> dict:
    mode = os.getenv("STOCK_SCHEDULER", "internal").strip().lower()
    log_path = LOGS_DIR / "systemd_jobs.log"
    if mode == "systemd":
        detail = "systemd timers on Pi host trigger /api/run"
        if log_path.exists():
            lines = log_path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
            if lines:
                detail = f"{detail} · last: {lines[-1][-120:]}"
        return _status(
            True,
            detail,
            mode=mode,
            live_updates=state.get("live_updates", False),
            running_job=state.get("running"),
        )
    return _status(
        True,
        f"Internal loop · started {state.get('scheduler_started_at', 'unknown')}",
        mode=mode,
        live_updates=state.get("live_updates", False),
        poll_seconds=int(os.getenv("STOCK_CRON_POLL_SECONDS", "30")),
        running_job=state.get("running"),
    )


def _resolve_binary(path_str: str) -> Optional[str]:
    path = Path(path_str)
    if path.is_file():
        return str(path)
    return shutil.which(path_str)


def _codex_workspace() -> Path:
    workspace = Path(CODEX_HOME)
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "cache").mkdir(parents=True, exist_ok=True)
    (workspace / "data").mkdir(parents=True, exist_ok=True)
    if CODEX_HOST_SEED.is_dir():
        for name in ("auth.json", "config.toml", "installation_id"):
            src = CODEX_HOST_SEED / name
            dest = workspace / name
            if src.is_file() and not dest.exists():
                shutil.copy2(src, dest)
    return workspace


def _codex_env() -> dict[str, str]:
    workspace = _codex_workspace()
    env = os.environ.copy()
    env["HOME"] = str(workspace)
    env["CODEX_HOME"] = str(workspace)
    env["XDG_DATA_HOME"] = str(workspace / "data")
    env["XDG_CACHE_HOME"] = str(workspace / "cache")
    env["TMPDIR"] = str(LOGS_DIR / "codex-tmp")
    Path(env["TMPDIR"]).mkdir(parents=True, exist_ok=True)
    return env


def check_codex_cli() -> dict:
    binary = shutil.which(CODEX_BIN)
    if not binary and Path(CODEX_BIN).is_file():
        binary = CODEX_BIN
    if not binary:
        return _status(
            False,
            f"{CODEX_BIN} not found — set CODEX_HOST_BIN in .env and mount in docker-compose.pi.yml",
            hint="Pi default: /home/admin/.codex/packages/standalone/current/bin/codex",
        )
    try:
        completed = subprocess.run(
            [binary, "--version"],
            cwd=PROJECT_DIR,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=15,
            env=_codex_env(),
        )
        output = (completed.stdout or "").strip()
        return _status(
            completed.returncode == 0,
            output or f"exit {completed.returncode}",
            binary=binary,
        )
    except Exception as exc:
        return _status(False, str(exc), binary=binary)


def check_codex_host_url() -> dict:
    if not CODEX_HOST_URL:
        return _status(True, "Not configured (optional)", configured=False)
    try:
        import urllib.request

        with urllib.request.urlopen(CODEX_HOST_URL, timeout=5) as response:
            code = response.getcode()
        return _status(200 <= code < 400, f"HTTP {code}", url=CODEX_HOST_URL, configured=True)
    except Exception as exc:
        return _status(False, str(exc), url=CODEX_HOST_URL, configured=True)


def check_image_python() -> dict:
    binary = _resolve_binary(IMAGE_PYTHON) or _resolve_binary("python3")
    if not binary:
        return _status(False, f"Missing interpreter: {IMAGE_PYTHON}", path=IMAGE_PYTHON)
    try:
        completed = subprocess.run(
            [binary, "-c", "import PIL; print(PIL.__version__)"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=20,
        )
        output = (completed.stdout or "").strip()
        return _status(completed.returncode == 0, output or "Pillow unavailable", path=binary)
    except Exception as exc:
        return _status(False, str(exc), path=binary)


def check_market_data() -> dict:
    try:
        import yfinance as yf

        frame = yf.download("SPY", period="2d", interval="1d", auto_adjust=False, progress=False)
        if frame.empty:
            return _status(False, "Yahoo Finance returned no SPY data")
        return _status(True, "Yahoo Finance reachable (SPY quote)")
    except Exception as exc:
        return _status(False, str(exc))


def check_github() -> dict:
    host = "github.com"
    try:
        with socket.create_connection((host, 443), timeout=5):
            return _status(True, f"{host}:443 reachable")
    except Exception as exc:
        return _status(False, str(exc))


def architecture_nodes() -> list[dict]:
    return [
        {"id": "browser", "label": "Your PC browser", "layer": "client", "detail": "Dashboard, jobs, health UI"},
        {"id": "pi", "label": "Raspberry Pi", "layer": "edge", "detail": "Host network · port 80"},
        {"id": "docker", "label": "Docker · stock_app", "layer": "app", "detail": "app_server.py + job API"},
        {"id": "job_timers", "label": "systemd job timers", "layer": "app", "detail": "morning · confirm · report · live"},
        {"id": "postgres", "label": "PostgreSQL", "layer": "data", "detail": "Trades, jobs, pipeline snapshots, analytics"},
        {"id": "exports", "label": "HTML reports", "layer": "data", "detail": "Dashboard and daily HTML views"},
        {"id": "backups", "label": "Git DB backups", "layer": "data", "detail": "Nightly pg_dump → backups/"},
        {"id": "github", "label": "GitHub", "layer": "deploy", "detail": "ndadosky/stocktrading main"},
        {"id": "timer", "label": "systemd timer", "layer": "deploy", "detail": "git pull + rebuild every 5m"},
        {"id": "codex", "label": "Codex CLI", "layer": "analysis", "detail": "Flashcards, reviews, chat probe"},
        {"id": "finviz", "label": "Finviz + Yahoo", "layer": "external", "detail": "Universe + market data"},
    ]


def architecture_edges() -> list[dict]:
    return [
        {"from": "browser", "to": "pi", "label": "HTTP :80"},
        {"from": "pi", "to": "docker", "label": "host network"},
        {"from": "docker", "to": "postgres", "label": "127.0.0.1:5432"},
        {"from": "docker", "to": "exports", "label": "HTML bind mount"},
        {"from": "timer", "to": "backups", "label": "nightly pg_dump"},
        {"from": "docker", "to": "finviz", "label": "morning / confirm jobs"},
        {"from": "docker", "to": "codex", "label": "pnl_flashcard · chat"},
        {"from": "timer", "to": "github", "label": "git fetch/pull"},
        {"from": "timer", "to": "docker", "label": "compose rebuild"},
        {"from": "job_timers", "to": "docker", "label": "curl /api/run"},
        {"from": "github", "to": "pi", "label": "/home/admin/stock"},
    ]


def codex_chat(message: str) -> dict:
    message = (message or "").strip()
    if not message:
        return {"ok": False, "error": "Message is required"}
    if len(message) > 2000:
        return {"ok": False, "error": "Message too long (max 2000 characters)"}

    binary = shutil.which(CODEX_BIN) or (CODEX_BIN if Path(CODEX_BIN).is_file() else None)
    if not binary:
        return {
            "ok": False,
            "error": f"Codex CLI not available ({CODEX_BIN}). Mount it into the container or install on the host.",
        }

    started = time.time()
    prompt = codex_prompt_with_context(message)
    try:
        completed = subprocess.run(
            [
                binary,
                "exec",
                "--ephemeral",
                "--sandbox",
                "read-only",
                prompt,
            ],
            cwd=PROJECT_DIR,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=CODEX_TIMEOUT_SECONDS,
            env=_codex_env(),
        )
        duration_ms = int((time.time() - started) * 1000)
        response = (completed.stdout or "").strip()
        stderr = (completed.stderr or "").strip()
        warnings = [line for line in stderr.splitlines() if line.strip().startswith("WARNING:")]
        errors = [line for line in stderr.splitlines() if line.strip() and not line.strip().startswith("WARNING:")]
        if completed.returncode != 0 and not response:
            detail = "\n".join(errors) if errors else stderr
            return {
                "ok": False,
                "error": detail or f"Codex exited with code {completed.returncode}",
                "warnings": warnings,
                "duration_ms": duration_ms,
            }
        return {
            "ok": True,
            "response": response,
            "warnings": warnings,
            "stderr_tail": "\n".join(errors)[-2000:] if errors else "",
            "duration_ms": duration_ms,
            "binary": binary,
            "context_chars": len(build_trading_context()),
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"Codex timed out after {CODEX_TIMEOUT_SECONDS}s"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def platform_health_payload(app_state: dict, jobs: dict) -> dict:
    pipeline = health_snapshot()
    jobs_db = job_health()
    components = {
        "app": check_app_process(),
        "docker": check_docker(),
        "postgresql": check_postgresql(),
        "postgres_data": check_postgres_data_source(),
        "exports_volume": check_exports_volume(),
        "scheduler": check_scheduler(app_state),
        "git_autodeploy": check_git_autodeploy(),
        "codex_cli": check_codex_cli(),
        "codex_host": check_codex_host_url(),
        "image_python": check_image_python(),
        "market_data": check_market_data(),
        "github": check_github(),
        "pipeline": _status(
            not any(
                stage.get("status") in {"FAILED", "BLOCKED"}
                for stage in pipeline.get("stages", {}).values()
                if stage.get("date") == pipeline.get("date")
            ),
            f"{len(pipeline.get('stages', {}))} stages tracked today",
            stages=pipeline.get("stages", {}),
            files=pipeline.get("files", {}),
        ),
        "job_database": _status(
            jobs_db["failed_runs"] == 0,
            f"{jobs_db['job_runs']} runs · {jobs_db['failed_runs']} failed",
            **jobs_db,
        ),
    }
    ledger = {
        "paper_trades": table_count("paper_trades"),
        "job_runs": table_count("job_runs"),
    }
    overall_ok = all(
        components[name]["ok"]
        for name in ("app", "postgresql", "postgres_data", "scheduler", "exports_volume")
    )
    return {
        "ok": overall_ok,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "version": version_label(),
        "database": {
            "engine": "postgresql",
            "target": database_url().split("@")[-1],
            "require_postgres": os.getenv("STOCK_REQUIRE_POSTGRES", "").lower() in {"1", "true", "yes"},
        },
        "architecture": {"nodes": architecture_nodes(), "edges": architecture_edges()},
        "components": components,
        "pipeline": pipeline,
        "scheduled_jobs": jobs,
        "ledger": ledger,
    }
