from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from app.config import DB_PATH, ensure_directories


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS scan_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    districts_json TEXT NOT NULL,
    mode TEXT NOT NULL CHECK(mode IN ('full', 'incremental')),
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    current_url TEXT NOT NULL DEFAULT '',
    current_district TEXT NOT NULL DEFAULT '',
    current_page INTEGER NOT NULL DEFAULT 1,
    current_item_index INTEGER NOT NULL DEFAULT -1,
    processed_count INTEGER NOT NULL DEFAULT 0,
    finding_count INTEGER NOT NULL DEFAULT 0,
    estimated_total INTEGER NOT NULL DEFAULT 0,
    max_documents INTEGER NOT NULL DEFAULT 0,
    batch_examined_count INTEGER NOT NULL DEFAULT 0,
    examined_count INTEGER NOT NULL DEFAULT 0,
    skipped_count INTEGER NOT NULL DEFAULT 0,
    current_district_index INTEGER NOT NULL DEFAULT 0,
    total_by_district_json TEXT NOT NULL DEFAULT '{}',
    examined_by_district_json TEXT NOT NULL DEFAULT '{}',
    coverage_status TEXT NOT NULL DEFAULT 'not_started',
    completion_kind TEXT NOT NULL DEFAULT '',
    access_count INTEGER NOT NULL DEFAULT 0,
    retry_count INTEGER NOT NULL DEFAULT 0,
    rest_count INTEGER NOT NULL DEFAULT 0,
    resumed_count INTEGER NOT NULL DEFAULT 0,
    pause_reason TEXT NOT NULL DEFAULT '',
    cooldown_until TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    safety_json TEXT NOT NULL DEFAULT '{}',
    baseline_job_id INTEGER,
    source_signature TEXT NOT NULL DEFAULT '[]',
    FOREIGN KEY(baseline_job_id) REFERENCES scan_jobs(id)
);

CREATE TABLE IF NOT EXISTS policy_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    district TEXT NOT NULL,
    source_id TEXT NOT NULL DEFAULT '',
    source_site TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    issuing_agency TEXT NOT NULL DEFAULT '',
    page_document_number TEXT NOT NULL DEFAULT '',
    published_date TEXT,
    authored_date TEXT,
    body_text TEXT NOT NULL DEFAULT '',
    topic_category TEXT NOT NULL DEFAULT '',
    disclosure_attribute TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL,
    last_detail_checked_at TEXT,
    first_seen_job_id INTEGER NOT NULL,
    last_seen_job_id INTEGER NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    FOREIGN KEY(first_seen_job_id) REFERENCES scan_jobs(id),
    FOREIGN KEY(last_seen_job_id) REFERENCES scan_jobs(id)
);

CREATE TABLE IF NOT EXISTS document_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    document_id INTEGER NOT NULL,
    captured_at TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    UNIQUE(job_id, document_id),
    FOREIGN KEY(job_id) REFERENCES scan_jobs(id),
    FOREIGN KEY(document_id) REFERENCES policy_documents(id)
);

CREATE TABLE IF NOT EXISTS rule_findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    document_id INTEGER NOT NULL,
    rule_code TEXT NOT NULL,
    category TEXT NOT NULL,
    severity TEXT NOT NULL,
    review_status TEXT NOT NULL,
    detail TEXT NOT NULL,
    page_value TEXT NOT NULL DEFAULT '',
    body_value TEXT NOT NULL DEFAULT '',
    evidence TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY(job_id) REFERENCES scan_jobs(id),
    FOREIGN KEY(document_id) REFERENCES policy_documents(id)
);

CREATE TABLE IF NOT EXISTS link_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    document_id INTEGER NOT NULL,
    link_kind TEXT NOT NULL,
    original_url TEXT NOT NULL,
    final_url TEXT NOT NULL DEFAULT '',
    status_code INTEGER,
    result TEXT NOT NULL,
    error_type TEXT NOT NULL DEFAULT '',
    page_title TEXT NOT NULL DEFAULT '',
    checked_at TEXT NOT NULL,
    redirect_chain_json TEXT NOT NULL DEFAULT '[]',
    source_area TEXT NOT NULL DEFAULT '',
    link_text TEXT NOT NULL DEFAULT '',
    source_page_url TEXT NOT NULL DEFAULT '',
    interaction_type TEXT NOT NULL DEFAULT '',
    visible INTEGER NOT NULL DEFAULT 1 CHECK(visible IN (0, 1)),
    review_status TEXT NOT NULL DEFAULT 'confirmed',
    evidence TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(job_id) REFERENCES scan_jobs(id),
    FOREIGN KEY(document_id) REFERENCES policy_documents(id)
);

CREATE TABLE IF NOT EXISTS scan_job_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    document_id INTEGER NOT NULL,
    district TEXT NOT NULL,
    page_number INTEGER NOT NULL,
    item_index INTEGER NOT NULL,
    action TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    recorded_at TEXT NOT NULL,
    UNIQUE(job_id, document_id),
    FOREIGN KEY(job_id) REFERENCES scan_jobs(id),
    FOREIGN KEY(document_id) REFERENCES policy_documents(id)
);

CREATE TABLE IF NOT EXISTS scan_item_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    target_key TEXT NOT NULL,
    source_label TEXT NOT NULL,
    channel_id TEXT NOT NULL DEFAULT '',
    page_number INTEGER NOT NULL,
    item_index INTEGER NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    listed_date TEXT,
    detail_status TEXT NOT NULL,
    header_detected INTEGER NOT NULL DEFAULT 0 CHECK(header_detected IN (0, 1)),
    source_id TEXT NOT NULL DEFAULT '',
    topic_category TEXT NOT NULL DEFAULT '',
    disclosure_attribute TEXT NOT NULL DEFAULT '',
    authored_date TEXT,
    page_document_number TEXT NOT NULL DEFAULT '',
    published_date TEXT,
    issuing_agency TEXT NOT NULL DEFAULT '',
    missing_fields_json TEXT NOT NULL DEFAULT '[]',
    document_id INTEGER,
    reused_document_id INTEGER,
    baseline_job_id INTEGER,
    link_check_version INTEGER NOT NULL DEFAULT 1,
    reason TEXT NOT NULL DEFAULT '',
    checked_at TEXT NOT NULL,
    UNIQUE(job_id, target_key, page_number, item_index),
    FOREIGN KEY(job_id) REFERENCES scan_jobs(id),
    FOREIGN KEY(document_id) REFERENCES policy_documents(id),
    FOREIGN KEY(reused_document_id) REFERENCES policy_documents(id),
    FOREIGN KEY(baseline_job_id) REFERENCES scan_jobs(id)
);

CREATE INDEX IF NOT EXISTS idx_scan_item_results_baseline_lookup
ON scan_item_results(job_id, target_key, url);

CREATE TABLE IF NOT EXISTS job_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    message TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY(job_id) REFERENCES scan_jobs(id)
);

CREATE TABLE IF NOT EXISTS scan_exceptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    district TEXT NOT NULL,
    page_number INTEGER NOT NULL,
    item_index INTEGER NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    category TEXT NOT NULL,
    first_error TEXT NOT NULL,
    last_error TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','resolved','review_required')),
    retry_count INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL,
    last_checked_at TEXT,
    resolved_at TEXT,
    UNIQUE(job_id, url),
    FOREIGN KEY(job_id) REFERENCES scan_jobs(id)
);

CREATE TABLE IF NOT EXISTS review_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    finding_id INTEGER NOT NULL UNIQUE,
    decision TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    reviewed_at TEXT NOT NULL,
    FOREIGN KEY(finding_id) REFERENCES rule_findings(id)
);

CREATE TABLE IF NOT EXISTS agency_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    district TEXT NOT NULL,
    agency_name TEXT NOT NULL,
    aliases_json TEXT NOT NULL,
    document_prefixes_json TEXT NOT NULL,
    UNIQUE(district, agency_name)
);

CREATE TABLE IF NOT EXISTS holiday_calendar (
    calendar_date TEXT PRIMARY KEY,
    is_workday INTEGER NOT NULL CHECK(is_workday IN (0, 1)),
    source_note TEXT NOT NULL DEFAULT ''
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: Path = DB_PATH):
        self.path = path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def initialize(self) -> None:
        ensure_directories()
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)
            self._seed_agencies(conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """为已有本地数据库执行只增不删的兼容迁移。"""
        additions = {
            "scan_jobs": {
                "max_documents": "INTEGER NOT NULL DEFAULT 0",
                "batch_examined_count": "INTEGER NOT NULL DEFAULT 0",
                "examined_count": "INTEGER NOT NULL DEFAULT 0",
                "skipped_count": "INTEGER NOT NULL DEFAULT 0",
                "current_district_index": "INTEGER NOT NULL DEFAULT 0",
                "total_by_district_json": "TEXT NOT NULL DEFAULT '{}'",
                "examined_by_district_json": "TEXT NOT NULL DEFAULT '{}'",
                "coverage_status": "TEXT NOT NULL DEFAULT 'not_started'",
                "completion_kind": "TEXT NOT NULL DEFAULT ''",
                "access_count": "INTEGER NOT NULL DEFAULT 0",
                "retry_count": "INTEGER NOT NULL DEFAULT 0",
                "rest_count": "INTEGER NOT NULL DEFAULT 0",
                "resumed_count": "INTEGER NOT NULL DEFAULT 0",
                "baseline_job_id": "INTEGER",
                "source_signature": "TEXT NOT NULL DEFAULT '[]'",
            },
            "policy_documents": {
                "last_detail_checked_at": "TEXT",
                "source_site": "TEXT NOT NULL DEFAULT ''",
                "topic_category": "TEXT NOT NULL DEFAULT ''",
                "disclosure_attribute": "TEXT NOT NULL DEFAULT ''",
            },
            "scan_item_results": {
                "link_check_version": "INTEGER NOT NULL DEFAULT 0",
            },
            "link_checks": {
                "redirect_chain_json": "TEXT NOT NULL DEFAULT '[]'",
                "source_area": "TEXT NOT NULL DEFAULT ''",
                "link_text": "TEXT NOT NULL DEFAULT ''",
                "source_page_url": "TEXT NOT NULL DEFAULT ''",
                "interaction_type": "TEXT NOT NULL DEFAULT ''",
                "visible": "INTEGER NOT NULL DEFAULT 1",
                "review_status": "TEXT NOT NULL DEFAULT 'confirmed'",
                "evidence": "TEXT NOT NULL DEFAULT ''",
            },
        }
        for table, columns in additions.items():
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            for name, definition in columns.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
        # 旧版接口抓取结果没有页面位置证据，不得进入客户导出或增量基线复用。
        conn.execute(
            """UPDATE link_checks SET visible=0,review_status='legacy_hidden'
            WHERE TRIM(COALESCE(source_area,''))='' OR TRIM(COALESCE(source_page_url,''))=''"""
        )
        # 旧版本曾把限量试扫误标为 completed；只修正没有新完成标记的遗留任务。
        conn.execute(
            """UPDATE scan_jobs SET status='partial',coverage_status='partial',completion_kind='legacy_incomplete',
            pause_reason=CASE WHEN pause_reason='' THEN '旧版本限量试扫未覆盖全部记录，可手动恢复' ELSE pause_reason END,
            max_documents=CASE WHEN max_documents=0 THEN MAX(processed_count,1) ELSE max_documents END,
            examined_count=MAX(examined_count,processed_count),batch_examined_count=MAX(batch_examined_count,processed_count)
            WHERE status='completed' AND completion_kind='' AND coverage_status='not_started'
              AND estimated_total>processed_count"""
        )
        conn.execute(
            """UPDATE scan_jobs SET examined_count=processed_count
            WHERE examined_count=0 AND processed_count>0"""
        )
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS scan_job_documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER NOT NULL, document_id INTEGER NOT NULL,
                district TEXT NOT NULL, page_number INTEGER NOT NULL, item_index INTEGER NOT NULL,
                action TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', recorded_at TEXT NOT NULL,
                UNIQUE(job_id, document_id), FOREIGN KEY(job_id) REFERENCES scan_jobs(id),
                FOREIGN KEY(document_id) REFERENCES policy_documents(id));
            CREATE TABLE IF NOT EXISTS job_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER NOT NULL, event_type TEXT NOT NULL,
                message TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '', details_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL, FOREIGN KEY(job_id) REFERENCES scan_jobs(id));
            DROP INDEX IF EXISTS uq_link_check_per_job_url;
            CREATE UNIQUE INDEX IF NOT EXISTS uq_link_check_per_occurrence
                ON link_checks(job_id, document_id, original_url, source_area, source_page_url, link_text);
            CREATE TABLE IF NOT EXISTS scan_item_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER NOT NULL, target_key TEXT NOT NULL,
                source_label TEXT NOT NULL, channel_id TEXT NOT NULL DEFAULT '', page_number INTEGER NOT NULL,
                item_index INTEGER NOT NULL, title TEXT NOT NULL, url TEXT NOT NULL, listed_date TEXT,
                detail_status TEXT NOT NULL, header_detected INTEGER NOT NULL DEFAULT 0,
                source_id TEXT NOT NULL DEFAULT '', topic_category TEXT NOT NULL DEFAULT '',
                disclosure_attribute TEXT NOT NULL DEFAULT '', authored_date TEXT, page_document_number TEXT NOT NULL DEFAULT '',
                published_date TEXT, issuing_agency TEXT NOT NULL DEFAULT '', missing_fields_json TEXT NOT NULL DEFAULT '[]',
                document_id INTEGER, reused_document_id INTEGER, baseline_job_id INTEGER, reason TEXT NOT NULL DEFAULT '',
                link_check_version INTEGER NOT NULL DEFAULT 1, checked_at TEXT NOT NULL,
                UNIQUE(job_id,target_key,page_number,item_index));
            CREATE INDEX IF NOT EXISTS idx_scan_item_results_baseline_lookup
                ON scan_item_results(job_id,target_key,url);
            """
        )

    @staticmethod
    def _seed_agencies(conn: sqlite3.Connection) -> None:
        rows = [
            ("普陀区", "上海市普陀区人民政府", ["普陀区人民政府", "普陀区政府"], ["普府", "普府办"]),
            ("崇明区", "上海市崇明区人民政府", ["崇明区人民政府", "崇明区政府"], ["沪崇府", "崇府", "沪崇府办"]),
        ]
        conn.executemany(
            "INSERT OR IGNORE INTO agency_rules(district,agency_name,aliases_json,document_prefixes_json) VALUES(?,?,?,?)",
            [(d, a, json.dumps(x, ensure_ascii=False), json.dumps(p, ensure_ascii=False)) for d, a, x, p in rows],
        )

    def create_job(
        self, districts: list[str], mode: str, safety: dict[str, Any], max_documents: int = 0,
        baseline_job_id: int | None = None,
    ) -> int:
        now = utc_now()
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO scan_jobs(districts_json,mode,status,created_at,safety_json,max_documents,
                coverage_status,baseline_job_id,source_signature) VALUES(?,?,?,?,?,?,?,?,?)""",
                (json.dumps(districts, ensure_ascii=False), mode, "pending", now,
                 json.dumps(safety, ensure_ascii=False), max_documents, "not_started", baseline_job_id,
                 json.dumps(sorted(districts), ensure_ascii=False)),
            )
            return int(cur.lastrowid)

    def update_job(self, job_id: int, **values: Any) -> None:
        if not values:
            return
        allowed = {
            "status", "started_at", "finished_at", "current_url", "current_district",
            "current_page", "processed_count", "finding_count", "estimated_total",
            "current_item_index",
            "max_documents", "batch_examined_count", "examined_count", "skipped_count",
            "current_district_index", "total_by_district_json", "examined_by_district_json",
            "coverage_status", "completion_kind", "access_count", "retry_count", "rest_count",
            "resumed_count", "pause_reason", "cooldown_until", "last_error",
            "baseline_job_id", "source_signature",
        }
        values = {k: v for k, v in values.items() if k in allowed}
        sql = ",".join(f"{key}=?" for key in values)
        with self.connect() as conn:
            conn.execute(f"UPDATE scan_jobs SET {sql} WHERE id=?", [*values.values(), job_id])

    def add_job_event(self, job_id: int, event_type: str, message: str = "", url: str = "", **details: Any) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO job_events(job_id,event_type,message,url,details_json,created_at) VALUES(?,?,?,?,?,?)",
                (job_id, event_type, message, url, json.dumps(details, ensure_ascii=False), utc_now()),
            )

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM scan_jobs WHERE id=?", (job_id,)).fetchone()
            return dict(row) if row else None

    def list_jobs(self, limit: int = 30) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM scan_jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [dict(row) for row in rows]

    def active_job(self) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM scan_jobs WHERE status IN ('pending','running','cooling') ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return dict(row) if row else None

    def eligible_baselines(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM scan_jobs WHERE mode='full' AND status='completed'
                AND coverage_status='complete'
                AND EXISTS(SELECT 1 FROM scan_item_results s WHERE s.job_id=scan_jobs.id)
                ORDER BY id DESC"""
            ).fetchall()
        return [dict(row) for row in rows]

    def agency_rules(self, district: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM agency_rules WHERE district=?", (district,)).fetchall()
            return [dict(row) for row in rows]
