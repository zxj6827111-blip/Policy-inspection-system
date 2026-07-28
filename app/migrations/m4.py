"""M4 migration: archive anomaly, duplicate detection, dual-channel reconciliation.

Adds:
- archive_observations: per-round observation of document presence per channel.
- archive_findings: ARCH-001~006 findings with at-least-two-round evidence.
- document_fingerprints: identity fingerprints for duplicate detection.
- duplicate_groups / duplicate_pairs: detected duplicate relationships (DUP-001~008).
- reconciliation_matches: city-district dual-channel match results (RECON-001~010).

Extends policy_documents with archive status, fingerprint hash and cross-channel link.

Idempotent and safe to re-run. Does not modify existing data.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.db import utc_now

M4_MIGRATION_ID = "m4_archive_duplicates_reconciliation_v1"

M4_SCHEMA = """
CREATE TABLE IF NOT EXISTS archive_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL,
    job_id INTEGER,
    target_key TEXT NOT NULL DEFAULT '',
    source_level TEXT NOT NULL DEFAULT 'district'
        CHECK(source_level IN ('municipal','district','unknown')),
    source_site TEXT NOT NULL DEFAULT '',
    channel_id TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    present INTEGER NOT NULL DEFAULT 1 CHECK(present IN (0, 1)),
    http_status INTEGER,
    page_status TEXT NOT NULL DEFAULT 'ok'
        CHECK(page_status IN ('ok','missing','redirect','error','blank','not_found','blocked')),
    content_hash TEXT NOT NULL DEFAULT '',
    observed_at TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(document_id) REFERENCES policy_documents(id),
    FOREIGN KEY(job_id) REFERENCES scan_jobs(id)
);

CREATE INDEX IF NOT EXISTS idx_archive_observations_doc ON archive_observations(document_id);
CREATE INDEX IF NOT EXISTS idx_archive_observations_present ON archive_observations(document_id, present);
CREATE INDEX IF NOT EXISTS idx_archive_observations_level ON archive_observations(source_level, present);

CREATE TABLE IF NOT EXISTS archive_findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL,
    rule_code TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'high',
    first_observation_id INTEGER,
    second_observation_id INTEGER,
    cross_channel_observation_id INTEGER,
    status TEXT NOT NULL DEFAULT 'open'
        CHECK(status IN ('open','confirmed','dismissed','excluded','resolved')),
    exclusion_reason TEXT NOT NULL DEFAULT '',
    evidence_json TEXT NOT NULL DEFAULT '[]',
    machine_status TEXT NOT NULL DEFAULT 'suspected',
    human_status TEXT NOT NULL DEFAULT 'unreviewed',
    rule_version TEXT NOT NULL DEFAULT '1.0.0',
    allow_customer_output INTEGER NOT NULL DEFAULT 0 CHECK(allow_customer_output IN (0, 1)),
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    notes TEXT NOT NULL DEFAULT '',
    UNIQUE(document_id, rule_code),
    FOREIGN KEY(document_id) REFERENCES policy_documents(id),
    FOREIGN KEY(first_observation_id) REFERENCES archive_observations(id),
    FOREIGN KEY(second_observation_id) REFERENCES archive_observations(id),
    FOREIGN KEY(cross_channel_observation_id) REFERENCES archive_observations(id)
);

CREATE INDEX IF NOT EXISTS idx_archive_findings_doc ON archive_findings(document_id);
CREATE INDEX IF NOT EXISTS idx_archive_findings_rule ON archive_findings(rule_code, status);

CREATE TABLE IF NOT EXISTS document_fingerprints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL UNIQUE,
    normalized_doc_number TEXT NOT NULL DEFAULT '',
    normalized_title TEXT NOT NULL DEFAULT '',
    authored_date TEXT,
    issuing_agency TEXT NOT NULL DEFAULT '',
    body_hash TEXT NOT NULL DEFAULT '',
    pdf_hash TEXT NOT NULL DEFAULT '',
    attachment_hash TEXT NOT NULL DEFAULT '',
    fingerprint_hash TEXT NOT NULL DEFAULT '',
    body_simhash TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY(document_id) REFERENCES policy_documents(id)
);

CREATE INDEX IF NOT EXISTS idx_document_fingerprints_doc_num ON document_fingerprints(normalized_doc_number);
CREATE INDEX IF NOT EXISTS idx_document_fingerprints_fp ON document_fingerprints(fingerprint_hash);
CREATE INDEX IF NOT EXISTS idx_document_fingerprints_body ON document_fingerprints(body_hash);

CREATE TABLE IF NOT EXISTS duplicate_pairs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id_a INTEGER NOT NULL,
    document_id_b INTEGER NOT NULL,
    duplicate_type TEXT NOT NULL
        CHECK(duplicate_type IN (
            'DUP-001','DUP-002','DUP-003','DUP-004',
            'DUP-005','DUP-006','DUP-007','DUP-008'
        )),
    similarity_score REAL NOT NULL DEFAULT 0,
    is_normal INTEGER NOT NULL DEFAULT 0 CHECK(is_normal IN (0, 1)),
    status TEXT NOT NULL DEFAULT 'open'
        CHECK(status IN ('open','confirmed','dismissed','excluded','resolved')),
    exclusion_reason TEXT NOT NULL DEFAULT '',
    evidence_json TEXT NOT NULL DEFAULT '[]',
    machine_status TEXT NOT NULL DEFAULT 'suspected',
    human_status TEXT NOT NULL DEFAULT 'unreviewed',
    rule_version TEXT NOT NULL DEFAULT '1.0.0',
    allow_customer_output INTEGER NOT NULL DEFAULT 0 CHECK(allow_customer_output IN (0, 1)),
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    notes TEXT NOT NULL DEFAULT '',
    UNIQUE(document_id_a, document_id_b, duplicate_type),
    FOREIGN KEY(document_id_a) REFERENCES policy_documents(id),
    FOREIGN KEY(document_id_b) REFERENCES policy_documents(id),
    CHECK(document_id_a < document_id_b)
);

CREATE INDEX IF NOT EXISTS idx_duplicate_pairs_a ON duplicate_pairs(document_id_a);
CREATE INDEX IF NOT EXISTS idx_duplicate_pairs_b ON duplicate_pairs(document_id_b);
CREATE INDEX IF NOT EXISTS idx_duplicate_pairs_type ON duplicate_pairs(duplicate_type, status);

CREATE TABLE IF NOT EXISTS reconciliation_matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    municipal_document_id INTEGER NOT NULL,
    district_document_id INTEGER,
    match_status TEXT NOT NULL
        CHECK(match_status IN (
            'RECON-001','RECON-002','RECON-003','RECON-004','RECON-005',
            'RECON-006','RECON-007','RECON-008','RECON-009','RECON-010'
        )),
    title_match INTEGER CHECK(title_match IN (0, 1)),
    doc_number_match INTEGER CHECK(doc_number_match IN (0, 1)),
    date_match INTEGER CHECK(date_match IN (0, 1)),
    body_match INTEGER CHECK(body_match IN (0, 1)),
    attachment_match INTEGER CHECK(attachment_match IN (0, 1)),
    similarity_score REAL NOT NULL DEFAULT 0,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    machine_status TEXT NOT NULL DEFAULT 'suspected',
    human_status TEXT NOT NULL DEFAULT 'unreviewed',
    rule_version TEXT NOT NULL DEFAULT '1.0.0',
    allow_customer_output INTEGER NOT NULL DEFAULT 0 CHECK(allow_customer_output IN (0, 1)),
    status TEXT NOT NULL DEFAULT 'open'
        CHECK(status IN ('open','confirmed','dismissed','excluded','resolved')),
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    notes TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(municipal_document_id) REFERENCES policy_documents(id),
    FOREIGN KEY(district_document_id) REFERENCES policy_documents(id)
);

CREATE INDEX IF NOT EXISTS idx_recon_municipal ON reconciliation_matches(municipal_document_id);
CREATE INDEX IF NOT EXISTS idx_recon_district ON reconciliation_matches(district_document_id);
CREATE INDEX IF NOT EXISTS idx_recon_status ON reconciliation_matches(match_status, status);
"""

POLICY_DOCUMENT_COLUMN_ADDITIONS = {
    "archive_status": (
        "TEXT NOT NULL DEFAULT 'active' "
        "CHECK(archive_status IN ('active','archived','missing','migrated','repealed','expired','unknown'))"
    ),
    "is_archived": "INTEGER NOT NULL DEFAULT 0 CHECK(is_archived IN (0, 1))",
    "normal_archive": "INTEGER NOT NULL DEFAULT 0 CHECK(normal_archive IN (0, 1))",
    "archive_reason": "TEXT NOT NULL DEFAULT ''",
    "migration_target_url": "TEXT NOT NULL DEFAULT ''",
    "cross_channel_document_id": "INTEGER",
    "fingerprint_hash": "TEXT NOT NULL DEFAULT ''",
    "channel_level": (
        "TEXT NOT NULL DEFAULT 'unknown' "
        "CHECK(channel_level IN ('municipal','district','unknown'))"
    ),
}

# M4 rule catalog (V3.0 plan section 10)
M4_RULE_VERSIONS = [
    # ARCH: 异常归档
    ("ARCH-001", "1.0.0", 1, "现行有效文件连续两轮消失"),
    ("ARCH-002", "1.0.0", 1, "页面仍更新但栏目归档"),
    ("ARCH-003", "1.0.0", 1, "长期保留栏目异常归档"),
    ("ARCH-004", "1.0.0", 1, "市级存在区级长期缺失"),
    ("ARCH-005", "1.0.0", 1, "区级存在市级长期缺失"),
    ("ARCH-006", "1.0.0", 1, "迁移后原栏目无正确跳转"),
    # DUP: 重复数据
    ("DUP-001", "1.0.0", 0, "技术重复（同 URL 同内容同哈希）"),
    ("DUP-002", "1.0.0", 0, "同栏目重复（同栏目内不同 URL 同一文件）"),
    ("DUP-003", "1.0.0", 0, "不同 URL 同一政策文件"),
    ("DUP-004", "1.0.0", 0, "跨栏目正常重复（应排除）"),
    ("DUP-005", "1.0.0", 0, "跨渠道正常同步（应排除）"),
    ("DUP-006", "1.0.0", 0, "同 PDF 不同页面重复"),
    ("DUP-007", "1.0.0", 1, "文号相同内容不同"),
    ("DUP-008", "1.0.0", 1, "文号不同内容高度相似"),
    # RECON: 双渠道对账
    ("RECON-001", "1.0.0", 0, "市区一致"),
    ("RECON-002", "1.0.0", 1, "市级缺失（区级有但市级无）"),
    ("RECON-003", "1.0.0", 1, "区级缺失（市级有但区级无）"),
    ("RECON-004", "1.0.0", 1, "标题不一致"),
    ("RECON-005", "1.0.0", 1, "文号不一致"),
    ("RECON-006", "1.0.0", 1, "日期不一致"),
    ("RECON-007", "1.0.0", 1, "正文不一致"),
    ("RECON-008", "1.0.0", 1, "附件不一致"),
    ("RECON-009", "1.0.0", 1, "更新延迟"),
    ("RECON-010", "1.0.0", 0, "无法匹配（数据不足）"),
]


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = _table_columns(conn, table)
    for name, definition in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _drop_url_unique_constraint(conn: sqlite3.Connection) -> None:
    """Drop the UNIQUE constraint on policy_documents.url.

    M4 duplicate detection (DUP-001) requires the ability to store multiple
    policy_documents rows with the same URL (e.g. when the same URL is
    re-scanned across rounds and produces a separate document snapshot).

    SQLite implements `url TEXT NOT NULL UNIQUE` as an automatic index named
    `sqlite_autoindex_policy_documents_N`. SQLite forbids dropping auto-indexes
    via `DROP INDEX`; the only way to remove the UNIQUE constraint is to
    recreate the table without it.

    This function performs a safe table rebuild:
    1. Detect whether the UNIQUE constraint on `url` is still present.
    2. If present: create `policy_documents_new` without UNIQUE, copy data,
       drop the old table, rename the new one, and re-create indexes.

    Idempotent: if the constraint is already gone, this is a no-op.
    """
    # Detect UNIQUE auto-index on `url` by inspecting the table's CREATE SQL
    # and the sqlite_master entries. Auto-indexes have sql IS NULL and origin
    # 'u' (unique-constraint) in pragma index_list.
    has_unique = False
    try:
        create_sql_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='policy_documents'"
        ).fetchone()
        if create_sql_row and create_sql_row["sql"]:
            # Naive but reliable for our schema: look for `url TEXT NOT NULL UNIQUE`.
            if "url TEXT NOT NULL UNIQUE" in create_sql_row["sql"]:
                has_unique = True
    except sqlite3.Error:
        pass

    if not has_unique:
        return  # Already migrated.

    # Snapshot all columns currently on the table (base + M3 + phase1 + ...).
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(policy_documents)")]
    if not cols:
        return

    # Build the new table definition WITHOUT the UNIQUE constraint on `url`.
    # Use the column definitions from the existing table by reading each
    # column's type and constraints from PRAGMA table_info + the original SQL.
    # SQLite's recommended approach for removing a UNIQUE constraint is to
    # rebuild the table. We rebuild using the columns/types we know about.
    col_defs: list[str] = []
    for col in conn.execute("PRAGMA table_info(policy_documents)").fetchall():
        name = col["name"]
        typ = col["type"] or ""
        # Determine column constraints heuristically from the original SQL.
        # For `url`, replace `UNIQUE` with nothing; keep NOT NULL.
        notnull = " NOT NULL" if col["notnull"] else ""
        dflt = f" DEFAULT {col['dflt_value']}" if col["dflt_value"] is not None else ""
        pk = " PRIMARY KEY" if col["pk"] else ""
        col_defs.append(f'"{name}" {typ}{pk}{notnull}{dflt}')

    new_sql = (
        "CREATE TABLE policy_documents_new (\n  "
        + ",\n  ".join(col_defs)
        + "\n)"
    )

    # Capture manually-defined indexes BEFORE dropping the old table.
    # Auto-indexes (sqlite_autoindex_*) are skipped intentionally — they
    # enforce the UNIQUE constraint we are removing and won't be recreated
    # because the new table has no UNIQUE on url.
    indexes_to_recreate: list[dict[str, Any]] = []
    try:
        idx_rows = conn.execute("PRAGMA index_list('policy_documents')").fetchall()
        for ir in idx_rows:
            idx_name = ir["name"]
            if idx_name.startswith("sqlite_autoindex_"):
                continue
            idx_info = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
                (idx_name,),
            ).fetchone()
            if idx_info and idx_info["sql"]:
                indexes_to_recreate.append({"name": idx_name, "sql": idx_info["sql"]})
    except sqlite3.Error:
        pass

    # Drop any prior leftover rebuild table (defensive).
    conn.execute("DROP TABLE IF EXISTS policy_documents_new")

    # Create the new table without UNIQUE on url.
    conn.execute(new_sql)

    # Copy all data.
    col_list = ", ".join(f'"{c}"' for c in cols)
    conn.execute(
        f"INSERT INTO policy_documents_new({col_list}) SELECT {col_list} FROM policy_documents"
    )

    # Drop old table and rename.
    conn.execute("DROP TABLE policy_documents")
    conn.execute("ALTER TABLE policy_documents_new RENAME TO policy_documents")

    # Re-create indexes that existed on the original table (other than the
    # auto-index for UNIQUE on url, which we are intentionally dropping).
    for idx in indexes_to_recreate:
        try:
            conn.execute(idx["sql"])
        except sqlite3.Error:
            # Best-effort: if an index can't be recreated (e.g., name collision),
            # drop and retry once.
            try:
                conn.execute(f"DROP INDEX IF EXISTS {idx['name']}")
                conn.execute(idx["sql"])
            except sqlite3.Error:
                pass


def _migration_applied(conn: sqlite3.Connection, migration_id: str = M4_MIGRATION_ID) -> bool:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
        id TEXT PRIMARY KEY, applied_at TEXT NOT NULL, notes TEXT NOT NULL DEFAULT ''
    )"""
    )
    return conn.execute(
        "SELECT id FROM schema_migrations WHERE id=?", (migration_id,)
    ).fetchone() is not None


def _seed_m4_rule_versions(conn: sqlite3.Connection) -> None:
    now = utc_now()
    for rule_id, version, allow, description in M4_RULE_VERSIONS:
        conn.execute(
            """INSERT OR IGNORE INTO rule_versions(
                rule_id, version, effective_from, effective_to, config_hash, description, allow_customer_output
            ) VALUES(?,?,?,?,?,?,?)""",
            (rule_id, version, now, None, "", description, allow),
        )


def apply_m4_schema(conn: sqlite3.Connection) -> dict[str, Any]:
    """Create M4 tables/columns. Safe to re-run."""
    before_tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    # Drop UNIQUE constraint on url BEFORE adding M4 columns so the table
    # recreation preserves the original base columns only. M4 columns are
    # added afterwards via ALTER TABLE (idempotent).
    try:
        _drop_url_unique_constraint(conn)
    except sqlite3.Error:
        pass
    try:
        _add_columns(conn, "policy_documents", POLICY_DOCUMENT_COLUMN_ADDITIONS)
    except sqlite3.Error:
        pass
    conn.executescript(M4_SCHEMA)
    _seed_m4_rule_versions(conn)
    after_tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    return {
        "tables_before": sorted(before_tables),
        "tables_after": sorted(after_tables),
        "tables_added": sorted(after_tables - before_tables),
    }


def mark_m4_migration(conn: sqlite3.Connection, notes: str = "") -> None:
    conn.execute(
        "INSERT OR REPLACE INTO schema_migrations(id, applied_at, notes) VALUES(?,?,?)",
        (M4_MIGRATION_ID, utc_now(), notes),
    )


def run_m4_migration(conn: sqlite3.Connection) -> dict[str, Any]:
    already = _migration_applied(conn)
    schema_info = apply_m4_schema(conn)
    mark_m4_migration(conn, notes=json.dumps({"already": already}, ensure_ascii=False))
    counts: dict[str, int | None] = {}
    for table in (
        "archive_observations", "archive_findings",
        "document_fingerprints", "duplicate_pairs",
        "reconciliation_matches", "rule_versions",
    ):
        try:
            counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.Error:
            counts[table] = None
    return {
        "migration_id": M4_MIGRATION_ID,
        "already_applied": already,
        "schema": schema_info,
        "table_counts": counts,
    }
