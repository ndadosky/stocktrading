from __future__ import annotations

import unittest
from unittest.mock import patch

import pandas as pd

import daily_report


class DailyPurchaseLimitTests(unittest.TestCase):
    @staticmethod
    def existing_positions(today_count: int = 0) -> pd.DataFrame:
        return pd.DataFrame([
            {
                "trade_date": "2026-06-29" if index < today_count else "2026-06-28",
                "ticker": f"OLD{index}",
                "sector": f"Old sector {index}",
                "entry_price": 10.0,
                "shares": 1,
                "remaining_shares": 1,
                "stop_8": 9.2,
                "active_stop": 10.0,
                "shares_sold_10": 1,
                "shares_sold_20": 0,
            }
            for index in range(15)
        ])

    @staticmethod
    def confirmations() -> pd.DataFrame:
        return pd.DataFrame([
            {
                "ticker": f"NEW{index}",
                "sector": f"New sector {index}",
                "score": 50 - index,
                "current": 10.0,
                "confirmed_at": "2026-06-29T09:50:00-04:00",
            }
            for index in range(7)
        ])

    @staticmethod
    def account_summary() -> dict:
        return {
            "cash": 100_000.0,
            "equity": 100_000.0,
            "open_positions": 15,
        }

    def test_six_new_purchases_are_allowed_with_existing_open_positions(self) -> None:
        existing = self.existing_positions()
        confirmations = self.confirmations()
        summary = {
            "cash": 100_000.0,
            "equity": 100_000.0,
            "open_positions": 15,
        }

        with patch.object(daily_report, "account_summary", return_value=summary):
            result = daily_report.add_new_trades(existing, confirmations, "2026-06-29")

        additions = result[result["trade_date"].astype(str).eq("2026-06-29")]
        self.assertEqual(len(additions), 6)

    def test_report_reruns_cannot_exceed_six_purchases_for_the_date(self) -> None:
        existing = self.existing_positions(today_count=4)

        with patch.object(daily_report, "account_summary", return_value=self.account_summary()):
            result = daily_report.add_new_trades(existing, self.confirmations(), "2026-06-29")

        purchases_today = result[result["trade_date"].astype(str).eq("2026-06-29")]
        self.assertEqual(len(purchases_today), 6)


if __name__ == "__main__":
    unittest.main()
