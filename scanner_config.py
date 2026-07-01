"""Shared configuration for the local stock screener.

Every setting can be overridden with an environment variable, which makes the
same scripts usable from cron, another checkout, or a temporary test folder.
"""

from __future__ import annotations

import os
import json
from pathlib import Path


PROJECT_DIR = Path(
    os.getenv("STOCK_SCREENER_HOME", Path(__file__).resolve().parent)
).expanduser().resolve()
WATCHLIST_EXPORT_DIR = Path(
    os.getenv("STOCK_EXPORT_DIR", PROJECT_DIR / "exports")
).expanduser().resolve()
LOGS_DIR = Path(os.getenv("STOCK_LOGS_DIR", PROJECT_DIR / "logs")).expanduser().resolve()
CANDIDATES_FILE = Path(
    os.getenv("STOCK_CANDIDATES_FILE", WATCHLIST_EXPORT_DIR / "candidates_latest.csv")
).expanduser().resolve()
PAPER_TRADES_FILE = Path(
    os.getenv("STOCK_PAPER_TRADES_FILE", PROJECT_DIR / "paper_trades.csv")
).expanduser().resolve()
# Legacy SQLite path — migration scripts only.
STOCK_DB_FILE = Path(
    os.getenv("STOCK_DB_FILE", WATCHLIST_EXPORT_DIR / "stock_app.sqlite")
).expanduser().resolve()
DASHBOARD_FILE = Path(
    os.getenv("STOCK_DASHBOARD_FILE", WATCHLIST_EXPORT_DIR / "dashboard.html")
).expanduser().resolve()
PIPELINE_STATE_FILE = WATCHLIST_EXPORT_DIR / "pipeline_state.json"

STARTING_CAPITAL = float(os.getenv("STOCK_STARTING_CAPITAL", "25000"))
STRATEGY_BASELINE_FILE = PROJECT_DIR / "strategy_baseline.json"
STRATEGY_SETTINGS_FILE = PROJECT_DIR / "strategy_settings.json"
RUNTIME_STRATEGY_SETTINGS_FILE = Path(
    os.getenv("STOCK_RUNTIME_STRATEGY_SETTINGS", LOGS_DIR / "strategy_optimizer" / "active_settings.json")
).expanduser().resolve()


def load_strategy_settings() -> dict:
    """Load active, versioned strategy controls without mutating the baseline."""
    source = RUNTIME_STRATEGY_SETTINGS_FILE if RUNTIME_STRATEGY_SETTINGS_FILE.exists() else STRATEGY_SETTINGS_FILE
    with source.open(encoding="utf-8") as handle:
        return json.load(handle)


STRATEGY = load_strategy_settings()
STRATEGY_VERSION = str(STRATEGY["version"])
MIN_SCORE_FOR_MONITOR = int(STRATEGY.get("selection", {}).get("morning_monitor_min_score", 26))
MIN_SCORE_FOR_CANDIDATE = int(STRATEGY.get("selection", {}).get("morning_candidate_min_score", 35))
MIN_SCORE_FOR_BUY_NEXT_SESSION = int(STRATEGY.get("selection", {}).get("morning_buy_min_score", 42))
MIN_CONFIRMATION_SCORE_TO_BUY = int(STRATEGY.get("selection", {}).get("confirmation_buy_min_score", 40))
MORNING_WEIGHTS = STRATEGY["morning_weights"]
CONFIRMATION_WEIGHTS = STRATEGY["confirmation_weights"]
FIRST_TARGET_GAIN_PCT = float(STRATEGY["risk"]["scale_out"]["first_target_gain_pct"])
SECOND_TARGET_GAIN_PCT = float(STRATEGY["risk"]["scale_out"]["second_target_gain_pct"])
STOP_LOSS_PCT = float(STRATEGY["risk"]["stop_loss_pct"])
MAX_HOLDING_DAYS = int(STRATEGY["risk"]["max_holding_days"])
SLIPPAGE_BPS = float(STRATEGY["execution"]["slippage_bps"])
MAX_SECTOR_EXPOSURE_PCT = float(STRATEGY["risk"]["max_sector_exposure_pct"])
MAX_PORTFOLIO_HEAT_PCT = float(STRATEGY["risk"]["max_portfolio_heat_pct"])
RISK_PER_TRADE_PCT = float(STRATEGY["risk"]["risk_per_trade_pct"])
MAX_DAILY_PAPER_TRADES = int(os.getenv(
    "STOCK_MAX_DAILY_TRADES", str(STRATEGY["risk"]["max_new_positions_per_day"])
))
MAX_SHARES_PER_POSITION = int(os.getenv(
    "STOCK_MAX_SHARES_PER_POSITION", str(STRATEGY["risk"]["max_shares_per_position"])
))
MAX_POSITION_EXPOSURE_PCT = float(STRATEGY["risk"]["max_position_exposure_pct"])
MINIMUM_CASH_RESERVE_PCT = float(STRATEGY["risk"]["minimum_cash_reserve_pct"])
MAX_SECTOR_POSITIONS = int(STRATEGY["risk"]["max_sector_positions"])
EARNINGS_BLACKOUT_SESSIONS = int(STRATEGY["risk"]["earnings_blackout_sessions"])
MAX_BID_ASK_SPREAD_PCT = float(STRATEGY["risk"]["max_bid_ask_spread_pct"])
SCALE_OUT_FIRST_PCT = float(STRATEGY["risk"]["scale_out"]["first_target_initial_shares_pct"])
SCALE_OUT_SECOND_PCT = float(STRATEGY["risk"]["scale_out"]["second_target_initial_shares_pct"])
BREAKEVEN_AFTER_FIRST_TARGET_PCT = float(
    STRATEGY["risk"]["scale_out"]["breakeven_after_first_target_pct"]
)
RUNNER_STOP_GAIN_PCT = float(STRATEGY["risk"]["scale_out"]["runner_stop_gain_pct"])
RUNNER_EXIT_SESSIONS = int(
    STRATEGY["risk"]["scale_out"]["runner_exit_sessions_after_second_target"]
)


def ensure_directories() -> None:
    """Create runtime directories before a script writes output."""
    WATCHLIST_EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
