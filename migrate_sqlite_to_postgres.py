"""One-time migration from legacy SQLite files to PostgreSQL."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import pandas as pd

from db import get_engine, init_schema
from stock_storage import normalize_for_sql, replace_table


SKIP_TABLES = {"sqlite_sequence"}


def sqlite_tables(path: Path) -> list[str]:
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
    return [name for (name,) in rows if name not in SKIP_TABLES]


def load_sqlite_table(path: Path, table: str) -> pd.DataFrame:
    with sqlite3.connect(path) as connection:
        return pd.read_sql_query(f'SELECT * FROM "{table}"', connection)


def migrate_table(path: Path, table: str, *, dry_run: bool = False) -> int:
    frame = load_sqlite_table(path, table)
    if frame.empty:
        print(f"  skip {table}: empty")
        return 0
    print(f"  migrate {table}: {len(frame)} rows")
    if dry_run:
        return len(frame)
    replace_table(table, frame)
    return len(frame)


def migrate_database(label: str, path: Path, *, dry_run: bool = False) -> int:
    if not path.exists():
        print(f"{label}: missing {path}")
        return 0
    total = 0
    print(f"{label}: {path}")
    for table in sqlite_tables(path):
        total += migrate_table(path, table, dry_run=dry_run)
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate SQLite exports into PostgreSQL")
    parser.add_argument(
        "--stock-db",
        type=Path,
        default=Path("exports/stock_app.sqlite"),
        help="Paper ledger and analytics SQLite file",
    )
    parser.add_argument(
        "--app-db",
        type=Path,
        default=Path("exports/app_server.sqlite"),
        help="Scheduler job history SQLite file",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report counts only")
    args = parser.parse_args()

    init_schema()
    total = 0
    total += migrate_database("stock database", args.stock_db, dry_run=args.dry_run)
    total += migrate_database("app database", args.app_db, dry_run=args.dry_run)

    if args.dry_run:
        print(f"Dry run complete — {total} rows would migrate")
    else:
        print(f"Migration complete — {total} rows copied to PostgreSQL")
        engine = get_engine()
        with engine.connect() as connection:
            tables = pd.read_sql_query(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public'
                ORDER BY table_name
                """,
                connection,
            )
        print("PostgreSQL tables:", ", ".join(tables["table_name"].tolist()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
