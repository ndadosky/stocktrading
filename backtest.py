"""Replay the 9:45 confirmation and paper-trade rules for a historical date.

Historical Finviz snapshots are not available from Finviz. Pass a saved raw or
scored candidate CSV from the date being tested for an unbiased replay. Using a
later snapshot is supported, but the report clearly marks the look-ahead bias.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
from html import escape
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
import yfinance as yf

from scanner_config import MAX_DAILY_PAPER_TRADES, WATCHLIST_EXPORT_DIR, ensure_directories


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
    end = target + timedelta(days=1)
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
    for condition, note in (
        (current > open_price, "above open"), (current > prior_close, "above prior close"),
        (current > vwap, "above VWAP"), (current > first_15m_high, "broke first 15m high"),
        (vs_open <= 5, "not overextended"),
    ):
        if condition:
            score += 10
            notes.append(note)
    if vs_open > 8:
        score -= 15
        notes.append("too extended")

    entry = float(later["Open"].iloc[0])
    after_entry = session[session.index >= later.index[0]]
    close_price = float(session["Close"].iloc[-1])
    high_after_entry = float(after_entry["High"].max())
    low_after_entry = float(after_entry["Low"].min())
    return {
        "ticker": ticker, "confirmation_score": score,
        "signal": "🔥 BUY TODAY" if score >= 40 else "👀 WAIT" if score >= 25 else "🔴 PASS",
        "confirmation_price": round(current, 4), "entry_time": str(later.index[0].time()),
        "entry_price": round(entry, 4), "close_price": round(close_price, 4),
        "close_return_%": round((close_price / entry - 1) * 100, 2),
        "max_gain_%": round((high_after_entry / entry - 1) * 100, 2),
        "max_drawdown_%": round((low_after_entry / entry - 1) * 100, 2),
        "hit_10": high_after_entry >= entry * 1.10,
        "hit_20": high_after_entry >= entry * 1.20,
        "hit_30": high_after_entry >= entry * 1.30,
        "stop_8_hit": low_after_entry <= entry * 0.92,
        "p_l_100_shares": round((close_price - entry) * 100, 2),
        "notes": ", ".join(notes),
    }


def report_html(results: pd.DataFrame, target: date, source: Path, biased: bool) -> str:
    trades = results[results["selected"]] if not results.empty else results
    total_cost = float((trades["entry_price"] * 100).sum()) if not trades.empty else 0
    total_pnl = float(trades["p_l_100_shares"].sum()) if not trades.empty else 0
    summary = {
        "Trades": len(trades), "Capital deployed": f"${total_cost:,.2f}",
        "Close P/L": f"${total_pnl:,.2f}",
        "Close return": f"{total_pnl / total_cost * 100:.2f}%" if total_cost else "0.00%",
        "Winners / losers": f"{(trades['close_return_%'] > 0).sum()} / {(trades['close_return_%'] < 0).sum()}" if not trades.empty else "0 / 0",
        "Touched 10/20/30%": f"{trades.hit_10.sum()} / {trades.hit_20.sum()} / {trades.hit_30.sum()}" if not trades.empty else "0 / 0 / 0",
        "Touched -8% stop": int(trades.stop_8_hit.sum()) if not trades.empty else 0,
    }
    cards = "".join(f"<div><b>{escape(str(k))}</b><span>{escape(str(v))}</span></div>" for k, v in summary.items())
    shown = results.copy()
    if not shown.empty:
        shown["close_return_%"] = shown["close_return_%"].map(
            lambda x: f"<span class='{'pos' if x >= 0 else 'neg'}'>{x:.2f}%</span>"
        )
    table = shown.to_html(index=False, escape=False, border=0)
    warning = ("LOOK-AHEAD-BIASED APPROXIMATION: the candidate snapshot is from a later date. "
               "Confirmation and price outcomes use only the requested date, but candidate selection does not.") if biased else "Candidate snapshot matches the backtest date."
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
    # Stable, pre-entry tie-breaking: never use future return to choose among
    # names with the same confirmation score.
    results = results.sort_values(
        ["confirmation_score", "candidate_rank"], ascending=[False, True], kind="mergesort"
    )
    results["selected"] = False
    qualifying = results.index[results["confirmation_score"] >= 40][:max(args.limit, 0)]
    results.loc[qualifying, "selected"] = True
    csv_path = WATCHLIST_EXPORT_DIR / f"backtest_{target}.csv"
    html_path = csv_path.with_suffix(".html")
    results.to_csv(csv_path, index=False)
    biased = snapshot_date(source) != target
    html_path.write_text(report_html(results, target, source, biased), encoding="utf-8")
    selected = results[results["selected"]]
    print(f"Saved {len(results)} confirmations and {len(selected)} selected trades to {csv_path}")
    if not selected.empty:
        print(selected[["ticker", "confirmation_score", "entry_price", "close_price", "close_return_%", "p_l_100_shares"]].to_string(index=False))
        print(f"Total close P/L: ${selected['p_l_100_shares'].sum():,.2f}")
    if biased:
        print("WARNING: candidate universe comes from a different date; results have look-ahead bias.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
