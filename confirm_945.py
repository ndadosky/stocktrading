"""Confirm morning candidates using regular-session five-minute data."""

from __future__ import annotations

from datetime import datetime
from html import escape
from typing import Optional

import pandas as pd
import yfinance as yf

from scanner_config import (
    CANDIDATES_FILE, CONFIRMATION_WEIGHTS, WATCHLIST_EXPORT_DIR, ensure_directories,
)
from pipeline_health import market_gate, record_stage, require_today_snapshot
from stock_storage import append_snapshot


def flatten_columns(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    if isinstance(frame.columns, pd.MultiIndex):
        frame.columns = frame.columns.get_level_values(0)
    return frame


def regular_session(frame: pd.DataFrame) -> pd.DataFrame:
    """Restrict intraday rows to 09:30–16:00 America/New_York."""
    frame = flatten_columns(frame)
    if not isinstance(frame.index, pd.DatetimeIndex):
        return frame
    if frame.index.tz is None:
        frame.index = frame.index.tz_localize("America/New_York")
    else:
        frame.index = frame.index.tz_convert("America/New_York")
    return frame.between_time("09:30", "16:00")


def confirm_ticker(ticker: str) -> Optional[dict]:
    data = yf.download(ticker, period="5d", interval="5m", auto_adjust=False, progress=False, prepost=False)
    data = regular_session(data).dropna(subset=["Open", "High", "Low", "Close"])
    if data.empty:
        return None
    latest_date = data.index[-1].date()
    full_session = data[data.index.date == latest_date]
    session = full_session.between_time("09:30", "09:45")
    prior = data[data.index.date < latest_date]
    if len(session) < 4 or prior.empty:
        return None

    open_price = float(session["Open"].iloc[0])
    current = float(session["Close"].iloc[-1])
    prior_close = float(prior["Close"].iloc[-1])
    first_15m_high = float(session["High"].iloc[:3].max())
    typical = (session["High"] + session["Low"] + session["Close"]) / 3
    cumulative_volume = float(session["Volume"].fillna(0).sum())
    if cumulative_volume <= 0:
        return None
    vwap = float((typical * session["Volume"].fillna(0)).sum() / cumulative_volume)
    vs_open = (current / open_price - 1) * 100
    vs_prior = (current / prior_close - 1) * 100

    score, notes = 0, []
    # Each confirmation is worth ten points; extension receives a strong penalty.
    for condition, note, weight in (
        (current > open_price, "above open", CONFIRMATION_WEIGHTS["above_open"]),
        (current > prior_close, "above prior close", CONFIRMATION_WEIGHTS["above_prior_close"]),
        (current > vwap, "above VWAP", CONFIRMATION_WEIGHTS["above_vwap"]),
        (current > first_15m_high, "broke first 15m high", CONFIRMATION_WEIGHTS["first_15m_breakout"]),
        (vs_open <= 5, "not overextended", CONFIRMATION_WEIGHTS["not_overextended"]),
    ):
        if condition:
            score += weight; notes.append(note)
    if vs_open > 8:
        score += CONFIRMATION_WEIGHTS["too_extended_penalty"]; notes.append("too extended")
    signal = "🔥 BUY TODAY" if score >= 40 else "👀 WAIT" if score >= 25 else "🔴 PASS"
    return {
        "ticker": ticker, "session_date": str(latest_date),
        "confirmed_at": session.index[-1].isoformat(), "open": round(open_price, 4),
        "current": round(current, 4), "prior_close": round(prior_close, 4),
        "vwap": round(vwap, 4), "first_15m_high": round(first_15m_high, 4),
        "current_vs_open_%": round(vs_open, 2), "current_vs_prior_%": round(vs_prior, 2),
        "score": score, "signal": signal, "notes": ", ".join(notes),
        "confirmation_volume": int(session["Volume"].fillna(0).sum()),
        "spread_proxy_pct": round((float(session["High"].iloc[-1]) - float(session["Low"].iloc[-1])) / current * 100, 3),
        "target_10": round(current * 1.10, 4), "target_20": round(current * 1.20, 4),
        "target_30": round(current * 1.30, 4), "stop_8": round(current * 0.92, 4),
    }


def market_regime() -> dict:
    """Describe broad trend context once per confirmation run."""
    try:
        spy = yf.download("SPY", period="3mo", interval="1d", auto_adjust=False, progress=False)
        spy = flatten_columns(spy).dropna(subset=["Close"])
        close = spy["Close"].astype(float)
        if len(close) < 50:
            return {"market_regime": "UNKNOWN", "spy_5d_%": pd.NA}
        price = float(close.iloc[-1])
        sma20 = float(close.rolling(20).mean().iloc[-1])
        sma50 = float(close.rolling(50).mean().iloc[-1])
        label = "RISK ON" if price > sma20 > sma50 else "RISK OFF" if price < sma20 < sma50 else "MIXED"
        return {"market_regime": label, "spy_5d_%": round((price / close.iloc[-6] - 1) * 100, 2)}
    except Exception as exc:
        print(f"Market regime unavailable: {exc}")
        return {"market_regime": "UNKNOWN", "spy_5d_%": pd.NA}


def live_quote(ticker: str) -> dict:
    """Best-effort actual quote; keep the bar-range proxy when unavailable."""
    try:
        info = yf.Ticker(ticker).get_info()
        bid, ask = float(info.get("bid") or 0), float(info.get("ask") or 0)
        if bid > 0 and ask >= bid:
            midpoint = (bid + ask) / 2
            return {"bid": bid, "ask": ask, "bid_ask_spread_pct": round((ask - bid) / midpoint * 100, 3), "quote_source": "LIVE BID/ASK"}
    except Exception as exc:
        print(f"Quote unavailable for {ticker}: {exc}")
    return {"bid": pd.NA, "ask": pd.NA, "bid_ask_spread_pct": pd.NA, "quote_source": "5M RANGE PROXY"}


def write_html(frame: pd.DataFrame, path, title: str) -> None:
    table = frame.to_html(index=False, escape=True, border=0)
    path.write_text(f"""<!doctype html><html><head><meta charset=\"utf-8\"><title>{escape(title)}</title>
<style>body{{font:14px Arial;margin:24px}}table{{border-collapse:collapse;width:100%}}th{{background:#222;color:#fff}}
th,td{{padding:8px;border-bottom:1px solid #ddd;text-align:left}}tr:hover{{background:#f3f3f3}}</style></head>
<body><h1>{escape(title)}</h1>{table}</body></html>""", encoding="utf-8")


def main() -> int:
    ensure_directories()
    if not market_gate("confirmation"):
        return 0
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    candidates = require_today_snapshot("morning_candidates", "scan_date", "confirmation")
    if candidates is None:
        print("Missing morning candidates in PostgreSQL. Run morning_candidates.py first.")
        return 1
    ticker_col = "Ticker" if "Ticker" in candidates.columns else "ticker"
    if ticker_col not in candidates.columns:
        print("Candidates file has no Ticker column."); return 1
    results = []
    regime = market_regime()
    metadata = candidates.copy()
    metadata["_ticker"] = metadata[ticker_col].astype(str).str.upper()
    metadata = metadata.drop_duplicates("_ticker").set_index("_ticker")
    for ticker in candidates[ticker_col].dropna().astype(str).str.upper().unique():
        try:
            result = confirm_ticker(ticker)
            if result:
                if result["session_date"] != today:
                    print(f"Skipped {ticker}: latest intraday session is {result['session_date']}, expected {today}")
                    continue
                source = metadata.loc[ticker] if ticker in metadata.index else pd.Series(dtype=object)
                result.update({
                    "sector": source.get("Sector", "Unknown"),
                    "rsi_14": source.get("rsi_14", pd.NA),
                    "morning_score": source.get("strategy_score", pd.NA),
                    "morning_signal": source.get("strategy_signal", ""),
                    "morning_components": source.get("strategy_notes", ""),
                    "earnings_date": source.get("earnings_date", pd.NA),
                    "sessions_to_earnings": source.get("sessions_to_earnings", pd.NA),
                    "earnings_blocked": source.get("earnings_blocked", False),
                    "earnings_status": source.get("earnings_status", "UNKNOWN"),
                    "confirmation_band": "A" if result["score"] >= 50 else "B" if result["score"] >= 40 else "C" if result["score"] >= 25 else "D",
                    **regime,
                })
                result.update(live_quote(ticker) if result["score"] >= 40 else {
                    "bid": pd.NA, "ask": pd.NA, "bid_ask_spread_pct": pd.NA, "quote_source": "NOT REQUESTED"
                })
                results.append(result)
            else: print(f"Skipped {ticker}: missing intraday data")
        except Exception as exc:
            print(f"Skipped {ticker}: {exc}")
    columns = ["ticker", "session_date", "confirmed_at", "sector", "market_regime", "spy_5d_%",
               "rsi_14", "morning_score", "morning_signal", "morning_components",
               "earnings_date", "sessions_to_earnings", "earnings_blocked", "earnings_status",
               "open", "current", "prior_close", "vwap", "first_15m_high",
               "current_vs_open_%", "current_vs_prior_%", "score", "signal", "notes",
               "confirmation_band", "confirmation_volume", "bid", "ask", "bid_ask_spread_pct", "quote_source", "spread_proxy_pct",
               "target_10", "target_20", "target_30", "stop_8"]
    output = pd.DataFrame(results, columns=columns).sort_values("score", ascending=False)
    append_snapshot("confirmations", output, "confirm_date", today)
    html_path = WATCHLIST_EXPORT_DIR / f"confirm_945_{today}.html"
    write_html(output, html_path, f"9:45 confirmation — {today}")
    print(f"Saved {len(output)} confirmations to PostgreSQL (confirmations.confirm_date={today})")
    coverage = len(output) / len(candidates) if len(candidates) else 0
    status = "SUCCESS" if coverage >= 0.70 else "DEGRADED" if results else "FAILED"
    record_stage("confirmation", status, len(output), f"{coverage:.0%} intraday coverage; live quotes requested for buys")
    return 0 if results else 1


if __name__ == "__main__":
    raise SystemExit(main())
