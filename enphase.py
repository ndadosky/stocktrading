"""Enphase OAuth and PostgreSQL-cached solar telemetry."""

from __future__ import annotations

import base64
import json
import os
import secrets
import threading
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

from psycopg2 import Error as DatabaseError
from psycopg2.extras import Json

from db import connect, init_schema


API_ROOT = "https://api.enphaseenergy.com"
TOKEN_URL = f"{API_ROOT}/oauth/token"
_REFRESH_LOCK = threading.Lock()
_SOLAR_LOCK = threading.Lock()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _configured() -> bool:
    return bool(os.getenv("ENPHASE_API_KEY", "").strip())


def _request_json(url: str, headers: dict[str, str], *, method: str = "GET") -> Any:
    request = Request(url, headers=headers, method=method)
    with urlopen(request, timeout=10.0) as response:
        return json.loads(response.read().decode("utf-8"))


def _token_row() -> dict[str, Any] | None:
    init_schema()
    with connect() as (_, cursor):
        cursor.execute(
            """
            SELECT access_token, refresh_token, expires_at
            FROM home_oauth_tokens WHERE provider = 'enphase'
            """
        )
        return cursor.fetchone()


def _save_tokens(payload: dict[str, Any]) -> None:
    access_token = str(payload.get("access_token") or "").strip()
    refresh_token = str(payload.get("refresh_token") or "").strip()
    if not access_token or not refresh_token:
        raise ValueError("Enphase did not return both OAuth tokens")
    expires_at = _utcnow() + timedelta(seconds=max(60, int(payload.get("expires_in") or 86400)))
    init_schema()
    with connect(dict_rows=False) as (_, cursor):
        cursor.execute(
            """
            INSERT INTO home_oauth_tokens (provider, access_token, refresh_token, expires_at, updated_at)
            VALUES ('enphase', %s, %s, %s, NOW())
            ON CONFLICT (provider) DO UPDATE SET
                access_token = EXCLUDED.access_token,
                refresh_token = EXCLUDED.refresh_token,
                expires_at = EXCLUDED.expires_at,
                updated_at = NOW()
            """,
            (access_token, refresh_token, expires_at),
        )


def _basic_authorization() -> str:
    client_id = os.getenv("ENPHASE_CLIENT_ID", "").strip()
    client_secret = os.getenv("ENPHASE_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise ValueError("Add ENPHASE_CLIENT_ID and ENPHASE_CLIENT_SECRET to .env")
    encoded = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    return f"Basic {encoded}"


def _valid_access_token() -> str | None:
    env_token = os.getenv("ENPHASE_ACCESS_TOKEN", "").strip()
    row = _token_row()
    if not row:
        return env_token or None
    expires_at = row.get("expires_at")
    if expires_at and expires_at > _utcnow() + timedelta(minutes=5):
        return str(row["access_token"])
    with _REFRESH_LOCK:
        row = _token_row()
        if row and row.get("expires_at") and row["expires_at"] > _utcnow() + timedelta(minutes=5):
            return str(row["access_token"])
        refresh_token = str((row or {}).get("refresh_token") or os.getenv("ENPHASE_REFRESH_TOKEN", "")).strip()
        if not refresh_token:
            return env_token or None
        query = urlencode({"grant_type": "refresh_token", "refresh_token": refresh_token})
        payload = _request_json(
            f"{TOKEN_URL}?{query}",
            {"Authorization": _basic_authorization(), "Accept": "application/json"},
            method="POST",
        )
        _save_tokens(payload)
        return str(payload["access_token"])


def begin_authorization(redirect_uri: str) -> str:
    """Create a short-lived OAuth state and return the Enphase authorization URL."""
    client_id = os.getenv("ENPHASE_CLIENT_ID", "").strip()
    configured_url = os.getenv("ENPHASE_AUTHORIZATION_URL", "").strip()
    if not configured_url and not client_id:
        raise ValueError("Add ENPHASE_AUTHORIZATION_URL or ENPHASE_CLIENT_ID to .env")
    state = secrets.token_urlsafe(32)
    init_schema()
    with connect(dict_rows=False) as (_, cursor):
        cursor.execute(
            """
            INSERT INTO home_oauth_states (provider, state, redirect_uri, expires_at)
            VALUES ('enphase', %s, %s, %s)
            ON CONFLICT (provider) DO UPDATE SET
                state = EXCLUDED.state,
                redirect_uri = EXCLUDED.redirect_uri,
                expires_at = EXCLUDED.expires_at
            """,
            (state, redirect_uri, _utcnow() + timedelta(minutes=10)),
        )
    base = configured_url or f"{API_ROOT}/oauth/authorize?response_type=code&client_id={client_id}"
    parsed = urlparse(base)
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    params.update({"redirect_uri": redirect_uri, "state": state})
    return urlunparse(parsed._replace(query=urlencode(params)))


def complete_authorization(code: str, state: str) -> None:
    """Validate the callback state, exchange its code, and persist rotating tokens."""
    init_schema()
    with connect() as (_, cursor):
        cursor.execute(
            """
            DELETE FROM home_oauth_states
            WHERE provider = 'enphase' AND state = %s AND expires_at > NOW()
            RETURNING redirect_uri
            """,
            (state,),
        )
        row = cursor.fetchone()
    if not row:
        raise ValueError("Enphase authorization state is invalid or expired")
    query = urlencode(
        {"grant_type": "authorization_code", "redirect_uri": row["redirect_uri"], "code": code}
    )
    payload = _request_json(
        f"{TOKEN_URL}?{query}",
        {"Authorization": _basic_authorization(), "Accept": "application/json"},
        method="POST",
    )
    _save_tokens(payload)


def _cached_solar() -> tuple[dict[str, Any] | None, datetime | None]:
    init_schema()
    with connect() as (_, cursor):
        cursor.execute(
            "SELECT payload, fetched_at FROM home_api_cache WHERE cache_key = 'enphase_solar'"
        )
        row = cursor.fetchone()
    if not row:
        return None, None
    return row["payload"], row["fetched_at"]


def _save_solar(payload: dict[str, Any]) -> None:
    init_schema()
    with connect(dict_rows=False) as (_, cursor):
        cursor.execute(
            """
            INSERT INTO home_api_cache (cache_key, payload, fetched_at)
            VALUES ('enphase_solar', %s, NOW())
            ON CONFLICT (cache_key) DO UPDATE SET payload = EXCLUDED.payload, fetched_at = NOW()
            """,
            (Json(payload),),
        )


def _positive_number(value: Any) -> float | None:
    try:
        number = float(value)
        return number if number >= 0 else None
    except (TypeError, ValueError):
        return None


def _normalize(system: dict[str, Any], *, system_id: str) -> dict[str, Any]:
    power_w = _positive_number(system.get("current_power"))
    energy_wh = _positive_number(system.get("energy_today"))
    return {
        "configured": True,
        "connected": True,
        "system_id": system_id,
        "power": round(power_w / 1000, 2) if power_w is not None else None,
        "power_unit": "kW",
        "today": round(energy_wh / 1000, 2) if energy_wh is not None else None,
        "today_unit": "kWh",
        "source": "Enphase Cloud",
        "system_status": str(system.get("status") or "unknown"),
    }


def _interval_rows(payload: Any, *, value_name: str) -> dict[int, float]:
    """Normalize Enphase's meter interval payload into epoch -> Wh readings."""
    if not isinstance(payload, dict):
        return {}
    rows = payload.get("intervals") or payload.get("telemetry") or payload.get("data") or []
    if not isinstance(rows, list):
        return {}
    normalized: dict[int, float] = {}
    value_keys = (value_name, "wh_del", "enwh", "watt_hours", "energy", "wh")
    for row in rows:
        if not isinstance(row, dict):
            continue
        timestamp = next(
            (row.get(key) for key in ("end_at", "interval_end_at", "timestamp", "start_at") if row.get(key) is not None),
            None,
        )
        value = next((row.get(key) for key in value_keys if row.get(key) is not None), None)
        try:
            epoch = int(float(timestamp))
            watt_hours = float(value)
        except (TypeError, ValueError):
            continue
        if epoch > 0 and watt_hours >= 0:
            normalized[epoch] = round(watt_hours, 2)
    return normalized


def _merge_intervals(production: Any, consumption: Any) -> list[dict[str, Any]]:
    produced = _interval_rows(production, value_name="production_wh")
    consumed = _interval_rows(consumption, value_name="consumption_wh")
    return [
        {
            "end_at": epoch,
            "production_wh": produced.get(epoch),
            "consumption_wh": consumed.get(epoch),
        }
        for epoch in sorted(set(produced) | set(consumed))
    ]


def _fetch_meter_intervals(
    access_token: str, api_key: str, system_id: str
) -> tuple[list[dict[str, Any]], str | None]:
    headers = {"Authorization": f"Bearer {access_token}", "key": api_key, "Accept": "application/json"}
    payloads: dict[str, Any] = {}
    failures: list[str] = []
    for name, endpoint in (
        ("production", "production_meter"),
        ("consumption", "consumption_meter"),
    ):
        query = urlencode({"key": api_key, "granularity": "day"})
        url = f"{API_ROOT}/api/v4/systems/{system_id}/telemetry/{endpoint}?{query}"
        try:
            payloads[name] = _request_json(url, headers)
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError):
            payloads[name] = {}
            failures.append(name)
    message = f"No {', '.join(failures)} interval data returned." if failures else None
    return _merge_intervals(payloads.get("production"), payloads.get("consumption")), message


def _fetch_solar(access_token: str, previous: dict[str, Any] | None) -> dict[str, Any]:
    api_key = os.getenv("ENPHASE_API_KEY", "").strip()
    system_id = os.getenv("ENPHASE_SYSTEM_ID", "").strip() or str((previous or {}).get("system_id") or "")
    headers = {"Authorization": f"Bearer {access_token}", "key": api_key, "Accept": "application/json"}
    if system_id:
        url = f"{API_ROOT}/api/v4/systems/{system_id}/summary?{urlencode({'key': api_key})}"
        payload = _request_json(url, headers)
        summary = payload.get("system") if isinstance(payload, dict) and isinstance(payload.get("system"), dict) else payload
        if not isinstance(summary, dict):
            raise ValueError("Enphase returned an unexpected system summary")
        solar = _normalize(summary, system_id=system_id)
        intervals, chart_message = _fetch_meter_intervals(access_token, api_key, system_id)
        solar["intervals"] = intervals
        solar["chart_message"] = chart_message
        return solar

    url = f"{API_ROOT}/api/v4/systems?{urlencode({'key': api_key})}"
    payload = _request_json(url, headers)
    systems = payload.get("systems") if isinstance(payload, dict) else None
    if not isinstance(systems, list) or not systems:
        raise ValueError("No authorized Enphase systems were found")
    system = systems[0]
    discovered_id = str(system.get("system_id") or "")
    solar = _normalize(system, system_id=discovered_id)
    solar["intervals"] = []
    solar["chart_message"] = "Interval history will load on the next refresh."
    return solar


def _solar_status_locked() -> dict[str, Any]:
    if not _configured():
        return {"configured": False, "connected": False}
    try:
        cached, fetched_at = _cached_solar()
        cache_minutes = max(30, int(os.getenv("ENPHASE_CACHE_MINUTES", "30")))
        cache_has_chart = isinstance(cached, dict) and "intervals" in cached
        if cached and cache_has_chart and fetched_at and fetched_at > _utcnow() - timedelta(minutes=cache_minutes):
            return {**cached, "cached": True, "fetched_at": fetched_at.isoformat()}
        access_token = _valid_access_token()
        if not access_token:
            return {
                "configured": True,
                "connected": False,
                "authorization_required": True,
                "message": "Authorize Enphase to connect this system.",
            }
        fresh = _fetch_solar(access_token, cached)
        _save_solar(fresh)
        return {**fresh, "cached": False, "fetched_at": _utcnow().isoformat()}
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError, ValueError, DatabaseError) as exc:
        try:
            cached, fetched_at = _cached_solar()
        except Exception:
            cached, fetched_at = None, None
        if cached:
            return {
                **cached,
                "connected": False,
                "cached": True,
                "fetched_at": fetched_at.isoformat() if fetched_at else None,
                "message": f"Enphase refresh failed: {type(exc).__name__}",
            }
        return {
            "configured": True,
            "connected": False,
            "message": f"Enphase is unavailable: {type(exc).__name__}",
        }


def solar_status() -> dict[str, Any]:
    """Return Enphase solar data, making at most one refresh batch per 30 minutes."""
    with _SOLAR_LOCK:
        return _solar_status_locked()
