"""M3 migration: PDF text/OCR/three-way compare support.

Adds PDF-related columns to policy_documents, new pdf_extractions and
attachment_records tables, and seeds M3 rule versions (PDF-001~007, ATT-001~003).

Idempotent and safe to re-run. Does not modify existing data.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.db import utc_now

M3_MIGRATION_ID = "m3_pdf_ocr_three_way_compare_v1"

M3_SCHEMA = """
CREATE TABLE IF NOT EXISTS pdf_extractions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL,
    page_number INTEGER NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    bbox_json TEXT NOT NULL DEFAULT '[]',
    source_type TEXT NOT NULL DEFAULT 'text_layer'
        CHECK(source_type IN ('text_layer','ocr','mixed','empty')),
    confidence REAL NOT NULL DEFAULT 0,
    char_count INTEGER NOT NULL DEFAULT 0,
    extracted_at TEXT NOT NULL,
    UNIQUE(document_id, page_number),
    FOREIGN KEY(document_id) REFERENCES policy_documents(id)
);

CREATE INDEX IF NOT EXISTS idx_pdf_extractions_doc ON pdf_extractions(document_id);

CREATE TABLE IF NOT EXISTS attachment_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL,
    url TEXT NOT NULL,
    filename TEXT NOT NULL DEFAULT '',
    expected_kind TEXT NOT NULL DEFAULT '',
    detected_kind TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','ok','missing','mismatch','broken','encrypted')),
    content_hash TEXT NOT NULL DEFAULT '',
    size_bytes INTEGER NOT NULL DEFAULT 0,
    checked_at TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    UNIQUE(document_id, url),
    FOREIGN KEY(document_id) REFERENCES policy_documents(id)
);

CREATE INDEX IF NOT EXISTS idx_attachment_records_doc ON attachment_records(document_id);
"""

POLICY_DOCUMENT_COLUMN_ADDITIONS = {
    "pdf_url": "TEXT NOT NULL DEFAULT ''",
    "pdf_path": "TEXT NOT NULL DEFAULT ''",
    "pdf_hash": "TEXT NOT NULL DEFAULT ''",
    "pdf_text": "TEXT NOT NULL DEFAULT ''",
    "pdf_page_count": "INTEGER NOT NULL DEFAULT 0",
    "pdf_metadata_json": "TEXT NOT NULL DEFAULT '{}'",
    "ocr_text": "TEXT NOT NULL DEFAULT ''",
    "ocr_status": "TEXT NOT NULL DEFAULT 'pending' CHECK(ocr_status IN ('pending','running','ok','failed','skipped','not_needed'))",
    "ocr_confidence": "REAL NOT NULL DEFAULT 0",
    "pdf_extracted_at": "TEXT",
    "pdf_status": "TEXT NOT NULL DEFAULT 'pending' CHECK(pdf_status IN ('pending','ok','corrupted','encrypted','blank','failed'))",
}

# M3 rule catalog (V3.0 plan section 9.2)
M3_RULE_VERSIONS = [
    ("PDF-001", "1.0.0", 0, "PDF 损坏"),
    ("PDF-002", "1.0.0", 0, "PDF 加密无法读取"),
    ("PDF-003", "1.0.0", 0, "PDF 空白"),
    ("PDF-004", "1.0.0", 1, "页面文号与 PDF 文号不一致"),
    ("PDF-005", "1.0.0", 1, "页面标题与 PDF 标题不一致"),
    ("PDF-006", "1.0.0", 1, "页面日期与 PDF 日期不一致"),
    ("PDF-007", "1.0.0", 1, "页面机构与 PDF 机构不一致"),
    ("ATT-001", "1.0.0", 1, "附件缺失"),
    ("ATT-002", "1.0.0", 1, "附件错挂"),
    ("ATT-003", "1.0.0", 1, "附件名称与内容不匹配"),
]


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = _table_columns(conn, table)
    for name, definition in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _migration_applied(conn: sqlite3.Connection, migration_id: str = M3_MIGRATION_ID) -> bool:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
        id TEXT PRIMARY KEY, applied_at TEXT NOT NULL, notes TEXT NOT NULL DEFAULT ''
    )"""
    )
    return conn.execute(
        "SELECT id FROM schema_migrations WHERE id=?", (migration_id,)
    ).fetchone() is not None


def _seed_m3_rule_versions(conn: sqlite3.Connection) -> None:
    now = utc_now()
    for rule_id, version, allow, description in M3_RULE_VERSIONS:
        conn.execute(
            """INSERT OR IGNORE INTO rule_versions(
                rule_id, version, effective_from, effective_to, config_hash, description, allow_customer_output
            ) VALUES(?,?,?,?,?,?,?)""",
            (rule_id, version, now, None, "", description, allow),
        )


def apply_m3_schema(conn: sqlite3.Connection) -> dict[str, Any]:
    """Create M3 tables/columns. Safe to re-run."""
    before_tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    try:
        _add_columns(conn, "policy_documents", POLICY_DOCUMENT_COLUMN_ADDITIONS)
    except sqlite3.Error:
        pass
    conn.executescript(M3_SCHEMA)
    _seed_m3_rule_versions(conn)
    after_tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    return {
        "tables_before": sorted(before_tables),
        "tables_after": sorted(after_tables),
        "tables_added": sorted(after_tables - before_tables),
    }


def mark_m3_migration(conn: sqlite3.Connection, notes: str = "") -> None:
    conn.execute(
        "INSERT OR REPLACE INTO schema_migrations(id, applied_at, notes) VALUES(?,?,?)",
        (M3_MIGRATION_ID, utc_now(), notes),
    )


def run_m3_migration(conn: sqlite3.Connection) -> dict[str, Any]:
    already = _migration_applied(conn)
    schema_info = apply_m3_schema(conn)
    mark_m3_migration(conn, notes=json.dumps({"already": already}, ensure_ascii=False))
    counts: dict[str, int | None] = {}
    for table in ("pdf_extractions", "attachment_records", "policy_documents", "rule_versions"):
        try:
            counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.Error:
            counts[table] = None
    return {
        "migration_id": M3_MIGRATION_ID,
        "already_applied": already,
        "schema": schema_info,
        "table_counts": counts,
    }
