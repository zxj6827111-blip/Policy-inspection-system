"""R-P1-4 M6 专用 pytest 套件：OCR 队列 / 备份校验 / 深度健康 / 21 项导航 / 关键索引 / Metrics。

覆盖 M6 主要功能，从演练脚本升级为 pytest 门禁。
每个测试用 tmp_path 隔离，不依赖生产 DB。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.db import Database
from app.jobs import JobManager
from tests.auth_helpers import bootstrap_users, login


# ---------- DB / App fixtures ----------

def _setup_db(tmp_path: Path, *, name: str = "m6.db") -> Database:
    """创建隔离的测试数据库并初始化全部 schema（含 M6 索引/队列/监控）。"""
    db_path = tmp_path / name
    db = Database(str(db_path))
    db.initialize()
    with db.connect() as conn:
        conn.execute(
            """INSERT INTO scan_jobs(
                districts_json, mode, status, created_at, safety_json, max_documents
            ) VALUES('["普陀区"]','full','completed',?,?,0)""",
            (datetime.now(timezone.utc).isoformat(), "{}"),
        )
    return db


def _seed_document(db: Database, *, url: str = "https://example.test/m6-doc") -> int:
    """插入一个 policy_documents 行作为 OCR 队列 FK 目标，返回 id。"""
    body_text = "上海市普陀区人民政府\n普府〔2024〕10号"
    content_hash = "0" * 64
    now = datetime.now(timezone.utc).isoformat()
    with db.connect() as conn:
        job_id = conn.execute("SELECT id FROM scan_jobs ORDER BY id DESC LIMIT 1").fetchone()["id"]
        cur = conn.execute(
            """INSERT INTO policy_documents(
                district, source_id, source_site, url, title, issuing_agency,
                page_document_number, published_date, authored_date, body_text,
                topic_category, disclosure_attribute, content_hash,
                first_seen_job_id, last_seen_job_id, first_seen_at, last_seen_at,
                channel_level
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("普陀区", "", "putuo", url, "测试政策文件", "上海市普陀区人民政府",
             "普府〔2024〕10号", None, "2024-03-15", body_text,
             "", "", content_hash, int(job_id), int(job_id), now, now,
             "district"),
        )
        return int(cur.lastrowid)


def _app_client(tmp_path: Path, monkeypatch, *, name: str = "m6_api.db"):
    """构造一个绑定隔离 DB 的 TestClient（auth 关闭，用于不涉及鉴权的端点测试）。"""
    db = Database(str(tmp_path / name))
    db.initialize()
    manager = JobManager(db)
    monkeypatch.setattr(main_module, "db", db)
    monkeypatch.setattr(main_module, "manager", manager)
    return db, TestClient(main_module.app)


def _app_client_real_auth(tmp_path: Path, monkeypatch, *, name: str = "m6_auth.db"):
    """构造一个启用真实鉴权的 TestClient，并 bootstrap 五角色用户。"""
    from app.runtime_config import reset_runtime_settings_cache

    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("POLICY_AUTH_DISABLED", "0")
    reset_runtime_settings_cache()
    db = Database(str(tmp_path / name))
    db.initialize()
    manager = JobManager(db)
    monkeypatch.setattr(main_module, "db", db)
    monkeypatch.setattr(main_module, "manager", manager)
    bootstrap_users(db)
    return db, TestClient(main_module.app)


# ===========================================================================
# 1. OCR 队列
# ===========================================================================

class TestOcrQueue:
    def test_enqueue_is_idempotent(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        doc_id = _seed_document(db)
        from app.ocr_queue import enqueue
        first = enqueue(db, doc_id)
        second = enqueue(db, doc_id)
        assert first == second, "同 doc_id 重复入队应返回现有 ID"

    def test_enqueue_different_docs_get_different_ids(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        doc_a = _seed_document(db, url="https://example.test/a")
        doc_b = _seed_document(db, url="https://example.test/b")
        from app.ocr_queue import enqueue
        id_a = enqueue(db, doc_a)
        id_b = enqueue(db, doc_b)
        assert id_a != id_b

    def test_next_pending_atomic_claim(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        doc_a = _seed_document(db, url="https://example.test/np-a")
        doc_b = _seed_document(db, url="https://example.test/np-b")
        from app.ocr_queue import enqueue, next_pending
        enqueue(db, doc_a, priority=10)
        enqueue(db, doc_b, priority=50)
        claimed = next_pending(db, worker_id="worker-x")
        assert claimed is not None
        assert claimed["status"] == "running"
        assert claimed["worker_id"] == "worker-x"
        assert int(claimed["attempts"]) == 1
        # priority=10 的 doc_a 应被先认领
        assert int(claimed["document_id"]) == doc_a
        # 第二次认领应拿到另一项
        claimed2 = next_pending(db, worker_id="worker-y")
        assert claimed2 is not None
        assert int(claimed2["document_id"]) == doc_b
        # 无 pending 时返回 None
        assert next_pending(db) is None

    def test_complete_ok_transitions_to_done(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        doc_id = _seed_document(db)
        from app.ocr_queue import enqueue, next_pending, complete, list_queue
        item_id = enqueue(db, doc_id)
        claimed = next_pending(db)
        assert claimed is not None
        result = complete(db, int(claimed["id"]), "ok", ocr_confidence=0.95,
                          pages_processed=2, pages_total=2)
        assert result["final_status"] == "ok"
        assert result["retried"] is False
        rows = list_queue(db, status="ok")
        assert any(int(r["id"]) == item_id for r in rows)

    def test_complete_failed_below_max_retries_to_pending(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        doc_id = _seed_document(db)
        from app.ocr_queue import enqueue, next_pending, complete, list_queue
        enqueue(db, doc_id, max_attempts=3)
        claimed = next_pending(db)
        assert claimed is not None
        # attempts=1 < max_attempts=3 → 回退 pending
        result = complete(db, int(claimed["id"]), "failed", error="ocr timeout")
        assert result["final_status"] == "pending"
        assert result["retried"] is True
        pending = list_queue(db, status="pending")
        assert any(int(r["id"]) == int(claimed["id"]) for r in pending)

    def test_complete_failed_at_max_transitions_to_failed(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        doc_id = _seed_document(db)
        from app.ocr_queue import enqueue, next_pending, complete, list_queue
        enqueue(db, doc_id, max_attempts=1)
        claimed = next_pending(db)
        assert claimed is not None
        # attempts=1 >= max_attempts=1 → 终态 failed
        result = complete(db, int(claimed["id"]), "failed", error="permanent")
        assert result["final_status"] == "failed"
        assert result["retried"] is False
        failed = list_queue(db, status="failed")
        assert any(int(r["id"]) == int(claimed["id"]) for r in failed)

    def test_cancel_marks_cancelled(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        doc_id = _seed_document(db)
        from app.ocr_queue import enqueue, cancel, list_queue
        item_id = enqueue(db, doc_id)
        ok = cancel(db, item_id, reason="user abort")
        assert ok is True
        cancelled = list_queue(db, status="cancelled")
        assert any(int(r["id"]) == item_id for r in cancelled)
        # 不存在的 item_id → False
        assert cancel(db, 999999) is False

    def test_queue_depth_stats_correct(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        doc_a = _seed_document(db, url="https://example.test/qd-a")
        doc_b = _seed_document(db, url="https://example.test/qd-b")
        doc_c = _seed_document(db, url="https://example.test/qd-c")
        from app.ocr_queue import enqueue, next_pending, complete, cancel, queue_depth
        enqueue(db, doc_a)
        enqueue(db, doc_b)
        enqueue(db, doc_c)
        # 认领一个 → running，再完成一个 → ok，再取消一个 → cancelled
        claimed1 = next_pending(db)
        complete(db, int(claimed1["id"]), "ok")
        claimed2 = next_pending(db)
        complete(db, int(claimed2["id"]), "failed")  # 回退 pending（attempts=1<3）
        # 现在 pending=2（含回退的）, running=0, ok=1
        depth = queue_depth(db)
        assert depth["total"] == 3
        assert depth["ok"] == 1
        assert depth["pending"] == 2
        assert depth["running"] == 0
        assert depth["failed"] == 0
        assert depth["cancelled"] == 0


# ===========================================================================
# 2. 备份 / 恢复
# ===========================================================================

class TestBackupRestore:
    def test_backup_database_creates_db_and_sha256_sidecar(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        from app.backup import backup_database
        dest_dir = tmp_path / "backups"
        path = backup_database(db, dest_dir, label="test")
        assert path.exists(), "备份文件应存在"
        sidecar = path.with_name(path.name + ".sha256")
        assert sidecar.exists(), "SHA256 旁路文件应存在"
        sidecar_text = sidecar.read_text(encoding="utf-8").strip()
        assert sidecar_text.split()[0], "SHA256 摘要非空"
        assert sidecar_text.split()[1] == path.name

    def test_validate_backup_passes(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        from app.backup import backup_database, validate_backup
        dest_dir = tmp_path / "backups"
        path = backup_database(db, dest_dir, label="validate")
        report = validate_backup(path)
        assert report["valid"] is True
        assert report["integrity"] == "ok"
        assert report["quick_check"] == "ok"
        assert report["fk_check"] == []
        assert isinstance(report["table_counts"], dict)
        # 关键表应被统计
        for table in ("rule_findings", "policy_documents", "scan_jobs", "operation_audits"):
            assert table in report["table_counts"]

    def test_restore_database_dry_run_does_not_write(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        from app.backup import backup_database, restore_database
        dest_dir = tmp_path / "backups"
        path = backup_database(db, dest_dir, label="dryrun")
        dest = tmp_path / "restored.db"
        report = restore_database(path, dest, dry_run=True)
        assert report["dry_run"] is True
        assert report["restored"] is False
        assert report["integrity"] == "ok"
        assert report["sha256_match"] is True
        # dry_run 不应写入目标文件
        assert not dest.exists(), "dry_run=True 不应写入目标"


# ===========================================================================
# 3. 深度健康检查
# ===========================================================================

class TestDeepHealth:
    def test_deep_health_returns_ok_with_integrity_fields(self, tmp_path: Path, monkeypatch):
        db, client = _app_client(tmp_path, monkeypatch, name="deep_health.db")
        with client:
            resp = client.get("/health/deep")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["status"] in {"ok", "healthy"}, f"期望 ok/healthy，实际 {payload['status']}"
        db_block = payload["database"]
        assert "integrity_check" in db_block
        assert "quick_check" in db_block
        assert "foreign_key_check" in db_block
        assert db_block["integrity_check"] == "ok"
        assert db_block["quick_check"] == "ok"
        assert db_block["foreign_key_check"] == []


# ===========================================================================
# 4. 21 项导航
# ===========================================================================

@pytest.mark.real_auth
class TestNavItems:
    def test_admin_sees_21_items(self, tmp_path: Path, monkeypatch):
        _db, client = _app_client_real_auth(tmp_path, monkeypatch, name="nav_admin.db")
        with client:
            headers = login(client, "admin", "AdminPass123!")["headers"]
            resp = client.get("/api/nav-items", headers=headers)
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 21, f"admin 应看到 21 项，实际 {len(items)}"

    def test_viewer_sees_fewer_than_admin(self, tmp_path: Path, monkeypatch):
        _db, client = _app_client_real_auth(tmp_path, monkeypatch, name="nav_viewer.db")
        with client:
            # 先以 admin 登录并获取其导航项（后续 viewer 登录会覆盖会话 cookie）。
            admin_headers = login(client, "admin", "AdminPass123!")["headers"]
            admin_items = client.get("/api/nav-items", headers=admin_headers).json()
            # 再以 viewer 登录并获取其导航项。
            viewer_headers = login(client, "viewer", "ViewerPass123!")["headers"]
            viewer_items = client.get("/api/nav-items", headers=viewer_headers).json()
        assert len(admin_items) == 21
        assert len(viewer_items) < len(admin_items), \
            f"viewer({len(viewer_items)}) 应少于 admin({len(admin_items)})"
        # viewer 不应看到 users / audits / settings（均需 admin/auditor 角色）
        viewer_keys = {item["key"] for item in viewer_items}
        assert "users" not in viewer_keys
        assert "audits" not in viewer_keys
        assert "settings" not in viewer_keys

    def test_unauthenticated_returns_empty(self, tmp_path: Path, monkeypatch):
        _db, client = _app_client_real_auth(tmp_path, monkeypatch, name="nav_anon.db")
        with client:
            resp = client.get("/api/nav-items")
        assert resp.status_code == 200
        assert resp.json() == [], "未登录应返回空导航列表"


# ===========================================================================
# 5. 关键索引存在性
# ===========================================================================

class TestKeyIndexes:
    """验证 M6 迁移创建的关键索引存在。

    任务列出的索引名与实际名称可能不一致（如 idx_pdf_extractions_doc_id 实为
    idx_pdf_extractions_doc），故先查询 sqlite_master 找到真实索引名再断言。
    """

    # 逻辑名 → 可接受的实际索引名集合（命中其一即通过）。
    REQUIRED_INDEXES: dict[str, set[str]] = {
        "idx_policy_documents_url": {"idx_policy_documents_url"},
        "idx_rule_findings_job_id": {
            "idx_rule_findings_job_id",
            "idx_rule_findings_current",        # (job_id, is_current)
            "idx_rule_findings_job_human_machine",
        },
        "idx_pdf_extractions_doc_id": {
            "idx_pdf_extractions_doc_id",
            "idx_pdf_extractions_doc",
        },
        "idx_archive_observations_doc_id": {
            "idx_archive_observations_doc_id",
            "idx_archive_observations_doc",
        },
        "idx_rectification_tasks_document_id": {
            "idx_rectification_tasks_document_id",
            "idx_rect_tasks_doc",
        },
    }

    def test_key_indexes_exist(self, tmp_path: Path):
        db = _setup_db(tmp_path, name="indexes.db")
        with db.connect() as conn:
            actual = {
                str(r[0]) for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            }
        for logical, acceptable in self.REQUIRED_INDEXES.items():
            matched = acceptable & actual
            assert matched, (
                f"缺少索引 {logical}：可接受名 {sorted(acceptable)} 均不存在；"
                f"实际索引含 {sorted(actual)[:20]}..."
            )


# ===========================================================================
# 6. Metrics 端点
# ===========================================================================

@pytest.mark.real_auth
class TestMetricsEndpoint:
    def test_metrics_unauthenticated_returns_401(self, tmp_path: Path, monkeypatch):
        _db, client = _app_client_real_auth(tmp_path, monkeypatch, name="metrics_401.db")
        with client:
            resp = client.get("/metrics")
        assert resp.status_code == 401, f"未登录访问 /metrics 应 401，实际 {resp.status_code}"

    def test_metrics_authenticated_returns_json(self, tmp_path: Path, monkeypatch):
        _db, client = _app_client_real_auth(tmp_path, monkeypatch, name="metrics_ok.db")
        with client:
            headers = login(client, "admin", "AdminPass123!")["headers"]
            resp = client.get("/metrics", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        # get_metrics 返回 db / jobs / ocr_queue / rectification / findings / audits 桶
        assert "db" in data
        assert "jobs" in data
        assert "ocr_queue" in data
        assert "findings" in data
