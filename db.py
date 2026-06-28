"""PostgreSQL connection helpers for the stock paper-trading app."""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator
from urllib.parse import quote_plus

import psycopg2
from psycopg2.extras import RealDictCursor
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

_ENGINE: Engine | None = None


def database_url() -> str:
    """Resolve DATABASE_URL or build one from POSTGRES_* environment variables."""
    url = os.getenv("DATABASE_URL", "").strip()
    if url:
        return url
    user = os.getenv("POSTGRES_USER", "stock")
    password = os.getenv("POSTGRES_PASSWORD", "stockpass")
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5432")
    db = os.getenv("POSTGRES_DB", "stocktrading")
    return f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{quote_plus(db)}"


def get_engine() -> Engine:
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = create_engine(database_url(), pool_pre_ping=True)
    return _ENGINE


@contextmanager
def connect(*, dict_rows: bool = True) -> Iterator[tuple[Any, Any]]:
    connection = psycopg2.connect(database_url())
    connection.autocommit = False
    cursor = connection.cursor(cursor_factory=RealDictCursor if dict_rows else None)
    try:
        yield connection, cursor
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()
        connection.close()


def table_exists(table: str) -> bool:
    with connect(dict_rows=False) as (_, cursor):
        cursor.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = %s
            )
            """,
            (table.lower(),),
        )
        return bool(cursor.fetchone()[0])


def init_schema() -> None:
    """Create fixed application tables. Dynamic analytics tables are created on first write."""
    with connect(dict_rows=False) as (_, cursor):
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS job_runs (
                id SERIAL PRIMARY KEY,
                job_name TEXT NOT NULL,
                reason TEXT NOT NULL,
                run_date TEXT NOT NULL,
                scheduled_for TEXT,
                ok INTEGER NOT NULL,
                returncode INTEGER,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                output_tail TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_job_runs_job_finished
            ON job_runs (job_name, finished_at DESC)
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_job_runs_scheduled
            ON job_runs (job_name, reason, scheduled_for)
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS account_events (
                id SERIAL PRIMARY KEY,
                event_type TEXT NOT NULL,
                amount DOUBLE PRECISION NOT NULL,
                note TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS home_api_cache (
                cache_key TEXT PRIMARY KEY,
                payload JSONB NOT NULL,
                fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS home_oauth_tokens (
                provider TEXT PRIMARY KEY,
                access_token TEXT NOT NULL,
                refresh_token TEXT NOT NULL,
                expires_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS home_oauth_states (
                provider TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                redirect_uri TEXT NOT NULL,
                expires_at TIMESTAMPTZ NOT NULL
            )
            """
        )


def wait_for_database(timeout_seconds: int = 60) -> None:
    """Block until PostgreSQL accepts connections."""
    import time

    deadline = time.time() + timeout_seconds
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            with connect(dict_rows=False) as (_, cursor):
                cursor.execute("SELECT 1")
            init_schema()
            return
        except Exception as exc:
            last_error = exc
            time.sleep(2)
    raise RuntimeError(f"PostgreSQL not ready after {timeout_seconds}s: {last_error}")
