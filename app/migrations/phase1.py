"""Idempotent phase-1 schema migration and data backfill."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.findings_model import (
    HUMAN_UNREVIEWED,
    map_legacy_status_to_human,
    map_legacy_status_to_machine,
)
from app.db import utc_now

PHASE1_MIGRATION_ID = "phase1_auth_findings_org_v1"

PHASE1_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    id TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS roles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_roles (
    user_id INTEGER NOT NULL,
    role_id INTEGER NOT NULL,
    assigned_at TEXT NOT NULL,
    assigned_by TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(user_id, role_id),
    FOREIGN KEY(user_id) REFERENCES users(id),
    FOREIGN KEY(role_id) REFERENCES roles(id)
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    csrf_token TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    ip TEXT NOT NULL DEFAULT '',
    user_agent TEXT NOT NULL DEFAULT '',
    revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0, 1)),
    FOREIGN KEY(user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS operation_audits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_user_id INTEGER,
    actor_username TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL DEFAULT '',
    resource_id TEXT NOT NULL DEFAULT '',
    before_json TEXT NOT NULL DEFAULT '{}',
    after_json TEXT NOT NULL DEFAULT '{}',
    detail_json TEXT NOT NULL DEFAULT '{}',
    ip TEXT NOT NULL DEFAULT '',
    user_agent TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rule_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id TEXT NOT NULL,
    version TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    config_hash TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    allow_customer_output INTEGER NOT NULL DEFAULT 0 CHECK(allow_customer_output IN (0, 1)),
    UNIQUE(rule_id, version)
);

CREATE TABLE IF NOT EXISTS finding_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    finding_id INTEGER NOT NULL,
    evidence_type TEXT NOT NULL DEFAULT 'field',
    source_type TEXT NOT NULL DEFAULT 'page',
    source_url TEXT NOT NULL DEFAULT '',
    field_name TEXT NOT NULL DEFAULT '',
    raw_value TEXT NOT NULL DEFAULT '',
    normalized_value TEXT NOT NULL DEFAULT '',
    excerpt TEXT NOT NULL DEFAULT '',
    page_number INTEGER,
    dom_selector TEXT NOT NULL DEFAULT '',
    bbox TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY(finding_id) REFERENCES rule_findings(id)
);

CREATE TABLE IF NOT EXISTS finding_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    finding_id INTEGER NOT NULL,
    reviewer_id INTEGER,
    reviewer_username TEXT NOT NULL DEFAULT '',
    decision TEXT NOT NULL,
    reason_code TEXT NOT NULL DEFAULT '',
    comment TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    supersedes_review_id INTEGER,
    FOREIGN KEY(finding_id) REFERENCES rule_findings(id)
);

CREATE TABLE IF NOT EXISTS review_reason_codes (
    code TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '',
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS organizations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    standard_name TEXT NOT NULL UNIQUE,
    org_type TEXT NOT NULL DEFAULT '',
    admin_level TEXT NOT NULL DEFAULT '',
    district TEXT NOT NULL DEFAULT '',
    effective_from TEXT,
    effective_to TEXT,
    notes TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS organization_aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER NOT NULL,
    alias TEXT NOT NULL,
    alias_kind TEXT NOT NULL DEFAULT 'alias',
    effective_from TEXT,
    effective_to TEXT,
    UNIQUE(organization_id, alias),
    FOREIGN KEY(organization_id) REFERENCES organizations(id)
);

CREATE TABLE IF NOT EXISTS document_symbols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    organization_id INTEGER NOT NULL,
    doc_type TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL DEFAULT '',
    effective_from TEXT,
    effective_to TEXT,
    UNIQUE(symbol, organization_id),
    FOREIGN KEY(organization_id) REFERENCES organizations(id)
);

CREATE TABLE IF NOT EXISTS organization_relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_organization_id INTEGER NOT NULL,
    to_organization_id INTEGER NOT NULL,
    relation_type TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    UNIQUE(from_organization_id, to_organization_id, relation_type),
    FOREIGN KEY(from_organization_id) REFERENCES organizations(id),
    FOREIGN KEY(to_organization_id) REFERENCES organizations(id)
);

CREATE INDEX IF NOT EXISTS idx_finding_evidence_finding ON finding_evidence(finding_id);
CREATE INDEX IF NOT EXISTS idx_finding_reviews_finding ON finding_reviews(finding_id);
"""

FINDING_COLUMN_ADDITIONS = {
    "machine_status": "TEXT NOT NULL DEFAULT 'suspected'",
    "human_status": "TEXT NOT NULL DEFAULT 'unreviewed'",
    "rule_version": "TEXT NOT NULL DEFAULT ''",
    "confidence": "REAL NOT NULL DEFAULT 0",
    "allow_customer_output": "INTEGER NOT NULL DEFAULT 0",
    "evidence_json": "TEXT NOT NULL DEFAULT '[]'",
}

DEFAULT_REASON_CODES = [
    ("joint_issuance", "联合发文", "exclude", 10),
    ("forwarded_document", "转发文件", "exclude", 20),
    ("public_body_not_issuer", "页面公开主体不是实际发文机关", "exclude", 30),
    ("historical_org_name", "历史机构名称", "exclude", 40),
    ("platform_migration", "平台迁移", "exclude", 50),
    ("normal_duplicate", "正常重复", "exclude", 60),
    ("normal_redirect", "正常重定向", "exclude", 70),
    ("temporary_network", "临时网络错误", "exclude", 80),
    ("rule_not_applicable", "规则不适用", "exclude", 90),
    ("insufficient_data", "数据不足", "defer", 100),
    ("collection_parse_error", "采集解析错误", "defer", 110),
    ("other", "其他", "other", 200),
]

DEFAULT_RULE_VERSIONS = [
    ("META-001", "1.0.0", 1, "元数据缺失，严格确认"),
    ("DOC-001", "1.0.0", 1, "页面文号与正文不一致"),
    ("DOC-002", "1.0.0", 1, "文号年份与成文年份不一致"),
    ("DOC-003", "1.0.0", 0, "多候选文号，仅线索"),
    ("DATE-001", "1.0.0", 1, "公开超期"),
    ("DATE-002", "1.0.0", 1, "日期倒置"),
    ("AGENCY-001", "1.0.0", 0, "兼容保留：机构与代字疑似不匹配，不进入客户正式问题"),
    ("AGENCY-002", "1.0.0", 0, "兼容保留：标题主体提示"),
    ("AGENCY-003", "1.0.0", 0, "兼容保留：落款提示"),
    ("ORG-001", "1.0.0", 0, "主发文机关与机关代字不匹配（待复核）"),
    ("ORG-002", "1.0.0", 0, "公开主体与实际发文机关不同（提示）"),
    ("ORG-003", "1.0.0", 0, "联合发文机关证据不一致"),
    ("ORG-004", "1.0.0", 0, "标题主体与实际发文机关冲突"),
    ("ORG-005", "1.0.0", 0, "正文落款与实际发文机关冲突"),
]


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = _table_columns(conn, table)
    for name, definition in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def migration_applied(conn: sqlite3.Connection, migration_id: str = PHASE1_MIGRATION_ID) -> bool:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
        id TEXT PRIMARY KEY, applied_at TEXT NOT NULL, notes TEXT NOT NULL DEFAULT ''
    )"""
    )
    row = conn.execute("SELECT id FROM schema_migrations WHERE id=?", (migration_id,)).fetchone()
    return row is not None


def apply_phase1_schema(conn: sqlite3.Connection) -> dict[str, Any]:
    """Create phase-1 tables/columns. Safe to re-run."""
    before_tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    # Add finding columns first so later indexes can reference them.
    if "rule_findings" in before_tables or True:
        try:
            _add_columns(conn, "rule_findings", FINDING_COLUMN_ADDITIONS)
        except sqlite3.Error:
            pass
    conn.executescript(PHASE1_SCHEMA)
    try:
        _add_columns(conn, "rule_findings", FINDING_COLUMN_ADDITIONS)
    except sqlite3.Error:
        pass
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_rule_findings_machine ON rule_findings(machine_status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_rule_findings_human ON rule_findings(human_status)"
    )
    after_tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    return {
        "tables_before": sorted(before_tables),
        "tables_after": sorted(after_tables),
        "tables_added": sorted(after_tables - before_tables),
    }


def seed_phase1_reference_data(conn: sqlite3.Connection) -> None:
    now = utc_now()
    for code, label, category, sort_order in DEFAULT_REASON_CODES:
        conn.execute(
            """INSERT OR IGNORE INTO review_reason_codes(code, label, category, sort_order)
            VALUES(?,?,?,?)""",
            (code, label, category, sort_order),
        )
    for rule_id, version, allow, description in DEFAULT_RULE_VERSIONS:
        conn.execute(
            """INSERT OR IGNORE INTO rule_versions(
                rule_id, version, effective_from, effective_to, config_hash, description, allow_customer_output
            ) VALUES(?,?,?,?,?,?,?)""",
            (rule_id, version, now, None, "", description, allow),
        )
    for role in ("admin", "operator", "reviewer", "viewer", "auditor"):
        conn.execute(
            "INSERT OR IGNORE INTO roles(name, description, created_at) VALUES(?,?,?)",
            (role, role, now),
        )


def backfill_findings(conn: sqlite3.Connection) -> dict[str, int]:
    """Populate machine/human fields and seed evidence/reviews from legacy data. Idempotent."""
    rows = conn.execute("SELECT * FROM rule_findings").fetchall()
    updated = 0
    evidence_added = 0
    reviews_added = 0
    for row in rows:
        finding_id = int(row["id"])
        rule_code = row["rule_code"]
        legacy = row["review_status"]
        decision = conn.execute(
            "SELECT * FROM review_decisions WHERE finding_id=?", (finding_id,)
        ).fetchone()
        has_decision = decision is not None
        # Only backfill when new columns still hold defaults / empty.
        current_machine = row["machine_status"] if "machine_status" in row.keys() else ""
        current_human = row["human_status"] if "human_status" in row.keys() else ""
        current_version = row["rule_version"] if "rule_version" in row.keys() else ""

        machine = current_machine or map_legacy_status_to_machine(legacy, rule_code)
        # Force AGENCY-001 family to suspected unless dismissed via review.
        if rule_code in {"AGENCY-001", "AGENCY-002", "AGENCY-003"}:
            if has_decision and decision["decision"] == "dismissed":
                machine = "auto_excluded"
                human = "human_dismissed"
            elif has_decision and decision["decision"] == "confirmed":
                machine = "suspected"
                human = "human_confirmed"
            else:
                machine = "suspected"
                human = map_legacy_status_to_human(legacy, has_decision)
        else:
            human = current_human or map_legacy_status_to_human(legacy, has_decision)
            if has_decision and decision["decision"] == "confirmed":
                human = "human_confirmed"
            elif has_decision and decision["decision"] == "dismissed":
                human = "human_dismissed"

        version_row = conn.execute(
            """SELECT version, allow_customer_output FROM rule_versions
            WHERE rule_id=? ORDER BY id DESC LIMIT 1""",
            (rule_code,),
        ).fetchone()
        rule_version = current_version or (version_row["version"] if version_row else "1.0.0")
        allow = 0 if rule_code.startswith("AGENCY-") or rule_code.startswith("ORG-") else (
            int(version_row["allow_customer_output"]) if version_row else 0
        )
        if rule_code in {"META-001", "DOC-001", "DOC-002", "DATE-001", "DATE-002"}:
            allow = 1

        conn.execute(
            """UPDATE rule_findings SET machine_status=?, human_status=?, rule_version=?,
            allow_customer_output=?, confidence=CASE WHEN confidence=0 THEN ? ELSE confidence END
            WHERE id=?""",
            (
                machine,
                human,
                rule_version,
                allow,
                0.55 if machine == "suspected" else 0.9 if machine == "confirmed_by_rule" else 0.4,
                finding_id,
            ),
        )
        updated += 1

        existing_ev = conn.execute(
            "SELECT COUNT(*) FROM finding_evidence WHERE finding_id=?", (finding_id,)
        ).fetchone()[0]
        if existing_ev == 0:
            now = utc_now()
            snippets = [
                ("page_value", "page_value", row["page_value"] or ""),
                ("body_value", "body_value", row["body_value"] or ""),
                ("evidence", "evidence", row["evidence"] or ""),
                ("detail", "detail", row["detail"] or ""),
            ]
            for field_name, _etype, raw in snippets:
                if not raw:
                    continue
                conn.execute(
                    """INSERT INTO finding_evidence(
                        finding_id, evidence_type, source_type, field_name, raw_value,
                        normalized_value, excerpt, confidence, created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        finding_id,
                        "field",
                        "page" if field_name == "page_value" else "body",
                        field_name,
                        raw,
                        raw,
                        raw[:500],
                        0.5,
                        now,
                    ),
                )
                evidence_added += 1

        if has_decision:
            existing_rev = conn.execute(
                "SELECT COUNT(*) FROM finding_reviews WHERE finding_id=?", (finding_id,)
            ).fetchone()[0]
            if existing_rev == 0:
                decision_map = {
                    "confirmed": "human_confirmed",
                    "dismissed": "human_dismissed",
                    "pending": "unreviewed",
                }
                conn.execute(
                    """INSERT INTO finding_reviews(
                        finding_id, reviewer_id, reviewer_username, decision, reason_code,
                        comment, created_at, supersedes_review_id
                    ) VALUES(?,?,?,?,?,?,?,NULL)""",
                    (
                        finding_id,
                        None,
                        "legacy",
                        decision_map.get(decision["decision"], decision["decision"]),
                        "other",
                        decision["note"] or "",
                        decision["reviewed_at"],
                    ),
                )
                reviews_added += 1

    return {
        "findings_updated": updated,
        "evidence_rows_added": evidence_added,
        "review_rows_added": reviews_added,
    }


def mark_migration(conn: sqlite3.Connection, notes: str = "") -> None:
    conn.execute(
        """INSERT OR REPLACE INTO schema_migrations(id, applied_at, notes) VALUES(?,?,?)""",
        (PHASE1_MIGRATION_ID, utc_now(), notes),
    )


def run_phase1_migration(conn: sqlite3.Connection, *, force_backfill: bool = False) -> dict[str, Any]:
    """Full phase-1 migration. Re-runnable.

    Schema always applied. Data backfill runs if migration not marked or force_backfill.
    """
    already = migration_applied(conn)
    schema_info = apply_phase1_schema(conn)
    seed_phase1_reference_data(conn)
    # Always ensure org knowledge seed via separate module if imported later
    backfill_stats = {"findings_updated": 0, "evidence_rows_added": 0, "review_rows_added": 0}
    if not already or force_backfill:
        backfill_stats = backfill_findings(conn)
    mark_migration(conn, notes=json.dumps({"already": already, **backfill_stats}, ensure_ascii=False))
    counts = {}
    for table in (
        "users",
        "roles",
        "rule_findings",
        "finding_evidence",
        "finding_reviews",
        "rule_versions",
        "organizations",
        "review_decisions",
        "operation_audits",
    ):
        try:
            counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.Error:
            counts[table] = None
    return {
        "migration_id": PHASE1_MIGRATION_ID,
        "already_applied": already,
        "schema": schema_info,
        "backfill": backfill_stats,
        "table_counts": counts,
    }
