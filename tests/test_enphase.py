from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import enphase


class EnphaseTests(unittest.TestCase):
    def test_normalize_converts_watts_and_watt_hours(self) -> None:
        result = enphase._normalize(
            {"current_power": 4321, "energy_today": 12750, "status": "normal"},
            system_id="42",
        )
        self.assertEqual(result["power"], 4.32)
        self.assertEqual(result["today"], 12.75)
        self.assertEqual(result["system_id"], "42")

    def test_fresh_database_cache_skips_network_and_token_lookup(self) -> None:
        cached = {"configured": True, "connected": True, "power": 1.5}
        with (
            patch.dict(os.environ, {"ENPHASE_API_KEY": "test", "ENPHASE_CACHE_MINUTES": "30"}),
            patch("enphase._cached_solar", return_value=(cached, datetime.now(timezone.utc))),
            patch("enphase._valid_access_token") as token,
        ):
            result = enphase.solar_status()
        token.assert_not_called()
        self.assertTrue(result["cached"])
        self.assertEqual(result["power"], 1.5)

    def test_cache_window_cannot_be_configured_below_thirty_minutes(self) -> None:
        cached = {"configured": True, "connected": True}
        with (
            patch.dict(os.environ, {"ENPHASE_API_KEY": "test", "ENPHASE_CACHE_MINUTES": "1"}),
            patch("enphase._cached_solar", return_value=(cached, datetime.now(timezone.utc))),
            patch("enphase._valid_access_token") as token,
        ):
            result = enphase.solar_status()
        token.assert_not_called()
        self.assertTrue(result["cached"])


if __name__ == "__main__":
    unittest.main()
