from __future__ import annotations

import unittest

import pandas as pd

from dashboard import trade_table


class DashboardOpenPositionTests(unittest.TestCase):
    def test_open_positions_show_remaining_share_count(self) -> None:
        frame = pd.DataFrame([
            {
                "ticker": "BFLY",
                "name": "Butterfly Network",
                "sector": "Healthcare",
                "remaining_shares": 50,
                "entry_price": 8.12,
                "current_price": 9.19,
                "p_l": 93.99,
                "p_l_%": 11.58,
                "holding_days": 3,
                "bid_ask_spread_pct": 0.494,
                "confirmation_band": "B",
            }
        ])

        html = trade_table(frame, is_open=True)

        self.assertIn(">Shares</th>", html)
        self.assertIn(">50</td>", html)
        self.assertIn("after any partial profit-taking exits", html)


if __name__ == "__main__":
    unittest.main()
