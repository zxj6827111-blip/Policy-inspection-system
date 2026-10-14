"""整改销号台账迁移：记录增量复查后「问题已消失」与「人工驳回但再次命中」。

rule_findings 的 is_current/superseded_by 只在同一个 job 内生效，跨任务没有
「这个问题上一轮报过、这一轮复查后已经没了」的表达位置。缺了它，增量报表里
「问题不见了」既可能是真整改、也可能是根本没复查，两者无法区分。
"""

from __future__ import annotations

import json
import sqlite3

from app.db import utc_now

RECTIFY_LEDGER_MIGRATION_ID = "rectify_ledger_transitions_v1"

TRANSITIONS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS finding_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    baseline_job_id INTEGER,
    document_id INTEGER NOT NULL,
    stable_key TEXT NOT NULL,
    rule_code TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '',
    severity TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL CHECK(kind IN ('closed','reconfirmed_after_dismissal')),
    detail TEXT NOT NULL DEFAULT '',
    before_value TEXT NOT NULL DEFAULT '',
    after_value TEXT NOT NULL DEFAULT '',
    source_site TEXT NOT NULL DEFAULT '',
    district TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    document_url TEXT NOT NULL DEFAULT '',
    recorded_at TEXT NOT NULL,
    UNIQUE(job_id, stable_key, kind),
    FOREIGN KEY(job_id) REFERENCES scan_jobs(id),
    FOREIGN KEY(document_id) REFERENCES policy_documents(id)
)
"""

TRANSITIONS_INDEX_SQL = "CREATE INDEX IF NOT EXISTS idx_finding_transitions_job_kind ON finding_transitions(job_id, kind)"


def migration_applied(conn: sqlite3.Connection) -> bool:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
        id TEXT PRIMARY KEY, applied_at TEXT NOT NULL, notes TEXT NOT NULL DEFAULT ''
    )"""
    )
    return conn.execute(
        "SELECT 1 FROM schema_migrations WHERE id=?", (RECTIFY_LEDGER_MIGRATION_ID,)
    ).fetchone() is not None


def run_rectify_ledger_migration(conn: sqlite3.Connection) -> dict:
    conn.row_factory = sqlite3.Row
    already = migration_applied(conn)
    if not already:
        conn.execute(TRANSITIONS_TABLE_SQL)
        conn.execute(TRANSITIONS_INDEX_SQL)
        conn.execute(
            "INSERT INTO schema_migrations(id, applied_at, notes) VALUES(?,?,?)",
            (RECTIFY_LEDGER_MIGRATION_ID, utc_now(), json.dumps({"table": "finding_transitions"})),
        )
    return {"migration_id": RECTIFY_LEDGER_MIGRATION_ID, "already_applied": already}
