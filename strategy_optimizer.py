"""Guarded, persistent one-lever-at-a-time optimizer for paper trading."""

from __future__ import annotations

import copy
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from scanner_config import LOGS_DIR, STRATEGY_SETTINGS_FILE, RUNTIME_STRATEGY_SETTINGS_FILE
from stock_storage import read_table


STATE_DIR = LOGS_DIR / "strategy_optimizer"
STATE_FILE = STATE_DIR / "state.json"
LEDGER_FILE = STATE_DIR / "change_ledger.jsonl"

LEVER_CATALOG = [
    {
        "key": "selection.confirmation_buy_min_score",
        "label": "Confirmation threshold",
        "area": "Entry",
        "reason": "Tests whether requiring stronger intraday confirmation improves trade quality.",
    },
    {
        "key": "risk.scale_out.first_target_gain_pct",
        "label": "First profit target",
        "area": "Exit",
        "reason": "Tests the trade-off between reaching the first scale-out and capturing more return.",
    },
    {
        "key": "risk.stop_loss_pct",
        "label": "Protective stop",
        "area": "Risk",
        "reason": "Tests a modestly tighter loss limit; the optimizer never widens this safeguard.",
    },
    {
        "key": "risk.max_holding_days",
        "label": "Maximum holding period",
        "area": "Exit",
        "reason": "Tests whether releasing stale capital sooner improves return per trade.",
    },
    {
        "key": "selection.morning_candidate_min_score",
        "label": "Morning screener threshold",
        "area": "Screener",
        "reason": "Tests whether filtering weaker morning setups improves downstream entries.",
    },
    {
        "key": "risk.scale_out.second_target_gain_pct",
        "label": "Second profit target",
        "area": "Exit",
        "reason": "Tests whether the second tranche should demand slightly more upside.",
    },
]


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _database_value(key: str) -> dict | None:
    try:
        from db import connect, init_schema

        init_schema()
        with connect() as (_, cursor):
            cursor.execute(
                "SELECT payload FROM strategy_optimizer_state WHERE state_key = %s",
                (key,),
            )
            row = cursor.fetchone()
        return row.get("payload") if row and isinstance(row.get("payload"), dict) else None
    except Exception:
        return None


def _save_database_value(key: str, payload: dict) -> bool:
    try:
        from db import connect, init_schema

        init_schema()
        with connect(dict_rows=False) as (_, cursor):
            cursor.execute(
                """
                INSERT INTO strategy_optimizer_state (state_key, payload, updated_at)
                VALUES (%s, %s::jsonb, NOW())
                ON CONFLICT (state_key) DO UPDATE
                SET payload = EXCLUDED.payload, updated_at = NOW()
                """,
                (key, json.dumps(payload)),
            )
        return True
    except Exception:
        return False


def load_active_settings() -> dict:
    database_settings = _database_value("active_settings")
    if database_settings:
        return database_settings
    source = RUNTIME_STRATEGY_SETTINGS_FILE if RUNTIME_STRATEGY_SETTINGS_FILE.exists() else STRATEGY_SETTINGS_FILE
    return json.loads(source.read_text(encoding="utf-8"))


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _save_state(state: dict) -> None:
    _save_database_value("optimizer_state", state)
    _atomic_json(STATE_FILE, state)


def _write_settings(settings: dict) -> None:
    _save_database_value("active_settings", settings)
    _atomic_json(RUNTIME_STRATEGY_SETTINGS_FILE, settings)


def load_optimizer_state() -> dict:
    database_state = _database_value("optimizer_state")
    if database_state:
        return database_state
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {
        "phase": "collecting_baseline",
        "cycle": 0,
        "lever_index": 0,
        "created_at": _now(),
        "history": [],
    }


def _value(settings: dict, path: str) -> Any:
    current: Any = settings
    for part in path.split("."):
        current = current[part]
    return current


def _set_value(settings: dict, path: str, value: Any) -> None:
    current = settings
    parts = path.split(".")
    for part in parts[:-1]:
        current = current[part]
    current[parts[-1]] = value


def _resolved_for_version(trades: pd.DataFrame, version: str) -> pd.DataFrame:
    if trades.empty:
        return trades.copy()
    remaining = pd.to_numeric(
        trades.get("remaining_shares", pd.Series(0, index=trades.index)), errors="coerce"
    ).fillna(0)
    entry_version = trades.get(
        "entry_strategy_version", pd.Series("legacy-unversioned", index=trades.index)
    ).fillna("legacy-unversioned").astype(str)
    changed = trades.get(
        "strategy_changed_mid_trade", pd.Series(False, index=trades.index)
    ).astype(str).str.lower().isin({"1", "true", "yes"})
    return trades[remaining.le(0) & entry_version.eq(version) & ~changed].copy()


def strategy_metrics(frame: pd.DataFrame) -> dict:
    if frame.empty:
        return {"resolved": 0, "win_rate_pct": 0.0, "expectancy_pct": 0.0,
                "profit_factor": None, "max_drawdown_pct": 0.0}
    frame = frame.copy()
    if "exit_datetime" in frame:
        frame = frame.assign(_exit_sort=pd.to_datetime(frame["exit_datetime"], errors="coerce", utc=True)).sort_values(
            "_exit_sort", kind="mergesort", na_position="last"
        )
    pnl = pd.to_numeric(frame.get("realized_p_l", pd.Series(0, index=frame.index)), errors="coerce").fillna(0)
    cost = pd.to_numeric(frame.get("initial_cost", pd.Series(pd.NA, index=frame.index)), errors="coerce")
    returns = (pnl / cost.replace(0, pd.NA) * 100).fillna(0)
    gross_loss = abs(float(pnl[pnl.lt(0)].sum()))
    profit_factor = round(float(pnl[pnl.gt(0)].sum()) / gross_loss, 3) if gross_loss else None
    cumulative = returns.cumsum()
    drawdown = cumulative - cumulative.cummax().clip(lower=0)
    return {
        "resolved": int(len(frame)),
        "win_rate_pct": round(float(pnl.gt(0).mean() * 100), 2),
        "expectancy_pct": round(float(returns.mean()), 3),
        "profit_factor": profit_factor,
        "max_drawdown_pct": round(abs(float(drawdown.min())), 3),
    }


def _candidate_change(settings: dict, index: int, metrics: dict) -> tuple[dict, dict]:
    lever = LEVER_CATALOG[index % len(LEVER_CATALOG)]
    candidate = copy.deepcopy(settings)
    old = _value(candidate, lever["key"])
    key = lever["key"]
    if key.endswith("confirmation_buy_min_score"):
        new = min(50, int(old) + 5)
    elif key.endswith("first_target_gain_pct"):
        new = max(3, min(8, float(old) + (1 if metrics["win_rate_pct"] >= 60 else -1)))
    elif key.endswith("stop_loss_pct"):
        new = max(3, round(float(old) - 0.5, 1))
    elif key.endswith("max_holding_days"):
        new = max(5, int(old) - 1)
    elif key.endswith("morning_candidate_min_score"):
        new = min(45, int(old) + 2)
    else:
        new = min(15, float(old) + 1)
    _set_value(candidate, key, new)
    return candidate, {**lever, "old_value": old, "new_value": new}


def _accepted(baseline: dict, candidate: dict, policy: dict) -> tuple[bool, str]:
    expectancy_gain = candidate["expectancy_pct"] - baseline["expectancy_pct"]
    pf_base = baseline["profit_factor"]
    pf_candidate = candidate["profit_factor"]
    if pf_candidate is None:
        pf_ok = True
    elif pf_base is None:
        pf_ok = False
    else:
        pf_ok = pf_candidate >= pf_base * (
            1 - float(policy["maximum_profit_factor_decline_pct"]) / 100
        )
    allowed_drawdown = max(0.5, baseline["max_drawdown_pct"] * (
        1 + float(policy["maximum_drawdown_worsening_pct"]) / 100
    ))
    drawdown_ok = candidate["max_drawdown_pct"] <= allowed_drawdown
    accepted = expectancy_gain >= float(policy["minimum_expectancy_improvement_pct"]) and pf_ok and drawdown_ok
    reason = (
        f"Expectancy change {expectancy_gain:+.3f} points; profit-factor guard "
        f"{'passed' if pf_ok else 'failed'}; drawdown guard {'passed' if drawdown_ok else 'failed'}."
    )
    return accepted, reason


def _record(state: dict, event: dict) -> None:
    event = {"recorded_at": _now(), **event}
    state.setdefault("history", []).append(event)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with LEDGER_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, separators=(",", ":")) + "\n")
    try:
        from db import connect, init_schema

        init_schema()
        with connect(dict_rows=False) as (_, cursor):
            cursor.execute(
                """
                INSERT INTO strategy_optimizer_events
                    (recorded_at, cycle, status, area, lever, payload)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                """,
                (
                    event["recorded_at"], int(event.get("cycle", 0)),
                    str(event.get("status", "")), str(event.get("area", "")),
                    str(event.get("lever", "")), json.dumps(event),
                ),
            )
    except Exception:
        # State history and JSONL provide a recovery copy during DB outages.
        pass


def _start_experiment(state: dict, settings: dict, baseline_metrics: dict, policy: dict) -> dict:
    start_index = int(state.get("lever_index", 0)) % len(LEVER_CATALOG)
    for offset in range(len(LEVER_CATALOG)):
        index = (start_index + offset) % len(LEVER_CATALOG)
        candidate, change = _candidate_change(settings, index, baseline_metrics)
        if change["new_value"] != change["old_value"]:
            break
    else:
        state["phase"] = "no_available_experiment"
        state["last_result_reason"] = "Every automatic lever has reached its configured safety bound."
        return state
    cycle = int(state.get("cycle", 0)) + 1
    candidate_version = f"auto-{cycle:03d}-{change['key'].split('.')[-1].replace('_', '-')}"
    candidate["version"] = candidate_version
    state.update({
        "phase": "experiment_running",
        "cycle": cycle,
        "baseline_version": str(settings["version"]),
        "baseline_settings": settings,
        "baseline_metrics": baseline_metrics,
        "candidate_version": candidate_version,
        "candidate_settings": candidate,
        "active_change": change,
        "active_lever_index": index,
        "experiment_target": int(policy["experiment_resolved_trades"]),
        "experiment_started_at": _now(),
        "last_action_date": datetime.now().astimezone().date().isoformat(),
    })
    _write_settings(candidate)
    _record(state, {
        "cycle": cycle, "status": "started", "strategy_version": candidate_version,
        "lever": change["key"], "area": change["area"], "old_value": change["old_value"],
        "new_value": change["new_value"], "rationale": change["reason"],
        "baseline_metrics": baseline_metrics,
    })
    return state


def run_optimizer_cycle() -> dict:
    """Advance at most one optimizer transition per market-day review."""
    policy_path = Path(__file__).resolve().parent / "optimizer_policy.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    settings = load_active_settings()
    # Migrates an existing file-backed active strategy on the first DB-backed run.
    _save_database_value("active_settings", settings)
    state = load_optimizer_state()
    trades = read_table("paper_trades")
    today = datetime.now().astimezone().date().isoformat()

    if state.get("last_action_date") == today:
        return optimizer_status(state, trades, policy)

    if state.get("phase") == "experiment_running":
        candidate_rows = _resolved_for_version(trades, str(state["candidate_version"]))
        target = int(policy["experiment_resolved_trades"])
        if len(candidate_rows) >= target:
            candidate_metrics = strategy_metrics(candidate_rows.tail(target))
            baseline_metrics = state["baseline_metrics"]
            keep, reason = _accepted(baseline_metrics, candidate_metrics, policy)
            if keep:
                final_settings = state["candidate_settings"]
                status = "kept"
                next_baseline_version = state["candidate_version"]
                next_baseline_metrics = candidate_metrics
            else:
                final_settings = state["baseline_settings"]
                status = "reverted"
                next_baseline_version = state["baseline_version"]
                next_baseline_metrics = baseline_metrics
            _write_settings(final_settings)
            _record(state, {
                "cycle": state["cycle"], "status": status,
                "strategy_version": state["candidate_version"],
                "lever": state["active_change"]["key"], "area": state["active_change"]["area"],
                "old_value": state["active_change"]["old_value"],
                "new_value": state["active_change"]["new_value"], "rationale": reason,
                "baseline_metrics": baseline_metrics, "candidate_metrics": candidate_metrics,
            })
            state.update({
                "phase": "ready_for_next_experiment",
                "baseline_version": next_baseline_version,
                "baseline_settings": final_settings,
                "baseline_metrics": next_baseline_metrics,
                "last_result": status,
                "last_result_reason": reason,
                "lever_index": (int(state.get("active_lever_index", state.get("lever_index", 0))) + 1) % len(LEVER_CATALOG),
                "last_action_date": today,
            })
    elif state.get("phase") == "ready_for_next_experiment":
        state = _start_experiment(state, settings, state["baseline_metrics"], policy)
    elif state.get("phase") == "no_available_experiment":
        pass
    else:
        baseline_rows = _resolved_for_version(trades, str(settings["version"]))
        if len(baseline_rows) >= int(policy["minimum_resolved_trades"]):
            state = _start_experiment(
                state, settings,
                strategy_metrics(baseline_rows.tail(int(policy["minimum_resolved_trades"]))), policy,
            )

    _save_state(state)
    return optimizer_status(state, trades, policy)


def optimizer_status(state: dict | None = None, trades: pd.DataFrame | None = None,
                     policy: dict | None = None) -> dict:
    state = state or load_optimizer_state()
    if policy is None:
        policy = json.loads((Path(__file__).resolve().parent / "optimizer_policy.json").read_text(encoding="utf-8"))
    if trades is None:
        trades = read_table("paper_trades")
    settings = load_active_settings()
    phase = state.get("phase", "collecting_baseline")
    if phase == "experiment_running":
        completed = len(_resolved_for_version(trades, str(state.get("candidate_version", ""))))
        target = int(policy["experiment_resolved_trades"])
        step = 3
    elif phase == "ready_for_next_experiment":
        completed, target, step = 1, 1, 4
    elif phase == "no_available_experiment":
        completed, target, step = 1, 1, 5
    else:
        completed = len(_resolved_for_version(trades, str(settings.get("version", ""))))
        target = int(policy["minimum_resolved_trades"])
        step = 1
    return {
        "phase": phase, "step": step, "completed": min(completed, target), "target": target,
        "progress_pct": round(min(100, completed / target * 100 if target else 0), 1),
        "cycle": int(state.get("cycle", 0)), "active_version": settings.get("version"),
        "baseline_version": state.get("baseline_version", settings.get("version")),
        "candidate_version": state.get("candidate_version"), "active_change": state.get("active_change"),
        "last_result": state.get("last_result"), "last_result_reason": state.get("last_result_reason"),
        "history": list(reversed(state.get("history", [])))[0:20], "levers": LEVER_CATALOG,
        "experiment_size": int(policy["experiment_resolved_trades"]),
        "baseline_size": int(policy["minimum_resolved_trades"]),
    }
