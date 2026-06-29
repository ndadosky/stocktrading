"""Generate the single, cumulative HTML dashboard for the paper account."""

from __future__ import annotations

from datetime import datetime
from html import escape
import json
import threading
from pathlib import Path
from typing import List

import pandas as pd
import yfinance as yf

from nav_html import finalize_dashboard_html
from scanner_config import (
    DASHBOARD_FILE, PIPELINE_STATE_FILE,
    STARTING_CAPITAL, WATCHLIST_EXPORT_DIR, MAX_DAILY_PAPER_TRADES,
    FIRST_TARGET_GAIN_PCT, MAX_PORTFOLIO_HEAT_PCT, MAX_SECTOR_EXPOSURE_PCT,
    RUNNER_EXIT_SESSIONS, SECOND_TARGET_GAIN_PCT, STOP_LOSS_PCT,
    RISK_PER_TRADE_PCT,
    ensure_directories,
)
from stock_storage import list_snapshot_dates, read_latest_snapshot, read_snapshot
from version import version_label

# Column selection for the positions table
_OPEN_COLS = [
    "ticker", "name", "sector", "remaining_shares", "entry_price", "current_price",
    "p_l", "p_l_%", "holding_days", "bid_ask_spread_pct", "confirmation_band",
]
_CLOSED_COLS = [
    "ticker", "name", "sector", "entry_price", "exit_price", "p_l", "p_l_%",
    "holding_days", "exit_reason",
]
_COL_LABELS = {
    "ticker": "Ticker", "name": "Name", "sector": "Sector", "earnings_date": "Earnings",
    "entry_price": "Entry", "current_price": "Price", "exit_price": "Exit",
    "remaining_shares": "Shares",
    "p_l": "P/L", "p_l_%": "P/L %", "holding_days": "Days",
    "bid_ask_spread_pct": "Spread", "confirmation_band": "Band",
    "exit_reason": "Exit reason",
}
_OPEN_HEADER_TOOLTIPS = {
    "Shares": "Shares currently held, after any partial profit-taking exits.",
    "P/L %": "Unrealized return for the open position, including partial exits already booked.",
    "Days": "Market sessions held since entry. Positions time-exit after the configured max holding period.",
    "Spread": "Bid/ask spread percentage captured at entry or the 5-minute range proxy when live quotes were unavailable.",
    "Band": "9:45 confirmation quality bucket: A is strongest, B is still buy-eligible, C/D are normally not opened.",
}


def daily_history() -> pd.DataFrame:
    """Roll daily performance snapshots from PostgreSQL into one equity history."""
    from daily_report import account_summary

    rows: List[dict] = []
    for report_date in list_snapshot_dates("paper_performance", "report_date"):
        try:
            frame = read_snapshot("paper_performance", "report_date", report_date)
            if frame.empty:
                continue
            summary = account_summary(frame)
            capital_base = float(summary.get("capital_base", STARTING_CAPITAL))
            rows.append({
                "date": report_date,
                "trades": len(frame),
                "deployed": summary["deployed"],
                "cash": summary["cash"],
                "equity": summary["equity"],
                "p_l": summary["equity"] - capital_base,
                "return_%": (summary["equity"] / capital_base - 1) * 100 if capital_base else 0,
            })
        except Exception as exc:
            print(f"Skipped history snapshot {report_date}: {exc}")
    return pd.DataFrame(rows)


def _grade_from_score(score: float) -> str:
    if score >= 92:
        return "A"
    if score >= 85:
        return "A-"
    if score >= 80:
        return "B+"
    if score >= 70:
        return "B"
    if score >= 65:
        return "C+"
    if score >= 55:
        return "C"
    if score >= 45:
        return "D"
    return "F"


def day_grade(performance: pd.DataFrame, summary: dict, history: pd.DataFrame, pipeline: dict) -> dict:
    """Grade current intraday book health; this is not optimizer evidence."""
    if performance.empty:
        return {"grade": "—", "score": 0.0, "status": "neutral", "note": "Awaiting trades"}

    capital_base = float(summary.get("capital_base", STARTING_CAPITAL) or STARTING_CAPITAL)
    deployed = float(summary.get("deployed", 0.0) or 0.0)
    total_pl = float(summary.get("realized_p_l", 0.0) or 0.0) + float(summary.get("unrealized_p_l", 0.0) or 0.0)
    return_on_deployed = total_pl / deployed * 100 if deployed else 0.0
    heat_pct = float(summary.get("portfolio_heat_pct", 0.0) or 0.0)

    remaining = pd.to_numeric(performance.get("remaining_shares"), errors="coerce").fillna(0)
    open_rows = performance[remaining.gt(0)].copy()
    closed_rows = performance[remaining.le(0)].copy()

    if open_rows.empty:
        green_pct = 50.0
        stop_pressure_pct = 0.0
        sector_pressure_pct = 0.0
    else:
        open_pl = pd.to_numeric(open_rows.get("p_l"), errors="coerce").fillna(0)
        green_pct = float(open_pl.gt(0).mean() * 100)
        current = pd.to_numeric(open_rows.get("current_price"), errors="coerce")
        stop = pd.to_numeric(open_rows.get("stop_8"), errors="coerce")
        entry = pd.to_numeric(open_rows.get("entry_price"), errors="coerce")
        stop_gap = ((current - stop) / entry * 100).replace([float("inf"), -float("inf")], pd.NA)
        stop_pressure_pct = float(stop_gap.le(2.0).fillna(False).mean() * 100)
        sector_cost = (
            pd.to_numeric(open_rows.get("entry_price"), errors="coerce").fillna(0)
            * pd.to_numeric(open_rows.get("remaining_shares"), errors="coerce").fillna(0)
        ).groupby(open_rows.get("sector").fillna("Unknown")).sum()
        sector_limit = capital_base * MAX_SECTOR_EXPOSURE_PCT / 100 if capital_base else 0
        sector_pressure_pct = float(sector_cost.max() / sector_limit * 100) if sector_limit and not sector_cost.empty else 0.0

    stopped_today = 0
    if not closed_rows.empty and "exit_reason" in closed_rows:
        stopped_today = int(closed_rows["exit_reason"].astype(str).str.contains("STOP", case=False, na=False).sum())
    stop_penalty = min(100.0, stopped_today * 25.0 + stop_pressure_pct)

    # 0% deployed return maps to a neutral 70. Strong gains/losses move the score quickly.
    pnl_score = max(0.0, min(100.0, 70.0 + return_on_deployed * 15.0))
    breadth_score = max(0.0, min(100.0, green_pct))
    stop_score = max(0.0, 100.0 - stop_penalty)
    heat_score = max(0.0, 100.0 - max(0.0, heat_pct - 3.0) * 20.0)
    concentration_score = max(0.0, 100.0 - max(0.0, sector_pressure_pct - 75.0) * 1.6)
    risk_score = heat_score * 0.55 + concentration_score * 0.45

    trend_score = 70.0
    if len(history) >= 2 and "p_l" in history:
        prior_pl = float(history.iloc[-2].get("p_l", 0.0) or 0.0)
        current_pl = float(history.iloc[-1].get("p_l", total_pl) or total_pl)
        trend_delta = (current_pl - prior_pl) / capital_base * 100 if capital_base else 0.0
        trend_score = max(0.0, min(100.0, 70.0 + trend_delta * 60.0))

    statuses = [str(info.get("status", "PENDING")).upper() for info in pipeline.values()]
    if not statuses:
        data_score = 65.0
    elif all(status == "SUCCESS" for status in statuses):
        data_score = 100.0
    elif any(status in {"FAILED", "BLOCKED"} for status in statuses):
        data_score = 35.0
    else:
        data_score = 65.0

    score = (
        pnl_score * 0.30
        + breadth_score * 0.20
        + stop_score * 0.15
        + risk_score * 0.15
        + trend_score * 0.10
        + data_score * 0.10
    )
    grade = _grade_from_score(score)
    status = "positive" if score >= 70 else "warning" if score >= 55 else "negative"
    note = f"{green_pct:.0f}% green · {return_on_deployed:+.2f}% deployed"
    if stopped_today:
        note += f" · {stopped_today} stopped"
    return {"grade": grade, "score": score, "status": status, "note": note}


def equity_svg(history: pd.DataFrame, capital_base: float = STARTING_CAPITAL) -> str:
    if history.empty:
        return ("<div class='empty'><div class='empty-icon'>↗</div>"
                "<strong>Your equity curve starts tomorrow</strong>"
                "<span>Performance will appear after the first confirmed trades.</span></div>")
    values = history["equity"].astype(float).tolist()
    # Use actual equity range — don't force STARTING_CAPITAL into the axis min,
    # which would crush all the action into the top strip of the chart.
    low = min(values)
    high = max(values)
    spread = max(high - low, capital_base * 0.015)  # minimum 1.5% of bankroll range
    pad = spread * 0.25
    data_min, data_max = max(low - pad, 0), high + pad
    data_range = max(data_max - data_min, 1.0)

    LEFT, RIGHT, TOP, BOT = 70, 760, 16, 170
    W, H = RIGHT - LEFT, BOT - TOP

    def px(i: int) -> float:
        n = max(len(values) - 1, 1)
        return LEFT + i / n * W

    def py(v: float) -> float:
        return BOT - (v - data_min) / data_range * H

    pts = [f"{px(i):.1f},{py(v):.1f}" for i, v in enumerate(values)]

    # For a single data point, extend to a full-width line for readability
    if len(pts) == 1:
        single_y = py(values[0])
        draw_pts = f"{LEFT},{single_y:.1f} {RIGHT},{single_y:.1f}"
        area_pts = f"{LEFT},{BOT} {LEFT},{single_y:.1f} {RIGHT},{single_y:.1f} {RIGHT},{BOT}"
    else:
        draw_pts = " ".join(pts)
        area_pts = f"{LEFT},{BOT} {' '.join(pts)} {RIGHT},{BOT}"

    base_y = py(capital_base)

    # Y-axis: 3 reference lines + labels
    y_items: list[str] = []
    ref_vals = sorted({data_min + pad * 0.5, float(capital_base), data_max - pad * 0.5})
    for val in ref_vals:
        yv = py(val)
        label = f"${val:,.0f}"
        y_items.append(
            f"<line x1='{LEFT}' y1='{yv:.1f}' x2='{RIGHT}' y2='{yv:.1f}' "
            f"stroke='#f3f4f6' stroke-dasharray='3,3'/>"
            f"<text x='{LEFT - 5}' y='{yv + 4:.1f}' text-anchor='end' "
            f"font-size='10' fill='#9ca3af'>{escape(label)}</text>"
        )

    # Green dashed baseline at bankroll (capital_base) with label
    baseline = (
        f"<line x1='{LEFT}' y1='{base_y:.1f}' x2='{RIGHT}' y2='{base_y:.1f}' "
        f"stroke='#86efac' stroke-dasharray='5,3' stroke-width='1.5'/>"
        f"<text x='{RIGHT + 3}' y='{base_y + 4:.1f}' font-size='9' fill='#86efac'>BASE</text>"
    )

    # Dot at latest value
    last_x = float(pts[-1].split(",")[0])
    last_y = float(pts[-1].split(",")[1])
    dot_color = "#16803c" if values[-1] >= STARTING_CAPITAL else "#c62828"
    dot = (
        f"<circle cx='{last_x:.1f}' cy='{last_y:.1f}' r='4.5' "
        f"fill='{dot_color}' stroke='white' stroke-width='2'/>"
    )

    return (
        "<svg viewBox='0 0 810 200' role='img' aria-label='Account equity history'>"
        "<defs><linearGradient id='ag' x1='0' y1='0' x2='0' y2='1'>"
        "<stop offset='0' stop-color='#2563eb' stop-opacity='.14'/>"
        "<stop offset='1' stop-color='#2563eb' stop-opacity='0'/>"
        "</linearGradient></defs>"
        + "".join(y_items) + baseline
        + f"<line x1='{LEFT}' y1='{BOT}' x2='{RIGHT}' y2='{BOT}' stroke='#e5e7eb'/>"
        f"<polygon points='{area_pts}' fill='url(#ag)'/>"
        f"<polyline points='{draw_pts}' fill='none' stroke='#2563eb' "
        f"stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'/>"
        + dot
        + f"<text x='{LEFT}' y='196' font-size='11' fill='#9ca3af'>"
        f"{escape(str(history.iloc[0]['date']))}</text>"
        f"<text x='{RIGHT}' y='196' text-anchor='end' font-size='11' fill='#9ca3af'>"
        f"{escape(str(history.iloc[-1]['date']))}</text>"
        "</svg>"
    )


def company_names() -> dict[str, str]:
    names: dict[str, str] = {}
    _, frame = read_latest_snapshot("morning_candidates", "scan_date")
    if frame.empty:
        return names
    ticker_column = "Ticker" if "Ticker" in frame.columns else "ticker" if "ticker" in frame.columns else None
    if ticker_column is None or "Company" not in frame.columns:
        return names
    for _, row in frame[[ticker_column, "Company"]].dropna().iterrows():
        ticker = str(row[ticker_column]).strip().upper()
        company = str(row["Company"]).strip()
        if ticker and company:
            names[ticker] = company
    return names


def trade_table(frame: pd.DataFrame, is_open: bool = True) -> str:
    if frame.empty:
        if is_open:
            return ("<div class='empty compact'><div class='empty-icon'>◎</div>"
                    "<strong>No open positions</strong>"
                    "<span>Tomorrow's confirmed setups will land here automatically.</span></div>")
        return ("<div class='empty compact'><div class='empty-icon'>◎</div>"
                "<strong>No resolved trades</strong>"
                "<span>Completed trades will appear here after exits.</span></div>")

    source = frame.copy()
    if "ticker" in source.columns and "name" not in source.columns:
        ticker_names = company_names()
        source.insert(
            source.columns.get_loc("ticker") + 1,
            "name",
            source["ticker"].astype(str).str.upper().map(ticker_names).fillna(""),
        )

    columns = _OPEN_COLS if is_open else _CLOSED_COLS
    display = source[[c for c in columns if c in source.columns]].copy()

    # Sort open positions worst-first by P/L %
    if is_open and "p_l_%" in frame.columns:
        sort_key = pd.to_numeric(frame.loc[display.index, "p_l_%"], errors="coerce")
        display = display.loc[sort_key.sort_values(ascending=True).index]

    # Flag upcoming earnings (within 14 days)
    today = pd.Timestamp.today().normalize()
    if "earnings_date" in display.columns:
        src = frame.loc[display.index, "earnings_date"]

        def fmt_earn(val: object) -> str:
            s = str(val) if pd.notna(val) else ""
            if not s or s in ("nan", "NaT", "None"):
                return ""
            try:
                d = pd.Timestamp(s)
                delta = (d - today).days
                lbl = escape(s[:10])
                if 0 <= delta <= 14:
                    return f"<span class='earn-soon' title='Earnings in {delta}d'>⚠ {lbl}</span>"
                return lbl
            except Exception:
                return escape(s[:10])

        display["earnings_date"] = src.map(fmt_earn)

    # Format price columns
    for col in ("entry_price", "current_price", "exit_price"):
        if col in display.columns and col in frame.columns:
            num = pd.to_numeric(frame.loc[display.index, col], errors="coerce")
            display[col] = num.map(lambda x: f"${x:,.2f}" if pd.notna(x) else "")

    # Show the live position size; partial exits reduce this value.
    if "remaining_shares" in display.columns and "remaining_shares" in frame.columns:
        num = pd.to_numeric(frame.loc[display.index, "remaining_shares"], errors="coerce")
        display["remaining_shares"] = num.map(
            lambda x: f"{int(x):,}" if pd.notna(x) and float(x).is_integer()
            else f"{x:,.2f}" if pd.notna(x) else ""
        )

    # Format P/L columns
    if "p_l" in display.columns and "p_l" in frame.columns:
        num = pd.to_numeric(frame.loc[display.index, "p_l"], errors="coerce")
        display["p_l"] = num.map(
            lambda x: (f"<span class='{'positive' if x >= 0 else 'negative'}'>${x:,.2f}</span>")
            if pd.notna(x) else ""
        )
    if "p_l_%" in display.columns and "p_l_%" in frame.columns:
        num = pd.to_numeric(frame.loc[display.index, "p_l_%"], errors="coerce")
        display["p_l_%"] = num.map(
            lambda x: (f"<span class='{'positive' if x >= 0 else 'negative'}'>{x:.2f}%</span>")
            if pd.notna(x) else ""
        )

    # Format spread as a percentage string
    if "bid_ask_spread_pct" in display.columns and "bid_ask_spread_pct" in frame.columns:
        num = pd.to_numeric(frame.loc[display.index, "bid_ask_spread_pct"], errors="coerce")
        display["bid_ask_spread_pct"] = num.map(lambda x: f"{x:.3f}%" if pd.notna(x) else "")

    # Escape remaining object columns
    already_fmt = {"p_l", "p_l_%", "earnings_date", "entry_price", "current_price",
                   "exit_price", "bid_ask_spread_pct"}
    for col in display.select_dtypes(include=["object"]).columns:
        if col not in already_fmt:
            display[col] = display[col].map(lambda v: escape(str(v)) if pd.notna(v) else "")

    # Rename to display-friendly headers
    display.columns = [_COL_LABELS.get(c, c) for c in display.columns]
    html = display.to_html(index=False, escape=False, border=0)
    if is_open:
        html = html.replace("<table", '<table id="open-pos-table"', 1)
        for label, tip in _OPEN_HEADER_TOOLTIPS.items():
            html = html.replace(
                f"<th>{label}</th>",
                f"<th data-tooltip=\"{escape(tip)}\" aria-label=\"{escape(label + ': ' + tip)}\">{label}</th>",
            )
    return html


def sector_warning_html(open_pos: pd.DataFrame, equity: float) -> str:
    """Return a warning banner if any sector exceeds the configured ceiling."""
    ceiling = float(MAX_SECTOR_EXPOSURE_PCT)
    if open_pos.empty or "sector" not in open_pos.columns or equity <= 0:
        return ""
    exposure = pd.to_numeric(open_pos.get("market_value"), errors="coerce").fillna(0)
    sector_exposure = exposure.groupby(open_pos["sector"].fillna("Unknown")).sum()
    violations = [(sector, value / equity * 100) for sector, value in sector_exposure.items() if value / equity * 100 > ceiling]
    if not violations:
        return ""
    detail = " · ".join(f"{escape(str(s))} {p:.0f}%" for s, p in violations)
    return (
        f"<div class='warn-banner'>"
        f"<b>⚠ Sector ceiling exceeded</b> — {detail} (max {ceiling:.0f}%)"
        f"</div>"
    )


def infographic_links() -> str:
    grouped: dict[str, dict[str, Path]] = {}
    for path in sorted(WATCHLIST_EXPORT_DIR.glob("infographic_summary_*.*")):
        if path.suffix.lower() not in {".html", ".png"}:
            continue
        date_key = path.stem.replace("infographic_summary_", "")
        grouped.setdefault(date_key, {})[path.suffix.lower()] = path
    if not grouped:
        return ("<div class='empty compact'><div class='empty-icon'>▧</div>"
                "<strong>No infographic exports yet</strong>"
                "<span>Daily summaries will appear here after generation.</span></div>")
    rows = []
    for date_key in sorted(grouped, reverse=True):
        rows.append(
            f"<div><b>{escape(date_key)}</b>"
            f"<span><a href='/day?date={escape(date_key)}'>Status page</a></span></div>"
        )
    return "<div class='schedule'>" + "".join(rows) + "</div>"


def strategy_review_panel() -> str:
    review_date, review = read_latest_snapshot("strategy_reviews", "stored_review_date")
    if review.empty:
        return ("<div class='empty compact'><strong>No strategy review yet</strong>"
                "<span>The 10:30 review will summarize improvement gates here.</span></div>")

    def metric_value(metric: str, default: str = "—") -> str:
        match = review[review["metric"].astype(str).eq(metric)] if "metric" in review else pd.DataFrame()
        if match.empty:
            return default
        return str(match.iloc[-1].get("value", default))

    def metric_status(metric: str, default: str = "monitor") -> str:
        match = review[review["metric"].astype(str).eq(metric)] if "metric" in review else pd.DataFrame()
        if match.empty:
            return default
        return str(match.iloc[-1].get("status", default)).lower()

    html_file = WATCHLIST_EXPORT_DIR / f"strategy_review_{review_date or 'latest'}.html"
    links = [f"<a href='/exports/{escape(html_file.name)}'>Open review</a>"] if html_file.exists() else []
    links.append("<a href='/strategy-review'>Strategy review page</a>")

    decision = metric_value("recommended_action", metric_value("settings_change", "no review decision"))
    dec_status = metric_status("recommended_action", metric_status("settings_change"))

    items = [
        ("Resolved trades", metric_value("resolved_trades", "0"), metric_status("resolved_trades", "blocked")),
        ("Holdout", metric_value("frozen_holdout_trades", "0"), metric_status("frozen_holdout_trades", "blocked")),
        ("Latest 50 success", metric_value("latest_50_success_rate", "0.0%"), metric_status("latest_50_success_rate", "monitor")),
        ("Failed jobs", metric_value("failed_job_runs", "0"), metric_status("failed_job_runs", "pass")),
    ]
    rows = "".join(
        f"<div><b>{escape(label)}</b>"
        f"<span class='review-status {escape(status)}'>{escape(value)}</span></div>"
        for label, value, status in items
    )
    return (
        f"<div class='decision-badge {escape(dec_status)}'>{escape(decision)}</div>"
        "<div class='review-grid'>" + rows + "</div>"
        f"<p class='review-links'>{' · '.join(links)} · Latest: {escape(str(review_date or '—'))}</p>"
    )


def market_indices_html() -> str:
    symbols = [("^DJI", "DJIA"), ("^IXIC", "Nasdaq"), ("^GSPC", "S&P 500")]
    try:
        data = yf.download(
            [symbol for symbol, _ in symbols],
            period="5d",
            interval="1d",
            auto_adjust=False,
            progress=False,
            group_by="ticker",
            threads=True,
        )
    except Exception as exc:
        print(f"Market index fetch failed: {exc}")
        data = pd.DataFrame()

    def index_card(symbol: str, label: str) -> str:
        try:
            frame = data[symbol] if isinstance(data.columns, pd.MultiIndex) else data
            close = pd.to_numeric(frame["Close"], errors="coerce").dropna()
            if len(close) < 2:
                raise ValueError("not enough close data")
            current = float(close.iloc[-1])
            prior = float(close.iloc[-2])
            change = current - prior
            pct = change / prior * 100 if prior else 0.0
            cls = "positive" if change >= 0 else "negative"
            return (
                f"<div class='index-card'><small>{escape(label)}</small>"
                f"<strong>{current:,.2f}</strong>"
                f"<span class='{cls}'>{change:+,.2f} ({pct:+.2f}%)</span></div>"
            )
        except Exception:
            return (
                f"<div class='index-card'><small>{escape(label)}</small>"
                "<strong>Unavailable</strong><span>Quote fetch failed</span></div>"
            )

    return "<div class='indices'>" + "".join(index_card(symbol, label) for symbol, label in symbols) + "</div>"


def build_dashboard(performance: pd.DataFrame, as_of: str) -> Path:
    ensure_directories()
    from daily_report import account_summary
    summary = account_summary(performance)
    cost, cash, equity = summary["deployed"], summary["cash"], summary["equity"]
    capital_base = float(summary.get("capital_base", STARTING_CAPITAL))
    deposits = float(summary.get("capital_deposits", 0.0))
    pnl = equity - capital_base
    open_positions = (
        performance[pd.to_numeric(performance["remaining_shares"], errors="coerce").fillna(0).gt(0)]
        if not performance.empty else performance
    )
    closed = (
        performance[pd.to_numeric(performance["remaining_shares"], errors="coerce").fillna(0).le(0)]
        if not performance.empty else performance
    )
    if not closed.empty and "success" in closed:
        success = closed["success"].astype(str).str.lower().isin({"1", "true", "yes"})
        hit_rate = float(success.mean() * 100)
    else:
        hit_rate = 0.0
    account_return = pnl / capital_base * 100 if capital_base else 0.0
    utilization_pct = min(cost / capital_base * 100 if capital_base else 0.0, 100.0)
    util_bar_color = "#2563eb" if utilization_pct < 80 else "#b45309"

    # Cards with P/L color coding and utilization bar on deployed card
    pnl_class = "positive" if pnl >= 0 else "negative"

    def render_card(label: str, value: str, cls: str = "", extra: str = "") -> str:
        return (
            f"<div class='card'>"
            f"<small>{escape(label)}</small>"
            f"<strong class='{cls}'>{escape(value)}</strong>"
            f"{extra}"
            f"</div>"
        )

    util_extra = (
        f"<div class='util-bar'>"
        f"<div class='util-fill' style='width:{utilization_pct:.1f}%;background:{util_bar_color}'></div>"
        f"</div>"
        f"<span class='util-label'>{utilization_pct:.0f}% of bankroll</span>"
    )

    try:
        pipeline = (
            json.loads(PIPELINE_STATE_FILE.read_text(encoding="utf-8")).get("stages", {})
            if PIPELINE_STATE_FILE.exists() else {}
        )
    except Exception:
        pipeline = {}
    history = daily_history()
    day = day_grade(performance, summary, history, pipeline)
    day_extra = f"<span class='util-label'>{escape(day['note'])} · {day['score']:.0f}/100</span>"
    bankroll_extra = f"<span class='util-label'>Deposits ${deposits:,.2f}</span>"

    cards_html = (
        render_card("Day grade", day["grade"], cls=f"grade-{day['status']}", extra=day_extra)
        + render_card("Bankroll", f"${capital_base:,.2f}", extra=bankroll_extra)
        + render_card("Available cash", f"${cash:,.2f}")
        + render_card("Capital deployed", f"${cost:,.2f}", extra=util_extra)
        + render_card("Cumulative P/L", f"${pnl:,.2f}", cls=pnl_class)
        + render_card("Open positions", str(summary["open_positions"]))
        + render_card("Portfolio heat", f"{summary.get('portfolio_heat_pct', 0):.2f}%")
        + render_card("Resolved success", f"{hit_rate:.1f}%" if len(closed) else "—")
    )

    history_display = history.copy()
    for col in ["deployed", "cash", "equity", "p_l"]:
        if col in history_display:
            history_display[col] = history_display[col].map(lambda x: f"${x:,.2f}")
    if "return_%" in history_display:
        history_display["return_%"] = history_display["return_%"].map(lambda x: f"{x:.2f}%")
    history_table = history_display.to_html(index=False, border=0) if not history_display.empty else ""

    _, band = read_latest_snapshot("score_band_performance", "report_date")
    _, components = read_latest_snapshot("component_performance", "report_date")
    components = components.head(12) if not components.empty else components
    analytics_html = (
        band.to_html(index=False, border=0) if not band.empty
        else "<div class='empty compact'><strong>Awaiting resolved trades</strong>"
             "<span>Score-band results need completed outcomes.</span></div>"
    )
    components_html = (
        components.to_html(index=False, border=0) if not components.empty
        else "<div class='empty compact'><strong>Awaiting resolved trades</strong>"
             "<span>Signal contribution appears after exits.</span></div>"
    )
    review_html = strategy_review_panel()
    indices_html = market_indices_html()

    stage_order = ["morning", "confirmation", "report"]
    health_rows = []
    for stage in stage_order:
        info = pipeline.get(stage, {})
        status = info.get("status", "PENDING")
        health_rows.append(
            f"<div><b>{escape(stage.title())}</b>"
            f"<span class='health {escape(status.lower())}'>{escape(status)}</span></div>"
        )
    health_html = "<div class='schedule'>" + "".join(health_rows) + "</div>"

    sector_warn = sector_warning_html(open_positions, equity)
    return_color = "var(--green)" if pnl >= 0 else "var(--red)"

    html = f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<meta http-equiv='refresh' content='300'><title>$25K Paper Portfolio</title><style>
:root{{--canvas:#f6f7f9;--surface:#ffffff;--ink:#111827;--muted:#6b7280;--line:#e5e7eb;--blue:#2563eb;--blue-soft:#eff6ff;--green:#16803c;--red:#c62828}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--canvas);color:var(--ink);font:14px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;-webkit-font-smoothing:antialiased}}
main{{max-width:1280px;margin:auto;padding:38px 28px 64px}}
header{{display:flex;justify-content:space-between;align-items:center;gap:24px;margin-bottom:34px}}
.brand{{display:flex;align-items:center;gap:13px}}
.mark{{width:38px;height:38px;border-radius:12px;background:linear-gradient(145deg,#1d4ed8,#60a5fa);box-shadow:0 8px 20px rgba(37,99,235,.18);display:grid;place-items:center;color:white;font-weight:800}}
.eyebrow{{margin:0 0 3px;color:var(--muted);font-size:11px;font-weight:700;letter-spacing:.12em;text-transform:uppercase}}
h1{{font-size:18px;letter-spacing:-.02em;margin:0}}
.status{{display:flex;align-items:center;gap:10px;background:var(--surface);border:1px solid var(--line);border-radius:999px;padding:9px 13px;color:#374151;font-size:12px;box-shadow:0 1px 2px rgba(17,24,39,.03)}}
.version{{font-weight:700;color:var(--blue);letter-spacing:.01em;white-space:nowrap}}
.dot{{width:7px;height:7px;background:#22c55e;border-radius:50%;box-shadow:0 0 0 4px #dcfce7}}
.hero{{display:grid;grid-template-columns:minmax(260px,.85fr) minmax(420px,1.4fr) minmax(180px,.55fr);align-items:end;gap:22px;padding:32px 34px;background:var(--surface);border:1px solid var(--line);border-radius:20px;box-shadow:0 8px 32px rgba(17,24,39,.045)}}
.hero-label{{color:var(--muted);font-size:13px;font-weight:600}}
.equity{{display:block;font-size:48px;line-height:1;letter-spacing:-.055em;margin-top:12px}}
.return{{text-align:right}}.return strong{{display:block;font-size:24px;letter-spacing:-.03em;color:{return_color}}}
.return span{{display:block;color:var(--muted);font-size:12px;margin-top:6px}}
.indices{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;align-self:stretch}}
.index-card{{border:1px solid var(--line);border-radius:12px;background:#fbfcfe;padding:12px 13px;min-width:0}}
.index-card small{{display:block;color:var(--muted);font-size:10px;font-weight:750;text-transform:uppercase;letter-spacing:.05em}}
.index-card strong{{display:block;margin-top:7px;font-size:18px;letter-spacing:-.02em;white-space:nowrap}}
.index-card span{{display:block;margin-top:5px;color:var(--muted);font-size:12px;white-space:nowrap}}
.index-card span.positive{{color:var(--green)}}.index-card span.negative{{color:var(--red)}}
.cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:14px 0 26px}}
.card{{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:17px 18px;box-shadow:0 2px 10px rgba(17,24,39,.025)}}
.card small,.card strong{{display:block}}
.card small{{color:var(--muted);font-size:11px;font-weight:650;letter-spacing:.025em}}
.card strong{{font-size:19px;letter-spacing:-.025em;margin-top:8px}}
.card strong.positive{{color:var(--green)}}.card strong.negative{{color:var(--red)}}
.card strong.grade-positive{{color:var(--green)}}.card strong.grade-warning{{color:#b45309}}.card strong.grade-negative{{color:var(--red)}}.card strong.grade-neutral{{color:var(--muted)}}
.util-bar{{height:4px;background:#f3f4f6;border-radius:999px;margin-top:10px;overflow:hidden}}
.util-fill{{height:100%;border-radius:999px}}
.util-label{{font-size:11px;color:var(--muted);display:block;margin-top:5px}}
.warn-banner{{background:#fffbeb;border:1px solid #fcd34d;border-radius:10px;padding:11px 16px;margin-bottom:14px;font-size:13px;color:#92400e}}
.table-hint{{margin:-4px 0 12px;color:var(--muted);font-size:12px}}
.layout{{display:grid;grid-template-columns:minmax(0,1.65fr) minmax(280px,.75fr);gap:16px;align-items:start}}
.panel{{background:var(--surface);border:1px solid var(--line);border-radius:18px;padding:22px;overflow:auto;box-shadow:0 3px 18px rgba(17,24,39,.03)}}
.panel.full{{grid-column:1/-1}}
.panel-head{{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px}}
h2{{font-size:15px;letter-spacing:-.01em;margin:0}}.subtle{{color:var(--muted);font-size:12px}}
svg{{width:100%;height:auto}}
.strategy{{background:linear-gradient(145deg,#f8fbff,#eff6ff);border-color:#dbeafe}}
.strategy p{{color:#4b5563;line-height:1.6;margin:10px 0 20px}}
.schedule{{display:grid;gap:0}}
.schedule div{{display:flex;justify-content:space-between;padding:11px 0;border-top:1px solid #dbeafe}}
.schedule b{{font-size:12px}}.schedule span{{color:var(--muted);font-size:12px}}
.health{{font-weight:750}}.health.success{{color:var(--green)}}.health.failed,.health.blocked{{color:var(--red)}}.health.skipped,.health.degraded{{color:#b7791f}}
.decision-badge{{display:inline-block;padding:9px 18px;border-radius:10px;font-size:15px;font-weight:700;margin-bottom:16px;border:1px solid}}
.decision-badge.pass,.decision-badge.approved{{background:#f0fdf4;border-color:#86efac;color:var(--green)}}
.decision-badge.blocked,.decision-badge.failed{{background:#fef2f2;border-color:#fca5a5;color:var(--red)}}
.decision-badge{{background:#eff6ff;border-color:#bfdbfe;color:#1d4ed8}}
.review-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:12px}}
.review-grid div{{border:1px solid var(--line);border-radius:10px;padding:12px;background:#fbfcfe}}
.review-grid b{{display:block;font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}}
.review-grid span{{display:block;margin-top:7px;font-size:16px;font-weight:750}}
.review-status.pass{{color:var(--green)}}.review-status.blocked,.review-status.failed{{color:var(--red)}}.review-status.monitor,.review-status.review{{color:var(--blue)}}
.review-links{{color:var(--muted);font-size:12px;margin:0}}
.earn-soon{{color:#b45309;font-weight:600}}
table{{border-collapse:collapse;width:100%;white-space:nowrap}}
th,td{{padding:10px;border-bottom:1px solid var(--line);text-align:left}}
th{{color:var(--muted);font-size:11px;letter-spacing:.05em;text-transform:uppercase;font-weight:600}}
th[data-tooltip]{{position:relative;cursor:help;text-decoration:underline dotted;text-underline-offset:3px}}
th[data-tooltip]:hover::after{{content:attr(data-tooltip);position:absolute;left:0;top:calc(100% + 8px);z-index:20;width:max-content;max-width:280px;padding:9px 11px;border-radius:8px;background:#111827;color:white;font-size:12px;line-height:1.35;font-weight:500;letter-spacing:0;text-transform:none;box-shadow:0 10px 24px rgba(17,24,39,.22);white-space:normal}}
th[data-tooltip]:hover::before{{content:"";position:absolute;left:14px;top:100%;z-index:21;border:6px solid transparent;border-bottom-color:#111827}}
tbody tr:last-child td{{border-bottom:0}}
tbody tr:hover td{{filter:brightness(.97)}}
.rpos td{{background:#f5fcf7}}.rneg td{{background:#fdf6f6}}
.positive{{color:var(--green);font-weight:700}}.negative{{color:var(--red);font-weight:700}}
.empty{{min-height:210px;display:flex;flex-direction:column;align-items:center;justify-content:center;color:var(--muted);text-align:center}}
.empty.compact{{min-height:160px}}
.empty-icon{{width:38px;height:38px;border-radius:12px;background:var(--blue-soft);color:var(--blue);display:grid;place-items:center;font-size:19px;margin-bottom:12px}}
.empty strong{{color:#374151;font-size:14px}}.empty span{{font-size:12px;margin-top:6px}}
a{{color:var(--blue)}}
#open-pos-table th{{cursor:pointer;user-select:none;white-space:nowrap}}
#open-pos-table th[data-sort='asc']::after{{content:'↑';opacity:1}}
#open-pos-table th[data-sort='desc']::after{{content:'↓';opacity:1}}
#open-pos-table th:hover{{color:var(--ink)}}
@media(max-width:1050px){{.hero{{grid-template-columns:1fr}}.return{{text-align:left}}.indices{{grid-template-columns:repeat(3,minmax(0,1fr))}}}}
@media(max-width:900px){{.cards{{grid-template-columns:repeat(2,1fr)}}.layout{{grid-template-columns:1fr}}.panel.full{{grid-column:auto}}}}
@media(max-width:560px){{main{{padding:24px 16px}}header{{align-items:flex-start}}.status{{display:none}}.hero{{padding:25px;align-items:flex-start}}.equity{{font-size:40px}}.indices{{grid-template-columns:1fr}}.cards{{grid-template-columns:1fr 1fr}}}}
</style></head><body><main>
<header>
  <div class='brand'><div class='mark'>P</div><div><p class='eyebrow'>Paper trading</p><h1>Pre-Breakout Portfolio</h1></div></div>
<div class='status'><span class='version'>{escape(version_label())}</span><span class='dot'></span>Updated {escape(as_of)}</div>
</header>
<section class='hero'>
  <div><span class='hero-label'>Total account equity</span><strong class='equity'>${equity:,.2f}</strong></div>
  {indices_html}
  <div class='return'><strong>{account_return:+.2f}%</strong><span>Trading return on ${capital_base:,.0f} bankroll</span></div>
</section>
<section class='cards'>{cards_html}</section>
<div class='layout'>
  <section class='panel'><div class='panel-head'><h2>Portfolio performance</h2><span class='subtle'>Equity over time</span></div>{equity_svg(history, capital_base)}</section>
  <aside class='panel strategy'><h2>Automated controls</h2><p>Take 50% at +{FIRST_TARGET_GAIN_PCT:g}% and move the balance to breakeven. Take another 25% at +{SECOND_TARGET_GAIN_PCT:g}%, then exit the runner {RUNNER_EXIT_SESSIONS} market sessions later.</p><div class='schedule'><div><b>Risk sizing</b><span>{RISK_PER_TRADE_PCT:g}% equity / trade</span></div><div><b>Entry pace</b><span>Up to {MAX_DAILY_PAPER_TRADES} new purchases daily</span></div><div><b>Open positions</b><span>Controlled by heat, cash, and sector limits</span></div><div><b>Exit ladder</b><span>50% at +{FIRST_TARGET_GAIN_PCT:g}% · 25% at +{SECOND_TARGET_GAIN_PCT:g}% · runner +{RUNNER_EXIT_SESSIONS} sessions</span></div><div><b>Protective stop</b><span>−{STOP_LOSS_PCT:g}% → breakeven at +{FIRST_TARGET_GAIN_PCT:g}%</span></div><div><b>Fallback time stop</b><span>10 sessions</span></div><div><b>Portfolio heat</b><span>{MAX_PORTFOLIO_HEAT_PCT:g}% max</span></div><div><b>Sector ceiling</b><span>{MAX_SECTOR_EXPOSURE_PCT:g}% · one position</span></div><div><b>Earnings blackout</b><span>5 sessions</span></div><div><b>Live spread cap</b><span>1%</span></div></div></aside>
  <section class='panel full'>
    <div class='panel-head'><h2>Open positions</h2><span class='subtle'>{len(open_positions)} active</span></div>
    <p class='table-hint'>Hover over P/L %, Days, Spread, or Band headers for definitions.</p>
    {sector_warn}{trade_table(open_positions, is_open=True)}
  </section>
  <section class='panel full'>
    <div class='panel-head'><h2>Resolved trades</h2><span class='subtle'>{len(closed)} completed</span></div>
    {trade_table(closed, is_open=False)}
  </section>
  <section class='panel'><div class='panel-head'><h2>Score-band performance</h2><span class='subtle'>Resolved outcomes</span></div>{analytics_html}</section>
  <section class='panel full'><div class='panel-head'><h2>Signal contribution</h2><span class='subtle'>Top components</span></div>{components_html}</section>
  <section class='panel full'><div class='panel-head'><h2>Strategy review</h2><span class='subtle'>10:30 improvement gate</span></div>{review_html}</section>
  <section class='panel full'><div class='panel-head'><h2>Pipeline health</h2><span class='subtle'>Same-day dependency gates</span></div>{health_html}</section>
  <section class='panel full'><div class='panel-head'><h2>Daily infographics</h2><span class='subtle'>Generated summaries</span></div>{infographic_links()}</section>
  <section class='panel full'><div class='panel-head'><h2>Account history</h2><span class='subtle'>Daily snapshots</span></div>{history_table if history_table else "<div class='empty compact'><div class='empty-icon'>▦</div><strong>No history yet</strong><span>Your first daily snapshot will appear tomorrow.</span></div>"}</section>
</div>
</main>
<script>
(function(){{
  var tbl=document.getElementById('open-pos-table');
  if(!tbl)return;
  var sortCol=null,asc=true;
  function val(td){{
    var s=(td.innerText||td.textContent||'').trim().replace(/[$,%]/g,'').replace(/,/g,'');
    var n=parseFloat(s);
    return isNaN(n)?s.toLowerCase():n;
  }}
  tbl.querySelectorAll('thead th').forEach(function(th,i){{
    th.addEventListener('click',function(){{
      if(sortCol===i)asc=!asc;else{{sortCol=i;asc=true;}}
      tbl.querySelectorAll('thead th').forEach(function(h){{h.removeAttribute('data-sort');}});
      th.setAttribute('data-sort',asc?'asc':'desc');
      var tbody=tbl.querySelector('tbody');
      var rows=Array.from(tbody.querySelectorAll('tr'));
      rows.sort(function(a,b){{
        var av=val(a.cells[i]),bv=val(b.cells[i]);
        if(typeof av==='number'&&typeof bv==='number')return asc?av-bv:bv-av;
        return asc?av.localeCompare(bv):bv.localeCompare(av);
      }});
      rows.forEach(function(r){{tbody.appendChild(r);}});
    }});
  }});
  // Row tinting
  tbl.querySelectorAll('tbody tr').forEach(function(tr){{
    if(tr.querySelector('.positive'))tr.classList.add('rpos');
    else if(tr.querySelector('.negative'))tr.classList.add('rneg');
  }});
}})();
</script>
</body></html>"""
    html = finalize_dashboard_html(html)
    DASHBOARD_FILE.write_text(html, encoding="utf-8")
    print(f"Updated overall dashboard: {DASHBOARD_FILE}")
    return DASHBOARD_FILE


_DASHBOARD_HTTP_LOCK = threading.Lock()


def dashboard_http_html() -> bytes:
    """Build dashboard HTML from PostgreSQL on each request (never serve stale exports)."""
    from daily_report import calculate_performance, load_trades

    with _DASHBOARD_HTTP_LOCK:
        performance = calculate_performance(load_trades())
        as_of = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
        build_dashboard(performance, as_of)
        return DASHBOARD_FILE.read_bytes()


def main() -> int:
    ensure_directories()
    from daily_report import calculate_performance, load_trades
    performance = calculate_performance(load_trades())
    build_dashboard(performance, datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
