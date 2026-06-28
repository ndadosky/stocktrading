"""PostgreSQL persistence for scheduler job history."""

from __future__ import annotations

from typing import Optional

from db import connect, init_schema


def row_to_run(row: dict | None) -> Optional[dict]:
    if row is None:
        return None
    return {
        "id": row["id"],
        "ok": bool(row["ok"]),
        "returncode": row["returncode"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "reason": row["reason"],
        "run_date": row["run_date"],
        "scheduled_for": row["scheduled_for"],
        "output_tail": row["output_tail"],
    }


def record_job_run(name: str, result: dict) -> dict:
    init_schema()
    with connect() as (_, cursor):
        cursor.execute(
            """
            INSERT INTO job_runs (
                job_name, reason, run_date, scheduled_for, ok, returncode,
                started_at, finished_at, output_tail
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                name,
                result["reason"],
                result["run_date"],
                result.get("scheduled_for"),
                1 if result["ok"] else 0,
                result["returncode"],
                result["started_at"],
                result["finished_at"],
                result["output_tail"],
            ),
        )
        row = cursor.fetchone()
        result["id"] = row["id"]
    return result


def last_run(name: str) -> Optional[dict]:
    init_schema()
    with connect() as (_, cursor):
        cursor.execute(
            """
            SELECT * FROM job_runs
            WHERE job_name = %s
            ORDER BY finished_at DESC, id DESC
            LIMIT 1
            """,
            (name,),
        )
        row = cursor.fetchone()
    return row_to_run(row)


def last_runs(job_names: list[str]) -> dict[str, dict]:
    return {name: run for name in job_names if (run := last_run(name)) is not None}


def job_history(limit: int = 50) -> list[dict]:
    init_schema()
    with connect() as (_, cursor):
        cursor.execute(
            """
            SELECT * FROM job_runs
            ORDER BY finished_at DESC, id DESC
            LIMIT %s
            """,
            (limit,),
        )
        rows = cursor.fetchall()
    return [dict(row_to_run(row) or {}, job_name=row["job_name"]) for row in rows]


def job_rows_for_date(target_date: str) -> list[dict]:
    init_schema()
    with connect() as (_, cursor):
        cursor.execute(
            """
            SELECT * FROM job_runs
            WHERE run_date = %s OR scheduled_for = %s OR substr(finished_at, 1, 10) = %s
            ORDER BY finished_at DESC, id DESC
            LIMIT 100
            """,
            (target_date, target_date, target_date),
        )
        rows = cursor.fetchall()
    return [dict(row) for row in rows]


def scheduled_already_ran(job_name: str, run_date: str) -> bool:
    init_schema()
    with connect() as (_, cursor):
        cursor.execute(
            """
            SELECT id FROM job_runs
            WHERE job_name = %s AND reason = 'scheduled' AND scheduled_for = %s
            LIMIT 1
            """,
            (job_name, run_date),
        )
        row = cursor.fetchone()
    return row is not None


def job_health() -> dict:
    init_schema()
    try:
        with connect() as (_, cursor):
            cursor.execute("SELECT count(*) AS total FROM job_runs")
            total = cursor.fetchone()["total"]
            cursor.execute("SELECT count(*) AS failed FROM job_runs WHERE ok = 0")
            failed = cursor.fetchone()["failed"]
            cursor.execute(
                """
                SELECT job_name, finished_at, output_tail
                FROM job_runs
                WHERE ok = 0
                ORDER BY finished_at DESC, id DESC
                LIMIT 1
                """
            )
            latest = cursor.fetchone()
        latest_failure = (
            ""
            if latest is None
            else f"{latest['job_name']} at {latest['finished_at']}: {str(latest['output_tail'])[:240]}"
        )
        return {"job_runs": int(total), "failed_runs": int(failed), "latest_failure": latest_failure}
    except Exception as exc:
        return {"job_runs": 0, "failed_runs": 0, "latest_failure": f"DB read failed: {exc}"}
