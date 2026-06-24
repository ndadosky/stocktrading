"""Build and score the morning watchlist from Finviz candidates."""

from __future__ import annotations

from datetime import date, datetime
from html import escape
from pathlib import Path
from typing import Optional

import pandas as pd
import yfinance as yf
from finvizfinance.screener.overview import Overview

from scanner_config import (
    CANDIDATES_FILE,
    EARNINGS_BLACKOUT_SESSIONS,
    MIN_SCORE_FOR_BUY_NEXT_SESSION,
    MIN_SCORE_FOR_CANDIDATE,
    MIN_SCORE_FOR_MONITOR,
    MORNING_WEIGHTS,
    WATCHLIST_EXPORT_DIR,
    ensure_directories,
)
from market_calendar import sessions_until
from pipeline_health import market_gate, record_stage

FINVIZ_FILTERS = {
    "Price": "Under $20",
    "Average Volume": "Over 1M",
    "Relative Volume": "Over 1",
    "Performance": "Week Up",
    "Country": "USA",
    "Industry": "Stocks only (ex-Funds)",
}


def exclude_etfs(frame: pd.DataFrame) -> pd.DataFrame:
    """Defense in depth if Finviz ever returns a fund despite its filter."""
    if frame.empty:
        return frame
    industry = frame.get("Industry", pd.Series("", index=frame.index)).fillna("").astype(str)
    company = frame.get("Company", pd.Series("", index=frame.index)).fillna("").astype(str)
    is_etf = industry.str.contains("Exchange Traded Fund", case=False, regex=False)
    is_etf |= company.str.contains(r"\b(?:ETF|ETN)\b", case=False, regex=True)
    removed = int(is_etf.sum())
    if removed:
        print(f"Excluded {removed} ETF/ETN candidates")
    return frame.loc[~is_etf].copy()


def flatten_yfinance_columns(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    if isinstance(frame.columns, pd.MultiIndex):
        frame.columns = frame.columns.get_level_values(0)
    return frame


def score_history(history: pd.DataFrame) -> Optional[dict]:
    """Score a controlled pre-breakout setup from daily OHLCV history."""
    history = flatten_yfinance_columns(history).dropna(subset=["Close", "High", "Low"])
    if len(history) < 55:
        return None

    close = history["Close"].astype(float)
    high = history["High"].astype(float)
    low = history["Low"].astype(float)
    volume = history["Volume"].astype(float).fillna(0)
    price = float(close.iloc[-1])
    day_move = float(close.pct_change().iloc[-1] * 100)
    week_move = float((price / close.iloc[-6] - 1) * 100)
    sma20 = float(close.rolling(20).mean().iloc[-1])
    sma50 = float(close.rolling(50).mean().iloc[-1])

    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = -delta.clip(upper=0).rolling(14).mean()
    rs = gain / loss.replace(0, float("nan"))
    rsi = float((100 - 100 / (1 + rs)).iloc[-1])
    if pd.isna(rsi):
        rsi = 100.0 if float(gain.iloc[-1]) > 0 else 50.0

    prior_20d_high = float(high.iloc[-21:-1].max())
    distance_to_breakout = float((prior_20d_high / price - 1) * 100)
    recent_low = float(low.iloc[-10:].min())
    downside = float((price / recent_low - 1) * 100) if recent_low else 999.0
    avg_volume20 = float(volume.iloc[-21:-1].mean())
    relative_volume = float(volume.iloc[-1] / avg_volume20) if avg_volume20 else 0.0
    range5 = ((high - low) / close.replace(0, float("nan"))).rolling(5).mean().iloc[-1]
    range20 = ((high - low) / close.replace(0, float("nan"))).rolling(20).mean().iloc[-1]
    compressed = bool(pd.notna(range5) and pd.notna(range20) and range5 < range20 * 0.8)
    accumulation = bool(close.iloc[-2] > close.iloc[-3] and volume.iloc[-2] > avg_volume20)

    score = 0
    notes = []
    # Reward early momentum, trend, nearby breakouts, compression, and sane risk.
    if 0 < week_move <= 12:
        score += MORNING_WEIGHTS["early_weekly_momentum"]; notes.append("early weekly momentum")
    if -2 <= day_move <= 5:
        score += MORNING_WEIGHTS["controlled_daily_move"]; notes.append("controlled daily move")
    if 45 <= rsi <= 70:
        score += MORNING_WEIGHTS["healthy_rsi"]; notes.append("healthy RSI")
    if price > sma20:
        score += MORNING_WEIGHTS["above_sma20"]; notes.append("above 20SMA")
    if price > sma50:
        score += MORNING_WEIGHTS["above_sma50"]; notes.append("above 50SMA")
    if 0 <= distance_to_breakout <= 8:
        score += MORNING_WEIGHTS["near_breakout"]; notes.append("near 20-day breakout")
    if compressed:
        score += MORNING_WEIGHTS["compression"]; notes.append("range compression")
    if accumulation:
        score += MORNING_WEIGHTS["accumulation"]; notes.append("prior accumulation")
    if downside <= 12:
        score += MORNING_WEIGHTS["manageable_downside"]; notes.append("manageable downside")
    if 1 <= relative_volume <= 3:
        score += MORNING_WEIGHTS["volume_confirmation"]; notes.append("volume confirmation")

    # Explicit exhaustion penalties keep the watchlist from chasing spikes.
    if day_move > 8:
        score += MORNING_WEIGHTS["daily_overextension_penalty"]; notes.append("penalty: daily move >8%")
    if week_move > 18:
        score += MORNING_WEIGHTS["weekly_overextension_penalty"]; notes.append("penalty: weekly move >18%")
    if rsi > 75:
        score += MORNING_WEIGHTS["high_rsi_penalty"]; notes.append("penalty: RSI >75")
    if relative_volume > 5 and day_move > 5:
        score += MORNING_WEIGHTS["volume_mania_penalty"]; notes.append("penalty: possible volume mania")

    if score >= MIN_SCORE_FOR_BUY_NEXT_SESSION:
        signal = "🔥 BUY NEXT SESSION"
    elif score >= MIN_SCORE_FOR_CANDIDATE:
        signal = "👀 PRE-BREAKOUT WATCH"
    elif score >= MIN_SCORE_FOR_MONITOR:
        signal = "🟡 MONITOR"
    else:
        signal = "🔴 PASS"

    return {
        "strategy_score": int(score), "strategy_signal": signal,
        "day_move_%": round(day_move, 2), "week_move_%": round(week_move, 2),
        "rsi_14": round(rsi, 1), "sma_20": round(sma20, 2),
        "sma_50": round(sma50, 2), "distance_to_20d_high_%": round(distance_to_breakout, 2),
        "downside_to_10d_low_%": round(downside, 2), "relative_volume_20d": round(relative_volume, 2),
        "strategy_notes": ", ".join(notes),
    }


def html_document(frame: pd.DataFrame, title: str) -> str:
    table = frame.to_html(index=False, escape=True, border=0, classes="results")
    return f"""<!doctype html><html><head><meta charset=\"utf-8\"><title>{escape(title)}</title>
<style>body{{font:14px Arial,sans-serif;margin:24px;color:#17202a}}table{{border-collapse:collapse;width:100%}}
th{{background:#17202a;color:white;position:sticky;top:0}}th,td{{padding:8px;border-bottom:1px solid #ddd;text-align:left}}
tr:hover{{background:#f4f6f7}}</style></head><body><h1>{escape(title)}</h1>{table}</body></html>"""


def get_finviz_candidates() -> pd.DataFrame:
    screener = Overview()
    screener.set_filter(filters_dict=FINVIZ_FILTERS)
    return screener.screener_view()


def earnings_context(ticker: str, as_of: date) -> dict:
    """Best-effort earnings date; unknown data never masquerades as a safe date."""
    try:
        calendar = yf.Ticker(ticker).get_calendar()
        value = calendar.get("Earnings Date") if isinstance(calendar, dict) else None
        if isinstance(value, (list, tuple)):
            value = value[0] if value else None
        if value is None or pd.isna(value):
            return {"earnings_date": pd.NA, "sessions_to_earnings": pd.NA, "earnings_blocked": False, "earnings_status": "UNKNOWN"}
        earnings_date = pd.Timestamp(value).date()
        sessions = sessions_until(as_of, earnings_date)
        return {
            "earnings_date": earnings_date.isoformat(), "sessions_to_earnings": sessions,
            "earnings_blocked": 0 <= sessions <= EARNINGS_BLACKOUT_SESSIONS,
            "earnings_status": "BLOCKED" if 0 <= sessions <= EARNINGS_BLACKOUT_SESSIONS else "CLEAR",
        }
    except Exception as exc:
        print(f"Earnings date unavailable for {ticker}: {exc}")
        return {"earnings_date": pd.NA, "sessions_to_earnings": pd.NA, "earnings_blocked": False, "earnings_status": "UNKNOWN"}


def main() -> int:
    ensure_directories()
    if not market_gate("morning"):
        return 0
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    raw = exclude_etfs(get_finviz_candidates())
    if raw.empty or "Ticker" not in raw.columns:
        record_stage("morning", "FAILED", 0, "Finviz returned no usable candidates")
        print("Finviz returned no usable candidates; no files were overwritten.")
        return 1

    raw_csv = WATCHLIST_EXPORT_DIR / f"finviz_raw_{today}.csv"
    raw_html = raw_csv.with_suffix(".html")
    raw.to_csv(raw_csv, index=False)
    raw_html.write_text(html_document(raw, f"Finviz raw candidates — {today}"), encoding="utf-8")

    rows = []
    for _, candidate in raw.iterrows():
        ticker = str(candidate["Ticker"]).strip().upper()
        try:
            history = yf.download(ticker, period="6mo", interval="1d", auto_adjust=False, progress=False)
            metrics = score_history(history)
            if metrics:
                row = candidate.to_dict(); row.update(metrics); rows.append(row)
            else:
                print(f"Skipped {ticker}: insufficient daily history")
        except Exception as exc:
            print(f"Skipped {ticker}: {exc}")

    candidates = pd.DataFrame(rows)
    if candidates.empty:
        record_stage("morning", "FAILED", 0, "No candidates had sufficient daily history")
        print("No candidates had enough market data; latest candidates were not overwritten.")
        return 1
    candidates = candidates.sort_values("strategy_score", ascending=False, kind="mergesort")
    for column, default in (("earnings_date", pd.NA), ("sessions_to_earnings", pd.NA), ("earnings_blocked", False), ("earnings_status", "NOT CHECKED")):
        candidates[column] = default
    review_mask = candidates["strategy_score"] >= MIN_SCORE_FOR_CANDIDATE
    for index, row in candidates[review_mask].iterrows():
        context = earnings_context(str(row["Ticker"]), datetime.now().astimezone().date())
        for key, value in context.items():
            candidates.at[index, key] = value
    candidates.to_csv(CANDIDATES_FILE, index=False)
    CANDIDATES_FILE.with_suffix(".html").write_text(
        html_document(candidates, f"Latest scored candidates — {today}"), encoding="utf-8"
    )
    scored_csv = WATCHLIST_EXPORT_DIR / f"morning_candidates_{today}.csv"
    candidates.to_csv(scored_csv, index=False)
    scored_csv.with_suffix(".html").write_text(
        html_document(candidates, f"Scored morning candidates — {today}"), encoding="utf-8"
    )
    print(f"Saved {len(candidates)} candidates to {CANDIDATES_FILE}")
    blocked = int(candidates["earnings_blocked"].fillna(False).astype(bool).sum())
    coverage = len(candidates) / len(raw) if len(raw) else 0
    status = "SUCCESS" if coverage >= 0.80 else "DEGRADED"
    record_stage("morning", status, len(candidates), f"{coverage:.0%} history coverage; {blocked} earnings blocks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
