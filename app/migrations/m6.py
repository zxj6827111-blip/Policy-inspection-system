"""M6 迁移：性能索引、OCR 队列、监控基线。

依据 V3.0 §十二（M6）：
- 重建 policy_documents.url 索引（M4 删除 UNIQUE 后遗留的性能 bug）；
- 补齐热点查询路径缺失的索引；
- 新增 ocr_queue 表支持 OCR 串行/并行/重试队列；
- 新增 system_settings 表支撑「系统设置」导航项；
- 新增 db_health_snapshots 表支撑 /metrics 与备份校验；
- 全部幂等，只增不删。
"""

from __future__ import annotations

import sqlite3

M6_INDEXES = [
    # === 关键修复：M4 删除 UNIQUE 后 policy_documents.url 无索引，每次 save_record 全表扫描 ===
    "CREATE INDEX IF NOT EXISTS idx_policy_documents_url ON policy_documents(url)",

    # === rule_findings 热查询补强 ===
    # PDF 三方比对 / 证据查询：WHERE document_id=? AND rule_code LIKE 'PDF-%'
    "CREATE INDEX IF NOT EXISTS idx_rule_findings_doc_rule_current ON rule_findings(document_id, rule_code, is_current)",
    # 复核队列分页：WHERE job_id=? AND human_status=? AND machine_status=?
    "CREATE INDEX IF NOT EXISTS idx_rule_findings_job_human_machine ON rule_findings(job_id, human_status, machine_status, is_current)",
    # 文档级 finding 反查
    "CREATE INDEX IF NOT EXISTS idx_rule_findings_document_id ON rule_findings(document_id, is_current)",

    # === scan_exceptions 热查询补强 ===
    # 采集异常：WHERE job_id=? AND status='review_required' ORDER BY page_number, item_index
    "CREATE INDEX IF NOT EXISTS idx_scan_exceptions_job_status ON scan_exceptions(job_id, status)",
    # === scan_item_results 热查询补强 ===
    # current_job_item：WHERE job_id=? AND url=? AND detail_status!=?
    "CREATE INDEX IF NOT EXISTS idx_scan_item_results_job_url ON scan_item_results(job_id, url)",

    # === scan_jobs 热查询补强 ===
    # active_job：WHERE status IN (...) ORDER BY id DESC
    "CREATE INDEX IF NOT EXISTS idx_scan_jobs_status_id ON scan_jobs(status, id)",
    # eligible_baselines：WHERE mode=? AND status=? AND coverage_status=?
    "CREATE INDEX IF NOT EXISTS idx_scan_jobs_baseline ON scan_jobs(mode, status, coverage_status)",

    # === generation_items 热查询补强 ===
    # latest_generation_for_job：WHERE job_id=? ORDER BY id DESC LIMIT 1
    "CREATE INDEX IF NOT EXISTS idx_generation_items_job_id ON generation_items(job_id, id)",

    # === link_checks 热查询补强 ===
    # exporter 与 repository：WHERE job_id=? AND document_id=? AND visible=?
    "CREATE INDEX IF NOT EXISTS idx_link_checks_job_doc_visible ON link_checks(job_id, document_id, visible)",

    # === source_inventory 热查询补强 ===
    "CREATE INDEX IF NOT EXISTS idx_source_inventory_target_present ON source_inventory(target_key, is_present)",

    # === continuous_schedules 热查询补强 ===
    # due_schedules：WHERE enabled=1 AND (next_incremental_at<=? OR next_full_reconcile_at<=?)
    "CREATE INDEX IF NOT EXISTS idx_continuous_schedules_due ON continuous_schedules(enabled, next_incremental_at, next_full_reconcile_at)",

    # === M5 表查询补强 ===
    # rectification_tasks 按状态过滤（待办、超期、复发）
    "CREATE INDEX IF NOT EXISTS idx_rect_tasks_status_deadline ON rectification_tasks(status, deadline)",
    "CREATE INDEX IF NOT EXISTS idx_rect_tasks_recurrence ON rectification_tasks(recurrence_count) WHERE recurrence_count > 0",

    # === archive_findings / duplicate_pairs / reconciliation_matches 补强（M4 已有部分，补 status 过滤） ===
    "CREATE INDEX IF NOT EXISTS idx_archive_findings_status ON archive_findings(machine_status, human_status, status)",
    "CREATE INDEX IF NOT EXISTS idx_recon_matches_status ON reconciliation_matches(match_status)",

    # === operation_audits 补强（M1 审计按 actor_username / action 查询） ===
    "CREATE INDEX IF NOT EXISTS idx_operation_audits_actor ON operation_audits(actor_username)",
    "CREATE INDEX IF NOT EXISTS idx_operation_audits_action ON operation_audits(action, created_at)",
]

M6_OCR_QUEUE_SCHEMA = """
CREATE TABLE IF NOT EXISTS ocr_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL,
    job_id INTEGER,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','running','ok','failed','skipped','cancelled')),
    priority INTEGER NOT NULL DEFAULT 100,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    requested_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    requested_by TEXT NOT NULL DEFAULT 'system',
    worker_id TEXT NOT NULL DEFAULT '',
    ocr_status TEXT NOT NULL DEFAULT '',
    ocr_confidence REAL,
    pages_processed INTEGER NOT NULL DEFAULT 0,
    pages_total INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(document_id) REFERENCES policy_documents(id),
    FOREIGN KEY(job_id) REFERENCES scan_jobs(id)
);

CREATE INDEX IF NOT EXISTS idx_ocr_queue_status_priority ON ocr_queue(status, priority, requested_at);
CREATE INDEX IF NOT EXISTS idx_ocr_queue_doc ON ocr_queue(document_id);
"""

M6_SYSTEM_SETTINGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS system_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT 'general',
    description TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL DEFAULT 'system'
);
"""

M6_DB_HEALTH_SNAPSHOTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS db_health_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_at TEXT NOT NULL,
    db_size_bytes INTEGER NOT NULL DEFAULT 0,
    wal_size_bytes INTEGER NOT NULL DEFAULT 0,
    page_count INTEGER NOT NULL DEFAULT 0,
    freelist_count INTEGER NOT NULL DEFAULT 0,
    journal_mode TEXT NOT NULL DEFAULT '',
    table_counts_json TEXT NOT NULL DEFAULT '{}',
    index_list_json TEXT NOT NULL DEFAULT '[]',
    notes TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_db_health_snapshots_at ON db_health_snapshots(snapshot_at);
"""


def run_m6_migration(conn: sqlite3.Connection) -> None:
    """执行 M6 迁移：索引 + OCR 队列 + 系统设置 + 健康快照。幂等。"""
    cur = conn.cursor()

    # 1. 性能索引（全部 IF NOT EXISTS，幂等）
    for ddl in M6_INDEXES:
        cur.execute(ddl)

    # 2. OCR 队列表
    cur.executescript(M6_OCR_QUEUE_SCHEMA)

    # 3. 系统设置表（支撑「系统设置」导航项）
    cur.executescript(M6_SYSTEM_SETTINGS_SCHEMA)
    _seed_default_settings(cur)

    # 4. 数据库健康快照表（支撑 /metrics 与备份校验）
    cur.executescript(M6_DB_HEALTH_SNAPSHOTS_SCHEMA)

    # 5. WAL checkpoint 一次（清理历史 WAL，避免膨胀）
    try:
        cur.execute("PRAGMA wal_checkpoint(PASSIVE)")
    except sqlite3.DatabaseError:
        pass

    conn.commit()


def _seed_default_settings(cur: sqlite3.Cursor) -> None:
    """种子默认系统设置。幂等：使用 INSERT OR IGNORE。"""
    from app.db import utc_now

    now = utc_now()
    defaults = [
        ("auth_enabled", "true", "security", "是否启用登录鉴权"),
        ("csrf_enabled", "true", "security", "是否启用 CSRF 防护"),
        ("docs_visible_in_production", "false", "security", "生产环境是否暴露 /docs /redoc /openapi.json"),
        ("ocr_max_workers", "1", "performance", "OCR 队列最大并发数（1=串行）"),
        ("ocr_default_max_attempts", "3", "performance", "OCR 默认最大重试次数"),
        ("ocr_default_priority", "100", "performance", "OCR 默认优先级（数字越小越优先）"),
        ("backup_retention_days", "30", "backup", "备份保留天数"),
        ("backup_wal_checkpoint_on_success", "true", "backup", "备份成功后是否执行 WAL checkpoint"),
        ("log_level", "INFO", "logging", "日志级别（DEBUG/INFO/WARNING/ERROR）"),
        ("metrics_enabled", "true", "monitoring", "是否暴露 /metrics 端点"),
        ("app_name", "政府政策文件扫描检查系统", "general", "应用名称"),
        ("app_version", "0.2.0-phase1", "general", "应用版本"),
    ]
    cur.executemany(
        """INSERT OR IGNORE INTO system_settings(key, value, category, description, updated_at, updated_by)
           VALUES(?, ?, ?, ?, ?, 'system')""",
        [(k, v, c, d, now) for (k, v, c, d) in defaults],
    )
    # 系统更名后，将存量数据库中的旧应用名称同步为现名（幂等）。
    cur.execute(
        """UPDATE system_settings SET value=?, updated_at=?, updated_by='system'
           WHERE key='app_name' AND value='上海区级政策文件安全巡检系统'""",
        ("政府政策文件扫描检查系统", now),
    )
