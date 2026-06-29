from __future__ import annotations

import unittest

from app_server import morning_purchase_summary, trade_exit_activity


class DayStatusExitActivityTests(unittest.TestCase):
    def test_partial_scale_out_is_visible_with_open_remainder(self) -> None:
        rows = [
            {
                "ticker": "BFLY",
                "shares_sold_10": 50,
                "target_10": 8.9299,
                "remaining_shares": 50,
                "realized_p_l": 40.14,
                "target_10_hit_at": "2026-06-29T09:30:00-04:00",
                "exit_datetime": "2026-06-29T09:30:00-04:00",
            }
        ]

        activity = trade_exit_activity(rows)

        self.assertEqual(len(activity), 1)
        self.assertEqual(activity[0]["event"], "Scale +10%")
        self.assertEqual(activity[0]["shares sold"], 50)
        self.assertEqual(activity[0]["remaining shares"], 50)
        self.assertEqual(activity[0]["position"], "Open remainder")

    def test_rows_without_sales_are_not_listed(self) -> None:
        self.assertEqual(
            trade_exit_activity([{"ticker": "TEST", "remaining_shares": 100}]),
            [],
        )


class DayStatusMorningPurchaseTests(unittest.TestCase):
    def test_zero_purchase_message_is_explicit(self) -> None:
        summary = morning_purchase_summary(
            [{"ticker": "BFLY", "trade_date": "2026-06-28"}],
            "2026-06-29",
        )

        self.assertEqual(summary["count"], 0)
        self.assertEqual(summary["message"], "0 stocks bought this morning")

    def test_today_purchase_lists_ticker(self) -> None:
        summary = morning_purchase_summary(
            [
                {"ticker": "BFLY", "trade_date": "2026-06-28"},
                {"ticker": "ABCD", "trade_date": "2026-06-29"},
            ],
            "2026-06-29",
        )

        self.assertEqual(summary["count"], 1)
        self.assertEqual(summary["tickers"], ["ABCD"])
        self.assertEqual(summary["message"], "1 stock bought this morning: ABCD")


if __name__ == "__main__":
    unittest.main()
