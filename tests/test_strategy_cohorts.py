from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import pandas as pd

import strategy_review


class StrategyCohortReviewTests(unittest.TestCase):
    def test_strategy_page_renders_optimizer_progress_and_ledger(self) -> None:
        status = {
            "phase": "experiment_running", "step": 3, "completed": 10, "target": 25,
            "progress_pct": 40.0, "cycle": 2, "active_version": "auto-002",
            "baseline_version": "auto-001", "candidate_version": "auto-002",
            "active_change": {
                "label": "Confirmation threshold", "old_value": 40, "new_value": 45,
                "reason": "Test stronger confirmation.",
            },
            "last_result": "kept", "last_result_reason": "Expectancy improved.",
            "history": [{
                "recorded_at": "2026-07-01T10:30:00-04:00", "cycle": 2,
                "status": "started", "area": "Entry", "lever": "selection.confirmation",
                "old_value": 40, "new_value": 45, "rationale": "Test stronger confirmation.",
            }],
            "levers": [{
                "area": "Entry", "label": "Confirmation threshold",
                "key": "selection.confirmation", "reason": "Test stronger confirmation.",
            }],
        }
        with patch.object(strategy_review, "optimizer_status", return_value=status):
            html = strategy_review.render_strategy_review_html([], "2026-07-01")

        self.assertIn("Continuous optimization · cycle 2", html)
        self.assertIn("10 resolved trades", html)
        self.assertIn("Optimizer change ledger", html)
        self.assertIn("Documented optimization levers", html)
        self.assertIn("Champion vs challenger settings", html)
        self.assertIn("Pause optimizer", html)
        self.assertIn("Open experiment positions", html)

    def test_analyze_now_suggests_entry_review_from_current_losses(self) -> None:
        current_version = json.loads(
            strategy_review.SETTINGS_FILE.read_text(encoding="utf-8")
        )["version"]
        trades = pd.DataFrame([
            {
                "remaining_shares": 0, "shares_sold_10": 0, "initial_cost": 1000,
                "realized_p_l": -50, "entry_strategy_version": current_version,
                "strategy_changed_mid_trade": False, "confirmation_band": "B",
                "exit_reason": "STOP -5%",
            },
            {
                "remaining_shares": 0, "shares_sold_10": 0, "initial_cost": 1000,
                "realized_p_l": -40, "entry_strategy_version": current_version,
                "strategy_changed_mid_trade": False, "confirmation_band": "B",
                "exit_reason": "STOP -5%",
            },
        ])

        with patch.object(strategy_review, "read_table", return_value=trades):
            result = strategy_review.current_data_suggestions()

        self.assertEqual(result["sample"]["resolved"], 2)
        self.assertEqual(result["sample"]["confidence"], "early signal")
        titles = [suggestion["title"] for suggestion in result["suggestions"]]
        self.assertIn("Test stricter entry selection", titles)
        self.assertIn("Investigate stop-outs by entry quality", titles)

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
