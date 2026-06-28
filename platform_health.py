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

from db import connect, database_url
from job_storage import job_health
from pipeline_health import health_snapshot
from scanner_config import LOGS_DIR, PROJECT_DIR, WATCHLIST_EXPORT_DIR
from stock_storage import table_count
from version import version_label

CODEX_BIN = os.getenv("CODEX_BIN", "codex")
CODEX_TIMEOUT_SECONDS = int(os.getenv("CODEX_PROBE_TIMEOUT_SECONDS", "120"))
CODEX_HOST_URL = os.getenv("CODEX_HEALTH_URL", "").strip()
IMAGE_PYTHON = os.getenv("STOCK_IMAGE_PYTHON", os.getenv("PYTHON", "python3"))


def _status(ok: bool, detail: str = "", **extra) -> dict:
    return {"ok": ok, "status": "healthy" if ok else "unhealthy", "detail": detail, **extra}


def check_postgresql() -> dict:
    try:
        with connect(dict_rows=False) as (_, cursor):
            cursor.execute("SELECT 1")
            cursor.execute("SELECT count(*) FROM paper_trades")
            trades = int(cursor.fetchone()[0])
        return _status(True, f"{trades} paper trades in ledger", trades=trades, target=database_url().split("@")[-1])
    except Exception as exc:
        return _status(False, str(exc), target=database_url().split("@")[-1])


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
    return _status(
        True,
        f"Started {state.get('scheduler_started_at', 'unknown')}",
        live_updates=state.get("live_updates", False),
        poll_seconds=int(os.getenv("STOCK_CRON_POLL_SECONDS", "30")),
        running_job=state.get("running"),
    )


def check_codex_cli() -> dict:
    binary = shutil.which(CODEX_BIN)
    if not binary and Path(CODEX_BIN).is_file():
        binary = CODEX_BIN
    if not binary:
        return _status(
            False,
            f"{CODEX_BIN} not found in container PATH — mount host Codex or set CODEX_BIN",
            hint="On Pi: add a volume for the host codex binary in docker-compose.pi.yml",
        )
    try:
        completed = subprocess.run(
            [binary, "--version"],
            cwd=PROJECT_DIR,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=15,
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
    path = Path(IMAGE_PYTHON)
    if not path.is_file():
        return _status(False, f"Missing interpreter: {IMAGE_PYTHON}", path=str(path))
    try:
        completed = subprocess.run(
            [str(path), "-c", "import PIL; print(PIL.__version__)"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=20,
        )
        output = (completed.stdout or "").strip()
        return _status(completed.returncode == 0, output or "Pillow unavailable", path=str(path))
    except Exception as exc:
        return _status(False, str(exc), path=str(path))


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
        {"id": "docker", "label": "Docker · stock_app", "layer": "app", "detail": "app_server.py + scheduler"},
        {"id": "postgres", "label": "PostgreSQL", "layer": "data", "detail": "Trades, jobs, analytics"},
        {"id": "exports", "label": "exports/ volume", "layer": "data", "detail": "CSV, HTML, infographics"},
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
        {"from": "docker", "to": "exports", "label": "bind mount"},
        {"from": "docker", "to": "finviz", "label": "morning / confirm jobs"},
        {"from": "docker", "to": "codex", "label": "pnl_flashcard · chat"},
        {"from": "timer", "to": "github", "label": "git fetch/pull"},
        {"from": "timer", "to": "docker", "label": "compose rebuild"},
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
    try:
        completed = subprocess.run(
            [
                binary,
                "exec",
                "--ephemeral",
                "--sandbox",
                "read-only",
                message,
            ],
            cwd=PROJECT_DIR,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=CODEX_TIMEOUT_SECONDS,
        )
        duration_ms = int((time.time() - started) * 1000)
        response = (completed.stdout or "").strip()
        stderr = (completed.stderr or "").strip()
        if completed.returncode != 0 and not response:
            return {
                "ok": False,
                "error": stderr or f"Codex exited with code {completed.returncode}",
                "duration_ms": duration_ms,
            }
        return {
            "ok": True,
            "response": response,
            "stderr_tail": stderr[-2000:] if stderr else "",
            "duration_ms": duration_ms,
            "binary": binary,
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
        for name in ("app", "postgresql", "scheduler", "exports_volume")
    )
    return {
        "ok": overall_ok,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "version": version_label(),
        "architecture": {"nodes": architecture_nodes(), "edges": architecture_edges()},
        "components": components,
        "pipeline": pipeline,
        "scheduled_jobs": jobs,
        "ledger": ledger,
    }
