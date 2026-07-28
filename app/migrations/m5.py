"""M5 migration: reading-linkage, policy interpretation, media checks, rectification.

Implements V3.0 plan section 11:
- 阅办联动深度检查 (READ-001~005)
- 图片/视频/附件可用性 (MEDIA-001~006)
- 多类型政策解读与解读对象匹配 (INTERP-001~008)
- 整改、复查、销号、超期、复发 (RECT-001~007)

Adds 7 new tables and extends policy_documents with reading/interpretation
columns. Idempotent and safe to re-run. Does not modify existing data.

Production boundary: this migration only runs on local/dev/test databases.
The production database is never touched in M5 (M0~M6 production boundary).
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.db import utc_now

M5_MIGRATION_ID = "m5_reading_interp_rectification_v1"

M5_SCHEMA = """
CREATE TABLE IF NOT EXISTS reading_linkage_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL,
    job_id INTEGER,
    button_label TEXT NOT NULL DEFAULT '',
    button_selector TEXT NOT NULL DEFAULT '',
    button_present INTEGER NOT NULL DEFAULT 1 CHECK(button_present IN (0, 1)),
    clickable INTEGER CHECK(clickable IN (0, 1)),
    target_url TEXT NOT NULL DEFAULT '',
    http_status INTEGER,
    page_status TEXT NOT NULL DEFAULT 'unknown'
        CHECK(page_status IN (
            'ok','missing','redirect','error','blank','not_found',
            'blocked','captcha','rate_limited','unknown','no_data'
        )),
    page_title TEXT NOT NULL DEFAULT '',
    page_body_length INTEGER NOT NULL DEFAULT 0,
    has_no_data_placeholder INTEGER NOT NULL DEFAULT 0 CHECK(has_no_data_placeholder IN (0, 1)),
    rule_code TEXT NOT NULL DEFAULT '',
    machine_status TEXT NOT NULL DEFAULT 'suspected',
    human_status TEXT NOT NULL DEFAULT 'unreviewed',
    rule_version TEXT NOT NULL DEFAULT '1.0.0',
    allow_customer_output INTEGER NOT NULL DEFAULT 0 CHECK(allow_customer_output IN (0, 1)),
    evidence_json TEXT NOT NULL DEFAULT '[]',
    checked_at TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(document_id) REFERENCES policy_documents(id),
    FOREIGN KEY(job_id) REFERENCES scan_jobs(id)
);

CREATE INDEX IF NOT EXISTS idx_reading_linkage_doc ON reading_linkage_checks(document_id);
CREATE INDEX IF NOT EXISTS idx_reading_linkage_status ON reading_linkage_checks(page_status, machine_status);

CREATE TABLE IF NOT EXISTS media_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL,
    job_id INTEGER,
    media_type TEXT NOT NULL
        CHECK(media_type IN ('image','video','attachment')),
    media_url TEXT NOT NULL,
    source_area TEXT NOT NULL DEFAULT '',
    http_status INTEGER,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN (
            'pending','ok','missing','broken','mismatch','blocked',
            'rate_limited','encrypted','empty','playback_failed'
        )),
    content_type TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL DEFAULT '',
    size_bytes INTEGER NOT NULL DEFAULT 0,
    detected_kind TEXT NOT NULL DEFAULT '',
    duration_seconds REAL,
    rule_code TEXT NOT NULL DEFAULT '',
    machine_status TEXT NOT NULL DEFAULT 'suspected',
    human_status TEXT NOT NULL DEFAULT 'unreviewed',
    rule_version TEXT NOT NULL DEFAULT '1.0.0',
    allow_customer_output INTEGER NOT NULL DEFAULT 0 CHECK(allow_customer_output IN (0, 1)),
    evidence_json TEXT NOT NULL DEFAULT '[]',
    checked_at TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(document_id) REFERENCES policy_documents(id),
    FOREIGN KEY(job_id) REFERENCES scan_jobs(id)
);

CREATE INDEX IF NOT EXISTS idx_media_checks_doc ON media_checks(document_id);
CREATE INDEX IF NOT EXISTS idx_media_checks_type_status ON media_checks(media_type, status);

CREATE TABLE IF NOT EXISTS policy_interpretations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL,
    interpretation_type TEXT NOT NULL
        CHECK(interpretation_type IN (
            'image_text','one_image','video','qa','leader','expert',
            'press_conference','media','unknown'
        )),
    interpretation_url TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    published_at TEXT,
    target_document_id INTEGER,
    target_doc_number TEXT NOT NULL DEFAULT '',
    target_title TEXT NOT NULL DEFAULT '',
    matched_target INTEGER CHECK(matched_target IN (0, 1)),
    match_confidence REAL NOT NULL DEFAULT 0,
    match_evidence_json TEXT NOT NULL DEFAULT '[]',
    FOREIGN KEY(document_id) REFERENCES policy_documents(id),
    FOREIGN KEY(target_document_id) REFERENCES policy_documents(id)
);

CREATE INDEX IF NOT EXISTS idx_interpretations_doc ON policy_interpretations(document_id);
CREATE INDEX IF NOT EXISTS idx_interpretations_target ON policy_interpretations(target_document_id);
CREATE INDEX IF NOT EXISTS idx_interpretations_type ON policy_interpretations(interpretation_type);

CREATE TABLE IF NOT EXISTS interpretation_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    interpretation_id INTEGER NOT NULL,
    job_id INTEGER,
    link_status TEXT NOT NULL DEFAULT 'unknown'
        CHECK(link_status IN (
            'ok','missing','redirect','error','blank','not_found',
            'blocked','captcha','rate_limited','unknown'
        )),
    http_status INTEGER,
    has_image INTEGER CHECK(has_image IN (0, 1)),
    image_status TEXT NOT NULL DEFAULT 'unknown'
        CHECK(image_status IN ('ok','missing','broken','blocked','unknown')),
    has_video INTEGER CHECK(has_video IN (0, 1)),
    video_status TEXT NOT NULL DEFAULT 'unknown'
        CHECK(video_status IN ('ok','missing','broken','playback_failed','blocked','unknown')),
    page_body_length INTEGER NOT NULL DEFAULT 0,
    is_empty_page INTEGER NOT NULL DEFAULT 0 CHECK(is_empty_page IN (0, 1)),
    published_at_present INTEGER CHECK(published_at_present IN (0, 1)),
    rule_code TEXT NOT NULL DEFAULT '',
    machine_status TEXT NOT NULL DEFAULT 'suspected',
    human_status TEXT NOT NULL DEFAULT 'unreviewed',
    rule_version TEXT NOT NULL DEFAULT '1.0.0',
    allow_customer_output INTEGER NOT NULL DEFAULT 0 CHECK(allow_customer_output IN (0, 1)),
    evidence_json TEXT NOT NULL DEFAULT '[]',
    checked_at TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(interpretation_id) REFERENCES policy_interpretations(id),
    FOREIGN KEY(job_id) REFERENCES scan_jobs(id)
);

CREATE INDEX IF NOT EXISTS idx_interp_checks_interp ON interpretation_checks(interpretation_id);
CREATE INDEX IF NOT EXISTS idx_interp_checks_rule ON interpretation_checks(rule_code, machine_status);

CREATE TABLE IF NOT EXISTS rectification_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    finding_id INTEGER,
    document_id INTEGER NOT NULL,
    rule_code TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    responsible_unit TEXT NOT NULL DEFAULT '',
    responsible_person TEXT NOT NULL DEFAULT '',
    deadline TEXT,
    notified_at TEXT,
    feedback_uploaded_at TEXT,
    feedback_notes TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'created'
        CHECK(status IN (
            'created','assigned','notified','feedback_uploaded',
            'rechecking','passed','signoff','failed','reopened'
        )),
    priority TEXT NOT NULL DEFAULT 'normal'
        CHECK(priority IN ('low','normal','high','urgent')),
    recurrence_count INTEGER NOT NULL DEFAULT 0,
    parent_task_id INTEGER,
    before_evidence_json TEXT NOT NULL DEFAULT '[]',
    after_evidence_json TEXT NOT NULL DEFAULT '[]',
    machine_status TEXT NOT NULL DEFAULT 'suspected',
    human_status TEXT NOT NULL DEFAULT 'unreviewed',
    rule_version TEXT NOT NULL DEFAULT '1.0.0',
    allow_customer_output INTEGER NOT NULL DEFAULT 0 CHECK(allow_customer_output IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    closed_at TEXT,
    notes TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(finding_id) REFERENCES rule_findings(id),
    FOREIGN KEY(document_id) REFERENCES policy_documents(id),
    FOREIGN KEY(parent_task_id) REFERENCES rectification_tasks(id)
);

CREATE INDEX IF NOT EXISTS idx_rect_tasks_doc ON rectification_tasks(document_id);
CREATE INDEX IF NOT EXISTS idx_rect_tasks_status ON rectification_tasks(status, deadline);
CREATE INDEX IF NOT EXISTS idx_rect_tasks_finding ON rectification_tasks(finding_id);
CREATE INDEX IF NOT EXISTS idx_rect_tasks_parent ON rectification_tasks(parent_task_id);

CREATE TABLE IF NOT EXISTS rectification_status_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    from_status TEXT NOT NULL DEFAULT '',
    to_status TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    evidence_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES rectification_tasks(id)
);

CREATE INDEX IF NOT EXISTS idx_rect_history_task ON rectification_status_history(task_id, created_at);
"""

POLICY_DOCUMENT_COLUMN_ADDITIONS = {
    "reading_check_status": (
        "TEXT NOT NULL DEFAULT 'pending' "
        "CHECK(reading_check_status IN ('pending','ok','has_issues','failed','not_applicable'))"
    ),
    "interpretation_count": "INTEGER NOT NULL DEFAULT 0",
    "has_interpretation_mismatch": "INTEGER NOT NULL DEFAULT 0 CHECK(has_interpretation_mismatch IN (0, 1))",
    "rectification_status": (
        "TEXT NOT NULL DEFAULT 'none' "
        "CHECK(rectification_status IN ('none','pending','in_progress','passed','failed','reopened'))"
    ),
}

# M5 rule catalog (V3.0 plan section 11)
M5_RULE_VERSIONS = [
    # READ: 阅办联动
    ("READ-001", "1.0.0", 1, "阅办按钮不存在"),
    ("READ-002", "1.0.0", 1, "阅办按钮存在但不可点击"),
    ("READ-003", "1.0.0", 1, "阅办跳转失败（HTTP 错误或页面失效）"),
    ("READ-004", "1.0.0", 1, "阅办跳转成功但页面空白或错误"),
    ("READ-005", "1.0.0", 1, "阅办页面返回 200 但显示暂无数据"),
    # MEDIA: 图片/视频
    ("MEDIA-001", "1.0.0", 1, "图片缺失"),
    ("MEDIA-002", "1.0.0", 1, "图片破损"),
    ("MEDIA-003", "1.0.0", 1, "视频缺失"),
    ("MEDIA-004", "1.0.0", 1, "视频失效或无法播放"),
    ("MEDIA-005", "1.0.0", 1, "页面资源整体为空"),
    ("MEDIA-006", "1.0.0", 1, "图片/视频被 403/429 拦截"),
    # INTERP: 政策解读
    ("INTERP-001", "1.0.0", 1, "解读链接失效"),
    ("INTERP-002", "1.0.0", 1, "解读对象错误（与原政策不匹配）"),
    ("INTERP-003", "1.0.0", 1, "解读发布时间缺失"),
    ("INTERP-004", "1.0.0", 1, "解读链接错挂"),
    ("INTERP-005", "1.0.0", 1, "解读图片失效"),
    ("INTERP-006", "1.0.0", 1, "解读视频失效"),
    ("INTERP-007", "1.0.0", 1, "解读页面空白"),
    ("INTERP-008", "1.0.0", 1, "解读类型缺失或无法识别"),
    # RECT: 整改闭环
    ("RECT-001", "1.0.0", 1, "整改任务未指定责任单位"),
    ("RECT-002", "1.0.0", 1, "整改任务超期"),
    ("RECT-003", "1.0.0", 1, "整改后证据缺失"),
    ("RECT-004", "1.0.0", 1, "整改未通过但未重新下发"),
    ("RECT-005", "1.0.0", 1, "销号后复发"),
    ("RECT-006", "1.0.0", 1, "整改状态历史缺失"),
    ("RECT-007", "1.0.0", 1, "整改期限缺失"),
]


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = _table_columns(conn, table)
    for name, definition in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _migration_applied(conn: sqlite3.Connection, migration_id: str = M5_MIGRATION_ID) -> bool:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
        id TEXT PRIMARY KEY, applied_at TEXT NOT NULL, notes TEXT NOT NULL DEFAULT ''
    )"""
    )
    return conn.execute(
        "SELECT id FROM schema_migrations WHERE id=?", (migration_id,)
    ).fetchone() is not None


def _seed_m5_rule_versions(conn: sqlite3.Connection) -> None:
    now = utc_now()
    for rule_id, version, allow, description in M5_RULE_VERSIONS:
        conn.execute(
            """INSERT OR IGNORE INTO rule_versions(
                rule_id, version, effective_from, effective_to, config_hash, description, allow_customer_output
            ) VALUES(?,?,?,?,?,?,?)""",
            (rule_id, version, now, None, "", description, allow),
        )


def apply_m5_schema(conn: sqlite3.Connection) -> dict[str, Any]:
    """Create M5 tables/columns. Safe to re-run."""
    before_tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    try:
        _add_columns(conn, "policy_documents", POLICY_DOCUMENT_COLUMN_ADDITIONS)
    except sqlite3.Error:
        pass
    conn.executescript(M5_SCHEMA)
    _seed_m5_rule_versions(conn)
    after_tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    return {
        "tables_before": sorted(before_tables),
        "tables_after": sorted(after_tables),
        "tables_added": sorted(after_tables - before_tables),
    }


def mark_m5_migration(conn: sqlite3.Connection, notes: str = "") -> None:
    conn.execute(
        "INSERT OR REPLACE INTO schema_migrations(id, applied_at, notes) VALUES(?,?,?)",
        (M5_MIGRATION_ID, utc_now(), notes),
    )


def run_m5_migration(conn: sqlite3.Connection) -> dict[str, Any]:
    already = _migration_applied(conn)
    schema_info = apply_m5_schema(conn)
    mark_m5_migration(conn, notes=json.dumps({"already": already}, ensure_ascii=False))
    counts: dict[str, int | None] = {}
    for table in (
        "reading_linkage_checks", "media_checks",
        "policy_interpretations", "interpretation_checks",
        "rectification_tasks", "rectification_status_history",
        "rule_versions",
    ):
        try:
            counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.Error:
            counts[table] = None
    return {
        "migration_id": M5_MIGRATION_ID,
        "already_applied": already,
        "schema": schema_info,
        "table_counts": counts,
    }
