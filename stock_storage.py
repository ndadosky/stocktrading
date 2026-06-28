"""PostgreSQL persistence for live app data. HTML exports are optional views only."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import pandas as pd
from sqlalchemy import text

from db import get_engine, init_schema, table_exists
from scanner_config import STARTING_CAPITAL, ensure_directories


def normalize_for_sql(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    return frame.copy().where(pd.notna(frame), None)


def table_count(table: str) -> int:
    if not table_exists(table):
        return 0
    engine = get_engine()
    with engine.connect() as connection:
        value = connection.execute(text(f'SELECT count(*) FROM "{table.lower()}"')).scalar()
    return int(value or 0)


def read_table(table: str, columns: list[str] | None = None) -> pd.DataFrame:
    if not table_exists(table):
        return pd.DataFrame(columns=columns or [])
    engine = get_engine()
    frame = pd.read_sql_query(f'SELECT * FROM "{table.lower()}"', engine)
    if columns is None:
        return frame
    for column in columns:
        if column not in frame.columns:
            frame[column] = pd.NA
    return frame[columns]


def replace_table(table: str, frame: pd.DataFrame) -> None:
    init_schema()
    normalize_for_sql(frame).to_sql(
        table.lower(),
        get_engine(),
        if_exists="replace",
        index=False,
        method="multi",
    )


def read_snapshot(table: str, date_column: str, date_value: str) -> pd.DataFrame:
    if not table_exists(table):
        return pd.DataFrame()
    engine = get_engine()
    frame = pd.read_sql_query(
        f'SELECT * FROM "{table.lower()}" WHERE "{date_column}" = %(date_value)s',
        engine,
        params={"date_value": date_value},
    )
    return frame.drop(columns=[date_column], errors="ignore")


def snapshot_count(table: str, date_column: str, date_value: str) -> int:
    if not table_exists(table):
        return 0
    engine = get_engine()
    with engine.connect() as connection:
        value = connection.execute(
            text(f'SELECT count(*) FROM "{table.lower()}" WHERE "{date_column}" = :date_value'),
            {"date_value": date_value},
        ).scalar()
    return int(value or 0)


def read_latest_snapshot(table: str, date_column: str) -> tuple[Optional[str], pd.DataFrame]:
    if not table_exists(table):
        return None, pd.DataFrame()
    engine = get_engine()
    latest = pd.read_sql_query(
        f'SELECT MAX("{date_column}") AS snapshot_date FROM "{table.lower()}"',
        engine,
    ).iloc[0]["snapshot_date"]
    if latest is None or pd.isna(latest):
        return None, pd.DataFrame()
    date_value = str(latest)
    return date_value, read_snapshot(table, date_column, date_value)


def list_snapshot_dates(table: str, date_column: str) -> list[str]:
    if not table_exists(table):
        return []
    engine = get_engine()
    frame = pd.read_sql_query(
        f'SELECT DISTINCT "{date_column}" AS snapshot_date FROM "{table.lower()}" ORDER BY 1',
        engine,
    )
    if frame.empty:
        return []
    return [str(value) for value in frame["snapshot_date"].tolist()]


def ensure_account_events() -> None:
    init_schema()


def add_bankroll_deposit(amount: float = 25000.0, note: str = "Manual bankroll injection") -> dict:
    ensure_account_events()
    created_at = pd.Timestamp.now(tz="America/New_York").isoformat(timespec="seconds")
    engine = get_engine()
    with engine.begin() as connection:
        row = connection.execute(
            text(
                """
                INSERT INTO account_events (event_type, amount, note, created_at)
                VALUES ('deposit', :amount, :note, :created_at)
                RETURNING id
                """
            ),
            {"amount": float(amount), "note": note, "created_at": created_at},
        ).fetchone()
    return {
        "id": int(row[0]),
        "event_type": "deposit",
        "amount": float(amount),
        "note": note,
        "created_at": created_at,
    }


def total_bankroll_deposits() -> float:
    ensure_account_events()
    engine = get_engine()
    with engine.connect() as connection:
        value = connection.execute(
            text("SELECT COALESCE(SUM(amount), 0) FROM account_events WHERE event_type = 'deposit'")
        ).scalar()
    return float(value or 0.0)


def bankroll_base() -> float:
    return float(STARTING_CAPITAL) + total_bankroll_deposits()


def append_snapshot(table: str, frame: pd.DataFrame, date_column: str, date_value: str) -> None:
    init_schema()
    snapshot = frame.copy()
    snapshot[date_column] = date_value
    engine = get_engine()
    with engine.begin() as connection:
        if table_exists(table):
            connection.execute(
                text(f'DELETE FROM "{table.lower()}" WHERE "{date_column}" = :date_value'),
                {"date_value": date_value},
            )
        normalize_for_sql(snapshot).to_sql(
            table.lower(),
            connection,
            if_exists="append",
            index=False,
            method="multi",
        )


def load_paper_trades(columns: list[str], csv_fallback: Path | None = None) -> pd.DataFrame:
    del csv_fallback
    ensure_directories()
    init_schema()
    return read_table("paper_trades", columns)


def save_paper_trades(frame: pd.DataFrame, columns: list[str], csv_export: Path | None = None) -> None:
    del csv_export
    export = frame.copy()
    for column in columns:
        if column not in export.columns:
            export[column] = pd.NA
    replace_table("paper_trades", export[columns])


def query_rows(
    table: str,
    clause: str = "",
    params: tuple = (),
    limit: int = 200,
) -> list[dict]:
    if not table_exists(table):
        return []
    clause_sql = clause.replace("?", "%s") if clause else ""
    sql = f'SELECT * FROM "{table.lower()}" {clause_sql} LIMIT %s'
    from db import connect

    with connect() as (_, cursor):
        cursor.execute(sql, (*params, limit))
        return [dict(row) for row in cursor.fetchall()]
