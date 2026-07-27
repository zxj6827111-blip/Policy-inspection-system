"""M5 test suite: reading linkage, media, interpretation, rectification.

Covers:
- READ-001~005 reading linkage rules (V3.0 §11.1, §11.3, §11.5).
- MEDIA-001~006 image/video/attachment availability (V3.0 §11.2).
- INTERP-001~008 policy interpretation + object matching (V3.0 §11.2).
- RECT-001~007 rectification closure: lifecycle, recurrence, overdue (V3.0 §11.4).
- M5 API endpoints (smoke-level with auth).
- V3.0 §11.5 guarantees:
  - 正常重定向不误报.
  - 403/429 不进入正式问题.
  - 视频页面打开但视频失效可识别.
  - 200 空页面可识别.
  - 解读对象错误可识别.
  - 所有正式问题可创建整改任务.
  - 整改前后证据完整.
  - 状态可追溯.
  - 销号后复发可识别.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from app.db import Database


# ---------- DB / App fixtures ----------

def _setup_db(tmp_path: Path) -> Database:
    db_path = tmp_path / "m5.db"
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


def _seed_document(
    db: Database, *,
    title: str = "测试政策文件",
    url: str | None = None,
    page_document_number: str = "普府〔2024〕10号",
    issuing_agency: str = "上海市普陀区人民政府",
    authored_date: str = "2024-03-15",
    body_text: str = "上海市普陀区人民政府\n普府〔2024〕10号\n2024年3月15日",
    channel_level: str = "district",
    source_site: str = "putuo",
) -> int:
    url = url or "https://example.test/m5-doc"
    content_hash = hashlib.sha256(body_text.encode("utf-8")).hexdigest()
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
            ("普陀区", "", source_site, url, title, issuing_agency,
             page_document_number, None, authored_date, body_text,
             "", "", content_hash, int(job_id), int(job_id), now, now,
             channel_level),
        )
        return int(cur.lastrowid)


# ---------- Migration tests ----------

class TestM5Migration:
    def test_m5_tables_created(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        with db.connect() as conn:
            tables = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        for expected in (
            "reading_linkage_checks", "media_checks",
            "policy_interpretations", "interpretation_checks",
            "rectification_tasks", "rectification_status_history",
        ):
            assert expected in tables, f"missing table {expected}"

    def test_m5_policy_documents_columns(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        with db.connect() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(policy_documents)")}
        for expected in (
            "reading_check_status", "interpretation_count",
            "has_interpretation_mismatch", "rectification_status",
        ):
            assert expected in cols, f"missing column {expected}"

    def test_m5_rule_versions_seeded(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        with db.connect() as conn:
            for rule_id in ("READ-001", "READ-005", "MEDIA-001", "MEDIA-006",
                            "INTERP-001", "INTERP-008", "RECT-001", "RECT-007"):
                row = conn.execute(
                    "SELECT rule_id FROM rule_versions WHERE rule_id=?", (rule_id,)
                ).fetchone()
                assert row is not None, f"missing rule {rule_id}"

    def test_migration_is_idempotent(self, tmp_path: Path):
        from app.migrations.m5 import run_m5_migration
        db = _setup_db(tmp_path)
        with db.connect() as conn:
            r1 = run_m5_migration(conn)
            r2 = run_m5_migration(conn)
        assert r1["migration_id"] == r2["migration_id"]
        assert r2["already_applied"] is True


# ---------- Reading linkage tests ----------

class TestReadingLinkage:
    """V3.0 §11.5: 正常重定向不误报; 403/429 不进入正式问题; 200 空页面可识别."""

    def test_read_001_button_missing(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        doc_id = _seed_document(db)
        from app.reading_linkage import ReadingLinkageInput, record_reading_check
        result = record_reading_check(db, ReadingLinkageInput(
            document_id=doc_id, button_present=False,
            button_label="阅办", target_url="https://example.test/read",
        ))
        assert result.rule_code == "READ-001"
        assert result.machine_status == "confirmed"
        assert result.allow_customer_output is True

    def test_read_002_button_present_but_not_clickable(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        doc_id = _seed_document(db)
        from app.reading_linkage import ReadingLinkageInput, record_reading_check
        result = record_reading_check(db, ReadingLinkageInput(
            document_id=doc_id, button_present=True, clickable=False,
            target_url="https://example.test/read", page_status="ok",
        ))
        assert result.rule_code == "READ-002"
        assert result.allow_customer_output is True

    def test_read_003_jump_failed_http_error(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        doc_id = _seed_document(db)
        from app.reading_linkage import ReadingLinkageInput, record_reading_check
        result = record_reading_check(db, ReadingLinkageInput(
            document_id=doc_id, button_present=True, clickable=True,
            target_url="https://example.test/read",
            http_status=500, page_status="error",
        ))
        assert result.rule_code == "READ-003"

    def test_read_003_page_not_found(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        doc_id = _seed_document(db)
        from app.reading_linkage import ReadingLinkageInput, record_reading_check
        result = record_reading_check(db, ReadingLinkageInput(
            document_id=doc_id, button_present=True, clickable=True,
            target_url="https://example.test/read",
            http_status=404, page_status="not_found",
        ))
        assert result.rule_code == "READ-003"

    def test_read_004_blank_page_after_jump(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        doc_id = _seed_document(db)
        from app.reading_linkage import ReadingLinkageInput, record_reading_check
        result = record_reading_check(db, ReadingLinkageInput(
            document_id=doc_id, button_present=True, clickable=True,
            target_url="https://example.test/read",
            http_status=200, page_status="ok", page_body_length=0,
        ))
        assert result.rule_code == "READ-004"
        assert result.allow_customer_output is True

    def test_read_005_no_data_placeholder(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        doc_id = _seed_document(db)
        from app.reading_linkage import ReadingLinkageInput, record_reading_check
        result = record_reading_check(db, ReadingLinkageInput(
            document_id=doc_id, button_present=True, clickable=True,
            target_url="https://example.test/read",
            http_status=200, page_status="ok", page_body_length=500,
            has_no_data_placeholder=True,
        ))
        assert result.rule_code == "READ-005"

    def test_redirect_is_not_misflagged(self, tmp_path: Path):
        """V3.0 §11.5: 正常重定向不误报."""
        db = _setup_db(tmp_path)
        doc_id = _seed_document(db)
        from app.reading_linkage import ReadingLinkageInput, record_reading_check
        result = record_reading_check(db, ReadingLinkageInput(
            document_id=doc_id, button_present=True, clickable=True,
            target_url="https://example.test/read",
            http_status=302, page_status="redirect", page_body_length=1234,
        ))
        assert result.rule_code == ""
        assert result.machine_status == "ok"

    def test_403_does_not_enter_confirmed_problem(self, tmp_path: Path):
        """V3.0 §11.5: 403/429 不进入正式问题."""
        db = _setup_db(tmp_path)
        doc_id = _seed_document(db)
        from app.reading_linkage import ReadingLinkageInput, record_reading_check
        result = record_reading_check(db, ReadingLinkageInput(
            document_id=doc_id, button_present=True, clickable=True,
            target_url="https://example.test/read",
            http_status=403, page_status="blocked",
        ))
        assert result.rule_code == ""
        assert result.machine_status == "transient_error"
        assert result.allow_customer_output is False

    def test_429_does_not_enter_confirmed_problem(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        doc_id = _seed_document(db)
        from app.reading_linkage import ReadingLinkageInput, record_reading_check
        result = record_reading_check(db, ReadingLinkageInput(
            document_id=doc_id, button_present=True, clickable=True,
            target_url="https://example.test/read",
            http_status=429, page_status="rate_limited",
        ))
        assert result.rule_code == ""
        assert result.machine_status == "transient_error"
        assert result.allow_customer_output is False

    def test_ok_no_finding(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        doc_id = _seed_document(db)
        from app.reading_linkage import ReadingLinkageInput, record_reading_check
        result = record_reading_check(db, ReadingLinkageInput(
            document_id=doc_id, button_present=True, clickable=True,
            target_url="https://example.test/read",
            http_status=200, page_status="ok", page_body_length=1500,
        ))
        assert result.rule_code == ""
        assert result.machine_status == "ok"

    def test_list_reading_checks_filter(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        doc_id = _seed_document(db)
        from app.reading_linkage import ReadingLinkageInput, record_reading_check, list_reading_checks
        record_reading_check(db, ReadingLinkageInput(
            document_id=doc_id, button_present=True, clickable=True,
            target_url="https://example.test/read",
            http_status=200, page_status="ok", page_body_length=1000,
        ))
        rows = list_reading_checks(db, document_id=doc_id)
        assert len(rows) == 1
        assert rows[0]["document_id"] == doc_id


# ---------- Media checks tests ----------

class TestMediaChecks:
    """V3.0 §11.5: 视频页面打开但视频失效可识别."""

    def test_media_001_image_missing(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        doc_id = _seed_document(db)
        from app.media import MediaCheckInput, record_media_check
        result = record_media_check(db, MediaCheckInput(
            document_id=doc_id, media_type="image",
            media_url="https://example.test/img.png", status="missing",
        ))
        assert result.rule_code == "MEDIA-001"
        assert result.allow_customer_output is True

    def test_media_002_image_broken(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        doc_id = _seed_document(db)
        from app.media import MediaCheckInput, record_media_check
        result = record_media_check(db, MediaCheckInput(
            document_id=doc_id, media_type="image",
            media_url="https://example.test/img.png", status="broken",
        ))
        assert result.rule_code == "MEDIA-002"

    def test_media_003_video_missing(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        doc_id = _seed_document(db)
        from app.media import MediaCheckInput, record_media_check
        result = record_media_check(db, MediaCheckInput(
            document_id=doc_id, media_type="video",
            media_url="https://example.test/v.mp4", status="missing",
        ))
        assert result.rule_code == "MEDIA-003"

    def test_media_004_video_playback_failed(self, tmp_path: Path):
        """V3.0 §11.5: 视频页面打开但视频失效可识别."""
        db = _setup_db(tmp_path)
        doc_id = _seed_document(db)
        from app.media import MediaCheckInput, record_media_check
        result = record_media_check(db, MediaCheckInput(
            document_id=doc_id, media_type="video",
            media_url="https://example.test/v.mp4", status="playback_failed",
        ))
        assert result.rule_code == "MEDIA-004"
        assert result.allow_customer_output is True

    def test_media_005_empty_page(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        doc_id = _seed_document(db)
        from app.media import MediaCheckInput, record_media_check
        result = record_media_check(db, MediaCheckInput(
            document_id=doc_id, media_type="image",
            media_url="https://example.test/img.png", status="empty",
        ))
        assert result.rule_code == "MEDIA-005"

    def test_media_006_blocked_not_confirmed(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        doc_id = _seed_document(db)
        from app.media import MediaCheckInput, record_media_check
        result = record_media_check(db, MediaCheckInput(
            document_id=doc_id, media_type="image",
            media_url="https://example.test/img.png",
            http_status=403, status="blocked",
        ))
        assert result.rule_code == "MEDIA-006"
        assert result.machine_status == "transient_error"
        assert result.allow_customer_output is False

    def test_media_ok_no_finding(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        doc_id = _seed_document(db)
        from app.media import MediaCheckInput, record_media_check
        result = record_media_check(db, MediaCheckInput(
            document_id=doc_id, media_type="image",
            media_url="https://example.test/img.png",
            http_status=200, status="ok",
        ))
        assert result.rule_code == ""


# ---------- Interpretation tests ----------

class TestInterpretation:
    """V3.0 §11.5: 解读对象错误可识别."""

    def test_create_interpretation_with_explicit_match(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        doc_id = _seed_document(db)
        from app.interpretation import InterpretationInput, create_interpretation, list_interpretations
        interp_id = create_interpretation(db, InterpretationInput(
            document_id=doc_id, interpretation_type="image_text",
            interpretation_url="https://example.test/interp",
            title="解读标题",
            published_at="2024-04-01",
            target_document_id=doc_id,
        ))
        assert interp_id > 0
        rows = list_interpretations(db, document_id=doc_id)
        assert len(rows) == 1
        assert rows[0]["matched_target"] == 1
        assert rows[0]["match_confidence"] == 1.0

    def test_interpretation_doc_number_match(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        doc_id = _seed_document(db, page_document_number="普府〔2024〕10号")
        from app.interpretation import InterpretationInput, create_interpretation
        interp_id = create_interpretation(db, InterpretationInput(
            document_id=doc_id, interpretation_type="image_text",
            interpretation_url="https://example.test/interp",
            target_doc_number="普府〔2024〕10号",
        ))
        with db.connect() as conn:
            row = conn.execute(
                "SELECT matched_target, match_confidence FROM policy_interpretations WHERE id=?",
                (interp_id,),
            ).fetchone()
        assert row["matched_target"] == 1
        assert row["match_confidence"] == 1.0

    def test_interpretation_doc_number_mismatch(self, tmp_path: Path):
        """V3.0 §11.5: 解读对象错误可识别."""
        db = _setup_db(tmp_path)
        doc_id = _seed_document(db, page_document_number="普府〔2024〕10号")
        from app.interpretation import InterpretationInput, create_interpretation
        interp_id = create_interpretation(db, InterpretationInput(
            document_id=doc_id, interpretation_type="image_text",
            interpretation_url="https://example.test/interp",
            target_doc_number="沪府〔2024〕99号",  # different doc number
        ))
        with db.connect() as conn:
            row = conn.execute(
                "SELECT matched_target, match_confidence FROM policy_interpretations WHERE id=?",
                (interp_id,),
            ).fetchone()
            doc = conn.execute(
                "SELECT has_interpretation_mismatch FROM policy_documents WHERE id=?",
                (doc_id,),
            ).fetchone()
        assert row["matched_target"] == 0
        assert row["match_confidence"] == 0.0
        assert doc["has_interpretation_mismatch"] == 1

    def test_interp_001_link_invalid(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        doc_id = _seed_document(db)
        from app.interpretation import (
            InterpretationInput, create_interpretation,
            InterpretationCheckInput, record_interpretation_check,
        )
        interp_id = create_interpretation(db, InterpretationInput(
            document_id=doc_id, interpretation_type="image_text",
            interpretation_url="https://example.test/interp",
            target_document_id=doc_id,
        ))
        result = record_interpretation_check(db, InterpretationCheckInput(
            interpretation_id=interp_id,
            link_status="not_found", http_status=404,
        ))
        assert result.rule_code == "INTERP-001"

    def test_interp_003_published_at_missing(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        doc_id = _seed_document(db)
        from app.interpretation import (
            InterpretationInput, create_interpretation,
            InterpretationCheckInput, record_interpretation_check,
        )
        interp_id = create_interpretation(db, InterpretationInput(
            document_id=doc_id, interpretation_type="qa",
            interpretation_url="https://example.test/interp",
            target_document_id=doc_id,
        ))
        result = record_interpretation_check(db, InterpretationCheckInput(
            interpretation_id=interp_id,
            link_status="ok", http_status=200,
            page_body_length=500,
            published_at_present=False,
        ))
        assert result.rule_code == "INTERP-003"

    def test_interp_005_image_broken(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        doc_id = _seed_document(db)
        from app.interpretation import (
            InterpretationInput, create_interpretation,
            InterpretationCheckInput, record_interpretation_check,
        )
        interp_id = create_interpretation(db, InterpretationInput(
            document_id=doc_id, interpretation_type="one_image",
            interpretation_url="https://example.test/interp",
            target_document_id=doc_id,
        ))
        result = record_interpretation_check(db, InterpretationCheckInput(
            interpretation_id=interp_id,
            link_status="ok", http_status=200,
            has_image=True, image_status="broken",
            page_body_length=500,
        ))
        assert result.rule_code == "INTERP-005"

    def test_interp_006_video_failed(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        doc_id = _seed_document(db)
        from app.interpretation import (
            InterpretationInput, create_interpretation,
            InterpretationCheckInput, record_interpretation_check,
        )
        interp_id = create_interpretation(db, InterpretationInput(
            document_id=doc_id, interpretation_type="video",
            interpretation_url="https://example.test/interp",
            target_document_id=doc_id,
        ))
        result = record_interpretation_check(db, InterpretationCheckInput(
            interpretation_id=interp_id,
            link_status="ok", http_status=200,
            has_video=True, video_status="playback_failed",
            page_body_length=500,
        ))
        assert result.rule_code == "INTERP-006"

    def test_interp_007_blank_page(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        doc_id = _seed_document(db)
        from app.interpretation import (
            InterpretationInput, create_interpretation,
            InterpretationCheckInput, record_interpretation_check,
        )
        interp_id = create_interpretation(db, InterpretationInput(
            document_id=doc_id, interpretation_type="image_text",
            interpretation_url="https://example.test/interp",
            target_document_id=doc_id,
        ))
        result = record_interpretation_check(db, InterpretationCheckInput(
            interpretation_id=interp_id,
            link_status="ok", http_status=200,
            page_body_length=0, is_empty_page=True,
        ))
        assert result.rule_code == "INTERP-007"

    def test_interp_ok_no_finding(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        doc_id = _seed_document(db)
        from app.interpretation import (
            InterpretationInput, create_interpretation,
            InterpretationCheckInput, record_interpretation_check,
        )
        interp_id = create_interpretation(db, InterpretationInput(
            document_id=doc_id, interpretation_type="image_text",
            interpretation_url="https://example.test/interp",
            target_document_id=doc_id,
        ))
        result = record_interpretation_check(db, InterpretationCheckInput(
            interpretation_id=interp_id,
            link_status="ok", http_status=200,
            has_image=True, image_status="ok",
            page_body_length=500,
            published_at_present=True,
        ))
        assert result.rule_code == ""


# ---------- Rectification tests ----------

class TestRectification:
    """V3.0 §11.4 + §11.5: 整改闭环 / 状态可追溯 / 整改前后证据完整 / 销号后复发可识别."""

    def test_rect_001_no_responsible_unit(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        doc_id = _seed_document(db)
        from app.rectification import RectificationTaskInput, create_task
        result = create_task(db, RectificationTaskInput(
            document_id=doc_id, rule_code="ORG-001",
            title="整改任务", deadline="2026-12-31",
        ))
        assert "RECT-001" in result.rule_codes_emitted

    def test_rect_007_no_deadline(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        doc_id = _seed_document(db)
        from app.rectification import RectificationTaskInput, create_task
        result = create_task(db, RectificationTaskInput(
            document_id=doc_id, rule_code="ORG-001",
            title="整改任务", responsible_unit="政策处",
        ))
        assert "RECT-007" in result.rule_codes_emitted

    def test_create_task_no_issues(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        doc_id = _seed_document(db)
        from app.rectification import RectificationTaskInput, create_task
        result = create_task(db, RectificationTaskInput(
            document_id=doc_id, rule_code="ORG-001",
            title="整改任务", responsible_unit="政策处",
            responsible_person="张三", deadline="2026-12-31",
        ))
        assert result.rule_codes_emitted == []

    def test_status_transition_happy_path(self, tmp_path: Path):
        """V3.0 §11.4: created → assigned → notified → feedback_uploaded → rechecking → passed → signoff."""
        db = _setup_db(tmp_path)
        doc_id = _seed_document(db)
        from app.rectification import (
            RectificationTaskInput, create_task, transition_status, get_task,
        )
        result = create_task(db, RectificationTaskInput(
            document_id=doc_id, rule_code="ORG-001",
            title="整改任务", responsible_unit="政策处",
            responsible_person="张三", deadline="2026-12-31",
        ))
        task_id = result.task_id
        transition_status(db, task_id, "assigned", actor="admin", reason="指派")
        transition_status(db, task_id, "notified", actor="admin", reason="通知已发")
        transition_status(
            db, task_id, "feedback_uploaded", actor="政策处", reason="反馈已上传",
            feedback_notes="已完成整改",
            after_evidence=[{"type": "screenshot", "url": "https://example.test/after.png"}],
        )
        transition_status(db, task_id, "rechecking", actor="reviewer", reason="开始复查")
        transition_status(
            db, task_id, "passed", actor="reviewer", reason="复查通过",
            after_evidence=[{"type": "screenshot", "url": "https://example.test/after.png"}],
        )
        transition_status(db, task_id, "signoff", actor="admin", reason="销号")
        task = get_task(db, task_id)
        assert task["status"] == "signoff"
        assert task["closed_at"] is not None

    def test_rect_003_after_evidence_missing(self, tmp_path: Path):
        """V3.0 §11.5: 整改前后证据完整."""
        db = _setup_db(tmp_path)
        doc_id = _seed_document(db)
        from app.rectification import (
            RectificationTaskInput, create_task, transition_status,
        )
        result = create_task(db, RectificationTaskInput(
            document_id=doc_id, rule_code="ORG-001",
            title="整改任务", responsible_unit="政策处",
            responsible_person="张三", deadline="2026-12-31",
        ))
        task_id = result.task_id
        transition_status(db, task_id, "assigned", actor="admin", reason="指派")
        transition_status(db, task_id, "notified", actor="admin", reason="通知")
        transition_status(
            db, task_id, "feedback_uploaded", actor="政策处", reason="反馈",
            feedback_notes="已完成整改",
            after_evidence=[],  # no after_evidence
        )
        r = transition_status(
            db, task_id, "rechecking", actor="reviewer", reason="复查",
            after_evidence=[],  # still no evidence
        )
        assert "RECT-003" in r["rule_codes_emitted"]

    def test_rect_004_failed_without_rework(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        doc_id = _seed_document(db)
        from app.rectification import (
            RectificationTaskInput, create_task, transition_status,
        )
        result = create_task(db, RectificationTaskInput(
            document_id=doc_id, rule_code="ORG-001",
            title="整改任务", responsible_unit="政策处",
            responsible_person="张三", deadline="2026-12-31",
        ))
        task_id = result.task_id
        transition_status(db, task_id, "assigned", actor="admin")
        transition_status(db, task_id, "notified", actor="admin")
        transition_status(
            db, task_id, "feedback_uploaded", actor="政策处",
            after_evidence=[{"type": "screenshot"}],
        )
        transition_status(db, task_id, "rechecking", actor="reviewer",
                          after_evidence=[{"type": "screenshot"}])
        r = transition_status(db, task_id, "failed", actor="reviewer", reason="整改不通过")
        assert "RECT-004" in r["rule_codes_emitted"]

    def test_rect_005_recurrence_after_signoff(self, tmp_path: Path):
        """V3.0 §11.5: 销号后复发可识别."""
        db = _setup_db(tmp_path)
        doc_id = _seed_document(db)
        from app.rectification import (
            RectificationTaskInput, create_task, transition_status,
            detect_recurrence, get_task,
        )
        result = create_task(db, RectificationTaskInput(
            document_id=doc_id, rule_code="ORG-001",
            title="整改任务", responsible_unit="政策处",
            responsible_person="张三", deadline="2026-12-31",
        ))
        task_id = result.task_id
        # Drive to signoff.
        transition_status(db, task_id, "assigned", actor="admin")
        transition_status(db, task_id, "notified", actor="admin")
        transition_status(
            db, task_id, "feedback_uploaded", actor="政策处",
            after_evidence=[{"type": "screenshot"}],
        )
        transition_status(db, task_id, "rechecking", actor="reviewer",
                          after_evidence=[{"type": "screenshot"}])
        transition_status(db, task_id, "passed", actor="reviewer",
                          after_evidence=[{"type": "screenshot"}])
        transition_status(db, task_id, "signoff", actor="admin", reason="销号")

        # Recurrence: reopen from signoff.
        r = transition_status(db, task_id, "reopened", actor="admin", reason="复发")
        assert "RECT-005" in r["rule_codes_emitted"]

        task = get_task(db, task_id)
        assert task["recurrence_count"] == 1

        # detect_recurrence should find it.
        recurrences = detect_recurrence(db, document_id=doc_id, rule_code="ORG-001")
        assert len(recurrences) == 1
        assert recurrences[0]["id"] == task_id

    def test_rect_002_overdue_detection(self, tmp_path: Path):
        """V3.0 §11.5: 超期提醒."""
        db = _setup_db(tmp_path)
        doc_id = _seed_document(db)
        from app.rectification import (
            RectificationTaskInput, create_task, transition_status, detect_overdue_tasks,
        )
        past = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        result = create_task(db, RectificationTaskInput(
            document_id=doc_id, rule_code="ORG-001",
            title="整改任务", responsible_unit="政策处",
            responsible_person="张三", deadline=past,
        ))
        task_id = result.task_id
        transition_status(db, task_id, "assigned", actor="admin")
        overdue = detect_overdue_tasks(db)
        assert any(t["id"] == task_id for t in overdue)

    def test_rect_006_status_history_missing(self, tmp_path: Path):
        """V3.0 §11.5: 状态可追溯. RECT-006 if history missing."""
        db = _setup_db(tmp_path)
        doc_id = _seed_document(db)
        from app.rectification import (
            RectificationTaskInput, create_task, transition_status,
        )
        result = create_task(db, RectificationTaskInput(
            document_id=doc_id, rule_code="ORG-001",
            title="整改任务", responsible_unit="政策处",
            responsible_person="张三", deadline="2026-12-31",
        ))
        task_id = result.task_id
        # First transition creates history entry #2 (create was #1).
        r = transition_status(db, task_id, "assigned", actor="admin")
        # history_count is 2 now (create + assigned), so no RECT-006.
        assert "RECT-006" not in r["rule_codes_emitted"]

    def test_invalid_transition_rejected(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        doc_id = _seed_document(db)
        from app.rectification import (
            RectificationTaskInput, create_task, transition_status,
        )
        result = create_task(db, RectificationTaskInput(
            document_id=doc_id, rule_code="ORG-001",
            title="整改任务", responsible_unit="政策处",
            responsible_person="张三", deadline="2026-12-31",
        ))
        task_id = result.task_id
        # 'created' → 'signoff' is not allowed (must go through full flow).
        with pytest.raises(ValueError, match="Invalid status transition"):
            transition_status(db, task_id, "signoff", actor="admin")

    def test_get_task_with_history(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        doc_id = _seed_document(db)
        from app.rectification import (
            RectificationTaskInput, create_task, transition_status,
            get_task, get_status_history,
        )
        result = create_task(db, RectificationTaskInput(
            document_id=doc_id, rule_code="ORG-001",
            title="整改任务", responsible_unit="政策处",
            responsible_person="张三", deadline="2026-12-31",
        ))
        task_id = result.task_id
        transition_status(db, task_id, "assigned", actor="admin")
        history = get_status_history(db, task_id)
        assert len(history) == 2  # create + assigned
        assert history[0]["to_status"] == "created"
        assert history[1]["to_status"] == "assigned"


# ---------- API tests ----------

class TestM5API:
    """Smoke tests for M5 endpoints — full auth flow tested elsewhere.

    Uses the same pattern as test_m4_archive_duplicates_recon.py.
    Marked with @pytest.mark.real_auth so auth is enabled.
    """

    @pytest.mark.real_auth
    def test_reading_check_endpoint_requires_auth(self, tmp_path: Path, monkeypatch):
        from fastapi.testclient import TestClient
        import app.main as main_module
        from app.db import Database
        from app.jobs import JobManager
        from app.runtime_config import reset_runtime_settings_cache

        monkeypatch.setenv("APP_ENV", "test")
        monkeypatch.setenv("POLICY_AUTH_DISABLED", "0")
        reset_runtime_settings_cache()
        db = Database(str(tmp_path / "m5_api.db"))
        db.initialize()
        monkeypatch.setattr(main_module, "db", db)
        monkeypatch.setattr(main_module, "manager", JobManager(db))
        with TestClient(main_module.app) as client:
            resp = client.post("/api/reading-checks/1", json={
                "button_present": False, "target_url": "https://example.test",
            })
            assert resp.status_code in (401, 403)

    @pytest.mark.real_auth
    def test_media_check_endpoint_requires_auth(self, tmp_path: Path, monkeypatch):
        from fastapi.testclient import TestClient
        import app.main as main_module
        from app.db import Database
        from app.jobs import JobManager
        from app.runtime_config import reset_runtime_settings_cache

        monkeypatch.setenv("APP_ENV", "test")
        monkeypatch.setenv("POLICY_AUTH_DISABLED", "0")
        reset_runtime_settings_cache()
        db = Database(str(tmp_path / "m5_api2.db"))
        db.initialize()
        monkeypatch.setattr(main_module, "db", db)
        monkeypatch.setattr(main_module, "manager", JobManager(db))
        with TestClient(main_module.app) as client:
            resp = client.post("/api/media-checks/1", json={
                "media_type": "image", "media_url": "https://example.test/i.png",
            })
            assert resp.status_code in (401, 403)

    @pytest.mark.real_auth
    def test_interpretation_endpoint_requires_auth(self, tmp_path: Path, monkeypatch):
        from fastapi.testclient import TestClient
        import app.main as main_module
        from app.db import Database
        from app.jobs import JobManager
        from app.runtime_config import reset_runtime_settings_cache

        monkeypatch.setenv("APP_ENV", "test")
        monkeypatch.setenv("POLICY_AUTH_DISABLED", "0")
        reset_runtime_settings_cache()
        db = Database(str(tmp_path / "m5_api3.db"))
        db.initialize()
        monkeypatch.setattr(main_module, "db", db)
        monkeypatch.setattr(main_module, "manager", JobManager(db))
        with TestClient(main_module.app) as client:
            resp = client.post("/api/interpretations/1", json={
                "interpretation_type": "image_text",
                "interpretation_url": "https://example.test/interp",
            })
            assert resp.status_code in (401, 403)

    @pytest.mark.real_auth
    def test_rectification_endpoint_requires_auth(self, tmp_path: Path, monkeypatch):
        from fastapi.testclient import TestClient
        import app.main as main_module
        from app.db import Database
        from app.jobs import JobManager
        from app.runtime_config import reset_runtime_settings_cache

        monkeypatch.setenv("APP_ENV", "test")
        monkeypatch.setenv("POLICY_AUTH_DISABLED", "0")
        reset_runtime_settings_cache()
        db = Database(str(tmp_path / "m5_api4.db"))
        db.initialize()
        monkeypatch.setattr(main_module, "db", db)
        monkeypatch.setattr(main_module, "manager", JobManager(db))
        with TestClient(main_module.app) as client:
            resp = client.post("/api/rectification/tasks", json={
                "document_id": 1, "rule_code": "ORG-001",
                "title": "test", "responsible_unit": "政策处",
                "deadline": "2026-12-31",
            })
            assert resp.status_code in (401, 403)

    @pytest.mark.real_auth
    def test_rectification_overdue_requires_login(self, tmp_path: Path, monkeypatch):
        from fastapi.testclient import TestClient
        import app.main as main_module
        from app.db import Database
        from app.jobs import JobManager
        from app.runtime_config import reset_runtime_settings_cache

        monkeypatch.setenv("APP_ENV", "test")
        monkeypatch.setenv("POLICY_AUTH_DISABLED", "0")
        reset_runtime_settings_cache()
        db = Database(str(tmp_path / "m5_api5.db"))
        db.initialize()
        monkeypatch.setattr(main_module, "db", db)
        monkeypatch.setattr(main_module, "manager", JobManager(db))
        with TestClient(main_module.app) as client:
            resp = client.get("/api/rectification/overdue")
            assert resp.status_code in (401, 403)
