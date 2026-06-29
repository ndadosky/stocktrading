"""Best-case P/L projection if open positions complete the scale-out ladder."""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from daily_report import account_summary, load_trades
from scanner_config import (
    FIRST_TARGET_GAIN_PCT,
    RUNNER_EXIT_SESSIONS,
    SCALE_OUT_FIRST_PCT,
    SCALE_OUT_SECOND_PCT,
    SECOND_TARGET_GAIN_PCT,
)
from stock_storage import total_bankroll_deposits, bankroll_base


def _planned_qty(initial_shares: float, pct: float) -> int:
    if initial_shares <= 0:
        return 0
    return max(1, round(initial_shares * pct / 100))


def _position_snapshot(row: pd.Series) -> dict:
    initial_shares = float(pd.to_numeric(row.get("shares"), errors="coerce") or 0)
    remaining = float(pd.to_numeric(row.get("remaining_shares"), errors="coerce") or 0)
    entry = float(pd.to_numeric(row.get("entry_price"), errors="coerce") or 0)
    current = float(pd.to_numeric(row.get("current_price"), errors="coerce") or entry)
    initial_cost = float(pd.to_numeric(row.get("initial_cost"), errors="coerce") or (entry * initial_shares))
    realized_proceeds = float(pd.to_numeric(row.get("realized_proceeds"), errors="coerce") or 0)
    sold10 = float(pd.to_numeric(row.get("shares_sold_10"), errors="coerce") or 0)
    sold20 = float(pd.to_numeric(row.get("shares_sold_20"), errors="coerce") or 0)
    t10 = float(pd.to_numeric(row.get("target_10"), errors="coerce") or entry * (1 + FIRST_TARGET_GAIN_PCT / 100))
    t20 = float(pd.to_numeric(row.get("target_20"), errors="coerce") or entry * (1 + SECOND_TARGET_GAIN_PCT / 100))

    current_value = realized_proceeds + remaining * current
    current_p_l = current_value - initial_cost

    is_open = remaining > 0
    if not is_open:
        return {
            "ticker": str(row.get("ticker", "")),
            "name": str(row.get("name", "") or ""),
            "status": "closed",
            "current_p_l": round(current_p_l, 2),
            "best_case_p_l": round(current_p_l, 2),
            "uplift": 0.0,
            "notes": "Already closed — actual result.",
        }

    q10 = _planned_qty(initial_shares, SCALE_OUT_FIRST_PCT)
    q20 = _planned_qty(initial_shares, SCALE_OUT_SECOND_PCT)
    best_proceeds = realized_proceeds
    rem = remaining

    if sold10 < q10 and rem > 0:
        qty = min(q10 - sold10, rem)
        best_proceeds += qty * t10
        rem -= qty
    if sold20 < q20 and rem > 0:
        qty = min(q20 - sold20, rem)
        best_proceeds += qty * t20
        rem -= qty
    if rem > 0:
        best_proceeds += rem * t20

    best_p_l = best_proceeds - initial_cost
    uplift = best_p_l - current_p_l
    notes = (
        f"Assumes +{FIRST_TARGET_GAIN_PCT:g}% / +{SECOND_TARGET_GAIN_PCT:g}% scale-outs "
        f"and the runner exits {RUNNER_EXIT_SESSIONS} sessions later at +{SECOND_TARGET_GAIN_PCT:g}%."
    )
    if sold10 > 0 or sold20 > 0:
        notes = "Uses actual partial exits; projects unsold tranches at targets."

    return {
        "ticker": str(row.get("ticker", "")),
        "name": str(row.get("name", "") or ""),
        "status": "open",
        "current_p_l": round(current_p_l, 2),
        "best_case_p_l": round(best_p_l, 2),
        "uplift": round(uplift, 2),
        "notes": notes,
    }


def compute_best_case() -> dict:
    trades = load_trades()
    summary = account_summary(trades)
    positions = [_position_snapshot(row) for _, row in trades.iterrows()] if not trades.empty else []

    current_total_p_l = float(summary.get("realized_p_l", 0)) + float(summary.get("unrealized_p_l", 0))
    uplift = sum(float(p["uplift"]) for p in positions)
    best_total_p_l = current_total_p_l + uplift
    capital_base = bankroll_base()
    deposits = total_bankroll_deposits()

    return {
        "ok": True,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "assumption": (
            f"Open positions scale 50% at +{FIRST_TARGET_GAIN_PCT:g}%, 25% at "
            f"+{SECOND_TARGET_GAIN_PCT:g}%, and the runner exits {RUNNER_EXIT_SESSIONS} sessions later."
        ),
        "current": {
            "equity": round(float(summary.get("equity", capital_base)), 2),
            "total_p_l": round(current_total_p_l, 2),
            "realized_p_l": round(float(summary.get("realized_p_l", 0)), 2),
            "unrealized_p_l": round(float(summary.get("unrealized_p_l", 0)), 2),
            "open_positions": int(summary.get("open_positions", 0)),
            "closed_trades": int(summary.get("closed_trades", 0)),
            "capital_base": round(capital_base, 2),
            "deposits": round(deposits, 2),
        },
        "best_case": {
            "equity": round(float(summary.get("equity", capital_base)) + uplift, 2),
            "total_p_l": round(best_total_p_l, 2),
            "uplift": round(uplift, 2),
            "return_on_bankroll_pct": round(
                best_total_p_l / capital_base * 100 if capital_base else 0.0,
                2,
            ),
        },
        "positions": positions,
    }
