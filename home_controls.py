"""Server-side adapters for the Home Controls dashboard."""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from enphase import solar_status as enphase_solar_status


POOL_MODE_ENTITIES = {
    "main_drain": "HOME_POOL_MAIN_DRAIN_ENTITY",
    "no_pool_jets": "HOME_POOL_NO_POOL_JETS_ENTITY",
    "spa_upper_jets": "HOME_POOL_SPA_UPPER_JETS_ENTITY",
}
DECK_JETS_ENTITY = "HOME_POOL_DECK_JETS_ENTITY"


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _json_get(url: str, headers: dict[str, str], timeout: float = 4.0) -> Any:
    request = Request(url, headers=headers, method="GET")
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _json_request(
    url: str,
    headers: dict[str, str],
    *,
    method: str,
    payload: dict[str, Any],
    timeout: float = 5.0,
) -> Any:
    body = json.dumps(payload).encode("utf-8")
    request = Request(url, data=body, headers=headers, method=method)
    with urlopen(request, timeout=timeout) as response:
        raw = response.read()
        if not raw:
            return {}
        decoded = raw.decode("utf-8", errors="replace")
        try:
            return json.loads(decoded)
        except json.JSONDecodeError:
            return {"response": decoded}


def _number(value: Any) -> float | None:
    try:
        return round(float(value), 1)
    except (TypeError, ValueError):
        return None


def _pool_device(payload: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    devices = payload.get("devices") or {}
    if isinstance(devices, list):
        devices = {str(index): value for index, value in enumerate(devices)}
    if not isinstance(devices, dict):
        return None, {}
    candidates: list[tuple[str, dict[str, Any]]] = []
    for device_id, raw in devices.items():
        if not isinstance(raw, dict):
            continue
        status = raw.get("status") or {}
        if isinstance(status, dict) and status.get("waterTemp") is not None:
            candidates.append((str(device_id), raw))
    if not candidates:
        return None, {}
    for device_id, device in candidates:
        name = str((device.get("nodeAttr") or {}).get("name") or "").lower()
        if "heat" in name:
            return device_id, device
    return candidates[0]


def poolsync_status() -> dict[str, Any]:
    url = os.getenv("POOLSYNC_URL", "").strip()
    authorization = os.getenv("POOLSYNC_AUTHORIZATION", "").strip()
    user_id = os.getenv("POOLSYNC_USER", "").strip()
    if not all((url, authorization, user_id)):
        return {
            "configured": False,
            "connected": False,
            "message": "Add PoolSync credentials to the Pi environment.",
        }
    try:
        payload = _json_get(url, {"Authorization": authorization, "user": user_id})
        if not isinstance(payload, dict):
            raise ValueError("PoolSync returned an unexpected response")
        device_id, device = _pool_device(payload)
        if not device:
            raise ValueError("No PoolSync water-temperature sensor was found")
        node = device.get("nodeAttr") or {}
        status = device.get("status") or {}
        config = device.get("config") or {}
        mode = "Off"
        if config.get("mode") in (1, "1", True):
            mode = "Heat spa" if config.get("poolSpaMode") in (1, "1", True) else "Heat pool"
        return {
            "configured": True,
            "connected": True,
            "device_id": device_id,
            "name": str(node.get("name") or "PoolSync heater"),
            "online": bool(node.get("online", True)),
            "temperature": _number(status.get("waterTemp")),
            "unit": "°F",
            "setpoint": _number(config.get("setpoint")),
            "mode": mode,
            "updated_at": _now(),
        }
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, ValueError, OSError) as exc:
        return {
            "configured": True,
            "connected": False,
            "message": f"PoolSync is unavailable: {type(exc).__name__}",
        }


def poolsync_control(action: str, value: Any = None) -> dict[str, Any]:
    url = os.getenv("POOLSYNC_URL", "").strip()
    authorization = os.getenv("POOLSYNC_AUTHORIZATION", "").strip()
    user_id = os.getenv("POOLSYNC_USER", "").strip()
    if not all((url, authorization, user_id)):
        return {"ok": False, "error": "PoolSync is not configured"}
    try:
        headers = {"Authorization": authorization, "user": user_id}
        payload = _json_get(url, headers)
        if not isinstance(payload, dict):
            raise ValueError("PoolSync returned an unexpected response")
        device_id, device = _pool_device(payload)
        if device_id is None or not device:
            raise ValueError("No PoolSync heater was found")
        config = device.get("config") or {}
        updates: dict[str, Any]
        if action == "adjust_temperature":
            delta = int(value)
            if delta not in (-1, 1):
                raise ValueError("Temperature adjustment must be one degree")
            current = _number(config.get("setpoint"))
            if current is None:
                raise ValueError("PoolSync did not report a setpoint")
            target = int(round(current + delta))
            if not 40 <= target <= 104:
                raise ValueError("Pool setpoint must be between 40°F and 104°F")
            updates = {"setpoint": target}
        elif action == "set_temperature":
            target = int(round(float(value)))
            if not 40 <= target <= 104:
                raise ValueError("Pool setpoint must be between 40°F and 104°F")
            updates = {"setpoint": target}
        elif action == "set_mode":
            mode = str(value or "").strip().lower()
            if mode == "heat":
                updates = {"mode": 1, "poolSpaMode": 0}
            elif mode == "off":
                updates = {"mode": 0}
            else:
                raise ValueError("Pool mode must be heat or off")
        else:
            raise ValueError("Unknown PoolSync action")
        endpoint = f"{url.split('?', 1)[0]}?cmd=devices&device={device_id}"
        _json_request(
            endpoint,
            {**headers, "Content-Type": "application/json"},
            method="PATCH",
            payload={"config": updates},
        )
        return {"ok": True, "action": action, "applied": updates, "pool": poolsync_status()}
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, TypeError, ValueError, OSError) as exc:
        message = str(exc) if isinstance(exc, ValueError) else f"PoolSync request failed: {type(exc).__name__}"
        return {"ok": False, "error": message}


def _ha_states() -> tuple[dict[str, dict[str, Any]], str | None]:
    base_url = os.getenv("HOME_ASSISTANT_URL", "").strip().rstrip("/")
    token = os.getenv("HOME_ASSISTANT_TOKEN", "").strip()
    if not base_url or not token:
        return {}, "Home Assistant is not configured"
    try:
        rows = _json_get(
            f"{base_url}/api/states",
            {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        states = {
            str(row.get("entity_id")): row
            for row in rows
            if isinstance(row, dict) and row.get("entity_id")
        }
        return states, None
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        return {}, f"Home Assistant is unavailable: {type(exc).__name__}"


def home_assistant_switch(channel: int, turn_on: bool) -> dict[str, Any]:
    if channel not in range(1, 5):
        return {"ok": False, "error": "eWeLink channel must be between 1 and 4"}
    base_url = os.getenv("HOME_ASSISTANT_URL", "").strip().rstrip("/")
    token = os.getenv("HOME_ASSISTANT_TOKEN", "").strip()
    entity_id = os.getenv(f"HOME_EWELINK_CHANNEL_{channel}_ENTITY", "").strip()
    if not base_url or not token or not entity_id:
        return {"ok": False, "error": f"eWeLink channel {channel} is not configured"}
    if not entity_id.startswith("switch."):
        return {"ok": False, "error": "eWeLink controls must map to Home Assistant switch entities"}
    return _home_assistant_switches([entity_id], turn_on)


def _home_assistant_switches(entity_ids: list[str], turn_on: bool) -> dict[str, Any]:
    """Set one or more explicitly configured Home Assistant switch entities."""
    if not entity_ids or any(not entity_id.startswith("switch.") for entity_id in entity_ids):
        return {"ok": False, "error": "Pool controls must map to Home Assistant switch entities"}
    base_url = os.getenv("HOME_ASSISTANT_URL", "").strip().rstrip("/")
    token = os.getenv("HOME_ASSISTANT_TOKEN", "").strip()
    if not base_url or not token:
        return {"ok": False, "error": "Home Assistant is not configured"}
    action = "turn_on" if turn_on else "turn_off"
    try:
        _json_request(
            f"{base_url}/api/services/switch/{action}",
            {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            method="POST",
            payload={"entity_id": entity_ids},
        )
        return {"ok": True, "entities": entity_ids, "state": "on" if turn_on else "off"}
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        return {"ok": False, "error": f"Home Assistant request failed: {type(exc).__name__}"}


def home_assistant_pool_control(action: str, value: Any = None) -> dict[str, Any]:
    """Apply Pool/Spa valve presets or independently control the deck jets."""
    if action == "set_mode":
        mode = str(value or "").strip().lower()
        if mode not in {"pool", "spa"}:
            return {"ok": False, "error": "Pool mode must be pool or spa"}
        entity_ids = [os.getenv(env_name, "").strip() for env_name in POOL_MODE_ENTITIES.values()]
        if not all(entity_ids):
            return {"ok": False, "error": "Pool mode switch entities are not configured"}
        result = _home_assistant_switches(entity_ids, mode == "spa")
        if result.get("ok"):
            result.update({"action": action, "mode": mode})
        return result
    if action == "set_deck_jets":
        entity_id = os.getenv(DECK_JETS_ENTITY, "").strip()
        if not entity_id:
            return {"ok": False, "error": "Deck Jets switch entity is not configured"}
        state = str(value or "").strip().lower()
        if state not in {"on", "off"}:
            return {"ok": False, "error": "Deck Jets state must be on or off"}
        result = _home_assistant_switches([entity_id], state == "on")
        if result.get("ok"):
            result.update({"action": action, "deck_jets": state})
        return result
    return {"ok": False, "error": "Unknown pool-mode action"}


def home_assistant_door_control(action: str) -> dict[str, Any]:
    """Lock or unlock the explicitly configured Home Assistant lock entity."""
    action = str(action or "").strip().lower()
    if action not in {"lock", "unlock"}:
        return {"ok": False, "error": "Front door action must be lock or unlock"}
    base_url = os.getenv("HOME_ASSISTANT_URL", "").strip().rstrip("/")
    token = os.getenv("HOME_ASSISTANT_TOKEN", "").strip()
    entity_id = os.getenv("HOME_FRONT_DOOR_ENTITY", "").strip()
    if not base_url or not token or not entity_id:
        return {"ok": False, "error": "Front door lock is not configured"}
    if not entity_id.startswith("lock."):
        return {"ok": False, "error": "Front door control must map to a Home Assistant lock entity"}
    try:
        _json_request(
            f"{base_url}/api/services/lock/{action}",
            {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            method="POST",
            payload={"entity_id": entity_id},
        )
        return {"ok": True, "action": action, "entity_id": entity_id}
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        return {"ok": False, "error": f"Home Assistant request failed: {type(exc).__name__}"}


def _entity(states: dict[str, dict[str, Any]], env_name: str) -> dict[str, Any] | None:
    entity_id = os.getenv(env_name, "").strip()
    return states.get(entity_id) if entity_id else None


def _state_value(entity: dict[str, Any] | None) -> Any:
    return entity.get("state") if entity else None


def home_controls_payload() -> dict[str, Any]:
    states, ha_error = _ha_states()
    door = _entity(states, "HOME_FRONT_DOOR_ENTITY")
    solar_power = _entity(states, "HOME_SOLAR_POWER_ENTITY")
    solar_today = _entity(states, "HOME_SOLAR_TODAY_ENTITY")
    enphase_solar = enphase_solar_status()
    pool_mode_entities = {
        name: _entity(states, env_name) for name, env_name in POOL_MODE_ENTITIES.items()
    }
    deck_jets = _entity(states, DECK_JETS_ENTITY)
    pool_mode_configured = all(os.getenv(env_name, "").strip() for env_name in POOL_MODE_ENTITIES.values())
    deck_jets_configured = bool(os.getenv(DECK_JETS_ENTITY, "").strip())
    valve_states = [str(_state_value(entity) or "unavailable").lower() for entity in pool_mode_entities.values()]
    pool_mode_connected = pool_mode_configured and all(entity is not None for entity in pool_mode_entities.values())
    if pool_mode_connected and all(state == "on" for state in valve_states):
        selected_pool_mode = "spa"
    elif pool_mode_connected and all(state == "off" for state in valve_states):
        selected_pool_mode = "pool"
    elif pool_mode_connected:
        selected_pool_mode = "mixed"
    else:
        selected_pool_mode = "unavailable"

    cars = []
    for name, battery_env, charging_env, range_env, image in (
        (
            "Kia EV9",
            "HOME_KIA_BATTERY_ENTITY",
            "HOME_KIA_CHARGING_ENTITY",
            "HOME_KIA_RANGE_ENTITY",
            "/assets/home/kia-ev9-white.jpg",
        ),
        (
            "Volvo XC40",
            "HOME_VOLVO_BATTERY_ENTITY",
            "HOME_VOLVO_CHARGING_ENTITY",
            "HOME_VOLVO_RANGE_ENTITY",
            "/assets/home/volvo-xc40-white-black-roof.jpg",
        ),
    ):
        battery = _entity(states, battery_env)
        charging = _entity(states, charging_env)
        vehicle_range = _entity(states, range_env)
        cars.append(
            {
                "name": name,
                "configured": bool(os.getenv(battery_env, "").strip()),
                "charge": _number(_state_value(battery)),
                "charging": _state_value(charging),
                "range": _number(_state_value(vehicle_range)),
                "range_unit": (vehicle_range or {}).get("attributes", {}).get(
                    "unit_of_measurement", "mi"
                ),
                "image": image,
            }
        )

    ewelink_channels = []
    for channel in range(1, 5):
        env_name = f"HOME_EWELINK_CHANNEL_{channel}_ENTITY"
        entity = _entity(states, env_name)
        entity_id = os.getenv(env_name, "").strip()
        if entity_id:
            ewelink_channels.append(
                {
                    "channel": channel,
                    "name": os.getenv(f"HOME_EWELINK_CHANNEL_{channel}_NAME", f"Channel {channel}"),
                    "entity_id": entity_id,
                    "state": str(_state_value(entity) or "unavailable").lower(),
                }
            )

    door_state = str(_state_value(door) or "unavailable").lower()
    return {
        "generated_at": _now(),
        "pool": poolsync_status(),
        "pool_mode": {
            "configured": pool_mode_configured and deck_jets_configured,
            "connected": pool_mode_connected and deck_jets is not None,
            "mode": selected_pool_mode,
            "valves": dict(zip(POOL_MODE_ENTITIES, valve_states)),
            "deck_jets": str(_state_value(deck_jets) or "unavailable").lower(),
        },
        "front_door": {
            "configured": bool(os.getenv("HOME_FRONT_DOOR_ENTITY", "").strip()),
            "state": door_state,
            "locked": door_state == "locked",
        },
        "solar": enphase_solar if enphase_solar.get("configured") else {
            "configured": bool(os.getenv("HOME_SOLAR_POWER_ENTITY", "").strip()),
            "connected": bool(solar_power),
            "power": _number(_state_value(solar_power)),
            "power_unit": (solar_power or {}).get("attributes", {}).get("unit_of_measurement", "kW"),
            "today": _number(_state_value(solar_today)),
            "today_unit": (solar_today or {}).get("attributes", {}).get("unit_of_measurement", "kWh"),
            "source": "Home Assistant",
        },
        "cars": cars,
        "ewelink": {
            "configured": bool(ewelink_channels),
            "device_id": os.getenv("EWELINK_DEVICE_ID", "").strip(),
            "model": os.getenv("EWELINK_MODEL", "PSF-B04-GL").strip(),
            "channels": ewelink_channels,
        },
        "home_assistant": {"connected": bool(states), "message": ha_error},
    }
