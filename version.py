"""Application version metadata for dashboards and deploy verification."""

from __future__ import annotations

import os
from pathlib import Path

_VERSION_FILE = Path(__file__).resolve().parent / "VERSION"


def release_version() -> str:
    env = os.getenv("APP_VERSION", "").strip()
    if env:
        return env
    if _VERSION_FILE.exists():
        return _VERSION_FILE.read_text(encoding="utf-8").strip()
    return "dev"


def git_commit() -> str:
    return os.getenv("APP_GIT_COMMIT", "local").strip() or "local"


def built_at() -> str:
    return os.getenv("APP_BUILT_AT", "").strip()


def version_label() -> str:
    """Compact label shown in the dashboard header."""
    parts = [f"v{release_version()}", git_commit()]
    stamp = built_at()
    if stamp:
        parts.append(stamp)
    return " · ".join(parts)
