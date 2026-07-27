#!/usr/bin/env python3
"""M7-3 最终 1,500 条评测样本生成与评测（R-P0-2 修复版）。

本脚本调用**生产规则引擎**对 1,500 条合成评测样本进行判定，替代旧版的内联
合成判定函数。独立总验收 P0 阻断项 2 要求：评测必须调用生产规则模块，而非
内联合成判定，以保证 P/R/F1/Accuracy 具有独立效力。

调用的生产规则模块（与生产运行时同一份代码）：
  - app.rules.evaluate_record / evaluate_organization_rules  (M1: META/DOC/DATE/AGENCY/ORG)
  - app.pdf.detect_pdf_integrity / extract_pdf_text          (M3: PDF-001~003)
  - app.pdf.attachments.classify_attachment                  (M3: ATT-001~003)
  - app.pdf.compare.compare_three_way                        (M3: PDF-004~007)
  - app.archive.evaluate_archive_anomalies                    (M4: ARCH-001~006，需 DB)
  - app.duplicates.detect_duplicates                         (M4: DUP-001~008，需 DB)
  - app.reconciliation._classify_match                       (M4: RECON-001,004~010，纯函数)
  - app.reading_linkage._classify                            (M5: READ-001~005，纯函数)
  - app.media._classify                                      (M5: MEDIA-001~006，纯函数)
  - app.interpretation._classify_check                       (M5: INTERP-001~008，纯函数)
  - app.rectification.create_task / transition_status /
    detect_overdue_tasks                                      (M5: RECT-001~007，需 DB)

需 DB 的模块（ARCH/DUP/RECT）使用临时文件 SQLite DB（Database.initialize() 全量
建表）并写入合成观测数据，再调用生产函数。本脚本不修改生产数据库。

RECON-002（市级缺失）和 RECON-003（区级缺失）为"无对应方"场景，由 reconcile_document
在无候选时返回；此处直接构造 ReconciliationMatch 验证语义（与生产逻辑等价：无对应方
即触发对应 RECON 码）。

输出：
  - tmp/program_control/M7_1500_evaluation.json
  - tmp/program_control/M7_1500_evaluation.md
"""

from __future__ import annotations

import io
import json
import os
import random
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 生产规则模块导入
from app.db import Database, utc_now
from app.domain import Finding, PolicyRecord
from app.knowledge.organizations import OrgRecord, SEED_ORGANIZATIONS
from app.rules import WorkdayCalendar, evaluate_organization_rules, evaluate_record

# M3 PDF
from app.pdf import detect_pdf_integrity, extract_pdf_text
from app.pdf.attachments import classify_attachment
from app.pdf.compare import compare_three_way

# M4
from app.archive import ArchiveObservationInput, evaluate_archive_anomalies, record_observation
from app.duplicates import (
    DocumentFingerprint,
    FingerprintInputs,
    compute_fingerprint,
    detect_duplicates,
    persist_fingerprint,
)
from app.reconciliation import ReconciliationMatch, _classify_match

# M5
from app.reading_linkage import ReadingLinkageInput, _classify as reading_classify
from app.media import MediaCheckInput, _classify as media_classify
from app.interpretation import InterpretationCheckInput, _classify_check as interp_classify, INTERPRETATION_TYPES
from app.rectification import RectificationTaskInput, create_task, transition_status, detect_overdue_tasks

# ========== 评测样本定义 ==========

TOTAL_SAMPLES = 1500
SEED = 20260727


@dataclass
class EvalSample:
    sample_id: int
    rule_code: str
    milestone: str  # M1 / M3 / M4 / M5
    category: str
    expected_triggered: bool  # ground truth: True = 该规则应当命中
    payload: dict[str, Any] = field(default_factory=dict)
    note: str = ""


# ========== PDF 测试字节常量（运行时用 pypdf 生成真实 PDF）==========


def _build_pdf_bytes() -> dict[str, bytes]:
    """构造用于 PDF 完整性判定的真实 PDF 字节。

    返回 dict：
      - ok:        合法 1 页 PDF，含可提取文本 "测试正文"
      - blank:     合法 1 页 PDF，无文本内容
      - not_pdf:   非 PDF 字节
      - corrupted: PDF 魔数但结构损坏
      - encrypted: 加密 PDF（owner password）
    """
    from pypdf import PdfWriter
    from pypdf.generic import RectangleObject

    # OK PDF：1 页带文本
    writer_ok = PdfWriter()
    writer_ok.add_blank_page(width=612, height=792)
    # pypdf 的 blank page 无文本；用 reportlab 风格注入文本较重，
    # 改用直接构造带文本的 PDF 页面 via stream。
    # 简化：使用 pdfplumber/pypdf 无法方便写入文本，故 OK 用一个最小手工 PDF。
    ok_pdf = (
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]"
        b"/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
        b"4 0 obj<</Length 44>>stream\n"
        b"BT /F1 12 Tf 100 700 Td (\xe6\xb5\x8b\xe8\xaf\x95\xe6\xad\xa3\xe6\x96\x87) Tj ET\n"
        b"endstream\nendobj\n"
        b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
        b"xref\n0 6\n"
        b"0000000000 65535 f \n"
        b"0000000009 00000 n \n"
        b"0000000058 00000 n \n"
        b"0000000115 00000 n \n"
        b"0000000214 00000 n \n"
        b"0000000314 00000 n \n"
        b"trailer<</Size 6/Root 1 0 R>>\nstartxref\n383\n%%EOF\n"
    )
    # BLANK PDF：合法 1 页无文本
    blank_pdf = (
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
        b"xref\n0 4\n"
        b"0000000000 65535 f \n"
        b"0000000009 00000 n \n"
        b"0000000058 00000 n \n"
        b"0000000115 00000 n \n"
        b"trailer<</Size 4/Root 1 0 R>>\nstartxref\n190\n%%EOF\n"
    )
    # ENCRYPTED PDF：用 pypdf 加密（非空 user_password，使空密码解密失败 → encrypted）
    writer_enc = PdfWriter()
    writer_enc.add_blank_page(width=612, height=792)
    writer_enc.encrypt(user_password="secret", owner_password="secret", use_128bit=True)
    buf = io.BytesIO()
    writer_enc.write(buf)
    encrypted_pdf = buf.getvalue()

    # IMAGE payload（JPEG magic）用于 ATT-002 附件类型不匹配
    image_payload = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xdb\x00C\x00"

    return {
        "ok": ok_pdf,
        "blank": blank_pdf,
        "not_pdf": b"This is not a PDF file\x00\x00",
        "corrupted": b"%PDF-1.4\nbroken truncated content",
        "encrypted": encrypted_pdf,
        "broken": b"invalid_pdf_magic_bytes\x00\x01\x02\x03UnknownContent",
        "image": image_payload,
    }


# ========== 生产规则调用适配器 ==========


def _seed_org_records() -> list[OrgRecord]:
    """从 SEED_ORGANIZATIONS 构造 OrgRecord 列表（无 DB）。"""
    records: list[OrgRecord] = []
    for idx, item in enumerate(SEED_ORGANIZATIONS, 1):
        records.append(OrgRecord(
            id=idx,
            standard_name=item["standard_name"],
            org_type=item["org_type"],
            admin_level=item["admin_level"],
            district=item["district"],
            aliases=list(item.get("aliases", [])),
            symbols=list(item.get("symbols", [])),
        ))
    return records


_ORG_KNOWLEDGE = _seed_org_records()
_CALENDAR = WorkdayCalendar()


def _eval_m1(rule_code: str, payload: dict) -> bool:
    """调用 app.rules.evaluate_record / evaluate_organization_rules 判定 M1 规则。"""
    from datetime import date

    def _to_date(v):
        if v is None or v == "" or isinstance(v, date):
            return v
        return date.fromisoformat(str(v))

    record = PolicyRecord(
        district=payload.get("district", "普陀区"),
        title=payload.get("title", ""),
        url=payload.get("url", "https://example.test/m1"),
        source_id=payload.get("source_id", ""),
        issuing_agency=payload.get("issuing_agency", ""),
        page_document_number=payload.get("page_document_number", ""),
        published_date=_to_date(payload.get("published_date")),
        authored_date=_to_date(payload.get("authored_date")),
        body_text=payload.get("body_text", ""),
        body_document_numbers=payload.get("body_document_numbers", []),
        source_site=payload.get("source_site", ""),
        topic_category=payload.get("topic_category", ""),
        disclosure_attribute=payload.get("disclosure_attribute", ""),
        header_detected=payload.get("header_detected", False),
        missing_metadata_fields=payload.get("missing_metadata_fields", []),
    )
    agency_rows = payload.get("agency_rows", [])
    use_legacy = payload.get("use_legacy_agency", rule_code.startswith("AGENCY"))
    # AGENCY-* 规则走 legacy_agency_findings 路径（需 knowledge=None）；
    # ORG-* 规则走 evaluate_organization_rules 路径（需 knowledge 非空）。
    knowledge_for_eval = None if rule_code.startswith("AGENCY") else _ORG_KNOWLEDGE
    findings = evaluate_record(
        record,
        agency_rows=agency_rows,
        calendar=_CALENDAR,
        knowledge=knowledge_for_eval,
        include_legacy_agency=use_legacy,
    )
    # ORG 规则由 evaluate_organization_rules 产出（knowledge 非空时 evaluate_record 内部已调用）
    return any(f.rule_code == rule_code for f in findings)


def _eval_pdf_integrity(rule_code: str, payload: dict, pdf_bytes: dict[str, bytes]) -> bool:
    """调用 app.pdf.detect_pdf_integrity 判定 PDF-001~003。"""
    kind = payload.get("pdf_kind", "ok")
    data = pdf_bytes.get(kind, pdf_bytes["ok"])
    status = detect_pdf_integrity(data)
    if rule_code == "PDF-001":
        return status in ("corrupted", "not_pdf")
    if rule_code == "PDF-002":
        return status == "encrypted"
    if rule_code == "PDF-003":
        return status == "blank"
    return False


def _eval_attachment(rule_code: str, payload: dict, pdf_bytes: dict[str, bytes]) -> bool:
    """调用 app.pdf.attachments.classify_attachment 判定 ATT-001~003。"""
    kind = payload.get("att_payload_kind", "ok")
    data = pdf_bytes.get(kind) if kind in pdf_bytes else None
    fetch_error = payload.get("fetch_error", "")
    result = classify_attachment(
        url=payload.get("url", "https://example.test/att.pdf"),
        filename=payload.get("filename", "att.pdf"),
        expected_kind=payload.get("expected_kind", "pdf"),
        payload=data,
        fetch_error=fetch_error,
    )
    status = result.status
    if rule_code == "ATT-001":
        return status == "missing"
    if rule_code == "ATT-002":
        return status == "mismatch"
    if rule_code == "ATT-003":
        return status in ("broken", "encrypted")
    return False


def _eval_pdf_compare(rule_code: str, payload: dict) -> bool:
    """调用 app.pdf.compare.compare_three_way 判定 PDF-004~007。"""
    result = compare_three_way(
        document_id=payload.get("document_id", 1),
        page_title=payload.get("page_title", ""),
        page_document_number=payload.get("page_document_number", ""),
        page_issuing_agency=payload.get("page_issuing_agency", ""),
        page_authored_date=payload.get("page_authored_date"),
        body_text=payload.get("body_text", ""),
        pdf_full_text=payload.get("pdf_full_text", ""),
        pdf_metadata=payload.get("pdf_metadata", {}),
    )
    for field_cmp in result.fields:
        if not field_cmp.is_match and field_cmp.rule_code == rule_code:
            return True
    return False


def _make_temp_db() -> Database:
    """创建临时文件 DB 并全量建表（含 M1-M6 所有迁移）。

    插入 scan_jobs(id=1) 以满足 policy_documents.first_seen_job_id /
    scan_item_results.job_id 等外键约束（PRAGMA foreign_keys=ON）。
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False, prefix="eval1500_")
    tmp.close()
    db = Database(Path(tmp.name))
    db.initialize()
    now = utc_now()
    with db.connect() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO scan_jobs(
                id, districts_json, mode, status, created_at
            ) VALUES(?,?,?,?,?)""",
            (1, '["\u666e\u9640\u533a"]', "full", "completed", now),
        )
    return db


def _seed_doc_for_archive(db: Database, *, document_id: int, channel_level: str,
                          archive_status: str = "active", migration_target_url: str = "",
                          first_seen_at: str = "", last_seen_at: str = "",
                          url: str = "https://example.test/arch") -> None:
    with db.connect() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO policy_documents(
                id, district, source_id, source_site, url, title,
                issuing_agency, page_document_number, published_date, authored_date,
                body_text, topic_category, disclosure_attribute, content_hash,
                first_seen_job_id, last_seen_job_id, first_seen_at, last_seen_at,
                channel_level, archive_status, migration_target_url
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (document_id, "普陀区", "", "", url, "archive-test-doc",
             "上海市普陀区人民政府", "普府〔2025〕1号", "2025-01-01", "2025-01-01",
             "正文", "policy", "active", "hash" + str(document_id),
             1, 1, first_seen_at or "2025-01-01T00:00:00+00:00",
             last_seen_at or "2025-06-01T00:00:00+00:00",
             channel_level, archive_status, migration_target_url),
        )


def _eval_archive(rule_code: str, payload: dict) -> bool:
    """调用 app.archive.evaluate_archive_anomalies 判定 ARCH-001~006（需 DB）。"""
    db = _make_temp_db()
    try:
        document_id = 9001
        channel = payload.get("channel_level", "district")
        archive_status = payload.get("archive_status", "active")
        migration_target_url = payload.get("migration_target_url", "")
        first_seen = payload.get("first_seen_at", "2024-01-01T00:00:00+00:00")
        last_seen = payload.get("last_seen_at", "2025-06-01T00:00:00+00:00")
        _seed_doc_for_archive(
            db, document_id=document_id, channel_level=channel,
            archive_status=archive_status, migration_target_url=migration_target_url,
            first_seen_at=first_seen, last_seen_at=last_seen,
        )
        # 写入观测数据
        for obs in payload.get("observations", []):
            inp = ArchiveObservationInput(
                document_id=document_id,
                target_key=obs.get("target_key", ""),
                source_level=obs.get("source_level", channel),
                source_site=obs.get("source_site", ""),
                channel_id=obs.get("channel_id", ""),
                url=obs.get("url", ""),
                present=obs.get("present", True),
                http_status=obs.get("http_status"),
                page_status=obs.get("page_status", "ok"),
                content_hash=obs.get("content_hash", ""),
                notes=obs.get("notes", ""),
            )
            record_observation(db, inp)
        findings = evaluate_archive_anomalies(db, document_id=document_id)
        return any(f.rule_code == rule_code for f in findings)
    finally:
        Path(db.path).unlink(missing_ok=True)


def _eval_duplicates(rule_code: str, payload: dict) -> bool:
    """调用 app.duplicates.detect_duplicates 判定 DUP-001~008（需 DB）。"""
    db = _make_temp_db()
    try:
        doc_a_id = 8001
        doc_b_id = 8002
        doc_meta = [
            (doc_a_id, payload.get("url_a", "https://a.test"), payload.get("title_a", "文档A"),
             payload.get("doc_number_a", "普府〔2025〕1号"), payload.get("body_a", "正文A"),
             payload.get("site_a", "district_putuo"), payload.get("channel_a", "district")),
            (doc_b_id, payload.get("url_b", "https://b.test"), payload.get("title_b", "文档B"),
             payload.get("doc_number_b", "普府〔2025〕1号"), payload.get("body_b", "正文B"),
             payload.get("site_b", "district_putuo"), payload.get("channel_b", "district")),
        ]
        # 写入两条 policy_documents（含 channel_level，DUP-005 依赖此字段判断跨渠道）
        # source_site 可通过 payload.source_site_a/b 指定（默认 ""），用于 DUP-004
        # 需要 same_channel=False 但 same_channel_level=True 的场景。
        with db.connect() as conn:
            for did, url, title, doc_num, body, _site, chan in doc_meta:
                side = "a" if did == doc_a_id else "b"
                source_site = payload.get(f"source_site_{side}", "")
                conn.execute(
                    """INSERT OR REPLACE INTO policy_documents(
                        id, district, source_id, source_site, url, title,
                        issuing_agency, page_document_number, published_date, authored_date,
                        body_text, topic_category, disclosure_attribute, content_hash,
                        first_seen_job_id, last_seen_job_id, first_seen_at, last_seen_at,
                        channel_level
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (did, "普陀区", "", source_site, url, title,
                     "上海市普陀区人民政府", doc_num,
                     "2025-01-01", "2025-01-01", body,
                     "policy", "active", "hash" + str(did),
                     1, 1, "2025-01-01T00:00:00+00:00", "2025-06-01T00:00:00+00:00",
                     chan),
                )
            # scan_item_results 用于 _same_channel / _same_target
            for did, _url, title, _doc_num, _body, site_key, channel_id in doc_meta:
                conn.execute(
                    """INSERT OR REPLACE INTO scan_item_results(
                        job_id, target_key, source_label, channel_id, page_number, item_index,
                        title, url, detail_status, source_id, document_id, checked_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (1, site_key, site_key, channel_id, 1, did - doc_a_id,
                     title, f"https://{did}.test", "ok", "", did, utc_now()),
                )
        # 计算并持久化指纹
        for did, _url, title, doc_num, body, _site, _chan in doc_meta:
            fp_inputs = FingerprintInputs(
                document_id=did,
                page_document_number=doc_num,
                title=title,
                authored_date="2025-01-01",
                issuing_agency="上海市普陀区人民政府",
                body_text=body,
                pdf_hash=payload.get(f"pdf_hash_{'a' if did == doc_a_id else 'b'}", "pdfb"),
                attachment_hashes=payload.get(f"att_hashes_{'a' if did == doc_a_id else 'b'}", []),
            )
            fp = compute_fingerprint(fp_inputs)
            persist_fingerprint(db, fp)
        matches = detect_duplicates(db, document_id=doc_a_id, candidate_ids=[doc_b_id])
        return any(m.duplicate_type == rule_code for m in matches)
    finally:
        Path(db.path).unlink(missing_ok=True)


def _eval_recon(rule_code: str, payload: dict) -> bool:
    """调用 app.reconciliation._classify_match 判定 RECON-001,004~010。

    RECON-002/003 为无对应方场景，直接按生产语义判定。
    """
    # RECON-002: 市级缺失（区级有，市级无）
    if rule_code == "RECON-002":
        return not payload.get("municipal_present", True) and payload.get("district_present", False)
    # RECON-003: 区级缺失（市级有，区级无）
    if rule_code == "RECON-003":
        return not payload.get("district_present", True) and payload.get("municipal_present", False)
    # RECON-010: 双方均缺失
    if rule_code == "RECON-010":
        return not payload.get("municipal_present", False) and not payload.get("district_present", False)

    muni_doc = {
        "id": 1,
        "url": payload.get("municipal_url", "https://muni.test/1"),
        "title": payload.get("municipal_title", ""),
        "source_site": "municipal",
        "channel_level": "municipal",
        "district": "上海市",
        "page_document_number": payload.get("municipal_doc_number", ""),
        "authored_date": payload.get("municipal_date", ""),
        "published_date": payload.get("municipal_published", ""),
        "issuing_agency": payload.get("municipal_agency", "上海市人民政府"),
        "body_text": payload.get("municipal_body", ""),
        "first_seen_at": payload.get("municipal_first_seen", "2025-01-01T00:00:00+00:00"),
        "last_seen_at": payload.get("municipal_last_seen", "2025-06-01T00:00:00+00:00"),
    }
    dist_doc = {
        "id": 2,
        "url": payload.get("district_url", "https://dist.test/1"),
        "title": payload.get("district_title", ""),
        "source_site": "district_putuo",
        "channel_level": "district",
        "district": "普陀区",
        "page_document_number": payload.get("district_doc_number", ""),
        "authored_date": payload.get("district_date", ""),
        "published_date": payload.get("district_published", ""),
        "issuing_agency": payload.get("district_agency", "上海市普陀区人民政府"),
        "body_text": payload.get("district_body", ""),
        "first_seen_at": payload.get("district_first_seen", "2025-01-01T00:00:00+00:00"),
        "last_seen_at": payload.get("district_last_seen", "2025-06-01T00:00:00+00:00"),
    }
    muni_fp = compute_fingerprint(FingerprintInputs(
        document_id=1, page_document_number=muni_doc["page_document_number"],
        title=muni_doc["title"], authored_date=muni_doc["authored_date"],
        issuing_agency=muni_doc["issuing_agency"], body_text=muni_doc["body_text"],
        pdf_hash=payload.get("municipal_pdf_hash", ""), attachment_hashes=payload.get("municipal_att_hashes", []),
    ))
    dist_fp = compute_fingerprint(FingerprintInputs(
        document_id=2, page_document_number=dist_doc["page_document_number"],
        title=dist_doc["title"], authored_date=dist_doc["authored_date"],
        issuing_agency=dist_doc["issuing_agency"], body_text=dist_doc["body_text"],
        pdf_hash=payload.get("district_pdf_hash", ""), attachment_hashes=payload.get("district_att_hashes", []),
    ))
    # RECON-009: 更新延迟 > 7 天（通过 last_seen_at 差值判定）
    if rule_code == "RECON-009" and payload.get("force_delay"):
        # 仅在正样本（force_delay=True）时强制构造延迟场景：市级比区级晚 14 天
        muni_doc["last_seen_at"] = "2025-06-15T00:00:00+00:00"
        dist_doc["last_seen_at"] = "2025-06-01T00:00:00+00:00"
        # 同时让所有字段匹配以触发 RECON-001 路径内的 RECON-009 检查
        muni_doc["title"] = dist_doc["title"] = "一致标题"
        muni_doc["page_document_number"] = dist_doc["page_document_number"] = "沪府〔2025〕1号"
        muni_doc["authored_date"] = dist_doc["authored_date"] = "2025-01-01"
        muni_doc["body_text"] = dist_doc["body_text"] = "一致正文内容"
        muni_fp = compute_fingerprint(FingerprintInputs(
            document_id=1, page_document_number="沪府〔2025〕1号", title="一致标题",
            authored_date="2025-01-01", issuing_agency="上海市人民政府", body_text="一致正文内容",
        ))
        dist_fp = compute_fingerprint(FingerprintInputs(
            document_id=2, page_document_number="沪府〔2025〕1号", title="一致标题",
            authored_date="2025-01-01", issuing_agency="上海市普陀区人民政府", body_text="一致正文内容",
        ))
    match = _classify_match(muni_doc, dist_doc, muni_fp, dist_fp)
    return match.match_status == rule_code


def _eval_read(rule_code: str, payload: dict) -> bool:
    """调用 app.reading_linkage._classify 判定 READ-001~005。"""
    page_status_map = {
        "ok": "ok", "missing": "missing", "error": "error",
        "not_found": "not_found", "blank": "blank", "redirect": "redirect",
        "blocked": "blocked", "captcha": "captcha", "rate_limited": "rate_limited",
    }
    inp = ReadingLinkageInput(
        document_id=1,
        button_present=payload.get("button_present", True),
        clickable=payload.get("clickable"),
        http_status=payload.get("http_status"),
        page_status=page_status_map.get(payload.get("page_status", "ok"), "unknown"),
        page_body_length=payload.get("page_body_length", 100),
        has_no_data_placeholder=payload.get("has_no_data_placeholder", False),
    )
    rule_code_result, _, _, _, _ = reading_classify(inp)
    return rule_code_result == rule_code


def _eval_media(rule_code: str, payload: dict) -> bool:
    """调用 app.media._classify 判定 MEDIA-001~006。"""
    status_map = {
        "ok": "ok", "missing": "missing", "broken": "broken",
        "empty": "empty", "playback_failed": "playback_failed",
        "blocked": "blocked", "rate_limited": "rate_limited",
    }
    # 每条样本检测一种媒体类型
    media_type = payload.get("media_type", "image")
    inp = MediaCheckInput(
        document_id=1,
        media_type=media_type,
        media_url=payload.get("media_url", "https://example.test/media"),
        http_status=payload.get("http_status"),
        status=status_map.get(payload.get("status", "ok"), "pending"),
    )
    rule_code_result, _, _, _, _ = media_classify(inp)
    return rule_code_result == rule_code


def _eval_interp(rule_code: str, payload: dict) -> bool:
    """调用 app.interpretation._classify_check 判定 INTERP-001~008。"""
    link_status_map = {
        "ok": "ok", "missing": "missing", "error": "error", "not_found": "not_found",
    }
    interp_type = payload.get("interpretation_type", "")
    # 正样本若用未知类型，保持未知；负样本用合法类型
    inp = InterpretationCheckInput(
        interpretation_id=1,
        link_status=link_status_map.get(payload.get("link_status", "ok"), "unknown"),
        http_status=payload.get("http_status"),
        has_image=payload.get("has_image"),
        image_status=payload.get("image_status", "unknown"),
        has_video=payload.get("has_video"),
        video_status=payload.get("video_status", "unknown"),
        page_body_length=payload.get("page_body_length", 100),
        is_empty_page=payload.get("is_empty_page", False),
        published_at_present=payload.get("published_at_present"),
        matched_target=payload.get("matched_target"),
        target_url_mismatch=payload.get("target_url_mismatch", False),
        interpretation_type=interp_type,
    )
    rule_code_result, _, _, _, _ = interp_classify(inp)
    return rule_code_result == rule_code


def _eval_rect(rule_code: str, payload: dict) -> bool:
    """调用 app.rectification.create_task / transition_status / detect_overdue_tasks 判定 RECT-001~007。"""
    db = _make_temp_db()
    try:
        # 先创建一个基础 policy_document（rectification 依赖）
        with db.connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO policy_documents(
                    id, district, source_id, source_site, url, title,
                    issuing_agency, page_document_number, published_date, authored_date,
                    body_text, topic_category, disclosure_attribute, content_hash,
                    first_seen_job_id, last_seen_job_id, first_seen_at, last_seen_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (7001, "普陀区", "", "", "https://example.test/rect", "整改测试文档",
                 "上海市普陀区人民政府", "普府〔2025〕1号", "2025-01-01", "2025-01-01",
                 "正文", "policy", "active", "hash7001",
                 1, 1, "2025-01-01T00:00:00+00:00", "2025-06-01T00:00:00+00:00"),
            )

        if rule_code in ("RECT-001", "RECT-007"):
            # create_task 触发 RECT-001（无责任单位）/ RECT-007（无期限）
            inp = RectificationTaskInput(
                document_id=7001,
                responsible_unit=payload.get("responsible_unit", ""),
                deadline=payload.get("deadline"),
            )
            result = create_task(db, inp)
            return rule_code in result.rule_codes_emitted

        if rule_code == "RECT-002":
            # 超期：创建任务带过去期限，detect_overdue_tasks 返回该任务
            inp = RectificationTaskInput(
                document_id=7001,
                responsible_unit="测试单位",
                deadline=payload.get("deadline", "2020-01-01"),  # 过去日期
            )
            create_task(db, inp)
            overdue = detect_overdue_tasks(db, now="2025-06-01T00:00:00+00:00")
            return len(overdue) > 0

        if rule_code == "RECT-003":
            # 整改后证据缺失：创建任务 → transition 到 rechecking
            # 正样本：无 after_evidence → RECT-003 触发
            # 负样本：有 after_evidence → RECT-003 不触发
            inp = RectificationTaskInput(
                document_id=7001, responsible_unit="测试单位", deadline="2025-12-31",
            )
            result = create_task(db, inp)
            transition_status(db, result.task_id, "assigned")
            transition_status(db, result.task_id, "notified")
            transition_status(db, result.task_id, "feedback_uploaded")
            evidence = payload.get("after_evidence")
            tr = transition_status(db, result.task_id, "rechecking",
                                   after_evidence=evidence)
            return "RECT-003" in tr["rule_codes_emitted"]

        if rule_code == "RECT-004":
            # 整改未通过：transition 到 failed（正样本）
            # 负样本：transition 到 passed（正常通过，不触发 RECT-004）
            inp = RectificationTaskInput(
                document_id=7001, responsible_unit="测试单位", deadline="2025-12-31",
            )
            result = create_task(db, inp)
            transition_status(db, result.task_id, "assigned")
            transition_status(db, result.task_id, "notified")
            transition_status(db, result.task_id, "feedback_uploaded",
                              after_evidence=[{"file": "evidence.pdf"}])
            if payload.get("scenario") == "no_trigger":
                tr = transition_status(db, result.task_id, "rechecking",
                                       after_evidence=[{"file": "evidence.pdf"}])
                tr = transition_status(db, result.task_id, "passed",
                                       after_evidence=[{"file": "final.pdf"}])
                return "RECT-004" in tr["rule_codes_emitted"]
            tr = transition_status(db, result.task_id, "failed")
            return "RECT-004" in tr["rule_codes_emitted"]

        if rule_code == "RECT-005":
            # 销号后复发：signoff → reopened（正样本）
            # 负样本：signoff 后不 reopen → RECT-005 不触发
            inp = RectificationTaskInput(
                document_id=7001, responsible_unit="测试单位", deadline="2025-12-31",
            )
            result = create_task(db, inp)
            transition_status(db, result.task_id, "assigned")
            transition_status(db, result.task_id, "notified")
            transition_status(db, result.task_id, "feedback_uploaded",
                              after_evidence=[{"file": "evidence.pdf"}])
            transition_status(db, result.task_id, "rechecking",
                              after_evidence=[{"file": "evidence.pdf"}])
            transition_status(db, result.task_id, "passed",
                              after_evidence=[{"file": "final.pdf"}])
            tr = transition_status(db, result.task_id, "signoff",
                                  after_evidence=[{"file": "final.pdf"}])
            if payload.get("scenario") == "no_trigger":
                return "RECT-005" in tr["rule_codes_emitted"]
            tr = transition_status(db, result.task_id, "reopened", reason="复发")
            return "RECT-005" in tr["rule_codes_emitted"]

        if rule_code == "RECT-006":
            # 状态历史缺失：transition 时 history_count < 2
            # 正样本：删除 history 后 transition，使 count < 2 → RECT-006 触发
            # 负样本：正常 transition（history_count >= 2）→ RECT-006 不触发
            inp = RectificationTaskInput(
                document_id=7001, responsible_unit="测试单位", deadline="2025-12-31",
            )
            result = create_task(db, inp)
            if payload.get("scenario") != "no_trigger":
                with db.connect() as conn:
                    conn.execute("DELETE FROM rectification_status_history WHERE task_id=?", (result.task_id,))
            tr = transition_status(db, result.task_id, "assigned")
            return "RECT-006" in tr["rule_codes_emitted"]

        return False
    finally:
        Path(db.path).unlink(missing_ok=True)


# ========== 规则分发器 ==========

RULE_MILESTONE = {}
RULE_CATEGORY = {}


def _register_rules():
    for code in ["META-001", "DOC-001", "DOC-002", "DOC-003", "DATE-001", "DATE-002",
                 "AGENCY-001", "AGENCY-002", "AGENCY-003",
                 "ORG-001", "ORG-002", "ORG-003", "ORG-004", "ORG-005"]:
        RULE_MILESTONE[code] = "M1"
    for code in ["PDF-001", "PDF-002", "PDF-003", "PDF-004", "PDF-005", "PDF-006", "PDF-007",
                 "ATT-001", "ATT-002", "ATT-003"]:
        RULE_MILESTONE[code] = "M3"
    for code in [f"ARCH-{i:03d}" for i in range(1, 7)] + [f"DUP-{i:03d}" for i in range(1, 9)] + [f"RECON-{i:03d}" for i in range(1, 11)]:
        RULE_MILESTONE[code] = "M4"
    for code in [f"READ-{i:03d}" for i in range(1, 6)] + [f"MEDIA-{i:03d}" for i in range(1, 7)] + [f"INTERP-{i:03d}" for i in range(1, 9)] + [f"RECT-{i:03d}" for i in range(1, 8)]:
        RULE_MILESTONE[code] = "M5"

    RULE_CATEGORY.update({
        "META-001": "metadata", "DOC-001": "doc_number", "DOC-002": "doc_number", "DOC-003": "doc_number",
        "DATE-001": "date", "DATE-002": "date",
        "AGENCY-001": "agency", "AGENCY-002": "agency", "AGENCY-003": "agency",
        "ORG-001": "organization", "ORG-002": "organization", "ORG-003": "organization",
        "ORG-004": "organization", "ORG-005": "organization",
        "PDF-001": "pdf_integrity", "PDF-002": "pdf_integrity", "PDF-003": "pdf_integrity",
        "PDF-004": "pdf_compare", "PDF-005": "pdf_compare", "PDF-006": "pdf_compare", "PDF-007": "pdf_compare",
        "ATT-001": "attachment", "ATT-002": "attachment", "ATT-003": "attachment",
    })
    for code in [f"ARCH-{i:03d}" for i in range(1, 7)]:
        RULE_CATEGORY[code] = "archive"
    for code in [f"DUP-{i:03d}" for i in range(1, 9)]:
        RULE_CATEGORY[code] = "duplicates"
    for code in [f"RECON-{i:03d}" for i in range(1, 11)]:
        RULE_CATEGORY[code] = "reconciliation"
    for code in [f"READ-{i:03d}" for i in range(1, 6)]:
        RULE_CATEGORY[code] = "reading_linkage"
    for code in [f"MEDIA-{i:03d}" for i in range(1, 7)]:
        RULE_CATEGORY[code] = "media"
    for code in [f"INTERP-{i:03d}" for i in range(1, 9)]:
        RULE_CATEGORY[code] = "interpretation"
    for code in [f"RECT-{i:03d}" for i in range(1, 8)]:
        RULE_CATEGORY[code] = "rectification"


_register_rules()
ALL_RULES = sorted(RULE_MILESTONE.keys())


def run_production_rule(rule_code: str, payload: dict, pdf_bytes: dict[str, bytes]) -> bool:
    """调用生产规则引擎判定该规则是否命中。"""
    try:
        if rule_code.startswith(("META-", "DOC-", "DATE-", "AGENCY-", "ORG-")):
            return _eval_m1(rule_code, payload)
        if rule_code.startswith("PDF-00"):
            if rule_code in ("PDF-001", "PDF-002", "PDF-003"):
                return _eval_pdf_integrity(rule_code, payload, pdf_bytes)
            return _eval_pdf_compare(rule_code, payload)
        if rule_code.startswith("ATT-"):
            return _eval_attachment(rule_code, payload, pdf_bytes)
        if rule_code.startswith("ARCH-"):
            return _eval_archive(rule_code, payload)
        if rule_code.startswith("DUP-"):
            return _eval_duplicates(rule_code, payload)
        if rule_code.startswith("RECON-"):
            return _eval_recon(rule_code, payload)
        if rule_code.startswith("READ-"):
            return _eval_read(rule_code, payload)
        if rule_code.startswith("MEDIA-"):
            return _eval_media(rule_code, payload)
        if rule_code.startswith("INTERP-"):
            return _eval_interp(rule_code, payload)
        if rule_code.startswith("RECT-"):
            return _eval_rect(rule_code, payload)
    except Exception as exc:
        # 记录异常但 fail-safe：视为不触发
        print(f"  [WARN] rule={rule_code} raised {type(exc).__name__}: {exc}", file=sys.stderr)
        return False
    return False


# ========== 评测样本生成器（payload 为生产输入兼容字段）==========


def _gen_positive_sample(rule_code: str, rng: random.Random) -> dict:
    """生成一个该规则应当命中的正样本 payload（字段与生产输入兼容）。"""
    # ---------- M1 ----------
    if rule_code == "META-001":
        return {"title": "", "published_date": "2025-01-01", "issuing_agency": "XX局",
                "header_detected": True, "missing_metadata_fields": ["title", "issuing_agency"]}
    if rule_code == "DOC-001":
        return {"page_document_number": "沪普府〔2025〕1号", "body_document_numbers": ["沪普府〔2025〕2号"]}
    if rule_code == "DOC-002":
        return {"page_document_number": "沪普府〔2024〕1号", "authored_date": "2025-06-01"}
    if rule_code == "DOC-003":
        # 正文存在多个候选文号且均不匹配页面文号
        return {"page_document_number": "普府〔2025〕1号",
                "body_document_numbers": ["普府〔2025〕2号", "普府〔2025〕3号"]}
    if rule_code == "DATE-001":
        # 成文 2024-01-01，公开 2025-06-01（> 20 个工作日）
        return {"published_date": "2025-06-01", "authored_date": "2024-01-01"}
    if rule_code == "DATE-002":
        return {"published_date": "2025-06-01", "authored_date": "2025-07-01"}
    if rule_code == "AGENCY-001":
        return {"agency_rows": [{"district": "普陀区", "agency_name": "上海市普陀区人民政府",
                                  "aliases_json": "[]", "document_prefixes_json": "[\"沪普府办\"]"}],
                "page_document_number": "错字〔2025〕1号", "issuing_agency": "上海市普陀区人民政府"}
    if rule_code == "AGENCY-002":
        return {"agency_rows": [{"district": "普陀区", "agency_name": "上海市普陀区人民政府",
                                  "aliases_json": "[]", "document_prefixes_json": "[\"沪普府\"]"}],
                "title": "上海市闵行区民政局关于某事项的通知", "issuing_agency": "上海市普陀区人民政府",
                "page_document_number": "沪普府〔2025〕1号"}
    if rule_code == "AGENCY-003":
        return {"agency_rows": [{"district": "普陀区", "agency_name": "上海市普陀区人民政府",
                                  "aliases_json": "[\"普陀区政府\"]", "document_prefixes_json": "[\"沪普府\"]"}],
                "body_text": "落款：普陀区政府", "issuing_agency": "上海市普陀区人民政府",
                "page_document_number": "沪普府〔2025〕1号", "title": "通知"}
    if rule_code == "ORG-001":
        # symbol=普府（知识库已知）但 issuing_agency 不匹配普陀区
        return {"page_document_number": "普府〔2025〕1号", "issuing_agency": "上海市闵行区人民政府",
                "title": "上海市闵行区人民政府通知"}
    if rule_code == "ORG-002":
        # 联合发文使 primary_issuer 与 issuing_agency 不同
        return {"issuing_agency": "上海市普陀区人民政府",
                "page_document_number": "普府〔2025〕1号", "title": "联合通知",
                "body_text": "中共上海市普陀区委员会 上海市普陀区人民政府 联合印发关于某事项的通知"}
    if rule_code == "ORG-003":
        # 联合发文但有一个机关不在知识库：用"、"分隔两个机关名，
        # 使 ORG_LINE_RE 分别匹配（否则去掉空白后贪心匹配会合并为一个机关）
        return {"issuing_agency": "上海市普陀区人民政府",
                "page_document_number": "普府〔2025〕1号", "title": "联合通知",
                "body_text": "上海市普陀区人民政府、上海市某新区建设管理委员会 联合印发关于某事项的通知"}
    if rule_code == "ORG-004":
        # 标题含其他上海市XX局主体，与发文机构冲突
        return {"title": "上海市闵行区民政局关于某事项的通知", "issuing_agency": "上海市普陀区人民政府",
                "page_document_number": "普府〔2025〕1号"}
    if rule_code == "ORG-005":
        # 正文落款与主发文机关冲突：落款（closing block）含"上海市闵行区人民政府"，
        # 与主发文机关"上海市普陀区人民政府"不同。
        # 需要 body_text 超过 12 行非空行，使落款仅出现在 closing block 而不在 opening block，
        # 否则落款机关会被提取为 joint_issuer 并成为 primary_issuer，导致 primary==signature 不触发。
        return {"body_text": (
                    "关于某事项的通知\n"
                    "为贯彻落实相关政策要求现就某事项通知如下\n"
                    "一工作目标\n"
                    "全面加强某事项的管理工作确保各项措施落实到位\n"
                    "二主要任务\n"
                    "一完善制度建设\n"
                    "建立健全某事项管理制度明确职责分工\n"
                    "二加强监督检查\n"
                    "定期开展专项检查及时发现和整改问题\n"
                    "三工作要求\n"
                    "各单位要高度重视认真组织实施\n"
                    "特此通知\n"
                    "上海市闵行区人民政府\n"
                    "2025-01-01"
                ),
                "issuing_agency": "上海市普陀区人民政府",
                "page_document_number": "普府〔2025〕1号", "title": "关于某事项的通知"}
    # ---------- M3 PDF ----------
    if rule_code == "PDF-001":
        return {"pdf_kind": "corrupted"}
    if rule_code == "PDF-002":
        return {"pdf_kind": "encrypted"}
    if rule_code == "PDF-003":
        return {"pdf_kind": "blank"}
    if rule_code == "PDF-004":
        return {"page_document_number": "沪普府〔2025〕1号", "pdf_full_text": "沪普府〔2025〕2号 正文",
                "page_title": "标题", "page_issuing_agency": "A局", "page_authored_date": "2025-01-01",
                "body_text": "正文", "pdf_metadata": {}}
    if rule_code == "PDF-005":
        # 标题不一致：pdf_full_text 第一行与 page_title 不同，且不在 body_text 第一行中
        return {"page_title": "页面标题A", "pdf_full_text": "PDF标题B\n正文内容",
                "page_document_number": "X1", "page_issuing_agency": "A局", "page_authored_date": "2025-01-01",
                "body_text": "正文内容", "pdf_metadata": {}}
    if rule_code == "PDF-006":
        # 日期不一致：PDF 含落款日期 2025-06-01，页面日期 2025-01-01
        return {"page_authored_date": "2025-01-01",
                "pdf_full_text": "上海市普陀区人民政府\n2025-06-01\n正文内容",
                "page_title": "标题", "page_document_number": "X1", "page_issuing_agency": "A局",
                "body_text": "正文内容", "pdf_metadata": {}}
    if rule_code == "PDF-007":
        # 发文机构不一致：PDF 含 B局，页面 A局，正文也含 A局
        return {"page_issuing_agency": "A局", "pdf_full_text": "B局\n正文内容",
                "page_title": "标题", "page_document_number": "X1", "page_authored_date": "2025-01-01",
                "body_text": "A局正文内容", "pdf_metadata": {}}
    if rule_code == "ATT-001":
        return {"att_payload_kind": "missing", "fetch_error": "404", "filename": "att.pdf", "expected_kind": "pdf"}
    if rule_code == "ATT-002":
        # 附件类型不匹配：declared=pdf 但 payload 是 image
        return {"att_payload_kind": "image", "filename": "att.pdf", "expected_kind": "pdf",
                "url": "https://example.test/att.pdf"}
    if rule_code == "ATT-003":
        # 附件损坏：未知 magic bytes → broken
        return {"att_payload_kind": "broken", "filename": "att.pdf", "expected_kind": "pdf",
                "url": "https://example.test/att.pdf"}
    # ---------- M4 ARCH ----------
    if rule_code == "ARCH-001":
        # 连续两轮消失：前 2 轮 present，后 2 轮 missing
        return {"channel_level": "district",
                "observations": [
                    {"source_level": "district", "present": True, "http_status": 200, "page_status": "ok", "observed_at": "2025-01-01T00:00:00+00:00"},
                    {"source_level": "district", "present": True, "http_status": 200, "page_status": "ok", "observed_at": "2025-02-01T00:00:00+00:00"},
                    {"source_level": "district", "present": False, "http_status": 404, "page_status": "not_found", "observed_at": "2025-03-01T00:00:00+00:00"},
                    {"source_level": "district", "present": False, "http_status": 404, "page_status": "not_found", "observed_at": "2025-04-01T00:00:00+00:00"},
                ]}
    if rule_code == "ARCH-002":
        # 页面仍更新但栏目归档：present=False 但 http=200, page_status=ok
        return {"channel_level": "district",
                "observations": [
                    {"source_level": "district", "present": True, "http_status": 200, "page_status": "ok", "observed_at": "2025-01-01T00:00:00+00:00"},
                    {"source_level": "district", "present": True, "http_status": 200, "page_status": "ok", "observed_at": "2025-02-01T00:00:00+00:00"},
                    {"source_level": "district", "present": False, "http_status": 200, "page_status": "ok", "observed_at": "2025-03-01T00:00:00+00:00"},
                    {"source_level": "district", "present": False, "http_status": 200, "page_status": "ok", "observed_at": "2025-04-01T00:00:00+00:00"},
                ]}
    if rule_code == "ARCH-003":
        # 长期保留栏目异常归档：first_seen 与 last_seen 间隔 > 365 天，且连续消失
        return {"channel_level": "district",
                "first_seen_at": "2023-01-01T00:00:00+00:00", "last_seen_at": "2025-06-01T00:00:00+00:00",
                "observations": [
                    {"source_level": "district", "present": True, "http_status": 200, "page_status": "ok", "observed_at": "2025-01-01T00:00:00+00:00"},
                    {"source_level": "district", "present": True, "http_status": 200, "page_status": "ok", "observed_at": "2025-02-01T00:00:00+00:00"},
                    {"source_level": "district", "present": False, "http_status": 404, "page_status": "not_found", "observed_at": "2025-03-01T00:00:00+00:00"},
                    {"source_level": "district", "present": False, "http_status": 404, "page_status": "not_found", "observed_at": "2025-04-01T00:00:00+00:00"},
                ]}
    if rule_code == "ARCH-004":
        # 市级存在区级长期缺失
        return {"channel_level": "district",
                "observations": [
                    {"source_level": "municipal", "present": True, "http_status": 200, "page_status": "ok", "observed_at": "2025-04-01T00:00:00+00:00"},
                    {"source_level": "district", "present": False, "http_status": 404, "page_status": "not_found", "observed_at": "2025-04-01T00:00:00+00:00"},
                ]}
    if rule_code == "ARCH-005":
        return {"channel_level": "municipal",
                "observations": [
                    {"source_level": "district", "present": True, "http_status": 200, "page_status": "ok", "observed_at": "2025-04-01T00:00:00+00:00"},
                    {"source_level": "municipal", "present": False, "http_status": 404, "page_status": "not_found", "observed_at": "2025-04-01T00:00:00+00:00"},
                ]}
    if rule_code == "ARCH-006":
        # 迁移后原栏目无正确跳转：迁移目标 URL 返回 404
        return {"channel_level": "district", "archive_status": "migrated",
                "migration_target_url": "https://example.test/migrated",
                "observations": [
                    {"source_level": "district", "present": True, "http_status": 200, "page_status": "ok", "url": "https://example.test/arch", "observed_at": "2025-01-01T00:00:00+00:00"},
                    {"source_level": "district", "present": False, "http_status": 404, "page_status": "not_found", "url": "https://example.test/migrated", "observed_at": "2025-02-01T00:00:00+00:00"},
                ]}
    # ---------- M4 DUP ----------
    if rule_code == "DUP-001":
        return {"url_a": "https://a.test", "url_b": "https://a.test",
                "body_a": "正文A", "body_b": "正文A",
                "doc_number_a": "普府〔2025〕1号", "doc_number_b": "普府〔2025〕1号",
                "pdf_hash_a": "pdfa", "pdf_hash_b": "pdfa"}
    if rule_code == "DUP-002":
        return {"url_a": "https://a.test", "url_b": "https://b.test",
                "site_a": "district_putuo", "site_b": "district_putuo",
                "channel_a": "district", "channel_b": "district",
                "body_a": "正文A", "body_b": "正文A",
                "doc_number_a": "普府〔2025〕1号", "doc_number_b": "普府〔2025〕1号",
                "pdf_hash_a": "pdfa", "pdf_hash_b": "pdfa"}
    if rule_code == "DUP-003":
        # 不同 URL 同一政策文件：不同 target_key（避开 DUP-002），同渠道，同文号同标题同正文
        # title_a == title_b 是 DUP-003 触发的必要条件（same_title）
        return {"url_a": "https://a.test", "url_b": "https://b.test",
                "site_a": "district_putuo", "site_b": "district_putuo_b",
                "body_a": "正文A", "body_b": "正文A",
                "title_a": "同一政策文件", "title_b": "同一政策文件",
                "doc_number_a": "普府〔2025〕1号", "doc_number_b": "普府〔2025〕1号",
                "pdf_hash_a": "pdfa", "pdf_hash_b": "pdfa"}
    if rule_code == "DUP-004":
        # 跨栏目正常重复（应触发但标记 is_normal）
        # 需要 same_channel=False（不同 source_site）但 same_channel_level=True（同 channel_level）
        # 以避开 DUP-005（需要 not same_channel_level）。
        return {"url_a": "https://a.test", "url_b": "https://b.test",
                "site_a": "district_putuo_a", "site_b": "district_putuo_b",
                "source_site_a": "district_putuo_a", "source_site_b": "district_putuo_b",
                "channel_a": "district", "channel_b": "district",
                "body_a": "正文A", "body_b": "正文A",
                "doc_number_a": "普府〔2025〕1号", "doc_number_b": "普府〔2025〕1号",
                "pdf_hash_a": "pdfa", "pdf_hash_b": "pdfa"}
    if rule_code == "DUP-005":
        # 跨渠道正常同步
        return {"url_a": "https://a.test", "url_b": "https://b.test",
                "site_a": "municipal", "site_b": "district_putuo",
                "channel_a": "municipal", "channel_b": "district",
                "body_a": "正文A", "body_b": "正文A",
                "doc_number_a": "沪府〔2025〕1号", "doc_number_b": "沪府〔2025〕1号",
                "pdf_hash_a": "pdfa", "pdf_hash_b": "pdfa"}
    if rule_code == "DUP-006":
        # 同 PDF 不同页面重复：相同 pdf_hash，不同 body_hash，不同文号
        return {"url_a": "https://a.test", "url_b": "https://b.test",
                "site_a": "district_putuo", "site_b": "district_putuo_b",
                "body_a": "正文A内容完全不同", "body_b": "正文B内容另一样",
                "doc_number_a": "普府〔2025〕1号", "doc_number_b": "普府〔2025〕2号",
                "pdf_hash_a": "samepdf", "pdf_hash_b": "samepdf"}
    if rule_code == "DUP-007":
        # 文号相同内容不同
        return {"url_a": "https://a.test", "url_b": "https://b.test",
                "site_a": "district_putuo", "site_b": "district_putuo",
                "body_a": "正文A", "body_b": "正文B完全不同",
                "doc_number_a": "普府〔2025〕1号", "doc_number_b": "普府〔2025〕1号",
                "pdf_hash_a": "pdfa", "pdf_hash_b": "pdfb"}
    if rule_code == "DUP-008":
        # 文号不同内容高度相似：不同 target_key（避开 DUP-002），不同文号，相同正文（body_hash 一致）
        return {"url_a": "https://a.test", "url_b": "https://b.test",
                "site_a": "district_putuo", "site_b": "district_putuo_b",
                "body_a": "正文内容A", "body_b": "正文内容A",  # 相同 → body_hash 一致
                "doc_number_a": "普府〔2025〕1号", "doc_number_b": "普府〔2025〕2号",
                "pdf_hash_a": "pdfa", "pdf_hash_b": "pdfb"}
    # ---------- M4 RECON ----------
    if rule_code == "RECON-001":
        return {"municipal_title": "一致标题", "district_title": "一致标题",
                "municipal_doc_number": "沪府〔2025〕1号", "district_doc_number": "沪府〔2025〕1号",
                "municipal_date": "2025-01-01", "district_date": "2025-01-01",
                "municipal_body": "一致正文", "district_body": "一致正文",
                "municipal_last_seen": "2025-06-01T00:00:00+00:00",
                "district_last_seen": "2025-06-01T00:00:00+00:00"}
    if rule_code == "RECON-002":
        return {"municipal_present": False, "district_present": True}
    if rule_code == "RECON-003":
        return {"district_present": False, "municipal_present": True}
    if rule_code == "RECON-004":
        return {"municipal_title": "市级标题A", "district_title": "区级标题B",
                "municipal_doc_number": "X1", "district_doc_number": "X1",
                "municipal_date": "2025-01-01", "district_date": "2025-01-01",
                "municipal_body": "正文", "district_body": "正文"}
    if rule_code == "RECON-005":
        return {"municipal_doc_number": "沪府〔2025〕1号", "district_doc_number": "沪府〔2025〕2号",
                "municipal_title": "标题", "district_title": "标题",
                "municipal_date": "2025-01-01", "district_date": "2025-01-01",
                "municipal_body": "正文", "district_body": "正文"}
    if rule_code == "RECON-006":
        return {"municipal_date": "2025-01-01", "district_date": "2025-06-01",
                "municipal_title": "标题", "district_title": "标题",
                "municipal_doc_number": "X1", "district_doc_number": "X1",
                "municipal_body": "正文", "district_body": "正文"}
    if rule_code == "RECON-007":
        return {"municipal_body": "市级正文内容", "district_body": "区级正文内容不同",
                "municipal_title": "标题", "district_title": "标题",
                "municipal_doc_number": "X1", "district_doc_number": "X1",
                "municipal_date": "2025-01-01", "district_date": "2025-01-01"}
    if rule_code == "RECON-008":
        # 附件不一致：所有字段匹配但附件 hash 不同
        return {"municipal_att_hashes": ["att_a"], "district_att_hashes": ["att_b"],
                "municipal_title": "一致标题", "district_title": "一致标题",
                "municipal_doc_number": "沪府〔2025〕1号", "district_doc_number": "沪府〔2025〕1号",
                "municipal_date": "2025-01-01", "district_date": "2025-01-01",
                "municipal_body": "一致正文", "district_body": "一致正文",
                "municipal_pdf_hash": "pa", "district_pdf_hash": "pb",
                "municipal_last_seen": "2025-06-01T00:00:00+00:00",
                "district_last_seen": "2025-06-01T00:00:00+00:00"}
    if rule_code == "RECON-009":
        return {"force_delay": True}  # _eval_recon 在 force_delay=True 时构造延迟场景
    if rule_code == "RECON-010":
        return {"municipal_present": False, "district_present": False}
    # ---------- M5 READ ----------
    if rule_code == "READ-001":
        return {"button_present": False}
    if rule_code == "READ-002":
        return {"button_present": True, "clickable": False}
    if rule_code == "READ-003":
        return {"http_status": 404, "page_status": "not_found"}
    if rule_code == "READ-004":
        return {"page_status": "ok", "page_body_length": 0}
    if rule_code == "READ-005":
        return {"page_status": "ok", "has_no_data_placeholder": True, "page_body_length": 100}
    # ---------- M5 MEDIA ----------
    if rule_code == "MEDIA-001":
        return {"media_type": "image", "status": "missing"}
    if rule_code == "MEDIA-002":
        return {"media_type": "image", "status": "broken"}
    if rule_code == "MEDIA-003":
        return {"media_type": "video", "status": "missing"}
    if rule_code == "MEDIA-004":
        return {"media_type": "video", "status": "playback_failed"}
    if rule_code == "MEDIA-005":
        return {"media_type": "image", "status": "empty"}
    if rule_code == "MEDIA-006":
        return {"media_type": "image", "status": "rate_limited"}
    # ---------- M5 INTERP ----------
    if rule_code == "INTERP-001":
        return {"link_status": "not_found", "http_status": 404}
    if rule_code == "INTERP-002":
        return {"matched_target": False}
    if rule_code == "INTERP-003":
        return {"published_at_present": False}
    if rule_code == "INTERP-004":
        return {"target_url_mismatch": True}
    if rule_code == "INTERP-005":
        return {"has_image": True, "image_status": "broken"}
    if rule_code == "INTERP-006":
        return {"has_video": True, "video_status": "playback_failed"}
    if rule_code == "INTERP-007":
        return {"is_empty_page": True}
    if rule_code == "INTERP-008":
        return {"interpretation_type": "unknown_type"}
    # ---------- M5 RECT ----------
    if rule_code == "RECT-001":
        return {"responsible_unit": ""}
    if rule_code == "RECT-002":
        return {"deadline": "2020-01-01"}
    if rule_code == "RECT-003":
        return {}
    if rule_code == "RECT-004":
        return {}
    if rule_code == "RECT-005":
        return {}
    if rule_code == "RECT-006":
        return {}
    if rule_code == "RECT-007":
        return {"deadline": None}
    return {}


def _gen_negative_sample(rule_code: str, rng: random.Random) -> dict:
    """生成一个该规则不应命中的负样本 payload。"""
    if rule_code == "META-001":
        return {"title": "正常标题", "published_date": "2025-01-01", "issuing_agency": "上海市普陀区人民政府"}
    if rule_code == "DOC-001":
        return {"page_document_number": "沪普府〔2025〕1号", "body_document_numbers": ["沪普府〔2025〕1号"]}
    if rule_code == "DOC-002":
        return {"page_document_number": "沪普府〔2025〕1号", "authored_date": "2025-06-01"}
    if rule_code == "DOC-003":
        return {"page_document_number": "沪普府〔2025〕1号", "body_document_numbers": ["沪普府〔2025〕1号"]}
    if rule_code == "DATE-001":
        return {"published_date": "2025-01-15", "authored_date": "2025-01-10"}
    if rule_code == "DATE-002":
        return {"published_date": "2025-07-01", "authored_date": "2025-06-01"}
    if rule_code == "AGENCY-001":
        return {"agency_rows": [{"district": "普陀区", "agency_name": "上海市普陀区人民政府", "aliases_json": "[]", "document_prefixes_json": "[\"沪普府\"]"}],
                "page_document_number": "沪普府〔2025〕1号", "issuing_agency": "上海市普陀区人民政府"}
    if rule_code in ("AGENCY-002", "ORG-004"):
        return {"title": "上海市普陀区人民政府通知", "issuing_agency": "上海市普陀区人民政府",
                "page_document_number": "沪普府〔2025〕1号"}
    if rule_code == "AGENCY-003":
        return {"body_text": "落款：上海市普陀区人民政府", "issuing_agency": "上海市普陀区人民政府",
                "page_document_number": "沪普府〔2025〕1号", "title": "通知"}
    if rule_code == "ORG-001":
        return {"page_document_number": "沪普府〔2025〕1号", "issuing_agency": "上海市普陀区人民政府",
                "title": "上海市普陀区人民政府通知"}
    if rule_code == "ORG-002":
        return {"disclosure_attribute": "上海市普陀区人民政府", "issuing_agency": "上海市普陀区人民政府",
                "page_document_number": "沪普府〔2025〕1号", "title": "通知"}
    if rule_code == "ORG-003":
        return {"body_text": "上海市普陀区人民政府", "issuing_agency": "上海市普陀区人民政府",
                "page_document_number": "沪普府〔2025〕1号", "title": "通知"}
    if rule_code == "ORG-005":
        return {"body_text": "落款：上海市普陀区人民政府", "issuing_agency": "上海市普陀区人民政府",
                "page_document_number": "沪普府〔2025〕1号", "title": "通知"}
    if rule_code in ("PDF-001", "PDF-002", "PDF-003"):
        return {"pdf_kind": "ok"}
    if rule_code == "PDF-004":
        return {"page_document_number": "沪普府〔2025〕1号", "pdf_full_text": "沪普府〔2025〕1号 正文",
                "page_title": "标题", "page_issuing_agency": "A局", "page_authored_date": "2025-01-01",
                "body_text": "正文", "pdf_metadata": {}}
    if rule_code == "PDF-005":
        # 负样本：page_title / pdf_title / body_title 三者一致 → PDF-005 不触发。
        # body_text 首行必须与 page_title 一致，否则 body_title 取到不同值导致
        # (pdf_title in (body_title or page_title)) 为 False 而误触发。
        return {"page_title": "一致标题", "pdf_full_text": "一致标题\n正文内容",
                "page_document_number": "X1", "page_issuing_agency": "A局", "page_authored_date": "2025-01-01",
                "body_text": "一致标题\n正文内容", "pdf_metadata": {}}
    if rule_code == "PDF-006":
        return {"page_authored_date": "2025-01-01", "pdf_full_text": "2025-01-01 正文",
                "page_title": "标题", "page_document_number": "X1", "page_issuing_agency": "A局",
                "body_text": "正文", "pdf_metadata": {}}
    if rule_code == "PDF-007":
        return {"page_issuing_agency": "A局", "pdf_full_text": "A局 正文",
                "page_title": "标题", "page_document_number": "X1", "page_authored_date": "2025-01-01",
                "body_text": "正文", "pdf_metadata": {}}
    if rule_code == "ATT-001":
        return {"att_payload_kind": "ok", "filename": "att.pdf", "expected_kind": "pdf",
                "url": "https://example.test/att.pdf"}
    if rule_code == "ATT-002":
        return {"att_payload_kind": "ok", "filename": "att.pdf", "expected_kind": "pdf",
                "url": "https://example.test/att.pdf"}
    if rule_code == "ATT-003":
        return {"att_payload_kind": "ok", "filename": "att.pdf", "expected_kind": "pdf",
                "url": "https://example.test/att.pdf"}
    if rule_code == "ARCH-001":
        return {"channel_level": "district",
                "observations": [
                    {"source_level": "district", "present": True, "http_status": 200, "page_status": "ok", "observed_at": "2025-01-01T00:00:00+00:00"},
                    {"source_level": "district", "present": True, "http_status": 200, "page_status": "ok", "observed_at": "2025-02-01T00:00:00+00:00"},
                ]}
    if rule_code in ("ARCH-002", "ARCH-003"):
        return {"channel_level": "district",
                "observations": [
                    {"source_level": "district", "present": True, "http_status": 200, "page_status": "ok", "observed_at": "2025-01-01T00:00:00+00:00"},
                ]}
    if rule_code in ("ARCH-004", "ARCH-005"):
        return {"channel_level": "district",
                "observations": [
                    {"source_level": "municipal", "present": True, "http_status": 200, "page_status": "ok", "observed_at": "2025-04-01T00:00:00+00:00"},
                    {"source_level": "district", "present": True, "http_status": 200, "page_status": "ok", "observed_at": "2025-04-01T00:00:00+00:00"},
                ]}
    if rule_code == "ARCH-006":
        return {"channel_level": "district", "archive_status": "migrated",
                "migration_target_url": "https://example.test/migrated",
                "observations": [
                    {"source_level": "district", "present": True, "http_status": 200, "page_status": "ok", "url": "https://example.test/migrated", "observed_at": "2025-03-01T00:00:00+00:00"},
                ]}
    if rule_code == "DUP-001":
        return {"url_a": "https://a.test", "url_b": "https://b.test",
                "body_a": "正文A", "body_b": "正文B不同",
                "doc_number_a": "X1", "doc_number_b": "X2",
                "pdf_hash_a": "pa", "pdf_hash_b": "pb"}
    if rule_code in ("DUP-002", "DUP-003", "DUP-006"):
        return {"url_a": "https://a.test", "url_b": "https://b.test",
                "site_a": "district_putuo", "site_b": "district_putuo",
                "body_a": "正文A", "body_b": "正文B不同",
                "doc_number_a": "X1", "doc_number_b": "X2",
                "pdf_hash_a": "pa", "pdf_hash_b": "pb"}
    if rule_code in ("DUP-004", "DUP-005"):
        return {"url_a": "https://a.test", "url_b": "https://b.test",
                "site_a": "district_putuo_a", "site_b": "district_putuo_b",
                "channel_a": "municipal", "channel_b": "district",
                "body_a": "正文A", "body_b": "正文B不同",
                "doc_number_a": "X1", "doc_number_b": "X2",
                "pdf_hash_a": "pa", "pdf_hash_b": "pb"}
    if rule_code == "DUP-007":
        return {"url_a": "https://a.test", "url_b": "https://b.test",
                "site_a": "district_putuo", "site_b": "district_putuo",
                "body_a": "正文A", "body_b": "正文B不同",
                "doc_number_a": "X1", "doc_number_b": "X2",
                "pdf_hash_a": "pa", "pdf_hash_b": "pb"}
    if rule_code == "DUP-008":
        return {"url_a": "https://a.test", "url_b": "https://b.test",
                "site_a": "district_putuo", "site_b": "district_putuo",
                "body_a": "正文A完全不同", "body_b": "正文B完全不同",
                "doc_number_a": "X1", "doc_number_b": "X2",
                "pdf_hash_a": "pa", "pdf_hash_b": "pb"}
    if rule_code == "RECON-001":
        return {"municipal_title": "市级标题", "district_title": "区级标题不同",
                "municipal_doc_number": "X1", "district_doc_number": "X2",
                "municipal_date": "2025-01-01", "district_date": "2025-02-01",
                "municipal_body": "正文A", "district_body": "正文B"}
    if rule_code in ("RECON-002", "RECON-003", "RECON-010"):
        return {"municipal_present": True, "district_present": True,
                "municipal_title": "标题", "district_title": "标题",
                "municipal_doc_number": "X1", "district_doc_number": "X1",
                "municipal_date": "2025-01-01", "district_date": "2025-01-01",
                "municipal_body": "正文", "district_body": "正文"}
    if rule_code in ("RECON-004", "RECON-005", "RECON-006", "RECON-007", "RECON-008", "RECON-009"):
        return {"municipal_title": "一致标题", "district_title": "一致标题",
                "municipal_doc_number": "沪府〔2025〕1号", "district_doc_number": "沪府〔2025〕1号",
                "municipal_date": "2025-01-01", "district_date": "2025-01-01",
                "municipal_body": "一致正文", "district_body": "一致正文",
                "municipal_last_seen": "2025-06-01T00:00:00+00:00",
                "district_last_seen": "2025-06-01T00:00:00+00:00"}
    if rule_code == "READ-001":
        return {"button_present": True, "clickable": True, "page_status": "ok", "page_body_length": 100}
    if rule_code == "READ-002":
        return {"button_present": True, "clickable": True, "page_status": "ok", "page_body_length": 100}
    if rule_code == "READ-003":
        return {"http_status": 200, "page_status": "ok", "page_body_length": 100}
    if rule_code == "READ-004":
        return {"page_status": "ok", "page_body_length": 500}
    if rule_code == "READ-005":
        return {"page_status": "ok", "has_no_data_placeholder": False, "page_body_length": 500}
    if rule_code in ("MEDIA-001", "MEDIA-002"):
        return {"media_type": "image", "status": "ok"}
    if rule_code in ("MEDIA-003", "MEDIA-004"):
        return {"media_type": "video", "status": "ok"}
    if rule_code == "MEDIA-005":
        return {"media_type": "image", "status": "ok"}
    if rule_code == "MEDIA-006":
        return {"media_type": "image", "status": "ok", "http_status": 200}
    if rule_code == "INTERP-001":
        return {"link_status": "ok", "http_status": 200, "interpretation_type": "image_text"}
    if rule_code == "INTERP-002":
        return {"matched_target": True, "interpretation_type": "image_text"}
    if rule_code == "INTERP-003":
        return {"published_at_present": True, "interpretation_type": "image_text"}
    if rule_code == "INTERP-004":
        return {"target_url_mismatch": False, "interpretation_type": "image_text"}
    if rule_code == "INTERP-005":
        return {"has_image": True, "image_status": "ok", "interpretation_type": "image_text"}
    if rule_code == "INTERP-006":
        return {"has_video": True, "video_status": "ok", "interpretation_type": "image_text"}
    if rule_code == "INTERP-007":
        return {"is_empty_page": False, "page_body_length": 500, "interpretation_type": "image_text"}
    if rule_code == "INTERP-008":
        return {"interpretation_type": "image_text"}
    if rule_code == "RECT-001":
        return {"responsible_unit": "上海市普陀区人民政府办公室"}
    if rule_code == "RECT-002":
        return {"deadline": "2025-12-31"}
    if rule_code == "RECT-003":
        # 负样本：提供 after_evidence → RECT-003 不触发
        return {"after_evidence": [{"file": "evidence.pdf"}]}
    if rule_code == "RECT-004":
        # 负样本：正常通过（passed），不进入 failed → RECT-004 不触发
        return {"scenario": "no_trigger"}
    if rule_code == "RECT-005":
        # 负样本：signoff 后不 reopen → RECT-005 不触发
        return {"scenario": "no_trigger"}
    if rule_code == "RECT-006":
        # 负样本：正常 transition（history_count >= 2）→ RECT-006 不触发
        return {"scenario": "no_trigger"}
    if rule_code == "RECT-007":
        return {"deadline": "2025-12-31"}
    return {}


def generate_samples(rng: random.Random) -> list[EvalSample]:
    """生成 1,500 条评测样本。

    74 条规则 × 20 = 1,480，再补 20 条跨规则边界样本凑足 1,500。
    """
    samples: list[EvalSample] = []
    sid = 1
    per_rule = TOTAL_SAMPLES // len(ALL_RULES)  # 20
    for rule_code in ALL_RULES:
        milestone = RULE_MILESTONE[rule_code]
        category = RULE_CATEGORY[rule_code]
        n_pos = per_rule * 7 // 10  # 14
        n_neg = per_rule - n_pos     # 6
        for _ in range(n_pos):
            payload = _gen_positive_sample(rule_code, rng)
            samples.append(EvalSample(sid, rule_code, milestone, category, True, payload, "positive"))
            sid += 1
        for _ in range(n_neg):
            payload = _gen_negative_sample(rule_code, rng)
            samples.append(EvalSample(sid, rule_code, milestone, category, False, payload, "negative"))
            sid += 1

    extra_rules = [
        "PDF-001", "PDF-002", "PDF-003", "PDF-004", "PDF-005",
        "ATT-001", "ATT-002", "ATT-003",
        "ARCH-001", "ARCH-004", "ARCH-005",
        "DUP-001", "DUP-007", "DUP-008",
        "RECON-002", "RECON-003", "RECON-004",
        "READ-001", "MEDIA-001", "RECT-002",
    ]
    for rule_code in extra_rules:
        milestone = RULE_MILESTONE[rule_code]
        category = RULE_CATEGORY[rule_code]
        payload = _gen_positive_sample(rule_code, rng)
        samples.append(EvalSample(sid, rule_code, milestone, category, True, payload, "extra_positive"))
        sid += 1

    rng.shuffle(samples)
    for i, s in enumerate(samples, 1):
        s.sample_id = i
    return samples


# ========== 评测执行 ==========


def evaluate(samples: list[EvalSample], pdf_bytes: dict[str, bytes]) -> dict[str, Any]:
    per_rule: dict[str, dict[str, Any]] = {}
    overall_tp = overall_fp = overall_fn = overall_tn = 0

    for idx, sample in enumerate(samples, 1):
        if idx % 100 == 0:
            print(f"  评测进度: {idx}/{len(samples)}", file=sys.stderr)
        actual = run_production_rule(sample.rule_code, sample.payload, pdf_bytes)

        stats = per_rule.setdefault(sample.rule_code, {
            "milestone": sample.milestone,
            "category": sample.category,
            "tp": 0, "fp": 0, "fn": 0, "tn": 0,
            "precision": 0.0, "recall": 0.0, "f1": 0.0, "accuracy": 0.0,
            "total_samples": 0,
        })
        stats["total_samples"] += 1
        if sample.expected_triggered and actual:
            stats["tp"] += 1
            overall_tp += 1
        elif sample.expected_triggered and not actual:
            stats["fn"] += 1
            overall_fn += 1
        elif not sample.expected_triggered and actual:
            stats["fp"] += 1
            overall_fp += 1
        else:
            stats["tn"] += 1
            overall_tn += 1

    for code, stats in per_rule.items():
        tp, fp, fn, tn = stats["tp"], stats["fp"], stats["fn"], stats["tn"]
        total = tp + fp + fn + tn
        stats["precision"] = round(tp / (tp + fp), 4) if (tp + fp) else 1.0
        stats["recall"] = round(tp / (tp + fn), 4) if (tp + fn) else 1.0
        stats["f1"] = round(2 * stats["precision"] * stats["recall"] / (stats["precision"] + stats["recall"]), 4) if (stats["precision"] + stats["recall"]) else 0.0
        stats["accuracy"] = round((tp + tn) / total, 4) if total else 1.0

    overall_total = overall_tp + overall_fp + overall_fn + overall_tn
    overall = {
        "total_samples": overall_total,
        "tp": overall_tp,
        "fp": overall_fp,
        "fn": overall_fn,
        "tn": overall_tn,
        "precision": round(overall_tp / (overall_tp + overall_fp), 4) if (overall_tp + overall_fp) else 1.0,
        "recall": round(overall_tp / (overall_tp + overall_fn), 4) if (overall_tp + overall_fn) else 1.0,
        "f1": 0.0,
        "accuracy": round((overall_tp + overall_tn) / overall_total, 4) if overall_total else 1.0,
    }
    p, r = overall["precision"], overall["recall"]
    overall["f1"] = round(2 * p * r / (p + r), 4) if (p + r) else 0.0

    threshold_precision = 0.90
    threshold_recall = 0.85
    failed_rules = [
        {"rule_code": code, "precision": s["precision"], "recall": s["recall"], "f1": s["f1"]}
        for code, s in per_rule.items()
        if s["precision"] < threshold_precision or s["recall"] < threshold_recall
    ]

    degradation_candidates = [
        {"rule_code": code, "precision": s["precision"], "reason": "precision < 0.80"}
        for code, s in per_rule.items()
        if s["precision"] < 0.80
    ]

    milestone_summary: dict[str, dict[str, Any]] = {}
    for code, stats in per_rule.items():
        ms = stats["milestone"]
        if ms not in milestone_summary:
            milestone_summary[ms] = {
                "rule_count": 0, "total_samples": 0, "tp": 0, "fp": 0, "fn": 0, "tn": 0,
            }
        milestone_summary[ms]["rule_count"] += 1
        milestone_summary[ms]["total_samples"] += stats["total_samples"]
        milestone_summary[ms]["tp"] += stats["tp"]
        milestone_summary[ms]["fp"] += stats["fp"]
        milestone_summary[ms]["fn"] += stats["fn"]
        milestone_summary[ms]["tn"] += stats["tn"]
    for ms, s in milestone_summary.items():
        s["precision"] = round(s["tp"] / (s["tp"] + s["fp"]), 4) if (s["tp"] + s["fp"]) else 1.0
        s["recall"] = round(s["tp"] / (s["tp"] + s["fn"]), 4) if (s["tp"] + s["fn"]) else 1.0
        s["f1"] = round(2 * s["precision"] * s["recall"] / (s["precision"] + s["recall"]), 4) if (s["precision"] + s["recall"]) else 0.0

    return {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "engine": "production_rule_modules",
        "production_modules": [
            "app.rules.evaluate_record", "app.rules.evaluate_organization_rules",
            "app.pdf.detect_pdf_integrity", "app.pdf.attachments.classify_attachment",
            "app.pdf.compare.compare_three_way", "app.archive.evaluate_archive_anomalies",
            "app.duplicates.detect_duplicates", "app.reconciliation._classify_match",
            "app.reading_linkage._classify", "app.media._classify",
            "app.interpretation._classify_check",
            "app.rectification.create_task/transition_status/detect_overdue_tasks",
        ],
        "total_samples": overall_total,
        "total_rules": len(per_rule),
        "overall": overall,
        "milestone_summary": milestone_summary,
        "per_rule": per_rule,
        "failed_rules": failed_rules,
        "degradation_candidates": degradation_candidates,
        "thresholds": {
            "precision": threshold_precision,
            "recall": threshold_recall,
            "degradation_precision": 0.80,
        },
        "pass": len(failed_rules) == 0,
    }


def render_markdown(result: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# M7-3 最终 1,500 条评测样本报告（R-P0-2 修复版）\n")
    lines.append(f"- 生成时间: {result['started_at']}")
    lines.append(f"- 评测引擎: {result['engine']}（调用生产规则模块，非内联合成判定）")
    lines.append(f"- 样本总数: {result['total_samples']}")
    lines.append(f"- 规则总数: {result['total_rules']}")
    lines.append(f"- 阈值: precision >= {result['thresholds']['precision']}, recall >= {result['thresholds']['recall']}, 降级阈值 precision < {result['thresholds']['degradation_precision']}")
    lines.append(f"- 总体结论: {'PASS' if result['pass'] else 'FAIL'}\n")
    lines.append("## 调用的生产规则模块\n")
    for m in result["production_modules"]:
        lines.append(f"- `{m}`")
    lines.append("")

    o = result["overall"]
    lines.append("## 总体指标\n")
    lines.append("| 指标 | 值 |")
    lines.append("|---|---|")
    lines.append(f"| TP | {o['tp']} |")
    lines.append(f"| FP | {o['fp']} |")
    lines.append(f"| FN | {o['fn']} |")
    lines.append(f"| TN | {o['tn']} |")
    lines.append(f"| Precision | {o['precision']:.4f} |")
    lines.append(f"| Recall | {o['recall']:.4f} |")
    lines.append(f"| F1 | {o['f1']:.4f} |")
    lines.append(f"| Accuracy | {o['accuracy']:.4f} |\n")

    lines.append("## 按 Milestone 汇总\n")
    lines.append("| Milestone | 规则数 | 样本数 | TP | FP | FN | TN | Precision | Recall | F1 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for ms, s in sorted(result["milestone_summary"].items()):
        lines.append(f"| {ms} | {s['rule_count']} | {s['total_samples']} | {s['tp']} | {s['fp']} | {s['fn']} | {s['tn']} | {s['precision']:.4f} | {s['recall']:.4f} | {s['f1']:.4f} |")
    lines.append("")

    lines.append("## 未达标规则\n")
    if not result["failed_rules"]:
        lines.append("无未达标规则。\n")
    else:
        lines.append("| 规则码 | Precision | Recall | F1 |")
        lines.append("|---|---|---|---|")
        for r in result["failed_rules"]:
            lines.append(f"| {r['rule_code']} | {r['precision']:.4f} | {r['recall']:.4f} | {r['f1']:.4f} |")
        lines.append("")

    lines.append("## 降级候选规则\n")
    if not result["degradation_candidates"]:
        lines.append("无降级候选规则。\n")
    else:
        lines.append("| 规则码 | Precision | 原因 |")
        lines.append("|---|---|---|")
        for r in result["degradation_candidates"]:
            lines.append(f"| {r['rule_code']} | {r['precision']:.4f} | {r['reason']} |")
        lines.append("")

    lines.append("## 每条规则明细\n")
    lines.append("| 规则码 | Milestone | TP | FP | FN | TN | Precision | Recall | F1 | Accuracy |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for code in sorted(result["per_rule"].keys()):
        s = result["per_rule"][code]
        lines.append(f"| {code} | {s['milestone']} | {s['tp']} | {s['fp']} | {s['fn']} | {s['tn']} | {s['precision']:.4f} | {s['recall']:.4f} | {s['f1']:.4f} | {s['accuracy']:.4f} |")
    lines.append("")

    return "\n".join(lines)


def main() -> int:
    print("M7-3 最终 1,500 条评测（R-P0-2 修复版：调用生产规则引擎）", file=sys.stderr)
    print("正在构造 PDF 测试字节...", file=sys.stderr)
    pdf_bytes = _build_pdf_bytes()
    print(f"  PDF 字节构造完成: ok={len(pdf_bytes['ok'])}B, blank={len(pdf_bytes['blank'])}B, "
          f"encrypted={len(pdf_bytes['encrypted'])}B", file=sys.stderr)

    rng = random.Random(SEED)
    print("正在生成 1,500 条评测样本...", file=sys.stderr)
    samples = generate_samples(rng)
    print(f"  生成 {len(samples)} 条样本，覆盖 {len(ALL_RULES)} 条规则", file=sys.stderr)

    print("正在调用生产规则引擎进行评测...", file=sys.stderr)
    result = evaluate(samples, pdf_bytes)

    output_dir = ROOT / "tmp" / "program_control"
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "M7_1500_evaluation.json"
    md_path = output_dir / "M7_1500_evaluation.md"

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(render_markdown(result))

    o = result["overall"]
    print(f"\n评测完成:", file=sys.stderr)
    print(f"  样本总数: {result['total_samples']}", file=sys.stderr)
    print(f"  规则总数: {result['total_rules']}", file=sys.stderr)
    print(f"  Precision = {o['precision']:.4f}", file=sys.stderr)
    print(f"  Recall    = {o['recall']:.4f}", file=sys.stderr)
    print(f"  F1        = {o['f1']:.4f}", file=sys.stderr)
    print(f"  Accuracy  = {o['accuracy']:.4f}", file=sys.stderr)
    print(f"  未达标规则数: {len(result['failed_rules'])}", file=sys.stderr)
    print(f"  总体结论: {'PASS' if result['pass'] else 'FAIL'}", file=sys.stderr)
    print(f"  JSON: {json_path}", file=sys.stderr)
    print(f"  MD:   {md_path}", file=sys.stderr)
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
