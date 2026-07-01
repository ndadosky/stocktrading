from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import strategy_optimizer


def resolved_rows(version: str, count: int, return_pct: float) -> pd.DataFrame:
    return pd.DataFrame([
        {
            "remaining_shares": 0,
            "entry_strategy_version": version,
            "strategy_changed_mid_trade": False,
            "initial_cost": 1000,
            "realized_p_l": 1000 * return_pct / 100,
        }
        for _ in range(count)
    ])


class StrategyOptimizerTests(unittest.TestCase):
    def test_rejects_higher_win_rate_when_expectancy_does_not_improve(self) -> None:
        policy = json.loads(
            (Path(__file__).resolve().parents[1] / "optimizer_policy.json").read_text(encoding="utf-8")
        )
        baseline = {
            "win_rate_pct": 60, "expectancy_pct": 1.0,
            "profit_factor": 2.0, "max_drawdown_pct": 4.0,
        }
        candidate = {
            "win_rate_pct": 80, "expectancy_pct": 0.8,
            "profit_factor": 2.2, "max_drawdown_pct": 3.0,
        }

        accepted, reason = strategy_optimizer._accepted(baseline, candidate, policy)

        self.assertFalse(accepted)
        self.assertIn("Expectancy change -0.200", reason)

    def test_starts_one_experiment_at_sixty_then_keeps_better_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "settings.json"
            runtime = root / "active.json"
            state_file = root / "state.json"
            ledger = root / "ledger.jsonl"
            settings = json.loads(
                strategy_optimizer.STRATEGY_SETTINGS_FILE.read_text(encoding="utf-8")
            )
            settings["version"] = "test-baseline"
            source.write_text(json.dumps(settings), encoding="utf-8")

            common = (
                patch.object(strategy_optimizer, "STRATEGY_SETTINGS_FILE", source),
                patch.object(strategy_optimizer, "RUNTIME_STRATEGY_SETTINGS_FILE", runtime),
                patch.object(strategy_optimizer, "STATE_DIR", root),
                patch.object(strategy_optimizer, "STATE_FILE", state_file),
                patch.object(strategy_optimizer, "LEDGER_FILE", ledger),
            )
            for item in common:
                item.start()
            self.addCleanup(lambda: [item.stop() for item in reversed(common)])

            with patch.object(
                strategy_optimizer, "read_table",
                return_value=resolved_rows("test-baseline", 60, 1.0),
            ):
                started = strategy_optimizer.run_optimizer_cycle()

            self.assertEqual(started["phase"], "experiment_running")
            self.assertEqual(started["target"], 25)
            candidate_version = started["candidate_version"]
            active = json.loads(runtime.read_text(encoding="utf-8"))
            self.assertEqual(active["version"], candidate_version)
            self.assertEqual(active["selection"]["confirmation_buy_min_score"], 45)

            state = json.loads(state_file.read_text(encoding="utf-8"))
            state["last_action_date"] = "2000-01-01"
            state_file.write_text(json.dumps(state), encoding="utf-8")
            with patch.object(
                strategy_optimizer, "read_table",
                return_value=resolved_rows(candidate_version, 25, 2.0),
            ):
                evaluated = strategy_optimizer.run_optimizer_cycle()

            self.assertEqual(evaluated["phase"], "ready_for_next_experiment")
            self.assertEqual(evaluated["last_result"], "kept")
            self.assertEqual(json.loads(runtime.read_text(encoding="utf-8"))["version"], candidate_version)


if __name__ == "__main__":
    unittest.main()
