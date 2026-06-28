"""Shared top navigation HTML for app pages."""


def header_nav(active: str) -> str:
    links = (
        ("/", "Dashboard", "nav-home"),
        ("/live-infographic", "Live infographic", "nav-live"),
        ("/scanner", "Scanner", "nav-scanner"),
        ("/jobs", "Jobs", "nav-jobs"),
        ("/day", "Day status", "nav-day"),
        ("/strategy-review", "Strategy review", "nav-review"),
        ("/healthcheck", "Health check", "nav-health"),
    )
    parts = []
    for href, label, nav_id in links:
        cls = "active" if active == href else ""
        id_attr = f' id="{nav_id}"' if nav_id else ""
        parts.append(f'<a href="{href}" class="{cls}"{id_attr}>{label}</a>')
    return "".join(parts)
