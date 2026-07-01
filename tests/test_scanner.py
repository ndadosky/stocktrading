from __future__ import annotations

import unittest
from unittest.mock import patch

import pandas as pd

from app_server import scanner_confirmation_status, scanner_html


class ScannerConfirmationStatusTests(unittest.TestCase):
    def test_scanner_disables_api_caching_and_refreshes_immediately(self) -> None:
        payload = {
            "files": {}, "last_scheduled_run": None, "preview_columns": [],
            "preview": [], "stats": None, "preview_source": "none",
            "preview_running": False, "preview_date": "2026-07-01",
            "server_time": "2026-07-01T09:50:00-04:00",
        }
        with patch("app_server.scanner_payload", return_value=payload):
            html = scanner_html().decode("utf-8")

        self.assertIn("fetch('/api/scanner',{cache:'no-store'})", html)
        self.assertIn("refreshScanner();\nsetInterval(refreshScanner,3000);", html)

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
