"""Generate the single, cumulative HTML dashboard for the paper account."""

from __future__ import annotations

from datetime import datetime
from html import escape
import json
from pathlib import Path
from typing import List

import pandas as pd

from scanner_config import (
    DASHBOARD_FILE, PAPER_TRADES_FILE, PIPELINE_STATE_FILE, STARTING_CAPITAL, WATCHLIST_EXPORT_DIR,
    ensure_directories,
)


def daily_history() -> pd.DataFrame:
    """Roll daily performance snapshots into one equity history."""
    rows: List[dict] = []
    for path in sorted(WATCHLIST_EXPORT_DIR.glob("paper_performance_*.csv")):
        try:
            frame = pd.read_csv(path)
            from daily_report import account_summary
            summary = account_summary(frame)
            report_date = path.stem.replace("paper_performance_", "")
            rows.append({
                "date": report_date, "trades": len(frame), "deployed": summary["deployed"],
                "cash": summary["cash"], "equity": summary["equity"],
                "p_l": summary["equity"] - STARTING_CAPITAL,
                "return_%": (summary["equity"] / STARTING_CAPITAL - 1) * 100 if STARTING_CAPITAL else 0,
            })
        except Exception as exc:
            print(f"Skipped history snapshot {path.name}: {exc}")
    return pd.DataFrame(rows)


def equity_svg(history: pd.DataFrame) -> str:
    if history.empty:
        return ("<div class='empty'><div class='empty-icon'>↗</div>"
                "<strong>Your equity curve starts tomorrow</strong>"
                "<span>Performance will appear after the first confirmed trades.</span></div>")
    values = history["equity"].astype(float).tolist()
    low, high = min(values + [STARTING_CAPITAL]), max(values + [STARTING_CAPITAL])
    spread = max(high - low, STARTING_CAPITAL * 0.01)
    points = []
    count = max(len(values) - 1, 1)
    for index, value in enumerate(values):
        x = 30 + index / count * 740
        y = 180 - (value - (low - spread * .1)) / (spread * 1.2) * 140
        points.append(f"{x:.1f},{y:.1f}")
    area_points = f"30,180 {' '.join(points)} 770,180"
    return ("<svg viewBox='0 0 800 210' role='img' aria-label='Account equity history'>"
            "<defs><linearGradient id='area' x1='0' y1='0' x2='0' y2='1'><stop offset='0' stop-color='#2563eb' stop-opacity='.18'/><stop offset='1' stop-color='#2563eb' stop-opacity='0'/></linearGradient></defs>"
            "<line x1='30' y1='180' x2='770' y2='180' stroke='#e7e9ee'/>"
            f"<polygon points='{area_points}' fill='url(#area)'/>"
            f"<polyline points='{' '.join(points)}' fill='none' stroke='#2563eb' stroke-width='3' stroke-linecap='round' stroke-linejoin='round'/>"
            f"<text x='30' y='202'>{escape(str(history.iloc[0]['date']))}</text>"
            f"<text x='680' y='202'>{escape(str(history.iloc[-1]['date']))}</text></svg>")


def trade_table(frame: pd.DataFrame) -> str:
    if frame.empty:
        return ("<div class='empty compact'><div class='empty-icon'>◎</div>"
                "<strong>No open positions</strong>"
                "<span>Tomorrow’s confirmed setups will land here automatically.</span></div>")
    columns = ["trade_date", "ticker", "sector", "earnings_date", "entry_price", "current_price", "shares", "remaining_shares",
               "p_l", "p_l_%", "status", "exit_reason", "holding_days", "confirmation_band",
               "shares_sold_10", "shares_sold_20", "shares_sold_30", "market_regime",
               "bid_ask_spread_pct", "quote_source", "review_flag"]
    display = frame[[column for column in columns if column in frame.columns]].copy()
    for column in display.select_dtypes(include=["object"]).columns:
        display[column] = display[column].map(lambda value: escape(str(value)) if pd.notna(value) else "")
    for column in ["entry_price", "current_price", "cost", "market_value"]:
        if column in display:
            display[column] = pd.to_numeric(frame[column], errors="coerce").map(lambda x: f"${x:,.2f}" if pd.notna(x) else "")
    if "p_l" in display:
        display["p_l"] = pd.to_numeric(frame["p_l"], errors="coerce").map(
            lambda x: f"<span class='{'positive' if x >= 0 else 'negative'}'>${x:,.2f}</span>" if pd.notna(x) else ""
        )
    if "p_l_%" in display:
        display["p_l_%"] = pd.to_numeric(frame["p_l_%"], errors="coerce").map(
            lambda x: f"<span class='{'positive' if x >= 0 else 'negative'}'>{x:.2f}%</span>" if pd.notna(x) else ""
        )
    return display.to_html(index=False, escape=False, border=0)


def build_dashboard(performance: pd.DataFrame, as_of: str) -> Path:
    ensure_directories()
    from daily_report import account_summary
    summary = account_summary(performance)
    cost, cash, equity = summary["deployed"], summary["cash"], summary["equity"]
    pnl = equity - STARTING_CAPITAL
    open_positions = performance[pd.to_numeric(performance["remaining_shares"], errors="coerce").fillna(0).gt(0)] if not performance.empty else performance
    closed = performance[pd.to_numeric(performance["remaining_shares"], errors="coerce").fillna(0).le(0)] if not performance.empty else performance
    hit_rate = float(closed["success"].mean() * 100) if not closed.empty and "success" in closed else 0.0
    account_return = pnl / STARTING_CAPITAL * 100 if STARTING_CAPITAL else 0.0
    cards = {
        "Available cash": f"${cash:,.2f}", "Capital deployed": f"${cost:,.2f}",
        "Cumulative P/L": f"${pnl:,.2f}", "Open positions": summary["open_positions"],
        "Portfolio heat": f"{summary.get('portfolio_heat_pct', 0):.2f}%",
        "Resolved success": f"{hit_rate:.1f}%" if len(closed) else "—",
    }
    card_html = "".join(f"<div class='card'><small>{escape(str(k))}</small><strong>{escape(str(v))}</strong></div>" for k, v in cards.items())
    history = daily_history()
    history_display = history.copy()
    for column in ["deployed", "cash", "equity", "p_l"]:
        if column in history_display:
            history_display[column] = history_display[column].map(lambda x: f"${x:,.2f}")
    if "return_%" in history_display:
        history_display["return_%"] = history_display["return_%"].map(lambda x: f"{x:.2f}%")
    history_table = history_display.to_html(index=False, border=0) if not history_display.empty else ""
    band_files = sorted(WATCHLIST_EXPORT_DIR.glob("score_band_performance_*.csv"))
    component_files = sorted(WATCHLIST_EXPORT_DIR.glob("component_performance_*.csv"))
    band = pd.read_csv(band_files[-1]) if band_files else pd.DataFrame()
    components = pd.read_csv(component_files[-1]).head(12) if component_files else pd.DataFrame()
    analytics_html = band.to_html(index=False, border=0) if not band.empty else "<div class='empty compact'><strong>Awaiting resolved trades</strong><span>Score-band results need completed outcomes.</span></div>"
    components_html = components.to_html(index=False, border=0) if not components.empty else "<div class='empty compact'><strong>Awaiting resolved trades</strong><span>Signal contribution appears after exits.</span></div>"
    try:
        pipeline = json.loads(PIPELINE_STATE_FILE.read_text(encoding="utf-8")).get("stages", {}) if PIPELINE_STATE_FILE.exists() else {}
    except Exception:
        pipeline = {}
    stage_order = ["morning", "confirmation", "report"]
    health_rows = []
    for stage in stage_order:
        info = pipeline.get(stage, {})
        status = info.get("status", "PENDING")
        health_rows.append(f"<div><b>{escape(stage.title())}</b><span class='health {escape(status.lower())}'>{escape(status)}</span></div>")
    health_html = "<div class='schedule'>" + "".join(health_rows) + "</div>"
    html = f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<meta http-equiv='refresh' content='300'><title>$25K Paper Portfolio</title><style>
:root{{--canvas:#f6f7f9;--surface:#ffffff;--ink:#111827;--muted:#6b7280;--line:#e5e7eb;--blue:#2563eb;--blue-soft:#eff6ff;--green:#16803c;--red:#c62828}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--canvas);color:var(--ink);font:14px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;-webkit-font-smoothing:antialiased}}
main{{max-width:1280px;margin:auto;padding:38px 28px 64px}}header{{display:flex;justify-content:space-between;align-items:center;gap:24px;margin-bottom:34px}}
.brand{{display:flex;align-items:center;gap:13px}}.mark{{width:38px;height:38px;border-radius:12px;background:linear-gradient(145deg,#1d4ed8,#60a5fa);box-shadow:0 8px 20px rgba(37,99,235,.18);display:grid;place-items:center;color:white;font-weight:800}}
.eyebrow{{margin:0 0 3px;color:var(--muted);font-size:11px;font-weight:700;letter-spacing:.12em;text-transform:uppercase}}h1{{font-size:18px;letter-spacing:-.02em;margin:0}}.status{{display:flex;align-items:center;gap:8px;background:var(--surface);border:1px solid var(--line);border-radius:999px;padding:9px 13px;color:#374151;font-size:12px;box-shadow:0 1px 2px rgba(17,24,39,.03)}}
.dot{{width:7px;height:7px;background:#22c55e;border-radius:50%;box-shadow:0 0 0 4px #dcfce7}}.hero{{display:flex;justify-content:space-between;align-items:end;gap:28px;padding:32px 34px;background:var(--surface);border:1px solid var(--line);border-radius:20px;box-shadow:0 8px 32px rgba(17,24,39,.045)}}
.hero-label{{color:var(--muted);font-size:13px;font-weight:600}}.equity{{display:block;font-size:48px;line-height:1;letter-spacing:-.055em;margin-top:12px}}.return{{text-align:right}}.return strong{{display:block;font-size:24px;letter-spacing:-.03em;color:{'var(--green)' if pnl >= 0 else 'var(--red)'}}}.return span{{display:block;color:var(--muted);font-size:12px;margin-top:6px}}
.cards{{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin:14px 0 26px}}.card{{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:17px 18px;box-shadow:0 2px 10px rgba(17,24,39,.025)}}.card small,.card strong{{display:block}}.card small{{color:var(--muted);font-size:11px;font-weight:650;letter-spacing:.025em}}.card strong{{font-size:19px;letter-spacing:-.025em;margin-top:8px}}
.layout{{display:grid;grid-template-columns:minmax(0,1.65fr) minmax(280px,.75fr);gap:16px;align-items:start}}.panel{{background:var(--surface);border:1px solid var(--line);border-radius:18px;padding:22px;overflow:auto;box-shadow:0 3px 18px rgba(17,24,39,.03)}}.panel.full{{grid-column:1/-1}}
.panel-head{{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px}}h2{{font-size:15px;letter-spacing:-.01em;margin:0}}.subtle{{color:var(--muted);font-size:12px}}svg{{width:100%;height:210px}}svg text{{fill:var(--muted);font-size:12px}}
.strategy{{background:linear-gradient(145deg,#f8fbff,#eff6ff);border-color:#dbeafe}}.strategy p{{color:#4b5563;line-height:1.6;margin:10px 0 20px}}.schedule{{display:grid;gap:0}}.schedule div{{display:flex;justify-content:space-between;padding:11px 0;border-top:1px solid #dbeafe}}.schedule b{{font-size:12px}}.schedule span{{color:var(--muted);font-size:12px}}.health{{font-weight:750}}.health.success{{color:var(--green)}}.health.failed,.health.blocked{{color:var(--red)}}.health.skipped,.health.degraded{{color:#b7791f}}
table{{border-collapse:collapse;width:100%;white-space:nowrap}}th,td{{padding:12px 10px;border-bottom:1px solid var(--line);text-align:left}}th{{color:var(--muted);font-size:10px;letter-spacing:.07em;text-transform:uppercase}}tbody tr:last-child td{{border-bottom:0}}.positive{{color:var(--green);font-weight:700}}.negative{{color:var(--red);font-weight:700}}
.empty{{min-height:210px;display:flex;flex-direction:column;align-items:center;justify-content:center;color:var(--muted);text-align:center}}.empty.compact{{min-height:160px}}.empty-icon{{width:38px;height:38px;border-radius:12px;background:var(--blue-soft);color:var(--blue);display:grid;place-items:center;font-size:19px;margin-bottom:12px}}.empty strong{{color:#374151;font-size:14px}}.empty span{{font-size:12px;margin-top:6px}}a{{color:var(--blue)}}
@media(max-width:900px){{.cards{{grid-template-columns:repeat(2,1fr)}}.layout{{grid-template-columns:1fr}}.panel.full{{grid-column:auto}}}}@media(max-width:560px){{main{{padding:24px 16px}}header{{align-items:flex-start}}.status{{display:none}}.hero{{padding:25px;align-items:flex-start;flex-direction:column}}.equity{{font-size:40px}}.return{{text-align:left}}.cards{{grid-template-columns:1fr 1fr}}}}
</style></head><body><main>
<header><div class='brand'><div class='mark'>P</div><div><p class='eyebrow'>Paper trading</p><h1>Pre-Breakout Portfolio</h1></div></div><div class='status'><span class='dot'></span>System ready · updated {escape(as_of)}</div></header>
<section class='hero'><div><span class='hero-label'>Total account equity</span><strong class='equity'>${equity:,.2f}</strong></div><div class='return'><strong>{account_return:+.2f}%</strong><span>All-time return on ${STARTING_CAPITAL:,.0f}</span></div></section>
<section class='cards'>{card_html}</section><div class='layout'><section class='panel'><div class='panel-head'><h2>Portfolio performance</h2><span class='subtle'>Equity over time</span></div>{equity_svg(history)}</section>
<aside class='panel strategy'><h2>Automated controls</h2><p>Scale out 50 shares at +10%, 25 at +20%, then take the final 25 at +30% or protect them at +10%.</p><div class='schedule'><div><b>Exit ladder</b><span>50 / 25 / 25</span></div><div><b>Protective stop</b><span>−8% · gap aware</span></div><div><b>Time stop</b><span>10 sessions</span></div><div><b>Portfolio heat</b><span>6% max</span></div><div><b>Sector ceiling</b><span>25%</span></div><div><b>Earnings blackout</b><span>5 sessions</span></div><div><b>Live spread cap</b><span>1%</span></div></div></aside>
<section class='panel full'><div class='panel-head'><h2>Open positions</h2><span class='subtle'>{len(open_positions)} active</span></div>{trade_table(open_positions)}</section>
<section class='panel full'><div class='panel-head'><h2>Resolved trades</h2><span class='subtle'>{len(closed)} completed</span></div>{trade_table(closed)}</section>
<section class='panel'><div class='panel-head'><h2>Score-band performance</h2><span class='subtle'>Resolved outcomes</span></div>{analytics_html}</section>
<section class='panel'><div class='panel-head'><h2>Signal contribution</h2><span class='subtle'>Top components</span></div>{components_html}</section>
<section class='panel full'><div class='panel-head'><h2>Pipeline health</h2><span class='subtle'>Same-day dependency gates</span></div>{health_html}</section>
<section class='panel full'><div class='panel-head'><h2>Account history</h2><span class='subtle'>Daily snapshots</span></div>{history_table if history_table else "<div class='empty compact'><div class='empty-icon'>▦</div><strong>No history yet</strong><span>Your first daily snapshot will appear tomorrow.</span></div>"}</section></div>
</main></body></html>"""
    DASHBOARD_FILE.write_text(html, encoding="utf-8")
    print(f"Updated overall dashboard: {DASHBOARD_FILE}")
    return DASHBOARD_FILE


def main() -> int:
    ensure_directories()
    from daily_report import calculate_performance, load_trades
    performance = calculate_performance(load_trades())
    build_dashboard(performance, datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
