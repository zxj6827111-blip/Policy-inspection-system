"""M2 migration: batch review, dual-person review, rule effectiveness support.

Adds:
- finding_reviews.review_round (1=first, 2=second, 3=arbitration)
- review_assignments table for dual-reviewer tracking and arbitration
- rule_versions.auto_confirm_allowed and rule_versions.degraded columns
- rule_effect_snapshots table for cached per-rule effectiveness stats

Idempotent and safe to re-run. Does not modify existing review data.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.db import utc_now

M2_MIGRATION_ID = "m2_batch_dual_review_rule_effect_v1"

M2_SCHEMA = """
CREATE TABLE IF NOT EXISTS review_assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    finding_id INTEGER NOT NULL,
    job_id INTEGER NOT NULL,
    first_reviewer_id INTEGER,
    first_reviewer_username TEXT NOT NULL DEFAULT '',
    second_reviewer_id INTEGER,
    second_reviewer_username TEXT NOT NULL DEFAULT '',
    arbitration_status TEXT NOT NULL DEFAULT 'none'
        CHECK(arbitration_status IN ('none','pending','resolved','single')),
    arbitrator_id INTEGER,
    arbitrator_username TEXT NOT NULL DEFAULT '',
    arbitration_decision TEXT NOT NULL DEFAULT '',
    consistency TEXT NOT NULL DEFAULT 'pending'
        CHECK(consistency IN ('pending','agree','disagree','arbitrated')),
    assigned_at TEXT NOT NULL,
    resolved_at TEXT,
    UNIQUE(finding_id),
    FOREIGN KEY(finding_id) REFERENCES rule_findings(id),
    FOREIGN KEY(job_id) REFERENCES scan_jobs(id)
);

CREATE INDEX IF NOT EXISTS idx_review_assignments_job ON review_assignments(job_id);
CREATE INDEX IF NOT EXISTS idx_review_assignments_arbitration
    ON review_assignments(arbitration_status) WHERE arbitration_status != 'none';

CREATE TABLE IF NOT EXISTS rule_effect_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_at TEXT NOT NULL,
    job_id INTEGER,
    rule_code TEXT NOT NULL,
    rule_version TEXT NOT NULL DEFAULT '',
    trigger_count INTEGER NOT NULL DEFAULT 0,
    reviewed_count INTEGER NOT NULL DEFAULT 0,
    confirmed_count INTEGER NOT NULL DEFAULT 0,
    dismissed_count INTEGER NOT NULL DEFAULT 0,
    needs_more_count INTEGER NOT NULL DEFAULT 0,
    precision_value REAL NOT NULL DEFAULT 0,
    false_positive_rate REAL NOT NULL DEFAULT 0,
    top_dismiss_reasons_json TEXT NOT NULL DEFAULT '[]',
    auto_confirm_allowed INTEGER NOT NULL DEFAULT 0 CHECK(auto_confirm_allowed IN (0,1)),
    customer_output_allowed INTEGER NOT NULL DEFAULT 0 CHECK(customer_output_allowed IN (0,1)),
    degraded INTEGER NOT NULL DEFAULT 0 CHECK(degraded IN (0,1)),
    sample_size INTEGER NOT NULL DEFAULT 0,
    snapshot_kind TEXT NOT NULL DEFAULT 'live'
        CHECK(snapshot_kind IN ('live','sample','evaluation')),
    UNIQUE(snapshot_at, job_id, rule_code, snapshot_kind)
);

CREATE INDEX IF NOT EXISTS idx_rule_effect_rule ON rule_effect_snapshots(rule_code);
"""

FINDING_REVIEW_COLUMN_ADDITIONS = {
    "review_round": "INTEGER NOT NULL DEFAULT 1 CHECK(review_round IN (1,2,3))",
}

RULE_VERSION_COLUMN_ADDITIONS = {
    "auto_confirm_allowed": "INTEGER NOT NULL DEFAULT 0 CHECK(auto_confirm_allowed IN (0,1))",
    "degraded": "INTEGER NOT NULL DEFAULT 0 CHECK(degraded IN (0,1))",
    "degraded_reason": "TEXT NOT NULL DEFAULT ''",
    "degraded_at": "TEXT",
}

# Precision thresholds per V2.0/M3.0 plan.
AUTO_CONFIRM_PRECISION_THRESHOLD = 0.95
SUSPECTED_PRECISION_THRESHOLD = 0.80


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = _table_columns(conn, table)
    for name, definition in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _migration_applied(conn: sqlite3.Connection, migration_id: str = M2_MIGRATION_ID) -> bool:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
        id TEXT PRIMARY KEY, applied_at TEXT NOT NULL, notes TEXT NOT NULL DEFAULT ''
    )"""
    )
    return conn.execute(
        "SELECT id FROM schema_migrations WHERE id=?", (migration_id,)
    ).fetchone() is not None


def _init_auto_confirm_flags(conn: sqlite3.Connection) -> None:
    """Seed auto_confirm_allowed from STRICT_AUTO_CONFIRM_RULES allow_customer_output."""
    from app.findings_model import STRICT_AUTO_CONFIRM_RULES

    placeholders = ",".join("?" for _ in STRICT_AUTO_CONFIRM_RULES)
    rows = conn.execute(
        f"SELECT rule_id, allow_customer_output, auto_confirm_allowed FROM rule_versions "
        f"WHERE rule_id IN ({placeholders})",
        tuple(STRICT_AUTO_CONFIRM_RULES),
    ).fetchall()
    for row in rows:
        if int(row["auto_confirm_allowed"]) == 0 and int(row["allow_customer_output"]) == 1:
            conn.execute(
                "UPDATE rule_versions SET auto_confirm_allowed=1 WHERE rule_id=?",
                (row["rule_id"],),
            )


def apply_m2_schema(conn: sqlite3.Connection) -> dict[str, Any]:
    """Create M2 tables/columns. Safe to re-run."""
    before_tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    try:
        _add_columns(conn, "finding_reviews", FINDING_REVIEW_COLUMN_ADDITIONS)
    except sqlite3.Error:
        pass
    try:
        _add_columns(conn, "rule_versions", RULE_VERSION_COLUMN_ADDITIONS)
    except sqlite3.Error:
        pass
    conn.executescript(M2_SCHEMA)
    _init_auto_confirm_flags(conn)
    after_tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    return {
        "tables_before": sorted(before_tables),
        "tables_after": sorted(after_tables),
        "tables_added": sorted(after_tables - before_tables),
    }


def mark_m2_migration(conn: sqlite3.Connection, notes: str = "") -> None:
    conn.execute(
        "INSERT OR REPLACE INTO schema_migrations(id, applied_at, notes) VALUES(?,?,?)",
        (M2_MIGRATION_ID, utc_now(), notes),
    )


def run_m2_migration(conn: sqlite3.Connection) -> dict[str, Any]:
    already = _migration_applied(conn)
    schema_info = apply_m2_schema(conn)
    mark_m2_migration(conn, notes=json.dumps({"already": already}, ensure_ascii=False))
    counts: dict[str, int | None] = {}
    for table in ("review_assignments", "rule_effect_snapshots", "rule_versions", "finding_reviews"):
        try:
            counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.Error:
            counts[table] = None
    return {
        "migration_id": M2_MIGRATION_ID,
        "already_applied": already,
        "schema": schema_info,
        "table_counts": counts,
    }
