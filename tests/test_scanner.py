from __future__ import annotations

import unittest

import pandas as pd

from app_server import scanner_confirmation_status


class ScannerConfirmationStatusTests(unittest.TestCase):
    def test_scanner_rows_include_confirmation_outcome(self) -> None:
        columns, rows = scanner_confirmation_status(
            ["Ticker", "strategy_score"],
            [{"Ticker": "BFLY", "strategy_score": "55"}, {"Ticker": "ABCD", "strategy_score": "48"}],
            pd.DataFrame([
                {"ticker": "BFLY", "score": 50, "confirmation_band": "A"},
                {"ticker": "ABCD", "score": 30, "confirmation_band": "C"},
            ]),
        )

        self.assertEqual(columns[-1], "Confirmation status")
        self.assertEqual(rows[0]["Confirmation status"], "Confirmed · A · 50")
        self.assertEqual(rows[1]["Confirmation status"], "Below threshold · C · 30")

    def test_missing_confirmation_is_pending_before_confirmation_run(self) -> None:
        _, rows = scanner_confirmation_status(
            ["Ticker"],
            [{"Ticker": "BFLY"}],
            pd.DataFrame(),
        )

        self.assertEqual(rows[0]["Confirmation status"], "Pending")

    def test_missing_ticker_is_not_checked_after_confirmation_run(self) -> None:
        _, rows = scanner_confirmation_status(
            ["Ticker"],
            [{"Ticker": "BFLY"}],
            pd.DataFrame([{"ticker": "OTHER", "score": 45, "confirmation_band": "B"}]),
        )

        self.assertEqual(rows[0]["Confirmation status"], "Not checked")


if __name__ == "__main__":
    unittest.main()
