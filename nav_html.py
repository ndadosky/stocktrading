"""Shared top navigation HTML and mobile layout CSS for app pages."""

from __future__ import annotations

import json
import os
from html import escape
from pathlib import Path


def html_head_meta() -> str:
    return (
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
        '<meta name="theme-color" content="#f4f6f8">'
        '<meta name="mobile-web-app-capable" content="yes">'
        '<meta name="apple-mobile-web-app-capable" content="yes">'
        '<meta name="apple-mobile-web-app-status-bar-style" content="default">'
    )


PAGE_LAYOUT_CSS = """
main{width:100%;max-width:1280px;margin-left:auto;margin-right:auto;padding:24px 24px 64px}
main.dash-frame-main{width:100%;max-width:1280px;margin-left:auto;margin-right:auto;padding:0;height:auto}
main.dash-frame-main iframe{display:block;width:100%;height:auto;min-height:0;border:0;border-radius:0;background:transparent;overflow:hidden}
"""


MOBILE_UI_CSS = """
html{-webkit-text-size-adjust:100%;text-size-adjust:100%}
body{-webkit-tap-highlight-color:transparent;touch-action:manipulation;padding:env(safe-area-inset-top) env(safe-area-inset-right) env(safe-area-inset-bottom) env(safe-area-inset-left)}
button,.btn-run,.hdr-nav a{touch-action:manipulation}
.nav-pipeline{display:inline-flex;align-items:center;gap:4px;margin-left:4px;padding-left:7px;border-left:1px solid #e5e7eb;flex-shrink:0}
.hdr-nav a.pipeline-icon{position:relative;width:30px;height:30px;min-height:30px;padding:0;display:inline-grid;place-items:center;border-radius:8px;color:#6b7280}
.pipeline-icon svg{width:16px;height:16px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
.pipeline-icon.pipeline-success{color:#16803c;background:#f0fdf4}.pipeline-icon.pipeline-warning{color:#b45309;background:#fffbeb}.pipeline-icon.pipeline-failed{color:#c62828;background:#fef2f2}.pipeline-icon.pipeline-pending{color:#6b7280;background:#f3f4f6}
.pipeline-icon::after{content:"";position:absolute;right:3px;bottom:3px;width:5px;height:5px;border-radius:50%;background:currentColor;box-shadow:0 0 0 1.5px #fff}
.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
input,select,textarea,button{font:inherit}
textarea,input,select{font-size:16px}
.hdr-nav a .lbl-short{display:none}
.panel{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{max-width:100%}
@media(max-width:900px){
  header{flex-wrap:wrap;height:auto;min-height:52px;padding:8px 12px;gap:8px;align-items:center}
  .hdr-left h1{font-size:14px}
  .hdr-nav{order:3;width:100%;flex:1 1 100%;overflow-x:auto;-webkit-overflow-scrolling:touch;flex-wrap:nowrap;gap:4px;padding:0 0 2px;scrollbar-width:none;mask-image:linear-gradient(to right,#000 92%,transparent)}
  .hdr-nav::-webkit-scrollbar{display:none}
  .hdr-nav a{padding:10px 12px;font-size:12px;flex-shrink:0;min-height:44px;display:inline-flex;align-items:center;white-space:nowrap}
  .hdr-nav a.pipeline-icon{width:40px;height:40px;min-height:40px;padding:0;display:inline-grid}
  .hdr-nav a .lbl-full{display:none}
  .hdr-nav a .lbl-short{display:inline}
  .hdr-right{order:2;margin-left:auto;flex-shrink:0}
  main{padding:16px 12px 48px}
  .page-title{flex-direction:column;align-items:stretch;gap:12px}
  .page-title h2{font-size:20px}
  .meta{grid-template-columns:1fr 1fr}
  .grid{grid-template-columns:1fr!important}
  .cards{grid-template-columns:repeat(2,minmax(0,1fr))!important}
  button,.btn-run{min-height:44px;width:100%;max-width:100%}
  .page-title .btn-run,.page-title button{width:auto;align-self:flex-start}
  th,td{font-size:12px;padding:9px 6px}
  .panel{padding:16px}
  .kv{grid-template-columns:1fr;gap:2px}
  .kv .v{text-align:left}
  .job-row{flex-direction:column;align-items:flex-start;gap:6px}
  .codex-box textarea{min-height:120px}
  #codex-send{width:100%}
  .headline{font-size:32px!important}
  .big-pnl{font-size:56px!important}
  .ticker-row strong{font-size:40px!important}
  .trade-pnl{font-size:28px!important}
  .metric-row,.status-grid{grid-template-columns:1fr 1fr!important}
  .mix{grid-template-columns:1fr!important}
  .watch-shell{grid-template-columns:1fr!important}
  main.dash-frame-main{padding:0;height:auto;min-height:0}
  .dash-frame-main iframe{border-radius:0;min-height:0}
}
@media(max-width:480px){
  .meta{grid-template-columns:1fr}
  .cards{grid-template-columns:1fr!important}
  .badge-top{font-size:11px;padding:4px 10px}
  .hdr-right .clock-txt{display:none}
}
"""


DASHBOARD_MOBILE_CSS = """
html{-webkit-text-size-adjust:100%;text-size-adjust:100%}
body{-webkit-tap-highlight-color:transparent}
.panel{overflow-x:auto;-webkit-overflow-scrolling:touch}
@media(max-width:560px){
  main{padding:16px 12px 48px}
  header{flex-wrap:wrap;gap:12px;margin-bottom:20px}
  .hero{padding:20px 18px;gap:16px}
  .equity{font-size:34px}
  .return strong{font-size:20px}
  .indices{grid-template-columns:1fr!important}
  .cards{grid-template-columns:1fr!important}
  .review-grid{grid-template-columns:repeat(2,minmax(0,1fr))}
  th,td{font-size:11px;padding:8px 6px}
}
"""


def _pipeline_stages() -> dict:
    export_dir = Path(os.getenv("STOCK_EXPORT_DIR", Path(__file__).resolve().parent / "exports"))
    state_file = export_dir.expanduser().resolve() / "pipeline_state.json"
    try:
        return json.loads(state_file.read_text(encoding="utf-8")).get("stages", {})
    except (OSError, ValueError, TypeError):
        return {}


def pipeline_health_icons(active: str, stages: dict | None = None) -> str:
    """Render compact, accessible status links for the three trading stages."""
    stages = _pipeline_stages() if stages is None else stages
    icons = {
        "morning": "<svg viewBox='0 0 24 24' aria-hidden='true'><circle cx='12' cy='12' r='3.5'/><path d='M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4'/></svg>",
        "confirmation": "<svg viewBox='0 0 24 24' aria-hidden='true'><circle cx='12' cy='12' r='9'/><path d='m8 12 2.5 2.5L16.5 9'/></svg>",
        "report": "<svg viewBox='0 0 24 24' aria-hidden='true'><path d='M6 3h9l3 3v15H6z'/><path d='M15 3v4h4M9 12h6M9 16h6'/></svg>",
    }
    tones = {
        "SUCCESS": "success",
        "DEGRADED": "warning",
        "SKIPPED": "warning",
        "FAILED": "failed",
        "BLOCKED": "failed",
    }
    links = []
    for stage in ("morning", "confirmation", "report"):
        info = stages.get(stage, {}) if isinstance(stages, dict) else {}
        status = str(info.get("status") or "PENDING").upper()
        tone = tones.get(status, "pending")
        detail = str(info.get("message") or "No status recorded")
        updated = str(info.get("updated_at") or "")
        title = f"{stage.title()}: {status} — {detail}"
        if updated:
            title += f" · {updated}"
        active_class = " active" if active in {"/healthcheck", "/health"} else ""
        links.append(
            f'<a href="/healthcheck" id="nav-health-{stage}" '
            f'class="pipeline-icon pipeline-{tone}{active_class}" title="{escape(title, quote=True)}">'
            f'{icons[stage]}<span class="sr-only">{escape(title)}</span></a>'
        )
    return '<span class="nav-pipeline" aria-label="Pipeline health">' + "".join(links) + "</span>"


def header_nav(active: str) -> str:
    links = (
        ("/", "Dashboard", "Dash", "nav-home"),
        ("/home", "Home Controls", "Home", "nav-home-controls"),
        ("/live-infographic", "Live infographic", "Live", "nav-live"),
        ("/scanner", "Scanner", "Scan", "nav-scanner"),
        ("/jobs", "Jobs", "Jobs", "nav-jobs"),
        ("/day", "Day status", "Day", "nav-day"),
        ("/strategy-review", "Strategy review", "Review", "nav-review"),
        ("/settings", "Settings", "Settings", "nav-settings"),
    )
    parts = []
    for href, label, short, nav_id in links:
        cls = "active" if active == href else ""
        id_attr = f' id="{nav_id}"' if nav_id else ""
        parts.append(
            f'<a href="{href}" class="{cls}"{id_attr}>'
            f'<span class="lbl-full">{label}</span><span class="lbl-short">{short}</span></a>'
        )
    return "".join(parts) + pipeline_health_icons(active)


def finalize_page_html(html: str, nav_active: str | None = None) -> bytes:
    if nav_active is not None:
        html = html.replace("__HEADER_NAV__", header_nav(nav_active))
    legacy_viewports = (
        '<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
        "<meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>",
    )
    for legacy in legacy_viewports:
        html = html.replace(legacy, html_head_meta())
    html = html.replace("</style>", PAGE_LAYOUT_CSS + MOBILE_UI_CSS + "</style>", 1)
    return html.encode("utf-8")


def finalize_dashboard_html(html: str) -> str:
    legacy_viewports = (
        '<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
        "<meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>",
    )
    for legacy in legacy_viewports:
        html = html.replace(legacy, html_head_meta())
    html = html.replace("</style>", PAGE_LAYOUT_CSS + DASHBOARD_MOBILE_CSS + "</style>", 1)
    return html
