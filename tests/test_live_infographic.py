from __future__ import annotations

import unittest

from app_server import visible_live_infographic_rows


class LiveInfographicRowsTests(unittest.TestCase):
    def test_previous_closed_trades_are_hidden(self) -> None:
        rows = [
            {"ticker": "OPEN", "remaining_shares": 100, "status": "OPEN"},
            {"ticker": "PART", "remaining_shares": 50, "status": "PARTIAL", "exit_datetime": "2026-06-28T10:00:00-04:00"},
            {"ticker": "TODAY", "remaining_shares": 0, "status": "CLOSED", "exit_datetime": "2026-06-29T10:00:00-04:00"},
            {"ticker": "OLD", "remaining_shares": 0, "status": "CLOSED", "exit_datetime": "2026-06-28T10:00:00-04:00"},
        ]

        visible = visible_live_infographic_rows(rows, "2026-06-29")

        self.assertEqual([row["ticker"] for row in visible], ["OPEN", "PART", "TODAY"])


if __name__ == "__main__":
    unittest.main()
