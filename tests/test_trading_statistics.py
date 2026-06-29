from __future__ import annotations

import unittest

import pandas as pd

from dashboard import trading_statistics


class TradingStatisticsTests(unittest.TestCase):
    def test_resolved_trade_statistics_and_score_bands(self) -> None:
        performance = pd.DataFrame([
            {"remaining_shares": 0, "exit_datetime": "2026-06-25", "p_l": 100, "p_l_%": 10, "holding_days": 2, "morning_score": 46},
            {"remaining_shares": 0, "exit_datetime": "2026-06-26", "p_l": -50, "p_l_%": -5, "holding_days": 3, "morning_score": 42},
            {"remaining_shares": 0, "exit_datetime": "2026-06-27", "p_l": -25, "p_l_%": -2.5, "holding_days": 4, "morning_score": 38},
            {"remaining_shares": 0, "exit_datetime": "2026-06-28", "p_l": 200, "p_l_%": 20, "holding_days": 5, "morning_score": 47},
            {"remaining_shares": 0, "exit_datetime": "2026-06-29", "p_l": 50, "p_l_%": 5, "holding_days": 1, "morning_score": 37},
            {"remaining_shares": 10, "p_l": 999, "p_l_%": 99, "holding_days": 1, "morning_score": 50},
        ])

        stats = trading_statistics(performance, 25_000)

        self.assertEqual(stats["total_trades"], 5)
        self.assertAlmostEqual(stats["win_rate"], 60.0)
        self.assertAlmostEqual(stats["average_winner"], 350 / 3)
        self.assertAlmostEqual(stats["average_loser"], -37.5)
        self.assertAlmostEqual(stats["average_holding_period"], 3.0)
        self.assertAlmostEqual(stats["average_return"], 5.5)
        self.assertAlmostEqual(stats["largest_drawdown"], 75.0)
        self.assertAlmostEqual(stats["largest_drawdown_pct"], 0.3)
        self.assertAlmostEqual(stats["profit_factor"], 350 / 75)
        self.assertEqual(stats["maximum_consecutive_losses"], 2)
        self.assertEqual(stats["maximum_consecutive_wins"], 2)
        self.assertEqual(stats["score_bands"][0]["score"], "45+")
        self.assertAlmostEqual(stats["score_bands"][0]["average_return"], 15.0)


if __name__ == "__main__":
    unittest.main()
