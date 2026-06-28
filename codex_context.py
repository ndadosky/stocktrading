"""Build PostgreSQL trading context for Codex chat prompts."""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from market_calendar import is_market_session
from stock_storage import (
    bankroll_base,
    list_snapshot_dates,
    query_rows,
    read_latest_snapshot,
    table_count,
    total_bankroll_deposits,
)

ET = ZoneInfo("America/New_York")
CONTEXT_MAX_CHARS = int(os.getenv("CODEX_CONTEXT_MAX_CHARS", "14000"))


def _today_et() -> date:
    return datetime.now(tz=ET).date()


def _prior_market_session(reference: date | None = None) -> date | None:
    cursor = (reference or _today_et()) - timedelta(days=1)
    for _ in range(21):
        if is_market_session(cursor):
            return cursor
        cursor -= timedelta(days=1)
    return None


def _money(value: object) -> str:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "—"
    if amount < 0:
        return f"-${abs(amount):,.2f}"
    return f"${amount:,.2f}"


def _pct(value: object) -> str:
    try:
        return f"{float(value):+.2f}%"
    except (TypeError, ValueError):
        return "—"


def _float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _snapshot_totals(rows: list[dict]) -> dict:
    if not rows:
        return {}
    base = bankroll_base()
    total_pl = sum(_float(row.get("p_l")) for row in rows)
    total_cost = sum(_float(row.get("cost")) for row in rows)
    open_rows = [row for row in rows if str(row.get("status") or "").upper() == "OPEN"]
    closed_rows = [row for row in rows if str(row.get("status") or "").upper() != "OPEN"]
    winners = sum(1 for row in rows if _float(row.get("p_l")) > 0)
    losers = sum(1 for row in rows if _float(row.get("p_l")) < 0)
    return {
        "positions": len(rows),
        "open": len(open_rows),
        "closed": len(closed_rows),
        "winners": winners,
        "losers": losers,
        "total_p_l": total_pl,
        "return_on_bankroll_pct": total_pl / base * 100 if base else 0.0,
        "return_on_deployed_pct": total_pl / total_cost * 100 if total_cost else 0.0,
    }


def _live_ledger_summary(ledger: list[dict]) -> dict[str, object]:
    base = bankroll_base()
    if not ledger:
        return {
            "equity": base,
            "cash": base,
            "deployed": 0.0,
            "open_positions": 0,
            "closed_trades": 0,
            "realized_p_l": 0.0,
            "unrealized_p_l": 0.0,
            "open_rows": [],
        }

    total_entry_cost = 0.0
    proceeds = 0.0
    realized = 0.0
    open_value = 0.0
    unrealized = 0.0
    open_rows: list[dict] = []
    closed = 0

    for row in ledger:
        entry = _float(row.get("entry_price"))
        shares = _float(row.get("shares"))
        remaining = _float(row.get("remaining_shares"), shares)
        current = _float(row.get("current_price"), entry)
        total_entry_cost += entry * shares
        proceeds += _float(row.get("realized_proceeds"))
        realized += _float(row.get("realized_p_l"))
        if remaining > 0:
            open_value += current * remaining
            unrealized += (current - entry) * remaining
            open_rows.append(row)
        else:
            closed += 1

    cash = base - total_entry_cost + proceeds
    equity = cash + open_value
    deployed = sum(_float(r.get("entry_price")) * _float(r.get("remaining_shares"), _float(r.get("shares"))) for r in open_rows)
    return {
        "equity": equity,
        "cash": cash,
        "deployed": deployed,
        "open_positions": len(open_rows),
        "closed_trades": closed,
        "realized_p_l": realized,
        "unrealized_p_l": unrealized,
        "open_rows": open_rows,
    }


def _format_position(row: dict) -> str:
    ticker = row.get("ticker") or "?"
    status = row.get("status") or "?"
    sector = row.get("sector") or ""
    trade_date = row.get("trade_date") or ""
    entry = row.get("entry_price")
    pl = row.get("p_l")
    pl_pct = row.get("p_l_%")
    exit_reason = row.get("exit_reason")
    parts = [
        f"{ticker} ({status})",
        f"sector={sector}" if sector else None,
        f"entry {trade_date} @{entry}",
        f"P/L {_money(pl)} ({_pct(pl_pct)})",
    ]
    if exit_reason and str(exit_reason) not in {"", "nan", "None"}:
        parts.append(f"exit={exit_reason}")
    return " · ".join(part for part in parts if part)


def _format_trade_ledger(row: dict) -> str:
    ticker = row.get("ticker") or "?"
    trade_date = row.get("trade_date") or "?"
    status = row.get("status") or "?"
    entry = row.get("entry_price")
    shares = row.get("shares")
    exit_dt = row.get("exit_datetime")
    exit_reason = row.get("exit_reason")
    realized = row.get("realized_p_l")
    parts = [f"{ticker} entered {trade_date}", f"{status}", f"{shares} sh @{entry}"]
    if exit_dt and str(exit_dt) not in {"", "None", "NaT"}:
        parts.append(f"exited {str(exit_dt)[:10]} ({exit_reason or 'n/a'})")
        parts.append(f"realized {_money(realized)}")
    return " · ".join(str(part) for part in parts if part)


def _day_section(title: str, report_date: str, rows: list[dict], ledger_entries: list[dict], ledger_exits: list[dict]) -> list[str]:
    lines = [f"### {title} ({report_date})"]
    if not rows and not ledger_entries and not ledger_exits:
        lines.append("No report snapshot or ledger activity for this date.")
        return lines

    totals = _snapshot_totals(rows)
    if totals:
        lines.append(
            "Snapshot: "
            f"{totals['positions']} positions "
            f"({totals['open']} open, {totals['closed']} closed), "
            f"P/L {_money(totals['total_p_l'])} "
            f"({_pct(totals['return_on_bankroll_pct'])} of bankroll, "
            f"{_pct(totals['return_on_deployed_pct'])} on deployed)."
        )
        lines.append(f"Winners/losers: {totals['winners']}/{totals['losers']}.")

    if ledger_entries:
        lines.append(f"New entries ({len(ledger_entries)}):")
        lines.extend(f"- {_format_trade_ledger(row)}" for row in ledger_entries[:12])
    if ledger_exits:
        lines.append(f"Exits ({len(ledger_exits)}):")
        lines.extend(f"- {_format_trade_ledger(row)}" for row in ledger_exits[:12])
    if rows:
        lines.append("Positions at close of report:")
        for row in sorted(rows, key=lambda item: _float(item.get("p_l"))):
            lines.append(f"- {_format_position(row)}")
    return lines


def _trim_lines(lines: list[str], max_chars: int) -> list[str]:
    text = "\n".join(lines)
    if len(text) <= max_chars:
        return lines
    trimmed = lines[:]
    while len("\n".join(trimmed)) > max_chars and trimmed:
        trimmed.pop()
    if trimmed and trimmed[-1] != "... [context truncated]":
        trimmed.append("... [context truncated]")
    return trimmed


def build_trading_context(max_chars: int | None = None) -> str:
    """Serialize authoritative PostgreSQL trading state for Codex prompts."""
    max_chars = max_chars or CONTEXT_MAX_CHARS
    now = datetime.now(tz=ET)
    today = now.date().isoformat()
    yesterday = _prior_market_session(now.date())
    yesterday_key = yesterday.isoformat() if yesterday else None

    snapshot_dates = list_snapshot_dates("paper_performance", "report_date")
    latest_snapshot = snapshot_dates[-1] if snapshot_dates else None

    ledger = query_rows("paper_trades", "ORDER BY trade_date DESC, ticker ASC", (), 500)
    entries_by_date: dict[str, list[dict]] = {}
    exits_by_date: dict[str, list[dict]] = {}
    for row in ledger:
        trade_date = str(row.get("trade_date") or "")
        if trade_date:
            entries_by_date.setdefault(trade_date, []).append(row)
        exit_dt = str(row.get("exit_datetime") or "")
        if len(exit_dt) >= 10:
            exits_by_date.setdefault(exit_dt[:10], []).append(row)

    lines = [
        "# Paper trading database context (PostgreSQL — authoritative)",
        f"Generated: {now.isoformat(timespec='seconds')}",
        f"Today (ET): {today} · market session today: {is_market_session(now.date())}",
        f"Prior market session ('yesterday'): {yesterday_key or 'none'}",
        f"Latest paper_performance snapshot date: {latest_snapshot or 'none'}",
        "",
        "## Account",
        f"Starting capital $25,000 · deposits {_money(total_bankroll_deposits())} · "
        f"bankroll base {_money(bankroll_base())}",
        f"Ledger rows in paper_trades: {table_count('paper_trades')}",
        "",
    ]

    try:
        summary = _live_ledger_summary(ledger)
        lines.extend([
            "## Current ledger (live paper_trades)",
            f"Equity {_money(summary['equity'])} · cash {_money(summary['cash'])} · "
            f"deployed {_money(summary['deployed'])}",
            f"Open {summary['open_positions']} · closed {summary['closed_trades']} · "
            f"realized {_money(summary['realized_p_l'])} · unrealized {_money(summary['unrealized_p_l'])}",
        ])
        for row in summary["open_rows"][:20]:
            lines.append(
                f"- {row.get('ticker')} entry {row.get('trade_date')} @{row.get('entry_price')} "
                f"rem={row.get('remaining_shares')} cur={row.get('current_price')}"
            )
        lines.append("")
    except Exception as exc:
        lines.extend(["## Current ledger", f"Could not load live trades: {exc}", ""])

    if yesterday_key:
        snap_rows = query_rows("paper_performance", "WHERE report_date = ?", (yesterday_key,), 500)
        lines.extend(
            _day_section(
                "Prior market session",
                yesterday_key,
                snap_rows,
                entries_by_date.get(yesterday_key, []),
                exits_by_date.get(yesterday_key, []),
            )
        )
        lines.append("")

    if latest_snapshot and latest_snapshot != yesterday_key:
        snap_rows = query_rows("paper_performance", "WHERE report_date = ?", (latest_snapshot,), 500)
        lines.extend(
            _day_section(
                "Latest report snapshot",
                latest_snapshot,
                snap_rows,
                entries_by_date.get(latest_snapshot, []),
                exits_by_date.get(latest_snapshot, []),
            )
        )
        lines.append("")

    if today != latest_snapshot:
        snap_rows = query_rows("paper_performance", "WHERE report_date = ?", (today,), 500)
        if snap_rows or entries_by_date.get(today) or exits_by_date.get(today):
            lines.extend(
                _day_section(
                    "Today",
                    today,
                    snap_rows,
                    entries_by_date.get(today, []),
                    exits_by_date.get(today, []),
                )
            )
            lines.append("")

    if snapshot_dates:
        lines.append(f"Available report dates: {', '.join(snapshot_dates[-8:])}")

    review_date, review_frame = read_latest_snapshot("strategy_reviews", "stored_review_date")
    if review_date and not review_frame.empty and "metric" in review_frame.columns:
        decision = review_frame[review_frame["metric"].astype(str).eq("recommended_action")]
        if not decision.empty:
            row = decision.iloc[-1]
            lines.append(
                f"Latest strategy review ({review_date}): "
                f"{row.get('value')} ({row.get('status')})"
            )

    recent_jobs = query_rows("job_runs", "ORDER BY finished_at DESC", (), 8)
    if recent_jobs:
        lines.append("Recent job runs:")
        for job in recent_jobs:
            lines.append(
                f"- {job.get('finished_at')} {job.get('job_name')} "
                f"{'OK' if job.get('ok') else 'FAILED'} ({job.get('reason')})"
            )

    return "\n".join(_trim_lines(lines, max_chars))


def codex_prompt_with_context(user_message: str) -> str:
    context = build_trading_context()
    return (
        "You are the assistant for a $25K pre-breakout paper trading system. "
        "Answer using ONLY the PostgreSQL trading data below. "
        "If the user asks about 'yesterday', use the prior market session section. "
        "If data is missing, say so explicitly — do not invent trades.\n\n"
        "=== LIVE TRADING DATA ===\n"
        f"{context}\n"
        "=== END DATA ===\n\n"
        f"User question: {user_message.strip()}"
    )
