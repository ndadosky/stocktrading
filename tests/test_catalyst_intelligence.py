from __future__ import annotations

import unittest
from unittest.mock import patch

import pandas as pd

import catalyst_intelligence
import strategy_optimizer


class CatalystIntelligenceTests(unittest.TestCase):
    def test_shadow_enrichment_classifies_positive_and_risk_news(self) -> None:
        candidates = pd.DataFrame([{"Ticker": "GOOD"}, {"Ticker": "RISK"}])
        news = [
            {"headline": "GOOD wins major contract", "summary": "", "symbols": ["GOOD"], "created_at": "2026-07-01T12:00:00Z", "url": "https://example.test/good"},
            {"headline": "RISK announces public offering", "summary": "", "symbols": ["RISK"], "created_at": "2026-07-01T12:00:00Z", "url": "https://example.test/risk"},
        ]
        with (
            patch.dict("os.environ", {"ALPACA_API_KEY_ID": "key", "ALPACA_API_SECRET_KEY": "secret"}),
            patch.object(catalyst_intelligence, "_request_news", return_value=news),
        ):
            enriched = catalyst_intelligence.enrich_candidates(candidates)

        good = enriched[enriched["Ticker"].eq("GOOD")].iloc[0]
        risk = enriched[enriched["Ticker"].eq("RISK")].iloc[0]
        self.assertGreater(good["catalyst_shadow_score"], 0)
        self.assertLess(risk["catalyst_shadow_score"], 0)
        self.assertEqual(good["catalyst_mode"], "SHADOW")

    def test_catalyst_lever_waits_for_configured_observation_gate(self) -> None:
        lever = {"key": "catalyst.use_for_selection"}
        policy = {"minimum_catalyst_observations": 50}
        observations = pd.DataFrame({"catalyst_configured": [True] * 49 + [False]})
        with patch.object(strategy_optimizer, "read_table", return_value=observations):
            self.assertFalse(strategy_optimizer._lever_ready(lever, policy))
        observations.loc[49, "catalyst_configured"] = True
        with patch.object(strategy_optimizer, "read_table", return_value=observations):
            self.assertTrue(strategy_optimizer._lever_ready(lever, policy))


if __name__ == "__main__":
    unittest.main()
