"""Manage the paper-account lifecycle and generate daily analytics."""

from __future__ import annotations

from datetime import datetime
from html import escape
from math import floor
from typing import Optional

import pandas as pd
import yfinance as yf

from scanner_config import (
    BREAKEVEN_AFTER_FIRST_TARGET_PCT, EARNINGS_BLACKOUT_SESSIONS,
    FIRST_TARGET_GAIN_PCT, MAX_BID_ASK_SPREAD_PCT, MAX_DAILY_PAPER_TRADES,
    MAX_HOLDING_DAYS, MAX_PORTFOLIO_HEAT_PCT,
    MAX_POSITION_EXPOSURE_PCT, MAX_SECTOR_EXPOSURE_PCT, MAX_SECTOR_POSITIONS,
    MAX_SHARES_PER_POSITION,
    MINIMUM_CASH_RESERVE_PCT, MIN_CONFIRMATION_SCORE_TO_BUY, PAPER_TRADES_FILE,
    RISK_PER_TRADE_PCT, RUNNER_EXIT_SESSIONS, RUNNER_STOP_GAIN_PCT,
    SCALE_OUT_FIRST_PCT, SCALE_OUT_SECOND_PCT, SECOND_TARGET_GAIN_PCT,
    SLIPPAGE_BPS, STARTING_CAPITAL, STOP_LOSS_PCT, STRATEGY_VERSION,
    WATCHLIST_EXPORT_DIR, conviction_multiplier, ensure_directories,
)
from market_calendar import sessions_until
from pipeline_health import market_gate, record_stage, require_today_snapshot
from stock_storage import append_snapshot, bankroll_base, load_paper_trades, save_paper_trades, total_bankroll_deposits
from strategy_optimizer import strategy_assignment, strategy_routing_context

TRADE_COLUMNS = [
    "trade_id", "trade_date", "entry_datetime", "ticker", "sector", "market_regime",
    "rsi_14", "morning_score", "confirmation_score", "confirmation_band", "earnings_date",
    "sessions_to_earnings", "earnings_status", "confirmation_components", "entry_volume", "bid", "ask",
    "catalyst_mode", "catalyst_configured", "news_count_72h", "catalyst_positive_count",
    "catalyst_risk_count", "catalyst_shadow_score", "catalyst_flags", "latest_headline", "latest_news_url",
    "bid_ask_spread_pct", "quote_source", "spread_proxy_pct", "quoted_entry_price",
    "entry_price", "initial_cost", "initial_risk", "shares", "remaining_shares", "status", "current_price", "target_10", "target_20",
    "target_30", "stop_8", "active_stop", "exit_datetime", "exit_price", "exit_reason",
    "realized_proceeds", "realized_p_l", "shares_sold_10", "shares_sold_20",
    "shares_sold_30", "shares_sold_protect", "shares_sold_stop", "shares_sold_time",
    "target_10_hit_at", "target_20_hit_at", "target_30_hit_at", "last_evaluated_at",
    "corporate_action_factor", "last_corporate_action_at", "data_failure_count", "review_flag",
    "holding_days", "entry_strategy_version", "active_strategy_version",
    "exit_strategy_version", "strategy_changed_mid_trade", "strategy_changed_at",
    "optimizer_arm", "strategy_first_target_pct", "strategy_second_target_pct",
    "strategy_stop_loss_pct", "strategy_max_holding_days", "strategy_runner_exit_sessions",
    "strategy_scale_first_pct", "strategy_scale_second_pct", "strategy_breakeven_pct",
    "strategy_runner_stop_pct", "strategy_trailing_stop_pct", "highest_price_since_entry",
    "strategy_slippage_bps",
    "last_updated",
]
OPEN_STATUS = "OPEN"
LEGACY_STRATEGY_VERSION = "legacy-pre-v2.2.5"


def flatten_columns(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    if isinstance(frame.columns, pd.MultiIndex):
        frame.columns = frame.columns.get_level_values(0)
    return frame


def intraday_history(ticker: str) -> pd.DataFrame:
    now = pd.Timestamp.now(tz="America/New_York")
    frame = yf.download(
        ticker, start=(now - pd.Timedelta(days=59)).date().isoformat(),
        end=(now + pd.Timedelta(days=1)).date().isoformat(), interval="5m",
        auto_adjust=False, progress=False, prepost=False
    )
    frame = flatten_columns(frame)
    if frame.empty or not isinstance(frame.index, pd.DatetimeIndex):
        return pd.DataFrame()
    frame = frame.dropna(subset=["Open", "High", "Low", "Close"])
    if frame.index.tz is None:
        frame.index = frame.index.tz_localize("America/New_York")
    else:
        frame.index = frame.index.tz_convert("America/New_York")
    return frame.between_time("09:30", "16:00")


def load_trades() -> pd.DataFrame:
    trades = load_paper_trades(TRADE_COLUMNS, PAPER_TRADES_FILE)
    for column in TRADE_COLUMNS:
        if column not in trades.columns:
            trades[column] = pd.NA
    if not trades.empty:
        trades["status"] = trades["status"].fillna(OPEN_STATUS)
        entry = pd.to_numeric(trades["entry_price"], errors="coerce")
        remaining = pd.to_numeric(trades["remaining_shares"], errors="coerce").fillna(
            pd.to_numeric(trades["shares"], errors="coerce").fillna(0)
        )
        active = remaining.gt(0)
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        missing_entry = trades["entry_strategy_version"].isna() | trades["entry_strategy_version"].astype(str).str.strip().eq("")
        trades.loc[missing_entry, "entry_strategy_version"] = LEGACY_STRATEGY_VERSION
        missing_active = trades["active_strategy_version"].isna() | trades["active_strategy_version"].astype(str).str.strip().eq("")
        trades.loc[missing_active & ~active, "active_strategy_version"] = trades.loc[missing_active & ~active, "entry_strategy_version"]
        trades.loc[missing_active & active, "active_strategy_version"] = STRATEGY_VERSION
        routed = trades["optimizer_arm"].fillna("").astype(str).isin({"champion", "challenger"})
        strategy_change = active & ~routed & trades["active_strategy_version"].astype(str).ne(STRATEGY_VERSION)
        trades.loc[strategy_change, "active_strategy_version"] = STRATEGY_VERSION
        entry_differs = active & trades["entry_strategy_version"].astype(str).ne(trades["active_strategy_version"].astype(str))
        trades["strategy_changed_mid_trade"] = (
            trades["strategy_changed_mid_trade"].astype(str).str.lower().isin({"1", "true", "yes"}) | entry_differs
        )
        changed_at_missing = trades["strategy_changed_at"].isna() | trades["strategy_changed_at"].astype(str).str.strip().eq("")
        trades.loc[entry_differs & changed_at_missing, "strategy_changed_at"] = now
        missing_exit = trades["exit_strategy_version"].isna() | trades["exit_strategy_version"].astype(str).str.strip().eq("")
        trades.loc[~active & missing_exit, "exit_strategy_version"] = trades.loc[~active & missing_exit, "active_strategy_version"]
        # Apply the active strategy to positions still carrying shares. Database
        # column names remain legacy names for compatibility with existing Pi data.
        legacy_active = active & ~routed
        trades.loc[legacy_active, "target_10"] = entry[legacy_active] * (1 + FIRST_TARGET_GAIN_PCT / 100)
        trades.loc[legacy_active, "target_20"] = entry[legacy_active] * (1 + SECOND_TARGET_GAIN_PCT / 100)
        trades.loc[legacy_active, "target_30"] = pd.NA
        trades.loc[legacy_active, "stop_8"] = entry[legacy_active] * (1 - STOP_LOSS_PCT / 100)
        trades["active_stop"] = effective_stops(trades)
    return trades[TRADE_COLUMNS]


def effective_stops(trades: pd.DataFrame) -> pd.Series:
    """Return the protective stop currently active for each trade."""
    entry = pd.to_numeric(trades["entry_price"], errors="coerce").fillna(0)
    original = pd.to_numeric(trades["stop_8"], errors="coerce").fillna(entry)
    if "active_stop" in trades.columns:
        stored = pd.to_numeric(trades["active_stop"], errors="coerce").fillna(original)
    else:
        stored = original.copy()
    sold10 = pd.to_numeric(trades["shares_sold_10"], errors="coerce").fillna(0)
    sold20 = pd.to_numeric(trades["shares_sold_20"], errors="coerce").fillna(0)
    calculated = original.copy()
    calculated = calculated.where(
        sold10.le(0), entry * (1 + BREAKEVEN_AFTER_FIRST_TARGET_PCT / 100)
    )
    calculated = calculated.where(
        sold20.le(0), entry * (1 + RUNNER_STOP_GAIN_PCT / 100)
    )
    return pd.concat([original, stored, calculated], axis=1).max(axis=1)


def calculate_position_size(
    equity: float, available_cash: float, entry_price: float,
    stop_loss_pct: float = STOP_LOSS_PCT, slippage_bps: float = SLIPPAGE_BPS,
    conviction_score: float | None = None,
) -> int:
    """Size a trade from stop risk while preserving exposure and cash limits.

    When conviction_score is provided, the risk budget is scaled by
    conviction_multiplier() so stronger setups receive more capital.
    """
    if equity <= 0 or available_cash <= 0 or entry_price <= 0:
        return 0
    stop_reference = entry_price * (1 - stop_loss_pct / 100)
    stop_fill = stop_reference * (1 - slippage_bps / 10_000)
    risk_per_share = entry_price - stop_fill
    risk_budget = equity * RISK_PER_TRADE_PCT / 100 * conviction_multiplier(conviction_score)
    exposure_budget = equity * MAX_POSITION_EXPOSURE_PCT / 100
    reserve = equity * MINIMUM_CASH_RESERVE_PCT / 100
    deployable_cash = max(0.0, available_cash - reserve)
    if risk_per_share <= 0 or deployable_cash <= 0:
        return 0
    return max(0, min(
        MAX_SHARES_PER_POSITION,
        floor(risk_budget / risk_per_share),
        floor(exposure_budget / entry_price),
        floor(deployable_cash / entry_price),
    ))


def account_summary(trades: pd.DataFrame) -> dict:
    deposits = total_bankroll_deposits()
    capital_base = bankroll_base()
    if trades.empty:
        return {
            "cash": capital_base, "open_value": 0.0, "equity": capital_base,
            "realized_p_l": 0.0, "unrealized_p_l": 0.0, "deployed": 0.0,
            "portfolio_heat": 0.0, "portfolio_heat_pct": 0.0,
            "open_positions": 0, "closed_trades": 0,
            "starting_capital": STARTING_CAPITAL, "capital_deposits": deposits,
            "capital_base": capital_base,
        }
    entry = pd.to_numeric(trades["entry_price"], errors="coerce").fillna(0)
    shares = pd.to_numeric(trades["shares"], errors="coerce").fillna(0)
    remaining = pd.to_numeric(trades["remaining_shares"], errors="coerce").fillna(shares)
    current = pd.to_numeric(trades["current_price"], errors="coerce").fillna(entry)
    proceeds = pd.to_numeric(trades["realized_proceeds"], errors="coerce").fillna(0)
    realized_by_row = pd.to_numeric(trades["realized_p_l"], errors="coerce").fillna(0)
    is_open = remaining > 0
    if "initial_cost" in trades.columns:
        initial_cost = pd.to_numeric(trades["initial_cost"], errors="coerce").fillna(entry * shares)
    else:
        initial_cost = entry * shares
    total_entry_cost = float(initial_cost.sum())
    cash = capital_base - total_entry_cost + float(proceeds.sum())
    open_value = float((current[is_open] * remaining[is_open]).sum())
    realized = float(realized_by_row.sum())
    unrealized = float(((current[is_open] - entry[is_open]) * remaining[is_open]).sum())
    stops = effective_stops(trades)
    portfolio_heat = float(((entry[is_open] - stops[is_open]).clip(lower=0) * remaining[is_open]).sum())
    equity = cash + open_value
    return {
        "cash": cash, "open_value": open_value, "equity": equity,
        "realized_p_l": realized, "unrealized_p_l": unrealized,
        "deployed": float((entry[is_open] * remaining[is_open]).sum()),
        "portfolio_heat": portfolio_heat,
        "portfolio_heat_pct": portfolio_heat / equity * 100 if equity else 0.0,
        "open_positions": int(is_open.sum()), "closed_trades": int((~is_open).sum()),
        "starting_capital": STARTING_CAPITAL, "capital_deposits": deposits,
        "capital_base": capital_base,
    }


def _entry_datetime(row, today: str) -> str:
    value = getattr(row, "confirmed_at", None)
    if value is not None and not pd.isna(value):
        return str(value)
    return f"{today}T09:50:00-04:00"


def add_new_trades(trades: pd.DataFrame, confirmations: pd.DataFrame, today: str) -> pd.DataFrame:
    """Add score-ranked entries while enforcing cash and sector concentration."""
    if confirmations.empty:
        return trades
    eligible = confirmations.sort_values("score", ascending=False, kind="mergesort")
    existing = set(zip(trades["trade_date"].astype(str), trades["ticker"].astype(str).str.upper()))
    purchases_today = int(trades["trade_date"].astype(str).eq(today).sum())
    daily_slots = max(0, MAX_DAILY_PAPER_TRADES - purchases_today)
    summary = account_summary(trades)
    available_cash = max(0.0, float(summary["cash"]))
    open_mask = pd.to_numeric(trades["remaining_shares"], errors="coerce").fillna(0).gt(0) if not trades.empty else pd.Series(dtype=bool)
    open_tickers = (
        set(trades.loc[open_mask, "ticker"].astype(str).str.upper())
        if not trades.empty else set()
    )
    active_cost = (
        pd.to_numeric(trades.loc[open_mask, "entry_price"], errors="coerce").fillna(0)
        * pd.to_numeric(trades.loc[open_mask, "remaining_shares"], errors="coerce").fillna(0)
    ) if not trades.empty else pd.Series(dtype=float)
    sector_cost = active_cost.groupby(trades.loc[open_mask, "sector"].fillna("Unknown")).sum().to_dict() if not trades.empty else {}
    sector_counts = trades.loc[open_mask, "sector"].fillna("Unknown").value_counts().to_dict() if not trades.empty else {}
    equity = float(summary["equity"])
    sector_limit = equity * MAX_SECTOR_EXPOSURE_PCT / 100
    current_heat = float(((
        pd.to_numeric(trades.loc[open_mask, "entry_price"], errors="coerce").fillna(0)
        - effective_stops(trades.loc[open_mask])
    ) * pd.to_numeric(trades.loc[open_mask, "remaining_shares"], errors="coerce").fillna(0)).sum()) if not trades.empty else 0.0
    heat_limit = equity * MAX_PORTFOLIO_HEAT_PCT / 100
    additions = []
    routing_context = strategy_routing_context()
    for row in eligible.itertuples(index=False):
        if len(additions) >= daily_slots:
            print(f"Skipped remaining candidates: {MAX_DAILY_PAPER_TRADES} new-purchase daily limit reached")
            break
        ticker = str(row.ticker).upper()
        assignment = strategy_assignment(ticker, today, routing_context)
        assigned_settings = assignment["settings"]
        selection = assigned_settings.get("selection", {})
        risk = assigned_settings["risk"]
        scale = risk["scale_out"]
        execution_settings = assigned_settings["execution"]
        confirmation_minimum = int(selection.get("confirmation_buy_min_score", MIN_CONFIRMATION_SCORE_TO_BUY))
        morning_minimum = int(selection.get("morning_candidate_min_score", 35))
        morning_score = pd.to_numeric(getattr(row, "morning_score", pd.NA), errors="coerce")
        if int(row.score) < confirmation_minimum:
            continue
        if pd.notna(morning_score) and float(morning_score) < morning_minimum:
            continue
        catalyst = assigned_settings.get("catalyst", {})
        if catalyst.get("use_for_selection", False):
            risk_value = pd.to_numeric(getattr(row, "catalyst_risk_count", 0), errors="coerce")
            score_value = pd.to_numeric(getattr(row, "catalyst_shadow_score", 0), errors="coerce")
            risk_count = int(risk_value) if pd.notna(risk_value) else 0
            shadow_score = float(score_value) if pd.notna(score_value) else 0.0
            if risk_count > 0 or shadow_score < float(catalyst.get("minimum_shadow_score", 0)):
                print(f"Skipped {ticker}: catalyst-v1 risk gate")
                continue
        if (today, ticker) in existing:
            continue
        if ticker in open_tickers:
            print(f"Skipped {ticker}: existing open position")
            continue
        earnings_blocked = getattr(row, "earnings_blocked", False)
        if str(earnings_blocked).strip().lower() in {"true", "1", "yes"}:
            print(f"Skipped {ticker}: earnings within {EARNINGS_BLACKOUT_SESSIONS} sessions")
            continue
        live_spread = pd.to_numeric(getattr(row, "bid_ask_spread_pct", pd.NA), errors="coerce")
        if pd.notna(live_spread) and float(live_spread) > MAX_BID_ASK_SPREAD_PCT:
            print(f"Skipped {ticker}: {float(live_spread):.2f}% spread exceeds {MAX_BID_ASK_SPREAD_PCT:.2f}%")
            continue
        quoted = float(row.current)
        ask = pd.to_numeric(getattr(row, "ask", pd.NA), errors="coerce")
        execution_reference = float(ask) if pd.notna(ask) and float(ask) > 0 else quoted
        trade_slippage_bps = float(execution_settings.get("slippage_bps", SLIPPAGE_BPS))
        trade_stop_pct = float(risk.get("stop_loss_pct", STOP_LOSS_PCT))
        execution = execution_reference * (1 + trade_slippage_bps / 10_000)
        shares = calculate_position_size(
            equity, available_cash, execution, trade_stop_pct, trade_slippage_bps,
            conviction_score=float(row.score),
        )
        if shares < 1:
            print(f"Skipped {ticker}: risk sizing, exposure, or cash reserve permits no shares")
            continue
        stop_reference = execution * (1 - trade_stop_pct / 100)
        trade_cost = execution * shares
        initial_risk = (execution - stop_reference * (1 - trade_slippage_bps / 10_000)) * shares
        sector_value = getattr(row, "sector", "Unknown")
        sector = "Unknown" if pd.isna(sector_value) or not str(sector_value).strip() else str(sector_value)
        if trade_cost > available_cash + 0.005:
            print(f"Skipped {ticker}: ${trade_cost:,.2f} exceeds ${available_cash:,.2f} cash")
            continue
        if float(sector_cost.get(sector, 0)) + trade_cost > sector_limit + 0.005:
            print(f"Skipped {ticker}: {sector} exposure would exceed {MAX_SECTOR_EXPOSURE_PCT:.0f}%")
            continue
        if int(sector_counts.get(sector, 0)) >= MAX_SECTOR_POSITIONS:
            print(f"Skipped {ticker}: already holding {MAX_SECTOR_POSITIONS} {sector} position")
            continue
        if current_heat + initial_risk > heat_limit + 0.005:
            print(f"Skipped {ticker}: portfolio heat would exceed {MAX_PORTFOLIO_HEAT_PCT:.1f}%")
            continue
        raw_components = [getattr(row, "morning_components", ""), getattr(row, "notes", "")]
        components = " | ".join(str(value) for value in raw_components if not pd.isna(value) and str(value).strip())
        regime = str(getattr(row, "market_regime", "UNKNOWN") or "UNKNOWN").upper()
        target_multiplier = (
            float(scale.get("risk_on_target_multiplier", 1.0)) if regime == "RISK ON"
            else float(scale.get("risk_off_target_multiplier", 1.0)) if regime == "RISK OFF"
            else 1.0
        )
        first_target_pct = float(scale["first_target_gain_pct"]) * target_multiplier
        second_target_pct = float(scale["second_target_gain_pct"]) * target_multiplier
        additions.append({
            "trade_id": f"{today}-{ticker}", "trade_date": today,
            "entry_datetime": _entry_datetime(row, today), "ticker": ticker,
            "sector": sector, "market_regime": getattr(row, "market_regime", "UNKNOWN"),
            "rsi_14": getattr(row, "rsi_14", pd.NA), "morning_score": getattr(row, "morning_score", pd.NA),
            "confirmation_score": int(row.score),
            "confirmation_band": getattr(row, "confirmation_band", "B"),
            "earnings_date": getattr(row, "earnings_date", pd.NA),
            "sessions_to_earnings": getattr(row, "sessions_to_earnings", pd.NA),
            "earnings_status": getattr(row, "earnings_status", "UNKNOWN"),
            "confirmation_components": components,
            "entry_volume": getattr(row, "confirmation_volume", pd.NA),
            "catalyst_mode": getattr(row, "catalyst_mode", "SHADOW"),
            "catalyst_configured": getattr(row, "catalyst_configured", False),
            "news_count_72h": getattr(row, "news_count_72h", 0),
            "catalyst_positive_count": getattr(row, "catalyst_positive_count", 0),
            "catalyst_risk_count": getattr(row, "catalyst_risk_count", 0),
            "catalyst_shadow_score": getattr(row, "catalyst_shadow_score", 0),
            "catalyst_flags": getattr(row, "catalyst_flags", ""),
            "latest_headline": getattr(row, "latest_headline", ""),
            "latest_news_url": getattr(row, "latest_news_url", ""),
            "bid": getattr(row, "bid", pd.NA), "ask": getattr(row, "ask", pd.NA),
            "bid_ask_spread_pct": getattr(row, "bid_ask_spread_pct", pd.NA),
            "quote_source": getattr(row, "quote_source", "5M RANGE PROXY"),
            "spread_proxy_pct": getattr(row, "spread_proxy_pct", pd.NA),
            "quoted_entry_price": quoted, "entry_price": execution,
            "initial_cost": trade_cost, "initial_risk": initial_risk,
            "shares": shares, "remaining_shares": shares,
            "status": OPEN_STATUS, "current_price": execution,
            "target_10": execution * (1 + first_target_pct / 100),
            "target_20": execution * (1 + second_target_pct / 100), "target_30": pd.NA,
            "stop_8": execution * (1 - trade_stop_pct / 100),
            "active_stop": execution * (1 - trade_stop_pct / 100),
            "exit_datetime": pd.NA, "exit_price": pd.NA, "exit_reason": pd.NA,
            "realized_proceeds": 0.0, "realized_p_l": 0.0,
            "shares_sold_10": 0, "shares_sold_20": 0, "shares_sold_30": 0,
            "shares_sold_protect": 0, "shares_sold_stop": 0, "shares_sold_time": 0,
            "target_10_hit_at": pd.NA, "target_20_hit_at": pd.NA, "target_30_hit_at": pd.NA,
            "last_evaluated_at": _entry_datetime(row, today), "holding_days": 1,
            "corporate_action_factor": 1.0, "last_corporate_action_at": pd.NA,
            "data_failure_count": 0, "review_flag": "",
            "entry_strategy_version": assignment["version"],
            "active_strategy_version": assignment["version"],
            "exit_strategy_version": pd.NA,
            "strategy_changed_mid_trade": False,
            "strategy_changed_at": pd.NA,
            "optimizer_arm": assignment["arm"],
            "strategy_first_target_pct": first_target_pct,
            "strategy_second_target_pct": second_target_pct,
            "strategy_stop_loss_pct": trade_stop_pct,
            "strategy_max_holding_days": int(risk["max_holding_days"]),
            "strategy_runner_exit_sessions": int(scale["runner_exit_sessions_after_second_target"]),
            "strategy_scale_first_pct": float(scale["first_target_initial_shares_pct"]),
            "strategy_scale_second_pct": float(scale["second_target_initial_shares_pct"]),
            "strategy_breakeven_pct": float(scale["breakeven_after_first_target_pct"]),
            "strategy_runner_stop_pct": float(scale["runner_stop_gain_pct"]),
            "strategy_trailing_stop_pct": float(scale.get("trailing_stop_pct", 0)),
            "highest_price_since_entry": execution,
            "strategy_slippage_bps": trade_slippage_bps,
            "last_updated": datetime.now().astimezone().isoformat(timespec="seconds"),
        })
        available_cash -= trade_cost
        sector_cost[sector] = float(sector_cost.get(sector, 0)) + trade_cost
        sector_counts[sector] = int(sector_counts.get(sector, 0)) + 1
        current_heat += initial_risk
        open_tickers.add(ticker)
    if additions:
        new_rows = pd.DataFrame(additions, columns=TRADE_COLUMNS)
        trades = new_rows if trades.empty else pd.concat([trades, new_rows], ignore_index=True)
    return trades


def _holding_days(entry: pd.Timestamp, current: pd.Timestamp) -> int:
    return max(1, sessions_until(entry.date(), current.date()) + 1)


def runner_exit_due(
    second_target_hit_at: object, current: pd.Timestamp,
    sessions: int = RUNNER_EXIT_SESSIONS,
) -> bool:
    """Return true after the configured sessions following the second target."""
    if second_target_hit_at is None or pd.isna(second_target_hit_at):
        return False
    hit = pd.Timestamp(second_target_hit_at)
    if hit.tzinfo is None:
        hit = hit.tz_localize("America/New_York")
    else:
        hit = hit.tz_convert("America/New_York")
    if current.tzinfo is None:
        current = current.tz_localize("America/New_York")
    else:
        current = current.tz_convert("America/New_York")
    return sessions_until(hit.date(), current.date()) >= sessions


def apply_corporate_actions(trades: pd.DataFrame) -> pd.DataFrame:
    """Adjust active share/price fields for splits; flag unavailable symbols."""
    if trades.empty:
        return trades
    result = trades.copy()
    active = pd.to_numeric(result["remaining_shares"], errors="coerce").fillna(0).gt(0)
    share_fields = ["shares", "remaining_shares", "shares_sold_10", "shares_sold_20", "shares_sold_30",
                    "shares_sold_protect", "shares_sold_stop", "shares_sold_time"]
    price_fields = ["entry_price", "current_price", "target_10", "target_20", "target_30", "stop_8", "active_stop"]
    for idx, row in result[active].iterrows():
        ticker = str(row["ticker"])
        try:
            actions = yf.Ticker(ticker).actions
            if actions.empty or "Stock Splits" not in actions:
                continue
            actions = actions.copy()
            if actions.index.tz is None:
                actions.index = actions.index.tz_localize("America/New_York")
            else:
                actions.index = actions.index.tz_convert("America/New_York")
            last_action = pd.Timestamp(row["last_corporate_action_at"]) if pd.notna(row["last_corporate_action_at"]) else pd.Timestamp(row["entry_datetime"])
            if last_action.tzinfo is None:
                last_action = last_action.tz_localize("America/New_York")
            splits = actions[actions.index > last_action]
            splits = splits[pd.to_numeric(splits["Stock Splits"], errors="coerce").fillna(0) > 0]
            for timestamp, action in splits.iterrows():
                ratio = float(action["Stock Splits"])
                for field in share_fields:
                    value = pd.to_numeric(result.at[idx, field], errors="coerce")
                    if pd.notna(value): result.at[idx, field] = float(value) * ratio
                for field in price_fields:
                    value = pd.to_numeric(result.at[idx, field], errors="coerce")
                    if pd.notna(value): result.at[idx, field] = float(value) / ratio
                factor = pd.to_numeric(result.at[idx, "corporate_action_factor"], errors="coerce")
                result.at[idx, "corporate_action_factor"] = (float(factor) if pd.notna(factor) else 1.0) * ratio
                result.at[idx, "last_corporate_action_at"] = timestamp.isoformat()
                result.at[idx, "review_flag"] = f"SPLIT {ratio:g}:1 APPLIED"
        except Exception as exc:
            print(f"Corporate actions unavailable for {ticker}: {exc}")
    return result


def _sell_lot(
    frame: pd.DataFrame, idx, shares_to_sell: float, reference_price: float,
    reason: str, timestamp: pd.Timestamp, bucket: str,
    slippage_bps: float = SLIPPAGE_BPS,
) -> None:
    """Book one partial exit with adverse exit slippage."""
    remaining = float(frame.at[idx, "remaining_shares"])
    quantity = min(remaining, max(0.0, float(shares_to_sell)))
    if quantity <= 0:
        return
    fill = reference_price * (1 - slippage_bps / 10_000)
    entry = float(frame.at[idx, "entry_price"])
    frame.at[idx, "remaining_shares"] = remaining - quantity
    frame.at[idx, "realized_proceeds"] = float(pd.to_numeric(frame.at[idx, "realized_proceeds"], errors="coerce") or 0) + fill * quantity
    frame.at[idx, "realized_p_l"] = float(pd.to_numeric(frame.at[idx, "realized_p_l"], errors="coerce") or 0) + (fill - entry) * quantity
    frame.at[idx, bucket] = float(pd.to_numeric(frame.at[idx, bucket], errors="coerce") or 0) + quantity
    frame.at[idx, "exit_datetime"] = timestamp.isoformat()
    frame.at[idx, "exit_price"] = fill
    frame.at[idx, "exit_reason"] = reason
    frame.at[idx, "status"] = "CLOSED" if remaining - quantity <= 0 else "PARTIAL"
    if remaining - quantity <= 0:
        active_version = frame.at[idx, "active_strategy_version"]
        frame.at[idx, "exit_strategy_version"] = (
            str(active_version) if pd.notna(active_version) and str(active_version).strip() else STRATEGY_VERSION
        )


def update_trade_lifecycle(trades: pd.DataFrame) -> pd.DataFrame:
    """Scale out 50/25/25 and ratchet protection after each target."""
    if trades.empty:
        return trades
    result = trades.copy()
    now = pd.Timestamp.now(tz="America/New_York")
    numeric_defaults = {
        "remaining_shares": result["shares"], "realized_proceeds": 0.0, "realized_p_l": 0.0,
        "shares_sold_10": 0.0, "shares_sold_20": 0.0, "shares_sold_30": 0.0,
        "shares_sold_protect": 0.0, "shares_sold_stop": 0.0, "shares_sold_time": 0.0,
    }
    for column, default in numeric_defaults.items():
        result[column] = pd.to_numeric(result[column], errors="coerce").fillna(default)
    result["active_stop"] = effective_stops(result)
    active = result["remaining_shares"].gt(0)
    for idx, row in result[active].iterrows():
        ticker = str(row["ticker"])
        try:
            def strategy_value(column: str, fallback: float) -> float:
                value = pd.to_numeric(row.get(column), errors="coerce")
                return float(value) if pd.notna(value) else float(fallback)

            first_target_pct = strategy_value("strategy_first_target_pct", FIRST_TARGET_GAIN_PCT)
            second_target_pct = strategy_value("strategy_second_target_pct", SECOND_TARGET_GAIN_PCT)
            stop_loss_pct = strategy_value("strategy_stop_loss_pct", STOP_LOSS_PCT)
            max_holding_days = int(strategy_value("strategy_max_holding_days", MAX_HOLDING_DAYS))
            runner_sessions = int(strategy_value("strategy_runner_exit_sessions", RUNNER_EXIT_SESSIONS))
            scale_first_pct = strategy_value("strategy_scale_first_pct", SCALE_OUT_FIRST_PCT)
            scale_second_pct = strategy_value("strategy_scale_second_pct", SCALE_OUT_SECOND_PCT)
            breakeven_pct = strategy_value("strategy_breakeven_pct", BREAKEVEN_AFTER_FIRST_TARGET_PCT)
            runner_stop_pct = strategy_value("strategy_runner_stop_pct", RUNNER_STOP_GAIN_PCT)
            trailing_stop_pct = strategy_value("strategy_trailing_stop_pct", 0)
            trade_slippage_bps = strategy_value("strategy_slippage_bps", SLIPPAGE_BPS)
            highest_value = pd.to_numeric(row.get("highest_price_since_entry"), errors="coerce")
            highest_price = float(highest_value) if pd.notna(highest_value) else float(row["entry_price"])
            bars = intraday_history(ticker)
            if bars.empty:
                prior_failures = pd.to_numeric(result.at[idx, "data_failure_count"], errors="coerce")
                failures = (int(prior_failures) if pd.notna(prior_failures) else 0) + 1
                result.at[idx, "data_failure_count"] = failures
                if failures >= 3:
                    result.at[idx, "review_flag"] = "REVIEW: 3 DATA FAILURES / SYMBOL ACTION"
                print(f"Kept {ticker} open: no intraday data")
                continue
            result.at[idx, "data_failure_count"] = 0
            entry_time = pd.Timestamp(row["entry_datetime"])
            if entry_time.tzinfo is None:
                entry_time = entry_time.tz_localize("America/New_York")
            else:
                entry_time = entry_time.tz_convert("America/New_York")
            last_value = row.get("last_evaluated_at")
            if pd.isna(last_value):
                last_time = entry_time
            else:
                last_time = pd.Timestamp(last_value)
                if last_time.tzinfo is None:
                    last_time = last_time.tz_localize("America/New_York")
                else:
                    last_time = last_time.tz_convert("America/New_York")
            after = bars[bars.index > last_time]
            current = float(bars["Close"].iloc[-1])
            result.at[idx, "current_price"] = current
            days = _holding_days(entry_time, now)
            result.at[idx, "holding_days"] = days
            first_target = float(row["target_10"])
            entry = float(row["entry_price"])
            breakeven_floor = entry * (1 + breakeven_pct / 100)
            protect_floor = entry * (1 + runner_stop_pct / 100)
            for timestamp, bar in after.iterrows():
                remaining = float(result.at[idx, "remaining_shares"])
                if remaining <= 0:
                    break
                low, high = float(bar["Low"]), float(bar["High"])
                result.at[idx, "last_evaluated_at"] = timestamp.isoformat()
                target10_already_active = float(result.at[idx, "shares_sold_10"]) > 0
                target20_already_active = float(result.at[idx, "shares_sold_20"]) > 0
                active_stop = float(result.at[idx, "active_stop"])
                # The stop already active before this bar wins any same-bar ambiguity.
                if low <= active_stop:
                    gap_aware_stop = min(active_stop, float(bar["Open"]))
                    if target20_already_active:
                        reason, bucket = f"PROTECT +{runner_stop_pct:g}%", "shares_sold_protect"
                    elif target10_already_active:
                        reason, bucket = "PROTECT BREAKEVEN", "shares_sold_protect"
                    else:
                        reason, bucket = f"STOP -{stop_loss_pct:g}%", "shares_sold_stop"
                    _sell_lot(result, idx, remaining, gap_aware_stop, reason, timestamp, bucket, trade_slippage_bps)
                    break
                if target20_already_active and runner_exit_due(result.at[idx, "target_20_hit_at"], timestamp, runner_sessions):
                    reason = f"RUNNER EXIT {runner_sessions}D AFTER +{second_target_pct:g}%"
                    _sell_lot(result, idx, remaining, float(bar["Open"]), reason, timestamp, "shares_sold_time", trade_slippage_bps)
                    break
                initial = float(result.at[idx, "shares"])
                if float(result.at[idx, "shares_sold_10"]) <= 0 and high >= first_target:
                    quantity = max(1, round(initial * scale_first_pct / 100))
                    _sell_lot(result, idx, quantity, first_target, f"SCALE +{first_target_pct:g}%", timestamp, "shares_sold_10", trade_slippage_bps)
                    result.at[idx, "target_10_hit_at"] = timestamp.isoformat()
                    result.at[idx, "active_stop"] = max(float(result.at[idx, "active_stop"]), breakeven_floor)
                if float(result.at[idx, "remaining_shares"]) > 0 and float(result.at[idx, "shares_sold_20"]) <= 0 and high >= float(row["target_20"]):
                    quantity = max(1, round(initial * scale_second_pct / 100))
                    _sell_lot(result, idx, quantity, float(row["target_20"]), f"SCALE +{second_target_pct:g}%", timestamp, "shares_sold_20", trade_slippage_bps)
                    result.at[idx, "target_20_hit_at"] = timestamp.isoformat()
                    result.at[idx, "active_stop"] = max(float(result.at[idx, "active_stop"]), protect_floor)
                highest_price = max(highest_price, high)
                result.at[idx, "highest_price_since_entry"] = highest_price
                if trailing_stop_pct > 0 and float(result.at[idx, "shares_sold_10"]) > 0:
                    trailing_floor = highest_price * (1 - trailing_stop_pct / 100)
                    result.at[idx, "active_stop"] = max(float(result.at[idx, "active_stop"]), trailing_floor)
            remaining = float(result.at[idx, "remaining_shares"])
            if remaining > 0 and days >= max_holding_days:
                timestamp = after.index[-1] if not after.empty else bars.index[-1]
                _sell_lot(result, idx, remaining, current, f"TIME EXIT {max_holding_days}D", timestamp, "shares_sold_time", trade_slippage_bps)
        except Exception as exc:
            print(f"Kept {ticker} open: {exc}")
    result["last_updated"] = datetime.now().astimezone().isoformat(timespec="seconds")
    return result


def calculate_performance(trades: pd.DataFrame) -> pd.DataFrame:
    result = trades.copy()
    numeric = ["entry_price", "quoted_entry_price", "shares", "remaining_shares", "confirmation_score", "current_price",
               "target_10", "target_20", "target_30", "stop_8", "active_stop", "exit_price", "realized_p_l",
               "realized_proceeds", "shares_sold_10", "shares_sold_20", "shares_sold_30",
               "shares_sold_protect", "shares_sold_stop", "shares_sold_time"]
    for column in numeric:
        if column not in result:
            result[column] = pd.NA
        result[column] = pd.to_numeric(result[column], errors="coerce")
    is_open = result["remaining_shares"].fillna(result["shares"]).gt(0) if not result.empty else pd.Series(dtype=bool)
    result["cost"] = result["entry_price"] * result["shares"]
    result["market_value"] = 0.0
    if not result.empty:
        result.loc[is_open, "market_value"] = result.loc[is_open, "current_price"] * result.loc[is_open, "remaining_shares"]
    result["p_l"] = result["realized_p_l"].fillna(0)
    if not result.empty:
        result.loc[is_open, "p_l"] += (
            (result.loc[is_open, "current_price"] - result.loc[is_open, "entry_price"])
            * result.loc[is_open, "remaining_shares"]
        )
    result["p_l_%"] = result["p_l"] / result["cost"] * 100
    result["success"] = result["shares_sold_10"].fillna(0).gt(0)
    return result.sort_values(["status", "p_l_%"], ascending=[False, False], kind="mergesort")


def grouped_analytics(frame: pd.DataFrame, field: str) -> pd.DataFrame:
    closed = frame[frame["status"].eq("CLOSED")].copy()
    if closed.empty or field not in closed:
        return pd.DataFrame(columns=[field, "resolved", "success_rate_%", "average_return_%", "total_p_l"])
    return closed.groupby(field, dropna=False).agg(
        resolved=("trade_id", "count"), success_rate_=("success", "mean"),
        average_return_=("p_l_%", "mean"), total_p_l=("p_l", "sum"),
    ).reset_index().rename(columns={"success_rate_": "success_rate_%", "average_return_": "average_return_%"}).assign(
        **{"success_rate_%": lambda x: (x["success_rate_%"] * 100).round(1),
           "average_return_%": lambda x: x["average_return_%"].round(2),
           "total_p_l": lambda x: x["total_p_l"].round(2)}
    )


def component_analytics(frame: pd.DataFrame) -> pd.DataFrame:
    closed = frame[frame["status"].eq("CLOSED")].copy()
    if closed.empty:
        return pd.DataFrame(columns=["component", "resolved", "success_rate_%", "average_return_%"])
    closed["component"] = closed["confirmation_components"].fillna("").str.replace(" | ", ", ", regex=False).str.split(", ")
    exploded = closed.explode("component")
    exploded = exploded[exploded["component"].astype(str).str.len() > 0]
    if exploded.empty:
        return pd.DataFrame(columns=["component", "resolved", "success_rate_%", "average_return_%"])
    result = exploded.groupby("component").agg(
        resolved=("trade_id", "count"), success_rate_=("success", "mean"), average_return_=("p_l_%", "mean")
    ).reset_index()
    result["success_rate_%"] = (result.pop("success_rate_") * 100).round(1)
    result["average_return_%"] = result.pop("average_return_").round(2)
    return result.sort_values(["success_rate_%", "resolved"], ascending=False)


def render_report(frame: pd.DataFrame, today: str, band: pd.DataFrame, components: pd.DataFrame) -> str:
    summary = account_summary(frame)
    resolved = frame[frame["status"].eq("CLOSED")]
    hit_rate = float(resolved["success"].mean() * 100) if not resolved.empty else 0.0
    stats = {
        "Account equity": f"${summary['equity']:,.2f}", "Available cash": f"${summary['cash']:,.2f}",
        "Open positions": summary["open_positions"], "Resolved trades": summary["closed_trades"],
        "Realized P/L": f"${summary['realized_p_l']:,.2f}", "Unrealized P/L": f"${summary['unrealized_p_l']:,.2f}",
        "Resolved success": f"{hit_rate:.1f}%",
        "Portfolio heat": f"{summary['portfolio_heat_pct']:.2f}%",
    }
    cards = "".join(f"<div class='card'><small>{escape(str(k))}</small><strong>{escape(str(v))}</strong></div>" for k, v in stats.items())
    table = frame.to_html(index=False, escape=True, border=0)
    band_html = band.to_html(index=False, border=0) if not band.empty else "<p>Waiting for resolved trades.</p>"
    component_html = components.to_html(index=False, border=0) if not components.empty else "<p>Waiting for resolved trades.</p>"
    return f"""<!doctype html><html><head><meta charset='utf-8'><title>Daily paper report — {today}</title><style>
body{{font:14px -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;margin:0;padding:32px;background:#f6f7f9;color:#111827}}h1{{letter-spacing:-.03em}}.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin:24px 0}}.card,section{{background:white;border:1px solid #e5e7eb;border-radius:14px;padding:18px}}.card small,.card strong{{display:block}}.card small{{color:#6b7280}}.card strong{{font-size:20px;margin-top:8px}}section{{margin-top:14px;overflow:auto}}table{{border-collapse:collapse;width:100%;white-space:nowrap}}th,td{{padding:9px;border-bottom:1px solid #e5e7eb;text-align:left}}th{{font-size:11px;color:#6b7280;text-transform:uppercase}}</style></head>
<body><h1>Daily paper report · {today}</h1><div class='cards'>{cards}</div><section><h2>Trades</h2>{table}</section><section><h2>Score-band performance</h2>{band_html}</section><section><h2>Signal-component performance</h2>{component_html}</section></body></html>"""


def main() -> int:
    ensure_directories()
    if not market_gate("report"):
        return 0
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    confirmations = require_today_snapshot("confirmations", "confirm_date", "report")
    if confirmations is None:
        print("Missing confirmations in PostgreSQL. Run confirm_945.py first.")
        return 1
    required = {"ticker", "current", "score"}
    if not required.issubset(confirmations.columns):
        print(f"Confirmation file is missing columns: {sorted(required - set(confirmations.columns))}")
        return 1
    if "session_date" in confirmations.columns:
        confirmations = confirmations[confirmations["session_date"].astype(str).eq(today)].copy()
    if confirmations.empty:
        record_stage("report", "BLOCKED", 0, "No confirmations from today's market session")
        print("Confirmation file contains no rows from today's market session.")
        return 1
    trades = update_trade_lifecycle(apply_corporate_actions(load_trades()))
    trades = add_new_trades(trades, confirmations, today)
    save_paper_trades(trades, TRADE_COLUMNS)
    performance = calculate_performance(trades)
    band = grouped_analytics(performance, "confirmation_band")
    components = component_analytics(performance)
    append_snapshot("paper_performance", performance, "report_date", today)
    append_snapshot("score_band_performance", band, "report_date", today)
    append_snapshot("component_performance", components, "report_date", today)
    report_html = render_report(performance, today, band, components)
    html_path = WATCHLIST_EXPORT_DIR / f"daily_report_{today}.html"
    html_path.write_text(report_html, encoding="utf-8")
    from dashboard import build_dashboard
    build_dashboard(performance, datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z"))
    print(f"Saved {len(performance)} trades to PostgreSQL and {html_path}")
    record_stage("report", "SUCCESS", len(performance), f"Equity ${account_summary(performance)['equity']:,.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
