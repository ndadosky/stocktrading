from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import pandas as pd

import strategy_review


class StrategyCohortReviewTests(unittest.TestCase):
    def test_review_uses_clean_current_cohort_and_reports_mixed_trades(self) -> None:
        current_version = json.loads(
            strategy_review.SETTINGS_FILE.read_text(encoding="utf-8")
        )["version"]
        trades = pd.DataFrame([
            {
                "remaining_shares": 0, "shares_sold_10": 50, "initial_cost": 1000,
                "realized_p_l": 100, "entry_strategy_version": current_version,
                "active_strategy_version": current_version,
                "exit_strategy_version": current_version, "strategy_changed_mid_trade": False,
            },
            {
                "remaining_shares": 0, "shares_sold_10": 50, "initial_cost": 1000,
                "realized_p_l": 50, "entry_strategy_version": "legacy-pre-v2.2.5",
                "active_strategy_version": current_version,
                "exit_strategy_version": current_version, "strategy_changed_mid_trade": True,
            },
            {
                "remaining_shares": 0, "shares_sold_10": 0, "initial_cost": 1000,
                "realized_p_l": -50, "entry_strategy_version": "legacy-pre-v2.2.5",
                "active_strategy_version": "legacy-pre-v2.2.5",
                "exit_strategy_version": "legacy-pre-v2.2.5", "strategy_changed_mid_trade": False,
            },
        ])

        with (
            patch.object(strategy_review, "read_table", return_value=trades),
            patch.object(strategy_review, "app_job_health", return_value={"job_runs": 1, "failed_runs": 0, "latest_failure": ""}),
            patch.object(strategy_review, "read_latest_snapshot", return_value=(None, pd.DataFrame())),
            patch.object(strategy_review, "latest_file", return_value=None),
        ):
            rows = strategy_review.build_review_rows("2026-06-29")

        metrics = {row["metric"]: row for row in rows}
        self.assertEqual(metrics["resolved_trades"]["value"], 1)
        self.assertEqual(metrics["all_resolved_trades"]["value"], 3)
        self.assertEqual(metrics["mixed_transition_trades"]["value"], 1)
        cohort_rows = [row for row in rows if row["section"] == "strategy_cohort"]
        self.assertEqual(len(cohort_rows), 3)


if __name__ == "__main__":
    unittest.main()
