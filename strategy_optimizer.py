"""Guarded, persistent one-lever-at-a-time optimizer for paper trading."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import numpy as np

from scanner_config import LOGS_DIR, STRATEGY_SETTINGS_FILE, RUNTIME_STRATEGY_SETTINGS_FILE
from stock_storage import read_table


STATE_DIR = LOGS_DIR / "strategy_optimizer"
STATE_FILE = STATE_DIR / "state.json"
LEDGER_FILE = STATE_DIR / "change_ledger.jsonl"

LEVER_CATALOG = [
    {
        "key": "catalyst.use_for_selection",
        "label": "Catalyst-informed selection",
        "area": "Catalyst",
        "bounds": "Shadow off → challenger on",
        "reason": "Tests whether blocking verified risk headlines while retaining neutral or positive news improves returns.",
    },
    {
        "key": "selection.confirmation_buy_min_score",
        "label": "Confirmation threshold",
        "area": "Entry",
        "bounds": "40–50 · step 5",
        "reason": "Tests whether requiring stronger intraday confirmation improves trade quality.",
    },
    {
        "key": "risk.scale_out.first_target_gain_pct",
        "label": "First profit target",
        "area": "Exit",
        "bounds": "3–8% · step 1",
        "reason": "Tests the trade-off between reaching the first scale-out and capturing more return.",
    },
    {
        "key": "risk.stop_loss_pct",
        "label": "Protective stop",
        "area": "Risk",
        "bounds": "3–5% · tighten 0.5",
        "reason": "Tests a modestly tighter loss limit; the optimizer never widens this safeguard.",
    },
    {
        "key": "risk.max_holding_days",
        "label": "Maximum holding period",
        "area": "Exit",
        "bounds": "5–10 sessions · step 1",
        "reason": "Tests whether releasing stale capital sooner improves return per trade.",
    },
    {
        "key": "selection.morning_candidate_min_score",
        "label": "Morning screener threshold",
        "area": "Screener",
        "bounds": "35–45 · step 2",
        "reason": "Tests whether filtering weaker morning setups improves downstream entries.",
    },
    {
        "key": "risk.scale_out.second_target_gain_pct",
        "label": "Second profit target",
        "area": "Exit",
        "bounds": "10–15% · step 1",
        "reason": "Tests whether the second tranche should demand slightly more upside.",
    },
    {
        "key": "risk.scale_out.first_target_initial_shares_pct",
        "label": "First target tranche",
        "area": "Exit",
        "bounds": "30–60% · step 10",
        "reason": "Tests taking less early profit so more shares participate in larger winners.",
    },
    {
        "key": "risk.scale_out.second_target_initial_shares_pct",
        "label": "Second target tranche",
        "area": "Exit",
        "bounds": "15–35% · step 5",
        "reason": "Tests the balance between banking the second target and preserving the runner.",
    },
    {
        "key": "risk.scale_out.runner_exit_sessions_after_second_target",
        "label": "Runner duration",
        "area": "Exit",
        "bounds": "1–5 sessions · step 1",
        "reason": "Tests whether giving successful runners more time improves realized returns.",
    },
    {
        "key": "risk.scale_out.breakeven_after_first_target_pct",
        "label": "Post-target protection",
        "area": "Exit",
        "bounds": "0–2% · step 0.5",
        "reason": "Tests how aggressively to protect the remainder after the first scale-out.",
    },
    {
        "key": "risk.scale_out.runner_stop_gain_pct",
        "label": "Runner protection",
        "area": "Exit",
        "bounds": "6–10% · step 1",
        "reason": "Tests how much room the runner receives after reaching the second target.",
    },
    {
        "key": "risk.scale_out.trailing_stop_pct",
        "label": "Trailing stop",
        "area": "Exit",
        "bounds": "Off or 2–5% · step 0.5",
        "reason": "Tests a high-water trailing exit after the first target instead of a fixed floor alone.",
    },
    {
        "key": "risk.scale_out.risk_on_target_multiplier",
        "label": "Risk-on target multiplier",
        "area": "Regime exit",
        "bounds": "1.0–1.25 · step 0.05",
        "reason": "Tests larger targets when the broad market trend is favorable.",
    },
    {
        "key": "risk.scale_out.risk_off_target_multiplier",
        "label": "Risk-off target multiplier",
        "area": "Regime exit",
        "bounds": "0.75–1.0 · step 0.05",
        "reason": "Tests faster profit-taking when the broad market trend is unfavorable.",
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


def _with_default_settings(settings: dict) -> dict:
    defaults = json.loads(STRATEGY_SETTINGS_FILE.read_text(encoding="utf-8"))

    def merge(target: dict, source: dict) -> None:
        for key, value in source.items():
            if key not in target:
                target[key] = copy.deepcopy(value)
            elif isinstance(value, dict) and isinstance(target[key], dict):
                merge(target[key], value)

    normalized = copy.deepcopy(settings)
    merge(normalized, defaults)
    return normalized


def load_active_settings() -> dict:
    database_settings = _database_value("active_settings")
    if database_settings:
        return _with_default_settings(database_settings)
    source = RUNTIME_STRATEGY_SETTINGS_FILE if RUNTIME_STRATEGY_SETTINGS_FILE.exists() else STRATEGY_SETTINGS_FILE
    return _with_default_settings(json.loads(source.read_text(encoding="utf-8")))


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


def strategy_routing_context() -> dict:
    """Load routing state once for a report run instead of once per candidate."""
    policy = json.loads((Path(__file__).resolve().parent / "optimizer_policy.json").read_text(encoding="utf-8"))
    return {"state": load_optimizer_state(), "active": load_active_settings(), "policy": policy}


def strategy_assignment(ticker: str, trade_date: str, context: dict | None = None) -> dict:
    """Deterministically route a new paper trade to champion or challenger."""
    context = context or strategy_routing_context()
    state = context["state"]
    active = context["active"]
    if state.get("phase") != "experiment_running":
        return {"arm": "champion", "version": str(active["version"]), "settings": active}
    policy = context["policy"]
    digest = hashlib.sha256(f"{trade_date}:{ticker.upper()}".encode()).hexdigest()
    allocation = min(
        int(policy.get("challenger_allocation_max_pct", 40)),
        int(state.get("challenger_allocation_pct", policy["challenger_allocation_pct"])),
    )
    challenger = int(digest[:8], 16) % 100 < allocation
    if challenger:
        return {
            "arm": "challenger", "version": str(state["candidate_version"]),
            "settings": _with_default_settings(state["candidate_settings"]),
        }
    return {
        "arm": "champion", "version": str(state["baseline_version"]),
        "settings": _with_default_settings(state["baseline_settings"]),
    }


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


def _arm_rows(trades: pd.DataFrame, version: str, arm: str, resolved: bool) -> pd.DataFrame:
    if trades.empty:
        return trades.copy()
    remaining = pd.to_numeric(
        trades.get("remaining_shares", pd.Series(0, index=trades.index)), errors="coerce"
    ).fillna(0)
    versions = trades.get(
        "entry_strategy_version", pd.Series("", index=trades.index)
    ).fillna("").astype(str)
    arms = trades.get("optimizer_arm", pd.Series("", index=trades.index)).fillna("").astype(str)
    state_mask = remaining.le(0) if resolved else remaining.gt(0)
    return trades[state_mask & versions.eq(version) & arms.eq(arm)].copy()


def _matched_champion_rows(champion: pd.DataFrame, challenger: pd.DataFrame) -> pd.DataFrame:
    """Select contemporaneous champion trades with similar observable entry context."""
    if champion.empty or challenger.empty:
        return champion.tail(len(challenger)).copy()
    available = champion.copy()
    matches = []
    for _, candidate in challenger.iterrows():
        pool = available
        for column in ("market_regime", "confirmation_band", "sector"):
            if column in pool and column in challenger and pd.notna(candidate.get(column)):
                exact = pool[pool[column].fillna("").astype(str).eq(str(candidate.get(column)))]
                if not exact.empty:
                    pool = exact
        if pool.empty:
            pool = available
        if "entry_datetime" in pool and pd.notna(candidate.get("entry_datetime")):
            candidate_time = pd.to_datetime(candidate.get("entry_datetime"), errors="coerce", utc=True)
            times = pd.to_datetime(pool["entry_datetime"], errors="coerce", utc=True)
            chosen_index = (times - candidate_time).abs().sort_values().index[0]
        else:
            chosen_index = pool.index[0]
        matches.append(available.loc[chosen_index])
        available = available.drop(index=chosen_index)
        if available.empty:
            break
    return pd.DataFrame(matches)


def _data_quality(frame: pd.DataFrame) -> dict:
    required = ("entry_price", "initial_cost", "realized_p_l", "exit_price", "entry_strategy_version")
    issues = []
    for column in required:
        if column not in frame:
            issues.append(f"missing column {column}")
            continue
        missing = int(frame[column].isna().sum())
        if missing:
            issues.append(f"{missing} missing {column}")
    duplicates = int(frame.get("trade_id", pd.Series(dtype=str)).duplicated().sum()) if "trade_id" in frame else 0
    if duplicates:
        issues.append(f"{duplicates} duplicate trade IDs")
    return {"passed": not issues, "issues": issues, "checked": int(len(frame))}


def strategy_metrics(frame: pd.DataFrame, policy: dict | None = None) -> dict:
    policy = policy or json.loads(
        (Path(__file__).resolve().parent / "optimizer_policy.json").read_text(encoding="utf-8")
    )
    if frame.empty:
        return {"resolved": 0, "win_rate_pct": 0.0, "expectancy_pct": 0.0,
                "weighted_expectancy_pct": 0.0, "stressed_expectancy_pct": 0.0,
                "average_winner_pct": 0.0, "average_loser_pct": 0.0,
                "profit_factor": None, "max_drawdown_pct": 0.0,
                "average_holding_days": 0.0, "maximum_consecutive_losses": 0,
                "returns": [], "segments": {}}
    frame = frame.copy()
    if "exit_datetime" in frame:
        frame = frame.assign(_exit_sort=pd.to_datetime(frame["exit_datetime"], errors="coerce", utc=True)).sort_values(
            "_exit_sort", kind="mergesort", na_position="last"
        )
    pnl = pd.to_numeric(frame.get("realized_p_l", pd.Series(0, index=frame.index)), errors="coerce").fillna(0)
    cost = pd.to_numeric(frame.get("initial_cost", pd.Series(pd.NA, index=frame.index)), errors="coerce")
    returns = (pnl / cost.replace(0, pd.NA) * 100).fillna(0)
    half_life = max(1.0, float(policy["recent_trade_half_life"]))
    ages = np.arange(len(returns) - 1, -1, -1, dtype=float)
    weights = np.power(0.5, ages / half_life)
    weighted_expectancy = float(np.average(returns.to_numpy(dtype=float), weights=weights))
    stressed_expectancy = weighted_expectancy - (2 * float(policy["stress_slippage_bps"]) / 100)
    gross_loss = abs(float(pnl[pnl.lt(0)].sum()))
    profit_factor = round(float(pnl[pnl.gt(0)].sum()) / gross_loss, 3) if gross_loss else None
    cumulative = returns.cumsum()
    drawdown = cumulative - cumulative.cummax().clip(lower=0)
    loss_streak = max_streak = 0
    for value in returns:
        loss_streak = loss_streak + 1 if value < 0 else 0
        max_streak = max(max_streak, loss_streak)
    holding = pd.to_numeric(
        frame.get("holding_days", pd.Series(pd.NA, index=frame.index)), errors="coerce"
    )
    segments: dict[str, dict] = {}
    for column in ("market_regime", "confirmation_band", "sector"):
        if column not in frame:
            continue
        for label, indexes in frame.groupby(frame[column].fillna("UNKNOWN").astype(str)).groups.items():
            segment_returns = returns.loc[indexes]
            segments[f"{column}:{label}"] = {
                "resolved": int(len(segment_returns)),
                "expectancy_pct": round(float(segment_returns.mean()), 3),
                "win_rate_pct": round(float(segment_returns.gt(0).mean() * 100), 2),
            }
    pf_for_score = min(5.0, profit_factor if profit_factor is not None else 5.0)
    avg_holding = float(holding.mean()) if holding.notna().any() else 0.0
    winning_returns = returns[returns.gt(0)]
    losing_returns = returns[returns.lt(0)]
    average_winner = float(winning_returns.mean()) if not winning_returns.empty else 0.0
    average_loser = float(losing_returns.mean()) if not losing_returns.empty else 0.0
    objective_score = (
        weighted_expectancy + float(pnl.gt(0).mean()) + 0.15 * pf_for_score
        + 0.10 * average_winner
        - 0.10 * abs(float(drawdown.min())) - 0.02 * avg_holding
    )
    return {
        "resolved": int(len(frame)),
        "win_rate_pct": round(float(pnl.gt(0).mean() * 100), 2),
        "expectancy_pct": round(float(returns.mean()), 3),
        "weighted_expectancy_pct": round(weighted_expectancy, 3),
        "stressed_expectancy_pct": round(stressed_expectancy, 3),
        "average_winner_pct": round(average_winner, 3),
        "average_loser_pct": round(average_loser, 3),
        "profit_factor": profit_factor,
        "max_drawdown_pct": round(abs(float(drawdown.min())), 3),
        "average_holding_days": round(avg_holding, 2),
        "maximum_consecutive_losses": int(max_streak),
        "objective_score": round(objective_score, 3),
        "returns": [round(float(value), 5) for value in returns.tolist()],
        "segments": segments,
    }


def _candidate_change(settings: dict, index: int, metrics: dict) -> tuple[dict, dict]:
    lever = LEVER_CATALOG[index % len(LEVER_CATALOG)]
    candidate = copy.deepcopy(settings)
    old = _value(candidate, lever["key"])
    key = lever["key"]
    if key.endswith("confirmation_buy_min_score"):
        new = min(50, int(old) + 5)
    elif key == "catalyst.use_for_selection":
        new = True
    elif key.endswith("first_target_gain_pct"):
        needs_larger_winners = float(metrics.get("average_winner_pct", 0)) < 5
        new = max(3, min(8, float(old) + (1 if needs_larger_winners or metrics["win_rate_pct"] >= 60 else -1)))
    elif key.endswith("stop_loss_pct"):
        new = max(3, round(float(old) - 0.5, 1))
    elif key.endswith("max_holding_days"):
        new = max(5, int(old) - 1)
    elif key.endswith("morning_candidate_min_score"):
        new = min(45, int(old) + 2)
    elif key.endswith("first_target_initial_shares_pct"):
        second_pct = int(settings["risk"]["scale_out"]["second_target_initial_shares_pct"])
        new = max(30, int(old) - 10) if metrics.get("average_winner_pct", 0) < 5 else min(60, 90 - second_pct, int(old) + 10)
    elif key.endswith("second_target_initial_shares_pct"):
        first_pct = int(settings["risk"]["scale_out"]["first_target_initial_shares_pct"])
        new = max(15, int(old) - 5) if metrics.get("average_winner_pct", 0) < 5 else min(35, 90 - first_pct, int(old) + 5)
    elif key.endswith("runner_exit_sessions_after_second_target"):
        new = min(5, int(old) + 1) if metrics.get("average_winner_pct", 0) < 5 else max(1, int(old) - 1)
    elif key.endswith("breakeven_after_first_target_pct"):
        new = min(2, round(float(old) + 0.5, 1))
    elif key.endswith("runner_stop_gain_pct"):
        new = max(6, float(old) - 1) if metrics.get("average_winner_pct", 0) < 5 else min(10, float(old) + 1)
    elif key.endswith("trailing_stop_pct"):
        new = 3.0 if float(old) <= 0 else max(2, round(float(old) - 0.5, 1))
    elif key.endswith("risk_on_target_multiplier"):
        new = min(1.25, round(float(old) + 0.05, 2))
    elif key.endswith("risk_off_target_multiplier"):
        new = max(0.75, round(float(old) - 0.05, 2))
    else:
        new = min(15, float(old) + 1)
    _set_value(candidate, key, new)
    return candidate, {**lever, "old_value": old, "new_value": new}


def _lever_ready(lever: dict, policy: dict) -> bool:
    if lever["key"] != "catalyst.use_for_selection":
        return True
    observations = read_table("catalyst_observations")
    if observations.empty or "catalyst_configured" not in observations:
        return False
    configured = observations["catalyst_configured"].astype(str).str.lower().isin({"1", "true", "yes"})
    return int(configured.sum()) >= int(policy["minimum_catalyst_observations"])


def _bootstrap_confidence(baseline: dict, candidate: dict, iterations: int) -> float:
    baseline_returns = np.asarray(baseline.get("returns", []), dtype=float)
    candidate_returns = np.asarray(candidate.get("returns", []), dtype=float)
    if not len(baseline_returns) or not len(candidate_returns):
        return 0.0
    rng = np.random.default_rng(20260701 + len(baseline_returns) + len(candidate_returns))
    baseline_means = rng.choice(baseline_returns, (iterations, len(baseline_returns)), replace=True).mean(axis=1)
    candidate_means = rng.choice(candidate_returns, (iterations, len(candidate_returns)), replace=True).mean(axis=1)
    return round(float((candidate_means > baseline_means).mean() * 100), 2)


def _dominant_regime(metrics: dict) -> str | None:
    regimes = {
        name.split(":", 1)[1]: values for name, values in metrics.get("segments", {}).items()
        if name.startswith("market_regime:")
    }
    return max(regimes, key=lambda name: regimes[name]["resolved"]) if regimes else None


def _promotion_evaluation(baseline: dict, candidate: dict, policy: dict) -> tuple[bool, str, dict]:
    expectancy_gain = candidate["weighted_expectancy_pct"] - baseline["weighted_expectancy_pct"]
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
    confidence = _bootstrap_confidence(baseline, candidate, int(policy["bootstrap_iterations"]))
    base_confidence = float(policy["minimum_promotion_confidence_pct"])
    cycle = max(1, int(policy.get("current_cycle", 1)))
    required_confidence = min(
        float(policy.get("multiple_test_confidence_cap_pct", 99)),
        base_confidence + max(0, cycle - 1),
    )
    confidence_ok = confidence >= required_confidence
    stress_ok = candidate["stressed_expectancy_pct"] > baseline["stressed_expectancy_pct"]
    objective_ok = candidate["objective_score"] > baseline["objective_score"]
    segment_ok = True
    weak_segments: list[str] = []
    minimum_segment = int(policy["minimum_segment_trades"])
    for name, candidate_segment in candidate.get("segments", {}).items():
        baseline_segment = baseline.get("segments", {}).get(name)
        if not baseline_segment:
            continue
        if min(candidate_segment["resolved"], baseline_segment["resolved"]) < minimum_segment:
            continue
        if candidate_segment["expectancy_pct"] < baseline_segment["expectancy_pct"] - 0.5:
            segment_ok = False
            weak_segments.append(name)
    guards = {
        "expectancy": expectancy_gain >= float(policy["minimum_expectancy_improvement_pct"]),
        "profit_factor": pf_ok, "drawdown": drawdown_ok,
        "confidence": confidence_ok, "cost_stress": stress_ok,
        "objective": objective_ok, "segments": segment_ok,
    }
    accepted = all(guards.values())
    reason = (
        f"Recency-weighted expectancy {expectancy_gain:+.3f} points; promotion confidence {confidence:.1f}% "
        f"(required {required_confidence:.1f}% after {cycle} tests); "
        f"cost stress {'passed' if stress_ok else 'failed'}; objective {'improved' if objective_ok else 'declined'}; "
        f"profit-factor guard {'passed' if pf_ok else 'failed'}; drawdown guard {'passed' if drawdown_ok else 'failed'}; "
        f"segment guard {'passed' if segment_ok else 'failed for ' + ', '.join(weak_segments)}."
    )
    details = {
        "guards": guards, "confidence_pct": confidence,
        "required_confidence_pct": required_confidence,
        "expectancy_change_pct": round(expectancy_gain, 3),
        "weak_segments": weak_segments,
    }
    return accepted, reason, details


def _accepted(baseline: dict, candidate: dict, policy: dict) -> tuple[bool, str]:
    accepted, reason, _ = _promotion_evaluation(baseline, candidate, policy)
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
    cooldown = int(policy["lever_cooldown_cycles"])
    recent_levers = {
        str(item.get("lever")) for item in state.get("history", [])
        if item.get("status") == "started" and int(item.get("cycle", 0)) > int(state.get("cycle", 0)) - cooldown
    }
    tried_values = {
        (str(item.get("lever")), str(item.get("new_value")))
        for item in state.get("history", []) if item.get("status") == "started"
    }
    for offset in range(len(LEVER_CATALOG)):
        index = (start_index + offset) % len(LEVER_CATALOG)
        if not _lever_ready(LEVER_CATALOG[index], policy):
            continue
        candidate, change = _candidate_change(settings, index, baseline_metrics)
        if (
            change["new_value"] != change["old_value"]
            and change["key"] not in recent_levers
            and (change["key"], str(change["new_value"])) not in tried_values
        ):
            break
    else:
        state["phase"] = "no_available_experiment"
        state["last_result_reason"] = "Every automatic lever has reached its configured safety bound."
        return state
    cycle = int(state.get("cycle", 0)) + 1
    candidate_version = (
        "catalyst-v1" if change["key"] == "catalyst.use_for_selection"
        else f"auto-{cycle:03d}-{change['key'].split('.')[-1].replace('_', '-')}"
    )
    candidate["version"] = candidate_version
    state.update({
        "phase": "experiment_running",
        "cycle": cycle,
        "baseline_version": str(settings["version"]),
        "baseline_settings": settings,
        "baseline_metrics": baseline_metrics,
        "baseline_regime": _dominant_regime(baseline_metrics),
        "candidate_version": candidate_version,
        "candidate_settings": candidate,
        "active_change": change,
        "active_lever_index": index,
        "experiment_target": int(policy["experiment_resolved_trades"]),
        "challenger_allocation_pct": int(policy["challenger_allocation_pct"]),
        "experiment_started_at": _now(),
        "last_action_date": datetime.now().astimezone().date().isoformat(),
    })
    acquisition_settings = copy.deepcopy(candidate)
    for key in (
        "morning_monitor_min_score", "morning_candidate_min_score",
        "morning_buy_min_score", "confirmation_buy_min_score",
    ):
        baseline_value = settings.get("selection", {}).get(key)
        candidate_value = candidate.get("selection", {}).get(key)
        if baseline_value is not None and candidate_value is not None:
            acquisition_settings["selection"][key] = min(baseline_value, candidate_value)
    _write_settings(acquisition_settings)
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

    if state.get("paused"):
        _save_state(state)
        return optimizer_status(state, trades, policy)

    if state.get("last_action_date") == today:
        return optimizer_status(state, trades, policy)

    if state.get("phase") == "experiment_running":
        candidate_rows = _resolved_for_version(trades, str(state["candidate_version"]))
        if "optimizer_arm" in candidate_rows:
            candidate_rows = candidate_rows[candidate_rows["optimizer_arm"].fillna("").eq("challenger")]
        champion_rows = _resolved_for_version(trades, str(state["baseline_version"]))
        if "optimizer_arm" in champion_rows:
            champion_rows = champion_rows[champion_rows["optimizer_arm"].fillna("").eq("champion")]
        started = pd.Timestamp(state.get("experiment_started_at"))
        started = (
            started.tz_localize("America/New_York") if started.tzinfo is None
            else started.tz_convert("America/New_York")
        )
        for name, frame in (("candidate", candidate_rows), ("champion", champion_rows)):
            if "entry_datetime" in frame:
                entered = pd.to_datetime(frame["entry_datetime"], errors="coerce", utc=True)
                filtered = frame[entered.ge(started.tz_convert("UTC"))]
                if name == "candidate":
                    candidate_rows = filtered
                else:
                    champion_rows = filtered
        target = int(policy["experiment_resolved_trades"])
        candidate_metrics = strategy_metrics(candidate_rows, policy)
        emergency = (
            len(candidate_rows) >= int(policy["emergency_minimum_trades"])
            and (
                candidate_metrics["maximum_consecutive_losses"] >= int(policy["emergency_consecutive_losses"])
                or candidate_metrics["max_drawdown_pct"] >= float(policy["emergency_drawdown_pct"])
            )
        )
        if (
            len(candidate_rows) >= int(policy["emergency_minimum_trades"])
            and not emergency and candidate_metrics["weighted_expectancy_pct"] > 0
            and candidate_metrics["maximum_consecutive_losses"] < int(policy["emergency_consecutive_losses"])
        ):
            state["challenger_allocation_pct"] = min(
                int(policy["challenger_allocation_passed_safety_pct"]),
                int(policy["challenger_allocation_max_pct"]),
            )
        if emergency:
            final_settings = state["baseline_settings"]
            reason = (
                f"Emergency rollback after {len(candidate_rows)} challenger trades: "
                f"{candidate_metrics['maximum_consecutive_losses']} consecutive losses and "
                f"{candidate_metrics['max_drawdown_pct']:.2f}% drawdown."
            )
            _write_settings(final_settings)
            _record(state, {
                "cycle": state["cycle"], "status": "emergency_reverted",
                "strategy_version": state["candidate_version"],
                "lever": state["active_change"]["key"], "area": state["active_change"]["area"],
                "old_value": state["active_change"]["old_value"],
                "new_value": state["active_change"]["new_value"], "rationale": reason,
                "baseline_metrics": state["baseline_metrics"], "candidate_metrics": candidate_metrics,
            })
            state.update({
                "phase": "ready_for_next_experiment", "last_result": "emergency_reverted",
                "last_result_reason": reason,
                "lever_index": (int(state.get("active_lever_index", 0)) + 1) % len(LEVER_CATALOG),
                "last_action_date": today,
            })
        elif len(candidate_rows) >= target:
            candidate_metrics = strategy_metrics(candidate_rows.tail(target), policy)
            matched_champion = _matched_champion_rows(champion_rows, candidate_rows.tail(target))
            baseline_metrics = (
                strategy_metrics(matched_champion, policy)
                if len(matched_champion) >= int(policy["minimum_segment_trades"])
                else state["baseline_metrics"]
            )
            if "weighted_expectancy_pct" not in baseline_metrics:
                historical_champion = _resolved_for_version(trades, str(state["baseline_version"]))
                baseline_metrics = strategy_metrics(
                    historical_champion.tail(int(policy["minimum_resolved_trades"])), policy
                )
            current_regime = _dominant_regime(baseline_metrics)
            if current_regime and state.get("baseline_regime") and current_regime != state.get("baseline_regime"):
                _record(state, {
                    "cycle": state["cycle"], "status": "regime_rebaseline",
                    "strategy_version": state["baseline_version"], "lever": "market_regime",
                    "area": "Optimizer", "old_value": state.get("baseline_regime"),
                    "new_value": current_regime,
                    "rationale": "Dominant market regime changed; refreshed the champion comparison baseline.",
                    "baseline_metrics": baseline_metrics,
                })
                state["baseline_regime"] = current_regime
            evaluation_policy = {**policy, "current_cycle": int(state.get("cycle", 1))}
            keep, reason, evaluation = _promotion_evaluation(
                baseline_metrics, candidate_metrics, evaluation_policy
            )
            quality = _data_quality(candidate_rows.tail(target))
            if not quality["passed"]:
                keep = False
                evaluation["guards"]["data_quality"] = False
                reason += " Data-quality gate failed: " + "; ".join(quality["issues"]) + "."
            else:
                evaluation["guards"]["data_quality"] = True
            evaluation["matched_champion_trades"] = int(len(matched_champion))
            state["last_guard_results"] = evaluation
            state["last_data_quality"] = quality
            if keep:
                state["previous_champion_settings"] = state["baseline_settings"]
                state["previous_champion_version"] = state["baseline_version"]
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
                "evaluation": evaluation, "data_quality": quality,
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
                "promoted_trades_since_rebaseline": (
                    int(state.get("promoted_trades_since_rebaseline", 0)) + target if keep else
                    int(state.get("promoted_trades_since_rebaseline", 0))
                ),
            })
            if keep and state["promoted_trades_since_rebaseline"] >= int(policy["rebaseline_after_promoted_trades"]):
                state["baseline_metrics"] = candidate_metrics
                state["promoted_trades_since_rebaseline"] = 0
                state["last_rebaseline_at"] = _now()
                _record(state, {
                    "cycle": state["cycle"], "status": "rebaselined",
                    "strategy_version": state["candidate_version"], "lever": "optimizer.baseline",
                    "area": "Optimizer", "old_value": "prior champion", "new_value": state["candidate_version"],
                    "rationale": f"Periodic re-baseline after {policy['rebaseline_after_promoted_trades']} promoted trades.",
                    "candidate_metrics": candidate_metrics,
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
                strategy_metrics(baseline_rows.tail(int(policy["minimum_resolved_trades"])), policy), policy,
            )

    _save_state(state)
    return optimizer_status(state, trades, policy)


def optimizer_control(action: str) -> dict:
    """Apply an explicit, audited operator control to the optimizer."""
    state = load_optimizer_state()
    action = str(action or "").strip().lower()
    if action in {"pause", "resume"}:
        state["paused"] = action == "pause"
        reason = "Optimizer paused by operator." if state["paused"] else "Optimizer resumed by operator."
    elif action == "reject":
        if state.get("phase") != "experiment_running":
            return {"ok": False, "error": "No running challenger to reject."}
        _write_settings(state["baseline_settings"])
        state["phase"] = "ready_for_next_experiment"
        state["last_result"] = "operator_rejected"
        reason = "Running challenger rejected by operator; champion restored."
    elif action == "rollback":
        previous = state.get("previous_champion_settings")
        if not previous:
            return {"ok": False, "error": "No previous champion snapshot is available."}
        current_settings = load_active_settings()
        _write_settings(previous)
        state["baseline_settings"] = previous
        state["baseline_version"] = state.get("previous_champion_version", previous.get("version"))
        state["previous_champion_settings"] = current_settings
        state["previous_champion_version"] = current_settings.get("version")
        state["phase"] = "ready_for_next_experiment"
        state["last_result"] = "operator_rollback"
        reason = "Previous champion restored by operator."
    else:
        return {"ok": False, "error": "Unknown optimizer action."}
    state["last_result_reason"] = reason
    state["last_action_date"] = datetime.now().astimezone().date().isoformat()
    _record(state, {
        "cycle": int(state.get("cycle", 0)), "status": action,
        "strategy_version": state.get("baseline_version", ""), "lever": "optimizer.control",
        "area": "Operator", "old_value": "", "new_value": action, "rationale": reason,
    })
    _save_state(state)
    return {"ok": True, "action": action, "status": optimizer_status(state)}


def _settings_diff(baseline: dict, challenger: dict, prefix: str = "") -> list[dict]:
    rows = []
    for key in sorted(set(baseline) | set(challenger)):
        path = f"{prefix}.{key}" if prefix else key
        left, right = baseline.get(key), challenger.get(key)
        if isinstance(left, dict) and isinstance(right, dict):
            rows.extend(_settings_diff(left, right, path))
        elif left != right and key != "version":
            rows.append({"setting": path, "champion": left, "challenger": right})
    return rows


def _equity_curve(frame: pd.DataFrame) -> list[float]:
    if frame.empty:
        return []
    cost = pd.to_numeric(frame.get("initial_cost"), errors="coerce").replace(0, pd.NA)
    pnl = pd.to_numeric(frame.get("realized_p_l"), errors="coerce").fillna(0)
    return [round(float(value), 3) for value in (pnl / cost * 100).fillna(0).cumsum().tolist()]


def optimizer_status(state: dict | None = None, trades: pd.DataFrame | None = None,
                     policy: dict | None = None) -> dict:
    state = state or load_optimizer_state()
    if policy is None:
        policy = json.loads((Path(__file__).resolve().parent / "optimizer_policy.json").read_text(encoding="utf-8"))
    if trades is None:
        trades = read_table("paper_trades")
    settings = load_active_settings()
    phase = state.get("phase", "collecting_baseline")
    champion_rows = pd.DataFrame()
    challenger_rows = pd.DataFrame()
    open_champion = pd.DataFrame()
    open_challenger = pd.DataFrame()
    if phase == "experiment_running":
        challenger_rows = _arm_rows(trades, str(state.get("candidate_version", "")), "challenger", True)
        champion_rows = _arm_rows(trades, str(state.get("baseline_version", "")), "champion", True)
        open_challenger = _arm_rows(trades, str(state.get("candidate_version", "")), "challenger", False)
        open_champion = _arm_rows(trades, str(state.get("baseline_version", "")), "champion", False)
        progress_rows = challenger_rows
        completed = len(progress_rows)
        target = int(policy["experiment_resolved_trades"])
        step = 3
    elif phase == "ready_for_next_experiment":
        completed, target, step = 1, 1, 4
    elif phase == "no_available_experiment":
        completed, target, step = 1, 1, 5
    else:
        completed = len(_resolved_for_version(trades, str(settings.get("version", ""))))
        progress_rows = _resolved_for_version(trades, str(settings.get("version", "")))
        target = int(policy["minimum_resolved_trades"])
        step = 1
    if phase != "experiment_running":
        progress_rows = _resolved_for_version(trades, str(state.get("baseline_version", settings.get("version", ""))))
    current_metrics = strategy_metrics(progress_rows.tail(max(target, 1)), policy)
    challenger_metrics = strategy_metrics(challenger_rows, policy) if not challenger_rows.empty else None
    matched_champion = _matched_champion_rows(champion_rows, challenger_rows)
    champion_metrics = strategy_metrics(matched_champion, policy) if not matched_champion.empty else None
    settings_diff = _settings_diff(
        state.get("baseline_settings", settings), state.get("candidate_settings", settings)
    ) if phase == "experiment_running" else []
    eta = None
    if phase == "experiment_running":
        started = pd.Timestamp(state.get("experiment_started_at"))
        started = (
            started.tz_localize("America/New_York") if started.tzinfo is None
            else started.tz_convert("America/New_York")
        )
        elapsed_days = max(1.0, (pd.Timestamp.now(tz="America/New_York") - started).total_seconds() / 86400)
        rate = len(challenger_rows) / elapsed_days
        remaining = max(0, target - len(challenger_rows))
        estimated_days = int(np.ceil(remaining / rate)) if rate > 0 else None
        eta = {
            "resolved_per_day": round(rate, 2), "remaining": remaining,
            "estimated_days": estimated_days,
            "estimated_date": (
                (datetime.now().astimezone() + timedelta(days=estimated_days)).date().isoformat()
                if estimated_days is not None else None
            ),
            "estimated_total_resolved_needed": int(np.ceil(target / max(0.01, int(state.get("challenger_allocation_pct", 20)) / 100))),
        }
    preview = None
    if challenger_metrics and champion_metrics:
        preview_policy = {**policy, "current_cycle": int(state.get("cycle", 1))}
        would_keep, preview_reason, preview_details = _promotion_evaluation(
            champion_metrics, challenger_metrics, preview_policy
        )
        preview = {"would_promote": would_keep, "reason": preview_reason, **preview_details}
    quality = _data_quality(challenger_rows) if not challenger_rows.empty else {"passed": True, "issues": [], "checked": 0}
    unresolved = []
    for arm, frame in (("Champion", open_champion), ("Challenger", open_challenger)):
        for _, row in frame.head(20).iterrows():
            unresolved.append({
                "arm": arm, "ticker": str(row.get("ticker", "")),
                "entry_price": row.get("entry_price"), "current_price": row.get("current_price"),
                "remaining_shares": row.get("remaining_shares"), "holding_days": row.get("holding_days"),
            })
    learning_summary = "Collecting enough clean evidence to begin optimization."
    if challenger_metrics and champion_metrics:
        winner_delta = challenger_metrics["average_winner_pct"] - champion_metrics["average_winner_pct"]
        win_delta = challenger_metrics["win_rate_pct"] - champion_metrics["win_rate_pct"]
        return_delta = challenger_metrics["weighted_expectancy_pct"] - champion_metrics["weighted_expectancy_pct"]
        learning_summary = (
            f"The challenger changed average winners by {winner_delta:+.2f} points, win rate by "
            f"{win_delta:+.1f} points, and weighted return by {return_delta:+.2f} points versus "
            f"{len(matched_champion)} matched champion trades."
        )
    goals = []
    goal_mapping = (
        ("Win rate", "win_rate_pct", "higher"),
        ("Average winner", "average_winner_pct", "higher"),
        ("Average return", "weighted_expectancy_pct", "higher"),
        ("Profit factor", "profit_factor", "higher"),
        ("Maximum drawdown", "max_drawdown_pct", "lower"),
        ("Average holding period", "average_holding_days", "lower"),
    )
    configured_goals = policy.get("goals", {})
    for label, metric, direction in goal_mapping:
        policy_key = "expectancy_pct" if metric == "weighted_expectancy_pct" else metric
        target_value = configured_goals.get(policy_key)
        current_value = current_metrics.get(metric)
        if target_value is None:
            continue
        comparable = current_value if current_value is not None else 999 if direction == "lower" else 0
        met = comparable >= target_value if direction == "higher" else comparable <= target_value
        goals.append({
            "label": label, "metric": metric, "current": current_value,
            "target": target_value, "direction": direction, "met": bool(met),
        })
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
        "challenger_allocation_pct": int(state.get("challenger_allocation_pct", policy["challenger_allocation_pct"])),
        "confidence_required_pct": (
            min(float(policy.get("multiple_test_confidence_cap_pct", 99)),
                float(policy["minimum_promotion_confidence_pct"]) + max(0, int(state.get("cycle", 1)) - 1))
        ),
        "stress_slippage_bps": float(policy["stress_slippage_bps"]),
        "emergency_loss_streak": int(policy["emergency_consecutive_losses"]),
        "emergency_drawdown_pct": float(policy["emergency_drawdown_pct"]),
        "current_metrics": current_metrics, "goals": goals,
        "last_rebaseline_at": state.get("last_rebaseline_at"),
        "paused": bool(state.get("paused")), "eta": eta,
        "settings_diff": settings_diff, "promotion_preview": preview,
        "data_quality": quality, "last_guard_results": state.get("last_guard_results"),
        "unresolved": unresolved,
        "open_champion": int(len(open_champion)), "open_challenger": int(len(open_challenger)),
        "champion_metrics": champion_metrics, "challenger_metrics": challenger_metrics,
        "champion_curve": _equity_curve(matched_champion),
        "challenger_curve": _equity_curve(challenger_rows),
        "learning_summary": learning_summary,
    }
