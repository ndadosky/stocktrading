from __future__ import annotations

import unittest
from unittest.mock import patch

import pandas as pd

from backtest import simulate_scaled_exit
import daily_report
from daily_report import runner_exit_due


class ExitStrategyTests(unittest.TestCase):
    def test_trailing_stop_ratchets_after_first_target(self) -> None:
        trade = {column: pd.NA for column in daily_report.TRADE_COLUMNS}
        trade.update({
            "trade_id": "trail-test", "ticker": "TEST", "entry_price": 10.0,
            "entry_datetime": "2026-06-29T09:30:00-04:00", "shares": 100,
            "remaining_shares": 100, "status": "OPEN", "current_price": 10.0,
            "target_10": 10.5, "target_20": 11.0, "stop_8": 9.5, "active_stop": 9.5,
            "realized_proceeds": 0.0, "realized_p_l": 0.0, "shares_sold_10": 0,
            "shares_sold_20": 0, "shares_sold_30": 0, "shares_sold_protect": 0,
            "shares_sold_stop": 0, "shares_sold_time": 0, "last_evaluated_at": pd.NA,
            "entry_strategy_version": "trail-test", "active_strategy_version": "trail-test",
            "strategy_first_target_pct": 5, "strategy_second_target_pct": 10,
            "strategy_stop_loss_pct": 5, "strategy_max_holding_days": 10,
            "strategy_runner_exit_sessions": 2, "strategy_scale_first_pct": 50,
            "strategy_scale_second_pct": 25, "strategy_breakeven_pct": 0,
            "strategy_runner_stop_pct": 10, "strategy_trailing_stop_pct": 3,
            "strategy_slippage_bps": 0, "highest_price_since_entry": 10.0,
        })
        bars = pd.DataFrame(
            {
                "Open": [10.0, 10.3], "High": [10.6, 10.35],
                "Low": [9.9, 10.2], "Close": [10.55, 10.25],
            },
            index=pd.DatetimeIndex(["2026-06-29T10:00:00-04:00", "2026-06-29T10:05:00-04:00"]),
        )

        with patch.object(daily_report, "intraday_history", return_value=bars):
            result = daily_report.update_trade_lifecycle(pd.DataFrame([trade]))

        self.assertEqual(float(result.iloc[0]["remaining_shares"]), 0)
        self.assertAlmostEqual(float(result.iloc[0]["exit_price"]), 10.282, places=3)
        self.assertIn("PROTECT", str(result.iloc[0]["exit_reason"]))

    def test_existing_open_position_receives_new_targets_and_stop(self) -> None:
        legacy = pd.DataFrame([
            {
                "ticker": "TEST",
                "entry_price": 10.0,
                "shares": 100,
                "remaining_shares": 100,
                "status": "OPEN",
                "target_10": 11.0,
                "target_20": 12.0,
                "target_30": 13.0,
                "stop_8": 9.2,
                "active_stop": 9.2,
                "shares_sold_10": 0,
                "shares_sold_20": 0,
            }
        ])

        with patch.object(daily_report, "load_paper_trades", return_value=legacy):
            trades = daily_report.load_trades()

        self.assertAlmostEqual(float(trades.iloc[0]["target_10"]), 10.5)
        self.assertAlmostEqual(float(trades.iloc[0]["target_20"]), 11.0)
        self.assertTrue(pd.isna(trades.iloc[0]["target_30"]))
        self.assertAlmostEqual(float(trades.iloc[0]["stop_8"]), 9.5)
        self.assertAlmostEqual(float(trades.iloc[0]["active_stop"]), 9.5)
        self.assertEqual(trades.iloc[0]["entry_strategy_version"], daily_report.LEGACY_STRATEGY_VERSION)
        self.assertEqual(trades.iloc[0]["active_strategy_version"], daily_report.STRATEGY_VERSION)
        self.assertTrue(bool(trades.iloc[0]["strategy_changed_mid_trade"]))
        self.assertTrue(pd.notna(trades.iloc[0]["strategy_changed_at"]))

    def test_runner_becomes_due_after_two_market_sessions(self) -> None:
        hit = "2026-06-29T10:00:00-04:00"

        self.assertFalse(runner_exit_due(hit, pd.Timestamp("2026-06-30T09:30:00-04:00")))
        self.assertTrue(runner_exit_due(hit, pd.Timestamp("2026-07-01T09:30:00-04:00")))

    def test_backtest_scales_50_25_then_exits_runner(self) -> None:
        index = pd.DatetimeIndex([
            "2026-06-29T10:00:00-04:00",
            "2026-06-30T09:30:00-04:00",
            "2026-07-01T09:30:00-04:00",
        ])
        bars = pd.DataFrame(
            {
                "Open": [101.0, 111.5, 112.0],
                "High": [111.0, 112.0, 113.0],
                "Low": [100.0, 111.0, 111.0],
                "Close": [110.5, 111.5, 112.5],
            },
            index=index,
        )

        result = simulate_scaled_exit(100.0, bars)

        self.assertEqual(result["shares_sold_10"], 50)
        self.assertEqual(result["shares_sold_20"], 25)
        self.assertEqual(result["shares_sold_time"], 25)
        self.assertEqual(result["remaining_shares"], 0)
        self.assertIn("RUNNER EXIT 2D AFTER +10%", result["exit_reason"])


if __name__ == "__main__":
    unittest.main()
