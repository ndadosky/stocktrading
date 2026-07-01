"""Generate a guarded strategy review from local app and trading data."""

from __future__ import annotations

import json
from datetime import datetime
from html import escape
from pathlib import Path

import pandas as pd

from nav_html import finalize_page_html
from scanner_config import WATCHLIST_EXPORT_DIR, ensure_directories
from job_storage import job_health
from stock_storage import append_snapshot, read_latest_snapshot, read_table


PROJECT_DIR = Path(__file__).resolve().parent
POLICY_FILE = PROJECT_DIR / "optimizer_policy.json"
SETTINGS_FILE = PROJECT_DIR / "strategy_settings.json"


def read_csv(path: Path) -> pd.DataFrame:
    try:
        return pd.read_csv(path)
    except Exception:
        return pd.DataFrame()


def app_job_health() -> dict:
    return job_health()


def latest_file(pattern: str) -> Path | None:
    files = sorted(WATCHLIST_EXPORT_DIR.glob(pattern))
    return files[-1] if files else None


def current_data_suggestions() -> dict:
    """Return cautious, evidence-backed ideas without bypassing tuning gates."""
    policy = json.loads(POLICY_FILE.read_text(encoding="utf-8"))
    settings = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    trades = read_table("paper_trades")
    current_version = str(settings.get("version") or "unknown")
    minimum = int(policy["minimum_resolved_trades"])

    if trades.empty:
        return {
            "sample": {"resolved": 0, "minimum": minimum, "strategy_version": current_version},
            "suggestions": [{
                "confidence": "insufficient data",
                "title": "Keep the current settings",
                "evidence": "There are no resolved trades in the current strategy cohort yet.",
                "change": "Collect resolved trades before evaluating an adjustment.",
            }],
        }

    remaining = pd.to_numeric(trades.get("remaining_shares"), errors="coerce").fillna(0)
    resolved = trades[remaining.le(0)].copy()
    if "entry_strategy_version" not in resolved:
        resolved["entry_strategy_version"] = "legacy-unversioned"
    changed = resolved.get(
        "strategy_changed_mid_trade", pd.Series(False, index=resolved.index)
    ).astype(str).str.lower().isin({"1", "true", "yes"})
    cohort = resolved[
        resolved["entry_strategy_version"].astype(str).eq(current_version) & ~changed
    ].copy()
    count = len(cohort)
    confidence = "decision-ready" if count >= minimum else "early signal"

    suggestions: list[dict] = []
    if not count:
        suggestions.append({
            "confidence": "insufficient data",
            "title": "Keep the current settings",
            "evidence": f"Historical trades exist, but none are clean {current_version} trades.",
            "change": "Wait for resolved trades entered under the current strategy.",
        })
    else:
        cost = pd.to_numeric(
            cohort.get("initial_cost", pd.Series(pd.NA, index=cohort.index)), errors="coerce"
        )
        pnl = pd.to_numeric(
            cohort.get("realized_p_l", pd.Series(0, index=cohort.index)), errors="coerce"
        ).fillna(0)
        returns = (pnl / cost.replace(0, pd.NA) * 100).dropna()
        wins = pnl[pnl.gt(0)]
        losses = pnl[pnl.lt(0)]
        profit_factor = float(wins.sum() / abs(losses.sum())) if not losses.empty else None
        expectancy = float(returns.mean()) if not returns.empty else 0.0
        hit = pd.to_numeric(
            cohort.get("shares_sold_10", pd.Series(0, index=cohort.index)), errors="coerce"
        ).fillna(0).gt(0).mean() * 100

        if expectancy <= 0 or (profit_factor is not None and profit_factor < 1):
            pf_text = "∞" if profit_factor is None else f"{profit_factor:.2f}"
            suggestions.append({
                "confidence": confidence,
                "title": "Test stricter entry selection",
                "evidence": f"Current-cohort expectancy is {expectancy:+.2f}% and profit factor is {pf_text} across {count} trades.",
                "change": "Backtest a higher confirmation threshold or exclude the weakest score band; do not change the live rule yet if this is still an early signal.",
            })
        else:
            pf_text = "no gross losses yet" if profit_factor is None else f"profit factor {profit_factor:.2f}"
            suggestions.append({
                "confidence": confidence,
                "title": "Keep the core exit and risk rules for now",
                "evidence": f"Current-cohort expectancy is {expectancy:+.2f}% with {pf_text}; {hit:.1f}% reached the first target across {count} trades.",
                "change": "Continue collecting trades; use score-band evidence for the next entry-rule experiment.",
            })

        if "confirmation_band" in cohort:
            band_frame = cohort.assign(_return=(pnl / cost.replace(0, pd.NA) * 100)).groupby(
                "confirmation_band", dropna=False
            ).agg(resolved=("confirmation_band", "size"), average_return=("_return", "mean")).reset_index()
            eligible = band_frame[band_frame["resolved"].ge(3)].dropna(subset=["average_return"])
            if len(eligible) >= 2:
                best = eligible.sort_values("average_return", ascending=False).iloc[0]
                worst = eligible.sort_values("average_return").iloc[0]
                suggestions.append({
                    "confidence": confidence,
                    "title": f"Favor {best['confirmation_band']} over {worst['confirmation_band']} candidates",
                    "evidence": f"Band {best['confirmation_band']} averages {best['average_return']:+.2f}% over {int(best['resolved'])} trades versus {worst['average_return']:+.2f}% over {int(worst['resolved'])} for {worst['confirmation_band']}.",
                    "change": f"Backtest raising the minimum accepted band above {worst['confirmation_band']} or reducing allocation to that band.",
                })

        reasons = cohort.get("exit_reason", pd.Series("", index=cohort.index)).fillna("").astype(str)
        stop_rate = reasons.str.contains("STOP", case=False).mean() * 100
        if stop_rate >= 40:
            suggestions.append({
                "confidence": confidence,
                "title": "Investigate stop-outs by entry quality",
                "evidence": f"{stop_rate:.1f}% of the {count} current-cohort exits were stop-related.",
                "change": "Compare stopped trades by score, spread, and opening extension before considering a wider stop; widening the stop alone increases risk.",
            })

    return {
        "sample": {
            "resolved": count,
            "minimum": minimum,
            "remaining": max(0, minimum - count),
            "strategy_version": current_version,
            "confidence": confidence,
        },
        "suggestions": suggestions,
    }


def build_review_rows(review_date: str | None = None) -> list[dict]:
    today = review_date or datetime.now().astimezone().date().isoformat()
    policy = json.loads(POLICY_FILE.read_text(encoding="utf-8"))
    settings = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    trades = read_table("paper_trades")
    health = app_job_health()

    if trades.empty:
        resolved = pd.DataFrame()
    else:
        remaining = pd.to_numeric(trades.get("remaining_shares"), errors="coerce").fillna(0)
        resolved = trades[remaining.le(0)].copy()
    if not resolved.empty:
        resolved["success"] = pd.to_numeric(resolved.get("shares_sold_10"), errors="coerce").fillna(0).gt(0)
        resolved["cost"] = pd.to_numeric(resolved.get("initial_cost"), errors="coerce")
        resolved["p_l"] = pd.to_numeric(resolved.get("realized_p_l"), errors="coerce").fillna(0)
        resolved["return_pct"] = resolved["p_l"] / resolved["cost"] * 100

    current_version = str(settings.get("version") or "unknown")
    if not resolved.empty:
        if "entry_strategy_version" not in resolved:
            resolved["entry_strategy_version"] = "legacy-unversioned"
        if "active_strategy_version" not in resolved:
            resolved["active_strategy_version"] = resolved["entry_strategy_version"]
        if "exit_strategy_version" not in resolved:
            resolved["exit_strategy_version"] = resolved["active_strategy_version"]
        changed = (
            resolved.get("strategy_changed_mid_trade", pd.Series(False, index=resolved.index))
            .astype(str).str.lower().isin({"1", "true", "yes"})
        )
        resolved["strategy_changed_mid_trade"] = changed
        clean_current = resolved[
            resolved["entry_strategy_version"].astype(str).eq(current_version) & ~changed
        ].copy()
    else:
        clean_current = resolved

    all_resolved = len(resolved)
    total_resolved = len(clean_current)
    hit_rate = float(clean_current["success"].mean() * 100) if total_resolved else 0.0
    expectancy = float(clean_current["return_pct"].mean()) if total_resolved else 0.0
    latest50 = clean_current.tail(50)
    latest50_hit = float(latest50["success"].mean() * 100) if len(latest50) else 0.0
    holdout_count = int(total_resolved * policy["chronological_holdout_pct"] / 100) if total_resolved >= policy["minimum_resolved_trades"] else 0
    training_count = max(0, total_resolved - holdout_count)

    _, score_band = read_latest_snapshot("score_band_performance", "report_date")
    _, components = read_latest_snapshot("component_performance", "report_date")
    backtest = latest_file("backtest_*.csv")

    rows: list[dict] = []

    def add(section: str, metric: str, value, status: str, notes: str = "") -> None:
        rows.append({"review_date": today, "section": section, "metric": metric, "value": value, "status": status, "notes": notes})

    min_resolved = int(policy["minimum_resolved_trades"])
    min_holdout = int(policy["minimum_holdout_trades"])
    add("sample", "resolved_trades", total_resolved, "blocked" if total_resolved < min_resolved else "pass", f"Clean {current_version} cohort only; {all_resolved} resolved across all cohorts. Need {max(0, min_resolved - total_resolved)} more before tuning.")
    add("sample", "all_resolved_trades", all_resolved, "monitor", "Historical baseline retained across strategy versions.")
    mixed_count = int(resolved["strategy_changed_mid_trade"].sum()) if not resolved.empty else 0
    add("sample", "mixed_transition_trades", mixed_count, "monitor", "Excluded from clean before/after tuning samples.")
    add("holdout", "training_trades", training_count, "blocked" if holdout_count < min_holdout else "pass")
    add("holdout", "frozen_holdout_trades", holdout_count, "blocked" if holdout_count < min_holdout else "pass", f"Minimum holdout trades: {min_holdout}.")
    first_target = settings["risk"]["scale_out"]["first_target_gain_pct"]
    stop_loss = settings["risk"]["stop_loss_pct"]
    add("performance", "overall_success_rate", f"{hit_rate:.1f}%", "monitor", f"Clean {current_version}: reached +{first_target}% before the -{stop_loss}% stop.")
    add("performance", "latest_50_success_rate", f"{latest50_hit:.1f}%", "monitor", f"Target is {policy['target_success_rate_pct']}%.")
    add("performance", "expectancy", f"{expectancy:.2f}%", "monitor")
    add("analytics", "score_band_rows", len(score_band), "blocked" if score_band.empty else "pass")
    add("analytics", "component_rows", len(components), "blocked" if components.empty else "pass")
    if not resolved.empty:
        cohort_labels = resolved["entry_strategy_version"].fillna("legacy-unversioned").astype(str)
        cohort_labels = cohort_labels.where(
            ~resolved["strategy_changed_mid_trade"],
            cohort_labels + " → " + resolved["exit_strategy_version"].fillna(resolved["active_strategy_version"]).astype(str) + " (mixed)",
        )
        for cohort, indexes in resolved.groupby(cohort_labels, sort=True).groups.items():
            cohort_rows = resolved.loc[indexes]
            cohort_hit = float(cohort_rows["success"].mean() * 100)
            cohort_expectancy = float(cohort_rows["return_pct"].mean())
            add(
                "strategy_cohort",
                f"cohort_{cohort}",
                f"{len(cohort_rows)} trades · {cohort_hit:.1f}% hit · {cohort_expectancy:+.2f}% expectancy",
                "monitor",
                "Mixed cohorts are descriptive only and excluded from tuning gates." if "(mixed)" in cohort else "Clean entry/exit cohort.",
            )
    add("database", "app_job_runs", health["job_runs"], "pass")
    add("database", "failed_job_runs", health["failed_runs"], "blocked" if health["failed_runs"] else "pass", health["latest_failure"])
    add("risk", "controls", "unchanged", "pass", f"Stop {settings['risk']['stop_loss_pct']}%, slippage {settings['execution']['slippage_bps']} bps, heat {settings['risk']['max_portfolio_heat_pct']}%, spread max {settings['risk']['max_bid_ask_spread_pct']}%.")
    add("backtest", "latest_artifact", backtest.name if backtest else "missing", "monitor" if backtest else "blocked")
    decision_status = "blocked" if total_resolved < min_resolved or holdout_count < min_holdout else "review"
    decision = "no settings change" if decision_status == "blocked" else "eligible for walk-forward review"
    add("decision", "recommended_action", decision, decision_status, "Safeguards prevent tuning until sample and holdout gates pass." if decision_status == "blocked" else "Run walk-forward selection before editing settings.")
    return rows


def load_latest_review_rows() -> tuple[list[dict], str, str | None]:
    review_date, frame = read_latest_snapshot("strategy_reviews", "stored_review_date")
    if frame.empty:
        return [], datetime.now().astimezone().date().isoformat(), None
    return frame.to_dict("records"), review_date or datetime.now().astimezone().date().isoformat(), None


def render_strategy_review_html(rows: list[dict], review_date: str, csv_filename: str | None = None) -> str:
    decision_row = next((r for r in rows if r.get("metric") == "recommended_action"), None)
    decision_value = escape(str(decision_row["value"])) if decision_row else "No review yet"
    decision_status = escape(str(decision_row.get("status", "monitor"))) if decision_row else "monitor"

    highlight_metrics = {"resolved_trades", "frozen_holdout_trades", "latest_50_success_rate", "failed_job_runs", "recommended_action"}
    card_rows = [r for r in rows if r.get("metric") in highlight_metrics]

    def _note(text: str) -> str:
        return f"<p class='note'>{escape(str(text))}</p>" if text else ""

    cards_html = "".join(
        f"<div class='card'>"
        f"<small>{escape(str(r.get('section', '')))}</small>"
        f"<strong>{escape(str(r.get('metric', '')).replace('_', ' '))}</strong>"
        f"<span class='val {escape(str(r.get('status', 'monitor')))}'>{escape(str(r.get('value', '')))}</span>"
        f"{_note(str(r.get('notes', '')))}"
        f"</div>"
        for r in card_rows
    ) or "<div class='empty'>Run the strategy review job to populate gate metrics.</div>"

    review = pd.DataFrame(rows) if rows else pd.DataFrame(columns=["review_date", "section", "metric", "value", "status", "notes"])
    table_html = review.to_html(index=False, escape=True, border=0) if not review.empty else "<p class='empty'>No review rows yet.</p>"
    generated_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    csv_link = f"<a href='/exports/{escape(csv_filename)}'>Download CSV</a>" if csv_filename else "—"

    return f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Strategy review · {escape(review_date)}</title><style>
:root{{--bg:#f4f6f8;--panel:#fff;--text:#17202a;--muted:#687386;--line:#dce3ec;--blue:#1d4ed8;--green:#16803c;--red:#b42318}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:14px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;-webkit-font-smoothing:antialiased}}
header{{padding:0 20px;background:var(--panel);border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;gap:16px;position:sticky;top:0;z-index:3;height:52px}}
.hdr-left h1{{font-size:15px;font-weight:700;margin:0;letter-spacing:-.01em;flex-shrink:0}}
.hdr-nav{{display:flex;align-items:center;gap:2px;flex:1;padding:0 8px}}
.hdr-nav a{{padding:6px 13px;border-radius:7px;font-size:13px;font-weight:600;color:var(--muted);text-decoration:none;transition:background .15s,color .15s;white-space:nowrap}}
.hdr-nav a:hover{{background:#f1f3f5;color:var(--text)}}
.hdr-nav a.active{{background:#eff6ff;color:var(--blue)}}
.hdr-right{{font-size:12px;color:var(--muted);flex-shrink:0}}
main{{max-width:1280px;margin:0 auto;padding:28px 20px 64px}}
.page-title{{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;margin-bottom:20px;flex-wrap:wrap}}
.page-title h2{{font-size:22px;font-weight:700;letter-spacing:-.02em;margin:0 0 3px}}
.page-title .sub{{font-size:12px;color:var(--muted)}}
.btn-analyze{{border:1px solid #bfcee3;background:#f8fbff;color:#183b75;border-radius:8px;padding:10px 16px;font-weight:700;cursor:pointer;font-size:13px;white-space:nowrap}}
.btn-analyze:hover{{background:#eff6ff;border-color:#7da2da}}
.btn-analyze:disabled{{opacity:.55;cursor:not-allowed}}
.decision{{display:inline-flex;align-items:center;gap:10px;padding:11px 20px;border-radius:10px;font-size:15px;font-weight:700;margin-bottom:24px;border:1px solid}}
.decision.pass{{background:#f0fdf4;border-color:#86efac;color:var(--green)}}
.decision.blocked{{background:#fef2f2;border-color:#fca5a5;color:var(--red)}}
.decision.review,.decision.monitor{{background:#eff6ff;border-color:#bfdbfe;color:var(--blue)}}
.decision-dot{{width:8px;height:8px;border-radius:50%;background:currentColor;flex-shrink:0}}
.cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:20px}}
.card{{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px 18px;box-shadow:0 1px 6px rgba(17,24,39,.03)}}
.card small{{display:block;color:var(--muted);font-size:10px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;margin-bottom:6px}}
.card strong{{display:block;font-size:13px;font-weight:600;color:var(--text);margin-bottom:10px}}
.card .val{{display:block;font-size:20px;font-weight:700;letter-spacing:-.02em}}
.card .val.pass{{color:var(--green)}}.card .val.blocked{{color:var(--red)}}.card .val.monitor,.card .val.review{{color:var(--blue)}}
.note{{color:var(--muted);font-size:12px;margin:8px 0 0;line-height:1.4}}
.panel{{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:20px;overflow:auto;box-shadow:0 1px 6px rgba(17,24,39,.03);margin-bottom:16px}}
.panel-head{{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:14px;gap:12px;flex-wrap:wrap}}
.panel-head h3{{font-size:14px;font-weight:700;margin:0}}
.panel-head .sub{{font-size:12px;color:var(--muted)}}
.bc-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px}}
.bc-card{{border:1px solid var(--line);border-radius:10px;padding:14px;background:#fbfcfe}}
.bc-card small{{display:block;color:var(--muted);font-size:10px;font-weight:700;text-transform:uppercase;margin-bottom:6px}}
.bc-card strong{{display:block;font-size:18px;font-weight:700}}
.pos{{color:var(--green)}}.neg{{color:var(--red)}}
.bc-table{{width:100%;border-collapse:collapse;font-size:13px}}
.bc-table th,.bc-table td{{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}}
.bc-table th{{color:var(--muted);font-size:11px;text-transform:uppercase}}
.suggestions{{display:grid;gap:10px;margin-bottom:18px}}
.suggestion{{border:1px solid var(--line);border-radius:10px;padding:14px;background:#fbfcfe}}
.suggestion-head{{display:flex;justify-content:space-between;gap:12px;align-items:center;margin-bottom:7px}}
.suggestion-head strong{{font-size:14px}}.confidence{{font-size:10px;font-weight:700;text-transform:uppercase;color:var(--blue);background:#eff6ff;padding:4px 7px;border-radius:999px}}
.suggestion p{{margin:4px 0;color:var(--muted);line-height:1.45}}.suggestion .change{{color:var(--text)}}
.empty{{color:var(--muted);padding:12px 0}}
.hidden{{display:none}}
table{{border-collapse:collapse;width:100%;font-size:13px;white-space:nowrap}}
th,td{{padding:9px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}}
th{{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.05em;font-weight:600}}
tbody tr:last-child td{{border-bottom:0}}
a{{color:var(--blue)}}
@media(max-width:900px){{.cards,.bc-grid{{grid-template-columns:repeat(2,1fr)}}}}
@media(max-width:560px){{.cards,.bc-grid{{grid-template-columns:1fr}}main{{padding:16px}}}}
</style></head><body>
<header>
  <div class='hdr-left'><h1>Stock Strategy App</h1></div>
  <nav class='hdr-nav'>__HEADER_NAV__</nav>
  <div class='hdr-right'>Generated {escape(generated_at)}</div>
</header>
<main>
  <div class='page-title'>
    <div>
      <h2>Strategy review · {escape(review_date)}</h2>
      <div class='sub'>Improvement and tuning gates — run at 10:30 each market day</div>
    </div>
    <button class='btn-analyze' id='analyze-now' type='button'>Analyze now</button>
  </div>
  <div class='decision {decision_status}'><span class='decision-dot'></span>{decision_value}</div>
  <section class='cards'>{cards_html}</section>
  <div id='best-case-panel' class='panel hidden'>
    <div class='panel-head'><h3>Current-data analysis</h3><span class='sub' id='bc-assumption'></span></div>
    <div class='suggestions' id='strategy-suggestions'></div>
    <div class='bc-grid' id='bc-summary'></div>
    <table class='bc-table'><thead><tr><th>Ticker</th><th>Status</th><th>Current P/L</th><th>Best-case P/L</th><th>Uplift</th><th>Notes</th></tr></thead><tbody id='bc-positions'></tbody></table>
  </div>
  <div class='panel'>
    <div class='panel-head'><h3>Full review data</h3><span class='sub'>{csv_link}</span></div>
    {table_html}
  </div>
</main>
<script>
function money(v){{const n=Number(v);return Number.isFinite(n)?(n<0?'-':'')+'$'+Math.abs(n).toLocaleString(undefined,{{maximumFractionDigits:0}}):'—'}}
function pct(v){{const n=Number(v);return Number.isFinite(n)?n.toFixed(2)+'%':'—'}}
function plClass(v){{return Number(v)>=0?'pos':'neg'}}
document.getElementById('analyze-now').onclick=async()=>{{
  const btn=document.getElementById('analyze-now');
  const panel=document.getElementById('best-case-panel');
  btn.disabled=true;btn.textContent='Analyzing…';
  try{{
    const res=await fetch('/api/strategy/best-case');
    const data=await res.json();
    if(!data.ok){{alert(data.error||'Analysis failed');return;}}
    panel.classList.remove('hidden');
    const sample=data.sample||{{}};
    document.getElementById('bc-assumption').textContent=sample.resolved<sample.minimum
      ? `${{sample.resolved||0}} of ${{sample.minimum||60}} current-strategy trades — suggestions are provisional`
      : `${{sample.resolved}} current-strategy trades — tuning threshold met`;
    document.getElementById('strategy-suggestions').innerHTML=(data.suggestions||[]).map(s=>`<div class="suggestion">
      <div class="suggestion-head"><strong>${{s.title}}</strong><span class="confidence">${{s.confidence}}</span></div>
      <p><b>Evidence:</b> ${{s.evidence}}</p><p class="change"><b>Possible change:</b> ${{s.change}}</p></div>`).join('');
    const c=data.current,b=data.best_case;
    document.getElementById('bc-summary').innerHTML=
      `<div class="bc-card"><small>Current equity</small><strong>${{money(c.equity)}}</strong></div>`+
      `<div class="bc-card"><small>Best-case equity</small><strong class="${{plClass(b.uplift)}}">${{money(b.equity)}}</strong></div>`+
      `<div class="bc-card"><small>Current total P/L</small><strong class="${{plClass(c.total_p_l)}}">${{money(c.total_p_l)}}</strong></div>`+
      `<div class="bc-card"><small>Best-case uplift</small><strong class="${{plClass(b.uplift)}}">${{money(b.uplift)}}</strong></div>`;
    document.getElementById('bc-positions').innerHTML=(data.positions||[]).map(p=>`<tr>
      <td><b>${{p.ticker}}</b></td><td>${{p.status}}</td>
      <td class="${{plClass(p.current_p_l)}}">${{money(p.current_p_l)}}</td>
      <td class="${{plClass(p.best_case_p_l)}}">${{money(p.best_case_p_l)}}</td>
      <td class="${{plClass(p.uplift)}}">${{money(p.uplift)}}</td>
      <td>${{p.notes||''}}</td></tr>`).join('')||'<tr><td colspan="6">No positions in ledger.</td></tr>';
  }}catch(err){{alert('Request failed: '+err);}}
  btn.disabled=false;btn.textContent='Analyze now';
}};
</script>
</body></html>"""


def main() -> int:
    ensure_directories()
    today = datetime.now().astimezone().date().isoformat()
    rows = build_review_rows(today)
    review = pd.DataFrame(rows)
    append_snapshot("strategy_reviews", review, "stored_review_date", today)
    html_path = WATCHLIST_EXPORT_DIR / f"strategy_review_{today}.html"
    html_path.write_bytes(finalize_page_html(render_strategy_review_html(rows, today, None), "/strategy-review"))
    print(f"Saved strategy review to PostgreSQL and {html_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
