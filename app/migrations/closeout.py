"""Phase1 closeout migration: finding lineage + job count columns."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.db import utc_now

CLOSEOUT_MIGRATION_ID = "phase1_closeout_lineage_counts_v1"

FINDING_LINEAGE_COLUMNS = {
    "is_current": "INTEGER NOT NULL DEFAULT 1",
    "superseded_by": "INTEGER",
    "lineage_key": "TEXT NOT NULL DEFAULT ''",
    "stable_key": "TEXT NOT NULL DEFAULT ''",
    "superseded_at": "TEXT",
}

JOB_COUNT_COLUMNS = {
    "finding_total_count": "INTEGER NOT NULL DEFAULT 0",
    "formal_finding_count": "INTEGER NOT NULL DEFAULT 0",
    "suspected_count": "INTEGER NOT NULL DEFAULT 0",
    "hint_count": "INTEGER NOT NULL DEFAULT 0",
    "insufficient_data_count": "INTEGER NOT NULL DEFAULT 0",
    "collection_error_count": "INTEGER NOT NULL DEFAULT 0",
    "auto_excluded_count": "INTEGER NOT NULL DEFAULT 0",
    "human_dismissed_count": "INTEGER NOT NULL DEFAULT 0",
    "human_confirmed_count": "INTEGER NOT NULL DEFAULT 0",
}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> list[str]:
    existing = _columns(conn, table)
    added = []
    for name, definition in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
            added.append(name)
    return added


def migration_applied(conn: sqlite3.Connection, migration_id: str = CLOSEOUT_MIGRATION_ID) -> bool:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
        id TEXT PRIMARY KEY, applied_at TEXT NOT NULL, notes TEXT NOT NULL DEFAULT ''
    )"""
    )
    return conn.execute("SELECT 1 FROM schema_migrations WHERE id=?", (migration_id,)).fetchone() is not None


def backfill_lineage_keys(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        "SELECT id, job_id, document_id, rule_code, stable_key, is_current FROM rule_findings"
    ).fetchall()
    updated = 0
    for row in rows:
        stable = row["stable_key"] or f"{row['document_id']}:{row['rule_code']}"
        current = row["is_current"]
        if current is None:
            current = 1
        conn.execute(
            """UPDATE rule_findings SET stable_key=?, lineage_key=?, is_current=?
            WHERE id=?""",
            (stable, stable, int(current), row["id"]),
        )
        updated += 1
    return updated


def recompute_all_job_counts(conn: sqlite3.Connection) -> int:
    from app.finding_counts import compute_counts_from_rows

    job_ids = [int(r[0]) for r in conn.execute("SELECT id FROM scan_jobs").fetchall()]
    for job_id in job_ids:
        rows = conn.execute(
            "SELECT * FROM rule_findings WHERE job_id=? AND COALESCE(is_current,1)=1",
            (job_id,),
        ).fetchall()
        counts = compute_counts_from_rows([dict(r) for r in rows])
        conn.execute(
            """UPDATE scan_jobs SET
                finding_count=?,
                finding_total_count=?,
                formal_finding_count=?,
                suspected_count=?,
                hint_count=?,
                insufficient_data_count=?,
                collection_error_count=?,
                auto_excluded_count=?,
                human_dismissed_count=?,
                human_confirmed_count=?
            WHERE id=?""",
            (
                counts["finding_count"],
                counts["finding_total_count"],
                counts["formal_finding_count"],
                counts["suspected_count"],
                counts["hint_count"],
                counts["insufficient_data_count"],
                counts["collection_error_count"],
                counts["auto_excluded_count"],
                counts["human_dismissed_count"],
                counts["human_confirmed_count"],
                job_id,
            ),
        )
    return len(job_ids)


def run_closeout_migration(conn: sqlite3.Connection, *, force: bool = False) -> dict[str, Any]:
    already = migration_applied(conn)
    added_finding = _add_columns(conn, "rule_findings", FINDING_LINEAGE_COLUMNS)
    added_job = _add_columns(conn, "scan_jobs", JOB_COUNT_COLUMNS)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_rule_findings_current ON rule_findings(job_id, is_current)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_rule_findings_stable ON rule_findings(job_id, document_id, rule_code, is_current)"
    )
    lineage_updated = 0
    jobs_updated = 0
    if not already or force or added_finding or added_job:
        lineage_updated = backfill_lineage_keys(conn)
        jobs_updated = recompute_all_job_counts(conn)
    conn.execute(
        """INSERT OR REPLACE INTO schema_migrations(id, applied_at, notes) VALUES(?,?,?)""",
        (
            CLOSEOUT_MIGRATION_ID,
            utc_now(),
            json.dumps(
                {
                    "already": already,
                    "added_finding_columns": added_finding,
                    "added_job_columns": added_job,
                    "lineage_updated": lineage_updated,
                    "jobs_updated": jobs_updated,
                },
                ensure_ascii=False,
            ),
        ),
    )
    return {
        "migration_id": CLOSEOUT_MIGRATION_ID,
        "already_applied": already,
        "added_finding_columns": added_finding,
        "added_job_columns": added_job,
        "lineage_rows_touched": lineage_updated,
        "jobs_recomputed": jobs_updated,
    }
