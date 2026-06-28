"""Replay the 9:45 confirmation and paper-trade rules for a historical date.

Historical Finviz snapshots are not available from Finviz. Pass a saved raw or
scored candidate CSV from the date being tested for an unbiased replay. Using a
later snapshot is supported, but the report clearly marks the look-ahead bias.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
from html import escape
from math import floor
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
import yfinance as yf

from scanner_config import (
    BREAKEVEN_AFTER_TARGET_10_PCT, CONFIRMATION_WEIGHTS, FINAL_LOT_FALLBACK_PCT,
    MAX_DAILY_PAPER_TRADES, MAX_HOLDING_DAYS, MAX_PORTFOLIO_HEAT_PCT,
    MAX_POSITION_EXPOSURE_PCT, MAX_SECTOR_EXPOSURE_PCT, MAX_SECTOR_POSITIONS,
    MINIMUM_CASH_RESERVE_PCT, RISK_PER_TRADE_PCT, SCALE_OUT_10_PCT,
    SCALE_OUT_20_PCT, SLIPPAGE_BPS, STARTING_CAPITAL, STOP_LOSS_PCT,
    TAKE_PROFIT_PCT,
    WATCHLIST_EXPORT_DIR, ensure_directories,
)

REFERENCE_SHARES = 100


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay the 9:45 paper-trading strategy")
    parser.add_argument("--date", required=True, help="Trading date in YYYY-MM-DD format")
    parser.add_argument("--candidates", type=Path, help="Candidate CSV; defaults to the newest Finviz raw export")
    parser.add_argument("--limit", type=int, default=MAX_DAILY_PAPER_TRADES, help="Maximum qualifying trades")
    return parser.parse_args()


def newest_candidate_file() -> Path:
    files = sorted(WATCHLIST_EXPORT_DIR.glob("finviz_raw_*.csv"))
    if not files:
        raise FileNotFoundError("No exports/finviz_raw_*.csv candidate snapshot was found")
    return files[-1]


def snapshot_date(path: Path) -> Optional[date]:
    try:
        return datetime.strptime(path.stem.rsplit("_", 1)[-1], "%Y-%m-%d").date()
    except ValueError:
        return None


def extract_ticker(batch: pd.DataFrame, ticker: str) -> pd.DataFrame:
    if batch.empty:
        return pd.DataFrame()
    if not isinstance(batch.columns, pd.MultiIndex):
        return batch.copy()
    if ticker in batch.columns.get_level_values(0):
        return batch[ticker].copy()
    if ticker in batch.columns.get_level_values(1):
        return batch.xs(ticker, axis=1, level=1).copy()
    return pd.DataFrame()


def fetch_intraday(tickers: List[str], target: date) -> Dict[str, pd.DataFrame]:
    """Download in chunks so one bad symbol does not discard the whole universe."""
    output: Dict[str, pd.DataFrame] = {}
    start = target - timedelta(days=7)
    end = min(target + timedelta(days=22), datetime.now().date() + timedelta(days=1))
    for offset in range(0, len(tickers), 40):
        chunk = tickers[offset:offset + 40]
        try:
            batch = yf.download(
                chunk, start=start.isoformat(), end=end.isoformat(), interval="5m",
                auto_adjust=False, progress=False, prepost=False, group_by="ticker", threads=True,
            )
        except Exception as exc:
            print(f"Download chunk failed ({chunk[0]}…): {exc}")
            continue
        for ticker in chunk:
            frame = extract_ticker(batch, ticker)
            if frame.empty or "Close" not in frame.columns:
                continue
            frame = frame.dropna(subset=["Open", "High", "Low", "Close"])
            if not isinstance(frame.index, pd.DatetimeIndex) or frame.empty:
                continue
            if frame.index.tz is None:
                frame.index = frame.index.tz_localize("America/New_York")
            else:
                frame.index = frame.index.tz_convert("America/New_York")
            output[ticker] = frame.between_time("09:30", "16:00")
    return output


def simulate_scaled_exit(entry_quote: float, bars: pd.DataFrame) -> dict:
    """Mirror production slippage, 50/25/25 scale-outs, gap stops, and time exit."""
    entry = entry_quote * (1 + SLIPPAGE_BPS / 10_000)
    target10, target20, target30 = entry * 1.10, entry * 1.20, entry * 1.30
    stop = entry * (1 - STOP_LOSS_PCT / 100)
    remaining = float(REFERENCE_SHARES)
    proceeds = 0.0
    sold10 = sold20 = sold30 = sold_protect = sold_stop = sold_time = 0.0
    exit_reason = "OPEN"
    session_dates = []
    for value in bars.index.date:
        if value not in session_dates:
            session_dates.append(value)
    allowed_dates = set(session_dates[:MAX_HOLDING_DAYS])
    test_bars = bars[[value in allowed_dates for value in bars.index.date]]

    def sell(quantity: float, reference: float, reason: str) -> float:
        nonlocal remaining, proceeds, exit_reason
        quantity = min(remaining, quantity)
        fill = reference * (1 - SLIPPAGE_BPS / 10_000)
        proceeds += quantity * fill
        remaining -= quantity
        exit_reason = reason
        return quantity

    for timestamp, bar in test_bars.iterrows():
        if remaining <= 0:
            break
        low, high, opening = float(bar["Low"]), float(bar["High"]), float(bar["Open"])
        active_stop = target10 if sold20 > 0 else entry * (1 + BREAKEVEN_AFTER_TARGET_10_PCT / 100) if sold10 > 0 else stop
        if low <= active_stop:
            if sold20 > 0:
                reason = "PROTECT +10%"
            elif sold10 > 0:
                reason = "PROTECT BREAKEVEN"
            else:
                reason = "STOP -8%"
            bucket = "protect" if sold10 > 0 else "stop"
            sold = sell(remaining, min(active_stop, opening), reason)
            if bucket == "protect":
                sold_protect += sold
            else:
                sold_stop += sold
            break
        if sold10 <= 0 and high >= target10:
            sold10 += sell(max(1, round(REFERENCE_SHARES * SCALE_OUT_10_PCT / 100)), target10, "SCALE +10%")
        if remaining > 0 and sold20 <= 0 and high >= target20:
            sold20 += sell(max(1, round(REFERENCE_SHARES * SCALE_OUT_20_PCT / 100)), target20, "SCALE +20%")
        if remaining > 0 and sold20 > 0 and high >= target30:
            sold30 += sell(remaining, target30, "FINAL +30%")
            break
    complete_window = len(session_dates) >= MAX_HOLDING_DAYS
    mark = float(test_bars["Close"].iloc[-1]) if not test_bars.empty else entry
    if remaining > 0 and complete_window:
        sold_time += sell(remaining, mark, f"TIME EXIT {MAX_HOLDING_DAYS}D")
    realized = proceeds - (REFERENCE_SHARES - remaining) * entry
    unrealized = remaining * (mark - entry)
    pnl = realized + unrealized
    return {
        "entry_price": round(entry, 4), "mark_price": round(mark, 4),
        "remaining_shares": remaining, "shares_sold_10": sold10, "shares_sold_20": sold20,
        "shares_sold_30": sold30, "shares_sold_protect": sold_protect,
        "shares_sold_stop": sold_stop, "shares_sold_time": sold_time,
        "exit_reason": exit_reason, "resolved": remaining <= 0,
        "success": sold10 > 0, "p_l_100_shares": round(pnl, 2),
        "strategy_return_%": round(pnl / (entry * REFERENCE_SHARES) * 100, 2),
    }


def replay_confirmation(ticker: str, data: pd.DataFrame, target: date) -> Optional[dict]:
    session = data[data.index.date == target]
    prior = data[data.index.date < target]
    # Bars stamped 09:30, 09:35, 09:40 and 09:45 establish confirmation;
    # the following 09:50 bar supplies a non-look-ahead entry price.
    confirmation = session.between_time("09:30", "09:45")
    later = session[session.index > confirmation.index[-1]] if not confirmation.empty else session.iloc[0:0]
    if len(confirmation) < 4 or later.empty or prior.empty:
        return None

    open_price = float(confirmation["Open"].iloc[0])
    current = float(confirmation["Close"].iloc[-1])
    prior_close = float(prior["Close"].iloc[-1])
    first_15m_high = float(confirmation["High"].iloc[:3].max())
    typical = (confirmation["High"] + confirmation["Low"] + confirmation["Close"]) / 3
    volume = confirmation["Volume"].fillna(0)
    if float(volume.sum()) <= 0:
        return None
    vwap = float((typical * volume).sum() / volume.sum())
    vs_open = (current / open_price - 1) * 100

    score, notes = 0, []
    for condition, note, weight in (
        (current > open_price, "above open", CONFIRMATION_WEIGHTS["above_open"]),
        (current > prior_close, "above prior close", CONFIRMATION_WEIGHTS["above_prior_close"]),
        (current > vwap, "above VWAP", CONFIRMATION_WEIGHTS["above_vwap"]),
        (current > first_15m_high, "broke first 15m high", CONFIRMATION_WEIGHTS["first_15m_breakout"]),
        (vs_open <= 5, "not overextended", CONFIRMATION_WEIGHTS["not_overextended"]),
    ):
        if condition:
            score += weight
            notes.append(note)
    if vs_open > 8:
        score += CONFIRMATION_WEIGHTS["too_extended_penalty"]
        notes.append("too extended")

    entry_quote = float(later["Open"].iloc[0])
    after_entry = data[data.index >= later.index[0]]
    close_price = float(session["Close"].iloc[-1])
    high_after_entry = float(after_entry["High"].max())
    low_after_entry = float(after_entry["Low"].min())
    simulated = simulate_scaled_exit(entry_quote, after_entry)
    return {
        "ticker": ticker, "confirmation_score": score,
        "signal": "🔥 BUY TODAY" if score >= 40 else "👀 WAIT" if score >= 25 else "🔴 PASS",
        "confirmation_price": round(current, 4), "entry_time": str(later.index[0].time()),
        "close_price": round(close_price, 4),
        "max_gain_%": round((high_after_entry / simulated["entry_price"] - 1) * 100, 2),
        "max_drawdown_%": round((low_after_entry / simulated["entry_price"] - 1) * 100, 2),
        "notes": ", ".join(notes),
        **simulated,
    }


def report_html(results: pd.DataFrame, target: date, source: Path, biased: bool) -> str:
    trades = results[results["selected"]] if not results.empty else results
    total_cost = float((trades["entry_price"] * 100).sum()) if not trades.empty else 0
    total_pnl = float(trades["p_l_100_shares"].sum()) if not trades.empty else 0
    summary = {
        "Trades": len(trades), "Capital deployed": f"${total_cost:,.2f}",
        "Close P/L": f"${total_pnl:,.2f}",
        "Strategy return": f"{total_pnl / total_cost * 100:.2f}%" if total_cost else "0.00%",
        "Winners / losers": f"{(trades['p_l_100_shares'] > 0).sum()} / {(trades['p_l_100_shares'] < 0).sum()}" if not trades.empty else "0 / 0",
        "Scaled 10/20/30": f"{(trades.shares_sold_10 > 0).sum()} / {(trades.shares_sold_20 > 0).sum()} / {(trades.shares_sold_30 > 0).sum()}" if not trades.empty else "0 / 0 / 0",
        "Stopped / time exit": f"{(trades.shares_sold_stop > 0).sum()} / {(trades.shares_sold_time > 0).sum()}" if not trades.empty else "0 / 0",
    }
    cards = "".join(f"<div><b>{escape(str(k))}</b><span>{escape(str(v))}</span></div>" for k, v in summary.items())
    shown = results.copy()
    if not shown.empty:
        shown["strategy_return_%"] = shown["strategy_return_%"].map(
            lambda x: f"<span class='{'pos' if x >= 0 else 'neg'}'>{x:.2f}%</span>"
        )
    table = shown.to_html(index=False, escape=False, border=0)
    warning = ("LOOK-AHEAD-BIASED APPROXIMATION: the candidate snapshot is from a later date. "
               "Confirmation and price outcomes use only the requested date, but candidate selection does not.") if biased else "Candidate snapshot matches the backtest date."
    warning += " Historical bid/ask is unavailable; 10-bps slippage is used. Earnings are enforced only when present in the saved snapshot."
    return f"""<!doctype html><html><head><meta charset=\"utf-8\"><title>Backtest — {target}</title>
<style>body{{font:14px Arial;margin:24px;background:#f6f8fa;color:#17202a}}.warning{{padding:12px;background:#fff3cd;border-left:5px solid #e0a800}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px}}.cards div{{background:white;padding:12px}}.cards span{{display:block;font-size:19px;margin-top:5px}}
table{{background:white;border-collapse:collapse;width:100%;margin-top:20px}}th{{background:#17202a;color:white}}th,td{{padding:8px;border-bottom:1px solid #ddd;text-align:left}}
.pos{{color:#16803c;font-weight:bold}}.neg{{color:#c62828;font-weight:bold}}</style></head><body><h1>9:45 strategy backtest — {target}</h1>
<p class=\"warning\"><b>Universe note:</b> {escape(warning)} Source: {escape(str(source))}</p><div class=\"cards\">{cards}</div>{table}</body></html>"""


def main() -> int:
    args = parse_args()
    ensure_directories()
    target = datetime.strptime(args.date, "%Y-%m-%d").date()
    source = (args.candidates or newest_candidate_file()).expanduser().resolve()
    candidates = pd.read_csv(source)
    ticker_column = "Ticker" if "Ticker" in candidates.columns else "ticker"
    if ticker_column not in candidates.columns:
        raise ValueError(f"{source} has no Ticker column")
    tickers = candidates[ticker_column].dropna().astype(str).str.upper().unique().tolist()
    print(f"Fetching {len(tickers)} symbols for {target}…")
    market_data = fetch_intraday(tickers, target)
    rows = []
    for ticker in tickers:
        if ticker not in market_data:
            continue
        result = replay_confirmation(ticker, market_data[ticker], target)
        if result:
            rows.append(result)
    results = pd.DataFrame(rows)
    if results.empty:
        print("No symbols had sufficient data for the requested session.")
        return 1
    candidate_rank = {ticker: rank + 1 for rank, ticker in enumerate(tickers)}
    results["candidate_rank"] = results["ticker"].map(candidate_rank)
    metadata = candidates.copy()
    metadata["_ticker"] = metadata[ticker_column].astype(str).str.upper()
    metadata = metadata.drop_duplicates("_ticker").set_index("_ticker")
    results["sector"] = results["ticker"].map(lambda ticker: metadata.loc[ticker].get("Sector", "Unknown") if ticker in metadata.index else "Unknown")
    results["earnings_blocked"] = results["ticker"].map(lambda ticker: metadata.loc[ticker].get("earnings_blocked", False) if ticker in metadata.index else False)
    # Stable, pre-entry tie-breaking: never use future return to choose among
    # names with the same confirmation score.
    results = results.sort_values(
        ["confirmation_score", "candidate_rank"], ascending=[False, True], kind="mergesort"
    )
    results["selected"] = False
    results["selection_note"] = "score below 40"
    cash, heat = STARTING_CAPITAL, 0.0
    sector_used: Dict[str, float] = {}
    sector_positions: Dict[str, int] = {}
    results["position_shares"] = 0
    results["position_cost"] = 0.0
    results["position_risk"] = 0.0
    results["sized_p_l"] = 0.0
    selected_count = 0
    for index, row in results[results["confirmation_score"] >= 40].iterrows():
        if selected_count >= max(args.limit, 0):
            results.at[index, "selection_note"] = "daily limit"
            continue
        entry = float(row["entry_price"])
        stop_fill = entry * (1 - STOP_LOSS_PCT / 100) * (1 - SLIPPAGE_BPS / 10_000)
        risk_per_share = entry - stop_fill
        deployable_cash = max(0.0, cash - STARTING_CAPITAL * MINIMUM_CASH_RESERVE_PCT / 100)
        shares = max(0, min(
            floor((STARTING_CAPITAL * RISK_PER_TRADE_PCT / 100) / risk_per_share),
            floor((STARTING_CAPITAL * MAX_POSITION_EXPOSURE_PCT / 100) / entry),
            floor(deployable_cash / entry),
        )) if risk_per_share > 0 else 0
        cost = entry * shares
        risk = risk_per_share * shares
        sector = str(row["sector"])
        if str(row["earnings_blocked"]).strip().lower() in {"true", "1", "yes"}:
            results.at[index, "selection_note"] = "earnings blackout"; continue
        if shares < 1 or cost > cash:
            results.at[index, "selection_note"] = "risk sizing/cash reserve"; continue
        if sector_used.get(sector, 0) + cost > STARTING_CAPITAL * MAX_SECTOR_EXPOSURE_PCT / 100:
            results.at[index, "selection_note"] = "sector limit"; continue
        if sector_positions.get(sector, 0) >= MAX_SECTOR_POSITIONS:
            results.at[index, "selection_note"] = "sector position limit"; continue
        if heat + risk > STARTING_CAPITAL * MAX_PORTFOLIO_HEAT_PCT / 100:
            results.at[index, "selection_note"] = "portfolio heat"; continue
        results.at[index, "selected"] = True
        results.at[index, "selection_note"] = "selected"
        results.at[index, "position_shares"] = shares
        results.at[index, "position_cost"] = round(cost, 2)
        results.at[index, "position_risk"] = round(risk, 2)
        results.at[index, "sized_p_l"] = round(float(row["strategy_return_%"]) / 100 * cost, 2)
        cash -= cost; heat += risk; sector_used[sector] = sector_used.get(sector, 0) + cost
        sector_positions[sector] = sector_positions.get(sector, 0) + 1
        selected_count += 1
    csv_path = WATCHLIST_EXPORT_DIR / f"backtest_{target}.csv"
    html_path = csv_path.with_suffix(".html")
    results.to_csv(csv_path, index=False)
    biased = snapshot_date(source) != target
    html_path.write_text(report_html(results, target, source, biased), encoding="utf-8")
    selected = results[results["selected"]]
    print(f"Saved {len(results)} confirmations and {len(selected)} selected trades to {csv_path}")
    if not selected.empty:
        print(selected[["ticker", "confirmation_score", "entry_price", "position_shares", "exit_reason", "strategy_return_%", "sized_p_l"]].to_string(index=False))
        print(f"Total sized P/L: ${selected['sized_p_l'].sum():,.2f}")
    if biased:
        print("WARNING: candidate universe comes from a different date; results have look-ahead bias.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
