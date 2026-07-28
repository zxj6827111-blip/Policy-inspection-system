"""R-P1-2 证据完整率 KPI 测试套件。

覆盖：
- 空库 → completeness_rate=1.0
- 完整 finding → completeness_rate=1.0
- 缺失 evidence 字段 → 命中 incomplete
- PDF 类 finding 缺少 source_type=pdf 证据 → 命中 incomplete
- ARCH 类 finding 缺少对应证据 → 命中 incomplete
- READ 类 finding 缺少对应证据 → 命中 incomplete
- 门禁：所有完整时 completeness_rate == 1.0
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from app.db import Database
from app.evidence_completeness import compute_evidence_completeness


# ---------- DB fixtures ----------

def _setup_db(tmp_path: Path) -> Database:
    """创建一个隔离的测试数据库并初始化全部 schema（含 phase1/closeout/M6）。"""
    db_path = tmp_path / "evidence.db"
    db = Database(str(db_path))
    db.initialize()
    # 创建一个 scan_jobs 行作为外键基础。
    with db.connect() as conn:
        conn.execute(
            """INSERT INTO scan_jobs(
                districts_json, mode, status, created_at, safety_json, max_documents
            ) VALUES('["普陀区"]','full','completed',?,?,0)""",
            (datetime.now(timezone.utc).isoformat(), "{}"),
        )
    return db


def _seed_document(db: Database, *, url: str = "https://example.test/ev-doc") -> int:
    """插入一个 policy_documents 行，返回 id。"""
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


def _insert_finding(
    db: Database,
    *,
    rule_code: str,
    evidence: str = "页面证据",
    page_value: str = "页面值",
    body_value: str = "正文值",
    rule_version: str = "1.0.0",
    evidence_json: str = "[]",
) -> int:
    """直接插入一条 rule_findings 行，返回 finding id。

    默认字段满足"必填字段"标准；调用方可覆盖 evidence_json 以测试类别证据要求。
    """
    now = datetime.now(timezone.utc).isoformat()
    with db.connect() as conn:
        job_id = conn.execute("SELECT id FROM scan_jobs ORDER BY id DESC LIMIT 1").fetchone()["id"]
        doc_id = conn.execute("SELECT id FROM policy_documents ORDER BY id DESC LIMIT 1").fetchone()["id"]
        cur = conn.execute(
            """INSERT INTO rule_findings(
                job_id, document_id, rule_code, category, severity, review_status,
                detail, page_value, body_value, evidence, created_at,
                machine_status, human_status, rule_version, confidence,
                allow_customer_output, evidence_json
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                int(job_id), int(doc_id), rule_code, "test", "medium", "pending",
                "测试详情", page_value, body_value, evidence, now,
                "suspected", "unreviewed", rule_version, 0.0, 0, evidence_json,
            ),
        )
        return int(cur.lastrowid)


# ---------- 测试 ----------

class TestEvidenceCompletenessEmpty:
    def test_empty_db_returns_rate_one(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        result = compute_evidence_completeness(db)
        assert result["total_findings"] == 0
        assert result["complete_findings"] == 0
        assert result["incomplete_findings"] == []
        assert result["completeness_rate"] == 1.0
        assert result["empty_database"] is True
        assert result["sample_adequate"] is False
        assert "empty" in (result.get("note") or "").lower()


class TestEvidenceCompletenessComplete:
    def test_complete_finding_yields_rate_one(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        _seed_document(db)
        # PDF 类 finding：必填字段齐全 + evidence_json 含 source_type=pdf。
        _insert_finding(
            db, rule_code="PDF-001",
            evidence_json=json.dumps([
                {"field_name": "PDF 状态", "raw_value": "损坏", "source_type": "pdf"},
            ]),
        )
        result = compute_evidence_completeness(db)
        assert result["total_findings"] == 1
        assert result["complete_findings"] == 1
        assert result["incomplete_findings"] == []
        assert result["completeness_rate"] == 1.0


class TestEvidenceCompletenessMissingBaseFields:
    def test_missing_evidence_field_flagged(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        _seed_document(db)
        # META 类（other 类别）：只测必填字段缺失，不涉及 source_type 要求。
        finding_id = _insert_finding(db, rule_code="META-001", evidence="")
        result = compute_evidence_completeness(db)
        assert result["total_findings"] == 1
        assert result["complete_findings"] == 0
        assert result["completeness_rate"] == 0.0
        incomplete = result["incomplete_findings"]
        assert len(incomplete) == 1
        assert incomplete[0]["finding_id"] == finding_id
        assert incomplete[0]["rule_code"] == "META-001"
        assert "evidence" in incomplete[0]["missing_fields"]

    def test_missing_page_and_body_value_flagged(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        _seed_document(db)
        _insert_finding(db, rule_code="META-001", page_value="", body_value="")
        result = compute_evidence_completeness(db)
        assert result["complete_findings"] == 0
        incomplete = result["incomplete_findings"]
        assert any("page_value_or_body_value" in item["missing_fields"] for item in incomplete)

    def test_missing_rule_version_flagged(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        _seed_document(db)
        _insert_finding(db, rule_code="META-001", rule_version="")
        result = compute_evidence_completeness(db)
        assert result["complete_findings"] == 0
        incomplete = result["incomplete_findings"]
        assert any("rule_version" in item["missing_fields"] for item in incomplete)


class TestEvidenceCompletenessPdfAttCategory:
    def test_pdf_finding_missing_pdf_source_type_flagged(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        _seed_document(db)
        # PDF 类 finding：必填字段齐全，但 evidence_json 没有 source_type=pdf/attachment。
        _insert_finding(
            db, rule_code="PDF-001",
            evidence_json=json.dumps([
                {"field_name": "页面值", "raw_value": "v", "source_type": "page"},
            ]),
        )
        result = compute_evidence_completeness(db)
        assert result["complete_findings"] == 0
        incomplete = result["incomplete_findings"]
        assert len(incomplete) == 1
        assert "pdf_or_attachment_evidence" in incomplete[0]["missing_fields"]

    def test_pdf_finding_with_attachment_source_type_passes(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        _seed_document(db)
        _insert_finding(
            db, rule_code="ATT-001",
            evidence_json=json.dumps([
                {"field_name": "附件", "raw_value": "缺失", "source_type": "attachment"},
            ]),
        )
        result = compute_evidence_completeness(db)
        assert result["complete_findings"] == 1
        assert result["completeness_rate"] == 1.0


class TestEvidenceCompletenessArchDupReconCategory:
    def test_arch_finding_missing_archive_source_type_flagged(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        _seed_document(db)
        # ARCH 类 finding：必填字段齐全，但 evidence_json 缺少 archive_observation/
        # fingerprint/reconciliation。
        _insert_finding(
            db, rule_code="ARCH-001",
            evidence_json=json.dumps([
                {"field_name": "页面值", "raw_value": "v", "source_type": "page"},
            ]),
        )
        result = compute_evidence_completeness(db)
        assert result["complete_findings"] == 0
        incomplete = result["incomplete_findings"]
        assert len(incomplete) == 1
        assert "archive_dup_recon_evidence" in incomplete[0]["missing_fields"]

    def test_arch_finding_with_fingerprint_source_type_passes(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        _seed_document(db)
        _insert_finding(
            db, rule_code="ARCH-002",
            evidence_json=json.dumps([
                {"field_name": "指纹", "raw_value": "abc", "source_type": "fingerprint"},
            ]),
        )
        result = compute_evidence_completeness(db)
        assert result["complete_findings"] == 1
        assert result["completeness_rate"] == 1.0

    def test_recon_finding_with_reconciliation_source_type_passes(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        _seed_document(db)
        _insert_finding(
            db, rule_code="RECON-001",
            evidence_json=json.dumps([
                {"field_name": "对账", "raw_value": "匹配", "source_type": "reconciliation"},
            ]),
        )
        result = compute_evidence_completeness(db)
        assert result["complete_findings"] == 1
        assert result["completeness_rate"] == 1.0


class TestEvidenceCompletenessReadMediaInterpRectCategory:
    def test_read_finding_missing_check_source_type_flagged(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        _seed_document(db)
        # READ 类 finding：必填字段齐全，但 evidence_json 缺少 check/media/
        # interpretation/rectification。
        _insert_finding(
            db, rule_code="READ-001",
            evidence_json=json.dumps([
                {"field_name": "页面值", "raw_value": "v", "source_type": "page"},
            ]),
        )
        result = compute_evidence_completeness(db)
        assert result["complete_findings"] == 0
        incomplete = result["incomplete_findings"]
        assert len(incomplete) == 1
        assert "read_media_interp_rect_evidence" in incomplete[0]["missing_fields"]

    def test_media_finding_with_media_source_type_passes(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        _seed_document(db)
        _insert_finding(
            db, rule_code="MEDIA-001",
            evidence_json=json.dumps([
                {"field_name": "图片", "raw_value": "缺失", "source_type": "media"},
            ]),
        )
        result = compute_evidence_completeness(db)
        assert result["complete_findings"] == 1
        assert result["completeness_rate"] == 1.0

    def test_interp_finding_with_interpretation_source_type_passes(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        _seed_document(db)
        _insert_finding(
            db, rule_code="INTERP-001",
            evidence_json=json.dumps([
                {"field_name": "解读", "raw_value": "失效", "source_type": "interpretation"},
            ]),
        )
        result = compute_evidence_completeness(db)
        assert result["complete_findings"] == 1
        assert result["completeness_rate"] == 1.0

    def test_rect_finding_with_rectification_source_type_passes(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        _seed_document(db)
        _insert_finding(
            db, rule_code="RECT-001",
            evidence_json=json.dumps([
                {"field_name": "整改", "raw_value": "未指定", "source_type": "rectification"},
            ]),
        )
        result = compute_evidence_completeness(db)
        assert result["complete_findings"] == 1
        assert result["completeness_rate"] == 1.0


class TestEvidenceCompletenessGate:
    def test_all_complete_yields_rate_one_gate(self, tmp_path: Path):
        """门禁：所有 finding 完整时 completeness_rate == 1.0。"""
        db = _setup_db(tmp_path)
        _seed_document(db)
        # 跨类别构造 5 条完整 finding。
        _insert_finding(
            db, rule_code="META-001",
            evidence_json="[]",  # other 类别，仅需必填字段
        )
        _insert_finding(
            db, rule_code="PDF-001",
            evidence_json=json.dumps([
                {"field_name": "PDF", "raw_value": "损坏", "source_type": "pdf"},
            ]),
        )
        _insert_finding(
            db, rule_code="ATT-002",
            evidence_json=json.dumps([
                {"field_name": "附件", "raw_value": "不匹配", "source_type": "attachment"},
            ]),
        )
        _insert_finding(
            db, rule_code="ARCH-001",
            evidence_json=json.dumps([
                {"field_name": "观察", "raw_value": "缺失", "source_type": "archive_observation"},
            ]),
        )
        _insert_finding(
            db, rule_code="READ-002",
            evidence_json=json.dumps([
                {"field_name": "阅办", "raw_value": "异常", "source_type": "check"},
            ]),
        )
        result = compute_evidence_completeness(db)
        assert result["total_findings"] == 5
        assert result["complete_findings"] == 5
        assert result["incomplete_findings"] == []
        assert result["completeness_rate"] == 1.0
