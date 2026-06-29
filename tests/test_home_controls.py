from __future__ import annotations

import os
import unittest
from unittest.mock import patch

import home_controls


POOL_ENV = {
    "HOME_ASSISTANT_URL": "http://127.0.0.1:8123",
    "HOME_ASSISTANT_TOKEN": "test-token",
    "HOME_POOL_MAIN_DRAIN_ENTITY": "switch.pool_sonoff_1001e73d82_1",
    "HOME_POOL_NO_POOL_JETS_ENTITY": "switch.pool_sonoff_1001e73d82_3",
    "HOME_POOL_SPA_UPPER_JETS_ENTITY": "switch.pool_sonoff_1001e73d82_4",
    "HOME_POOL_DECK_JETS_ENTITY": "switch.pool_sonoff_1001e73d82_2",
}

DOOR_ENV = {
    "HOME_ASSISTANT_URL": "http://127.0.0.1:8123",
    "HOME_ASSISTANT_TOKEN": "test-token",
    "HOME_FRONT_DOOR_ENTITY": "lock.front_front_door",
}

CAR_ENV = {
    "HOME_KIA_BATTERY_ENTITY": "sensor.ev9_ev_battery_level",
    "HOME_KIA_CHARGING_ENTITY": "binary_sensor.ev9_charging",
    "HOME_KIA_RANGE_ENTITY": "sensor.ev9_range",
    "HOME_VOLVO_BATTERY_ENTITY": "sensor.xc40_battery_charge_level",
    "HOME_VOLVO_CHARGING_ENTITY": "binary_sensor.xc40_charging",
    "HOME_VOLVO_RANGE_ENTITY": "sensor.xc40_electric_range",
}


class PoolModeTests(unittest.TestCase):
    def test_pool_mode_turns_all_three_valve_entities_off(self) -> None:
        with patch.dict(os.environ, POOL_ENV, clear=True), patch(
            "home_controls._json_request", return_value={}
        ) as request:
            result = home_controls.home_assistant_pool_control("set_mode", "pool")

        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "pool")
        self.assertIn("/api/services/switch/turn_off", request.call_args.args[0])
        self.assertEqual(
            request.call_args.kwargs["payload"]["entity_id"],
            [
                POOL_ENV["HOME_POOL_MAIN_DRAIN_ENTITY"],
                POOL_ENV["HOME_POOL_NO_POOL_JETS_ENTITY"],
                POOL_ENV["HOME_POOL_SPA_UPPER_JETS_ENTITY"],
            ],
        )

    def test_spa_mode_turns_all_three_valve_entities_on(self) -> None:
        with patch.dict(os.environ, POOL_ENV, clear=True), patch(
            "home_controls._json_request", return_value={}
        ) as request:
            result = home_controls.home_assistant_pool_control("set_mode", "spa")

        self.assertTrue(result["ok"])
        self.assertIn("/api/services/switch/turn_on", request.call_args.args[0])

    def test_deck_jets_are_controlled_independently(self) -> None:
        with patch.dict(os.environ, POOL_ENV, clear=True), patch(
            "home_controls._json_request", return_value={}
        ) as request:
            result = home_controls.home_assistant_pool_control("set_deck_jets", "on")

        self.assertTrue(result["ok"])
        self.assertEqual(
            request.call_args.kwargs["payload"]["entity_id"],
            [POOL_ENV["HOME_POOL_DECK_JETS_ENTITY"]],
        )


class FrontDoorTests(unittest.TestCase):
    def test_lock_calls_home_assistant_lock_service(self) -> None:
        with patch.dict(os.environ, DOOR_ENV, clear=True), patch(
            "home_controls._json_request", return_value={}
        ) as request:
            result = home_controls.home_assistant_door_control("lock")

        self.assertTrue(result["ok"])
        self.assertIn("/api/services/lock/lock", request.call_args.args[0])
        self.assertEqual(
            request.call_args.kwargs["payload"]["entity_id"],
            DOOR_ENV["HOME_FRONT_DOOR_ENTITY"],
        )

    def test_unlock_calls_home_assistant_unlock_service(self) -> None:
        with patch.dict(os.environ, DOOR_ENV, clear=True), patch(
            "home_controls._json_request", return_value={}
        ) as request:
            result = home_controls.home_assistant_door_control("unlock")

        self.assertTrue(result["ok"])
        self.assertIn("/api/services/lock/unlock", request.call_args.args[0])

    def test_rejects_unknown_action(self) -> None:
        with patch.dict(os.environ, DOOR_ENV, clear=True):
            result = home_controls.home_assistant_door_control("open")

        self.assertFalse(result["ok"])
        self.assertIn("lock or unlock", result["error"])


class VehicleStatusTests(unittest.TestCase):
    def test_payload_includes_vehicle_charge_range_units_and_artwork(self) -> None:
        states = {
            CAR_ENV["HOME_KIA_BATTERY_ENTITY"]: {"state": "72", "attributes": {}},
            CAR_ENV["HOME_KIA_CHARGING_ENTITY"]: {"state": "off", "attributes": {}},
            CAR_ENV["HOME_KIA_RANGE_ENTITY"]: {
                "state": "241",
                "attributes": {"unit_of_measurement": "mi"},
            },
            CAR_ENV["HOME_VOLVO_BATTERY_ENTITY"]: {"state": "48", "attributes": {}},
            CAR_ENV["HOME_VOLVO_CHARGING_ENTITY"]: {"state": "on", "attributes": {}},
            CAR_ENV["HOME_VOLVO_RANGE_ENTITY"]: {
                "state": "116",
                "attributes": {"unit_of_measurement": "mi"},
            },
        }
        with patch.dict(os.environ, CAR_ENV, clear=True), patch(
            "home_controls._ha_states", return_value=(states, None)
        ), patch(
            "home_controls.poolsync_status", return_value={"configured": False}
        ), patch(
            "home_controls.enphase_solar_status", return_value={"configured": False}
        ):
            cars = home_controls.home_controls_payload()["cars"]

        self.assertEqual(cars[0]["charge"], 72.0)
        self.assertEqual(cars[0]["range"], 241.0)
        self.assertEqual(cars[0]["range_unit"], "mi")
        self.assertEqual(cars[0]["image"], "/assets/home/kia-ev9-white.jpg")
        self.assertEqual(cars[1]["name"], "Volvo XC40")
        self.assertIn("black-roof", cars[1]["image"])


if __name__ == "__main__":
    unittest.main()
