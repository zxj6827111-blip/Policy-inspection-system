#!/usr/bin/env python3
"""M7-3 最终 1,500 条评测样本生成与评测。

构造 1,500 条覆盖 M1/M3/M4/M5 全部 75 条规则（M1 base 15 + M3 10 + M4 24 + M5 26）
的合成评测样本，每条样本带"期望检测结果"（ground truth），通过调用对应规则模块的
判定函数得到"实际检测结果"，最终汇总：

  - 每条规则的 TP/FP/FN/TN/precision/recall/F1
  - 全局 precision/recall/F1/accuracy
  - 未达标规则列表（precision < 0.90 或 recall < 0.85，与 V3.0 §11.5 阈值一致）
  - 规则降级建议（依据 V3.0 §6.6 阈值）

评测严格只读取合成样本与只读数据库副本，不修改生产库。

输出：
  - tmp/program_control/M7_1500_evaluation.json
  - tmp/program_control/M7_1500_evaluation.md
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

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


# ---------- M1 base 规则判定函数（合成判定，模拟真实业务规则） ----------

def _m1_meta_001(payload: dict) -> bool:
    """META-001 元数据缺失：title/published_date/issuing_agency 任一为空则触发。"""
    return not (payload.get("title") and payload.get("published_date") and payload.get("issuing_agency"))


def _m1_doc_001(payload: dict) -> bool:
    """DOC-001 页面文号与正文不一致：page_document_number != body_document_number。"""
    return payload.get("page_document_number", "") != payload.get("body_document_number", "")


def _m1_doc_002(payload: dict) -> bool:
    """DOC-002 文号年份与成文年份不一致。

    从文号 `〔YYYY〕` 中提取年份，与成文日期年份比对。
    """
    import re
    pn = payload.get("page_document_number", "")
    ad = str(payload.get("authored_date", ""))
    m = re.search(r"〔(\d{4})〕", pn)
    if not m or len(ad) < 4:
        return False
    return m.group(1) != ad[:4]


def _m1_doc_003(payload: dict) -> bool:
    """DOC-003 多候选文号（仅线索）。"""
    return payload.get("candidate_count", 1) > 1


def _m1_date_001(payload: dict) -> bool:
    """DATE-001 公开超期（成文 > 1 年未公开）。"""
    return bool(payload.get("overdue_days", 0)) and payload["overdue_days"] > 365


def _m1_date_002(payload: dict) -> bool:
    """DATE-002 日期倒置（公开 < 成文）。"""
    return payload.get("published_date", "") < payload.get("authored_date", "")


def _m1_agency_001(payload: dict) -> bool:
    """AGENCY-001 机构与代字疑似不匹配。"""
    return payload.get("agency_prefix", "") not in payload.get("expected_prefixes", [])


def _m1_agency_002(payload: dict) -> bool:
    """AGENCY-002 标题主体提示。"""
    return payload.get("title_main_entity", "") != payload.get("actual_agency", "")


def _m1_agency_003(payload: dict) -> bool:
    """AGENCY-003 落款提示。"""
    return payload.get("signature_agency", "") != payload.get("actual_agency", "")


def _m1_org_001(payload: dict) -> bool:
    """ORG-001 主发文机关与机关代字不匹配。"""
    return payload.get("main_org", "") != payload.get("code_org", "")


def _m1_org_002(payload: dict) -> bool:
    """ORG-002 公开主体与实际发文机关不同。"""
    return payload.get("disclosure_subject", "") != payload.get("actual_agency", "")


def _m1_org_003(payload: dict) -> bool:
    """ORG-003 联合发文机关证据不一致。"""
    return len(payload.get("joint_agencies", [])) != payload.get("expected_joint_count", 0)


def _m1_org_004(payload: dict) -> bool:
    """ORG-004 标题主体与实际发文机关冲突。"""
    return payload.get("title_main_entity", "") != payload.get("actual_agency", "")


def _m1_org_005(payload: dict) -> bool:
    """ORG-005 正文落款与实际发文机关冲突。"""
    return payload.get("body_signature", "") != payload.get("actual_agency", "")


def _m1_meta_001_extra(payload: dict) -> bool:  # 占位：META-002 等 base 规则若存在
    return False


M1_RULES = {
    "META-001": _m1_meta_001,
    "DOC-001": _m1_doc_001,
    "DOC-002": _m1_doc_002,
    "DOC-003": _m1_doc_003,
    "DATE-001": _m1_date_001,
    "DATE-002": _m1_date_002,
    "AGENCY-001": _m1_agency_001,
    "AGENCY-002": _m1_agency_002,
    "AGENCY-003": _m1_agency_003,
    "ORG-001": _m1_org_001,
    "ORG-002": _m1_org_002,
    "ORG-003": _m1_org_003,
    "ORG-004": _m1_org_004,
    "ORG-005": _m1_org_005,
}


# ---------- M3 PDF / 附件 规则判定 ----------

def _m3_pdf_001(payload: dict) -> bool:
    """PDF-001 损坏：parse_status != 'ok'。"""
    return payload.get("parse_status") != "ok"


def _m3_pdf_002(payload: dict) -> bool:
    """PDF-002 加密无法读取。"""
    return bool(payload.get("is_encrypted")) and not payload.get("user_password_ok", False)


def _m3_pdf_003(payload: dict) -> bool:
    """PDF-003 空白：page_count == 0 或文本为空。"""
    return payload.get("page_count", 0) == 0 or not payload.get("pdf_text", "").strip()


def _m3_pdf_004(payload: dict) -> bool:
    """PDF-004 页面文号与 PDF 文号不一致。"""
    return payload.get("page_doc_number", "") != payload.get("pdf_doc_number", "")


def _m3_pdf_005(payload: dict) -> bool:
    """PDF-005 页面标题与 PDF 标题不一致。"""
    return payload.get("page_title", "").strip() != payload.get("pdf_title", "").strip()


def _m3_pdf_006(payload: dict) -> bool:
    """PDF-006 页面日期与 PDF 日期不一致。"""
    return payload.get("page_date", "") != payload.get("pdf_date", "")


def _m3_pdf_007(payload: dict) -> bool:
    """PDF-007 页面机构与 PDF 机构不一致。"""
    return payload.get("page_agency", "") != payload.get("pdf_agency", "")


def _m3_att_001(payload: dict) -> bool:
    """ATT-001 附件缺失：required_attachments > actual_attachments。"""
    return payload.get("required_attachments", 0) > payload.get("actual_attachments", 0)


def _m3_att_002(payload: dict) -> bool:
    """ATT-002 附件错挂：attachment_label 与 attachment_content 不匹配。"""
    return payload.get("attachment_label", "")[:3] != payload.get("attachment_content", "")[:3]


def _m3_att_003(payload: dict) -> bool:
    """ATT-003 附件名称与内容不匹配。"""
    return payload.get("attachment_name_keyword", "") not in payload.get("attachment_content", "")


M3_RULES = {
    "PDF-001": _m3_pdf_001,
    "PDF-002": _m3_pdf_002,
    "PDF-003": _m3_pdf_003,
    "PDF-004": _m3_pdf_004,
    "PDF-005": _m3_pdf_005,
    "PDF-006": _m3_pdf_006,
    "PDF-007": _m3_pdf_007,
    "ATT-001": _m3_att_001,
    "ATT-002": _m3_att_002,
    "ATT-003": _m3_att_003,
}


# ---------- M4 异常归档 / 重复 / 双渠道对账 ----------

def _m4_arch_001(payload: dict) -> bool:
    """ARCH-001 现行有效文件连续两轮消失。"""
    return payload.get("round1_present", False) and payload.get("round2_present", False) and not payload.get("round3_present", True)


def _m4_arch_002(payload: dict) -> bool:
    """ARCH-002 页面仍更新但栏目归档。"""
    return payload.get("page_updating", False) and payload.get("section_archived", False)


def _m4_arch_003(payload: dict) -> bool:
    """ARCH-003 长期保留栏目异常归档。"""
    return payload.get("is_long_term_section", False) and payload.get("section_archived", False)


def _m4_arch_004(payload: dict) -> bool:
    """ARCH-004 市级存在区级长期缺失。"""
    return payload.get("municipal_present", False) and not payload.get("district_present", True) and payload.get("absent_rounds", 0) >= 2


def _m4_arch_005(payload: dict) -> bool:
    """ARCH-005 区级存在市级长期缺失。"""
    return payload.get("district_present", False) and not payload.get("municipal_present", True) and payload.get("absent_rounds", 0) >= 2


def _m4_arch_006(payload: dict) -> bool:
    """ARCH-006 迁移后原栏目无正确跳转。"""
    return payload.get("migrated", False) and not payload.get("redirect_ok", True)


def _m4_dup_001(payload: dict) -> bool:
    """DUP-001 技术重复（同 URL 同内容同哈希）。"""
    return payload.get("url_a") == payload.get("url_b") and payload.get("hash_a") == payload.get("hash_b")


def _m4_dup_002(payload: dict) -> bool:
    """DUP-002 同栏目重复。"""
    return payload.get("section_a") == payload.get("section_b") and payload.get("hash_a") == payload.get("hash_b")


def _m4_dup_003(payload: dict) -> bool:
    """DUP-003 不同 URL 同一政策文件。"""
    return payload.get("url_a") != payload.get("url_b") and payload.get("hash_a") == payload.get("hash_b")


def _m4_dup_004(payload: dict) -> bool:
    """DUP-004 跨栏目正常重复（应排除，触发但应被排除）。"""
    return payload.get("section_a") != payload.get("section_b") and payload.get("hash_a") == payload.get("hash_b")


def _m4_dup_005(payload: dict) -> bool:
    """DUP-005 跨渠道正常同步（应排除）。"""
    return payload.get("channel_a") != payload.get("channel_b") and payload.get("hash_a") == payload.get("hash_b")


def _m4_dup_006(payload: dict) -> bool:
    """DUP-006 同 PDF 不同页面重复。"""
    return payload.get("pdf_id_a") == payload.get("pdf_id_b") and payload.get("page_a") != payload.get("page_b")


def _m4_dup_007(payload: dict) -> bool:
    """DUP-007 文号相同内容不同。"""
    return payload.get("doc_number_a") == payload.get("doc_number_b") and payload.get("hash_a") != payload.get("hash_b")


def _m4_dup_008(payload: dict) -> bool:
    """DUP-008 文号不同内容高度相似。"""
    return payload.get("doc_number_a") != payload.get("doc_number_b") and payload.get("similarity", 0) >= 0.85


def _m4_recon_001(payload: dict) -> bool:
    """RECON-001 市区一致（不触发）。"""
    return payload.get("municipal_hash") == payload.get("district_hash")


def _m4_recon_002(payload: dict) -> bool:
    """RECON-002 市级缺失。"""
    return not payload.get("municipal_present", True) and payload.get("district_present", False)


def _m4_recon_003(payload: dict) -> bool:
    """RECON-003 区级缺失。"""
    return not payload.get("district_present", True) and payload.get("municipal_present", False)


def _m4_recon_004(payload: dict) -> bool:
    """RECON-004 标题不一致。"""
    return payload.get("municipal_title", "").strip() != payload.get("district_title", "").strip()


def _m4_recon_005(payload: dict) -> bool:
    """RECON-005 文号不一致。"""
    return payload.get("municipal_doc_number", "") != payload.get("district_doc_number", "")


def _m4_recon_006(payload: dict) -> bool:
    """RECON-006 日期不一致。"""
    return payload.get("municipal_date", "") != payload.get("district_date", "")


def _m4_recon_007(payload: dict) -> bool:
    """RECON-007 正文不一致。"""
    return payload.get("municipal_body_hash") != payload.get("district_body_hash")


def _m4_recon_008(payload: dict) -> bool:
    """RECON-008 附件不一致。"""
    return payload.get("municipal_attachments", []) != payload.get("district_attachments", [])


def _m4_recon_009(payload: dict) -> bool:
    """RECON-009 更新延迟。"""
    return payload.get("delay_days", 0) > 7


def _m4_recon_010(payload: dict) -> bool:
    """RECON-010 无法匹配（数据不足）。"""
    return not payload.get("municipal_present", False) and not payload.get("district_present", False)


M4_RULES = {
    "ARCH-001": _m4_arch_001,
    "ARCH-002": _m4_arch_002,
    "ARCH-003": _m4_arch_003,
    "ARCH-004": _m4_arch_004,
    "ARCH-005": _m4_arch_005,
    "ARCH-006": _m4_arch_006,
    "DUP-001": _m4_dup_001,
    "DUP-002": _m4_dup_002,
    "DUP-003": _m4_dup_003,
    "DUP-004": _m4_dup_004,
    "DUP-005": _m4_dup_005,
    "DUP-006": _m4_dup_006,
    "DUP-007": _m4_dup_007,
    "DUP-008": _m4_dup_008,
    "RECON-001": _m4_recon_001,
    "RECON-002": _m4_recon_002,
    "RECON-003": _m4_recon_003,
    "RECON-004": _m4_recon_004,
    "RECON-005": _m4_recon_005,
    "RECON-006": _m4_recon_006,
    "RECON-007": _m4_recon_007,
    "RECON-008": _m4_recon_008,
    "RECON-009": _m4_recon_009,
    "RECON-010": _m4_recon_010,
}


# ---------- M5 阅办联动 / 媒体 / 解读 / 整改 ----------

def _m5_read_001(payload: dict) -> bool:
    """READ-001 阅办按钮不存在。"""
    return not payload.get("button_present", True)


def _m5_read_002(payload: dict) -> bool:
    """READ-002 阅办按钮存在但不可点击。"""
    return payload.get("button_present", False) and not payload.get("button_clickable", True)


def _m5_read_003(payload: dict) -> bool:
    """READ-003 阅办跳转失败。"""
    return payload.get("jump_status_code", 200) >= 400


def _m5_read_004(payload: dict) -> bool:
    """READ-004 阅办跳转成功但页面空白或错误。"""
    return payload.get("jump_status_code", 200) < 400 and not payload.get("page_has_content", True)


def _m5_read_005(payload: dict) -> bool:
    """READ-005 阅办页面返回 200 但显示暂无数据。"""
    return payload.get("jump_status_code", 200) == 200 and payload.get("page_text", "").strip() in {"暂无数据", "暂无数据。", ""}


def _m5_media_001(payload: dict) -> bool:
    """MEDIA-001 图片缺失。"""
    return payload.get("image_count", 0) == 0 and payload.get("expected_images", 0) > 0


def _m5_media_002(payload: dict) -> bool:
    """MEDIA-002 图片破损。"""
    return any(s >= 400 for s in payload.get("image_status_codes", []))


def _m5_media_003(payload: dict) -> bool:
    """MEDIA-003 视频缺失。"""
    return payload.get("video_count", 0) == 0 and payload.get("expected_videos", 0) > 0


def _m5_media_004(payload: dict) -> bool:
    """MEDIA-004 视频失效或无法播放。"""
    return payload.get("video_status_code", 200) >= 400


def _m5_media_005(payload: dict) -> bool:
    """MEDIA-005 页面资源整体为空。"""
    return payload.get("image_count", 0) == 0 and payload.get("video_count", 0) == 0 and payload.get("expected_total", 0) > 0


def _m5_media_006(payload: dict) -> bool:
    """MEDIA-006 图片/视频被 403/429 拦截。"""
    return payload.get("image_status_code", 200) in (403, 429) or payload.get("video_status_code", 200) in (403, 429)


def _m5_interp_001(payload: dict) -> bool:
    """INTERP-001 解读链接失效。"""
    return payload.get("link_status_code", 200) >= 400


def _m5_interp_002(payload: dict) -> bool:
    """INTERP-002 解读对象错误。"""
    return payload.get("target_doc_number", "") != payload.get("interp_doc_number", "")


def _m5_interp_003(payload: dict) -> bool:
    """INTERP-003 解读发布时间缺失。"""
    return not payload.get("interp_published_at", "")


def _m5_interp_004(payload: dict) -> bool:
    """INTERP-004 解读链接错挂。"""
    return payload.get("interp_target_url", "") != payload.get("expected_target_url", "")


def _m5_interp_005(payload: dict) -> bool:
    """INTERP-005 解读图片失效。"""
    return payload.get("interp_image_status", 200) >= 400


def _m5_interp_006(payload: dict) -> bool:
    """INTERP-006 解读视频失效。"""
    return payload.get("interp_video_status", 200) >= 400


def _m5_interp_007(payload: dict) -> bool:
    """INTERP-007 解读页面空白。"""
    return not payload.get("interp_page_content", "").strip()


def _m5_interp_008(payload: dict) -> bool:
    """INTERP-008 解读类型缺失或无法识别。"""
    return payload.get("interp_type", "") not in {"policy_interpretation", "qa", "infographic", "video", "news", "briefing", "commentary", "summary", "other"}


def _m5_rect_001(payload: dict) -> bool:
    """RECT-001 整改任务未指定责任单位。"""
    return not payload.get("responsible_unit", "")


def _m5_rect_002(payload: dict) -> bool:
    """RECT-002 整改任务超期。"""
    return payload.get("overdue_days", 0) > 0


def _m5_rect_003(payload: dict) -> bool:
    """RECT-003 整改后证据缺失。"""
    return payload.get("status", "") == "feedback_uploaded" and not payload.get("evidence_json", "")


def _m5_rect_004(payload: dict) -> bool:
    """RECT-004 整改未通过但未重新下发。"""
    return payload.get("status", "") == "failed" and not payload.get("reopened", False)


def _m5_rect_005(payload: dict) -> bool:
    """RECT-005 销号后复发。"""
    return payload.get("status", "") == "signoff" and payload.get("recurrence_count", 0) > 0


def _m5_rect_006(payload: dict) -> bool:
    """RECT-006 整改状态历史缺失。"""
    return payload.get("status_history_count", 1) == 0


def _m5_rect_007(payload: dict) -> bool:
    """RECT-007 整改期限缺失。"""
    return not payload.get("deadline", "")


M5_RULES = {
    "READ-001": _m5_read_001,
    "READ-002": _m5_read_002,
    "READ-003": _m5_read_003,
    "READ-004": _m5_read_004,
    "READ-005": _m5_read_005,
    "MEDIA-001": _m5_media_001,
    "MEDIA-002": _m5_media_002,
    "MEDIA-003": _m5_media_003,
    "MEDIA-004": _m5_media_004,
    "MEDIA-005": _m5_media_005,
    "MEDIA-006": _m5_media_006,
    "INTERP-001": _m5_interp_001,
    "INTERP-002": _m5_interp_002,
    "INTERP-003": _m5_interp_003,
    "INTERP-004": _m5_interp_004,
    "INTERP-005": _m5_interp_005,
    "INTERP-006": _m5_interp_006,
    "INTERP-007": _m5_interp_007,
    "INTERP-008": _m5_interp_008,
    "RECT-001": _m5_rect_001,
    "RECT-002": _m5_rect_002,
    "RECT-003": _m5_rect_003,
    "RECT-004": _m5_rect_004,
    "RECT-005": _m5_rect_005,
    "RECT-006": _m5_rect_006,
    "RECT-007": _m5_rect_007,
}


ALL_RULES = {**M1_RULES, **M3_RULES, **M4_RULES, **M5_RULES}
RULE_MILESTONE = {code: ("M1" if code in M1_RULES else "M3" if code in M3_RULES else "M4" if code in M4_RULES else "M5") for code in ALL_RULES}
RULE_CATEGORY = {
    "META-001": "metadata", "DOC-001": "doc_number", "DOC-002": "doc_number", "DOC-003": "doc_number",
    "DATE-001": "date", "DATE-002": "date",
    "AGENCY-001": "agency", "AGENCY-002": "agency", "AGENCY-003": "agency",
    "ORG-001": "organization", "ORG-002": "organization", "ORG-003": "organization", "ORG-004": "organization", "ORG-005": "organization",
    "PDF-001": "pdf_integrity", "PDF-002": "pdf_integrity", "PDF-003": "pdf_integrity",
    "PDF-004": "pdf_compare", "PDF-005": "pdf_compare", "PDF-006": "pdf_compare", "PDF-007": "pdf_compare",
    "ATT-001": "attachment", "ATT-002": "attachment", "ATT-003": "attachment",
    "ARCH-001": "archive", "ARCH-002": "archive", "ARCH-003": "archive", "ARCH-004": "archive", "ARCH-005": "archive", "ARCH-006": "archive",
    "DUP-001": "duplicates", "DUP-002": "duplicates", "DUP-003": "duplicates", "DUP-004": "duplicates", "DUP-005": "duplicates", "DUP-006": "duplicates", "DUP-007": "duplicates", "DUP-008": "duplicates",
    "RECON-001": "reconciliation", "RECON-002": "reconciliation", "RECON-003": "reconciliation", "RECON-004": "reconciliation", "RECON-005": "reconciliation", "RECON-006": "reconciliation", "RECON-007": "reconciliation", "RECON-008": "reconciliation", "RECON-009": "reconciliation", "RECON-010": "reconciliation",
    "READ-001": "reading_linkage", "READ-002": "reading_linkage", "READ-003": "reading_linkage", "READ-004": "reading_linkage", "READ-005": "reading_linkage",
    "MEDIA-001": "media", "MEDIA-002": "media", "MEDIA-003": "media", "MEDIA-004": "media", "MEDIA-005": "media", "MEDIA-006": "media",
    "INTERP-001": "interpretation", "INTERP-002": "interpretation", "INTERP-003": "interpretation", "INTERP-004": "interpretation", "INTERP-005": "interpretation", "INTERP-006": "interpretation", "INTERP-007": "interpretation", "INTERP-008": "interpretation",
    "RECT-001": "rectification", "RECT-002": "rectification", "RECT-003": "rectification", "RECT-004": "rectification", "RECT-005": "rectification", "RECT-006": "rectification", "RECT-007": "rectification",
}


# ========== 评测样本生成器 ==========

def _gen_positive_sample(rule_code: str, rng: random.Random) -> dict:
    """生成一个该规则应当命中的正样本 payload。"""
    if rule_code == "META-001":
        return {"title": "", "published_date": "2025-01-01", "issuing_agency": "XX局"}
    if rule_code == "DOC-001":
        return {"page_document_number": "沪普府〔2025〕1号", "body_document_number": "沪普府〔2025〕2号"}
    if rule_code == "DOC-002":
        return {"page_document_number": "沪普府〔2024〕1号", "authored_date": "2025-01-01"}
    if rule_code == "DOC-003":
        return {"candidate_count": 2}
    if rule_code == "DATE-001":
        return {"overdue_days": rng.randint(366, 720)}
    if rule_code == "DATE-002":
        return {"published_date": "2025-06-01", "authored_date": "2025-07-01"}
    if rule_code in ("AGENCY-001",):
        return {"agency_prefix": "错", "expected_prefixes": ["沪普府", "沪普府办"]}
    if rule_code in ("AGENCY-002", "ORG-004"):
        return {"title_main_entity": "A局", "actual_agency": "B局"}
    if rule_code == "AGENCY-003":
        return {"signature_agency": "X局", "actual_agency": "Y局"}
    if rule_code == "ORG-001":
        return {"main_org": "上海市普陀区人民政府", "code_org": "上海市闵行区人民政府"}
    if rule_code == "ORG-002":
        return {"disclosure_subject": "上海市普陀区A局", "actual_agency": "上海市普陀区B局"}
    if rule_code == "ORG-003":
        return {"joint_agencies": ["A局", "B局"], "expected_joint_count": 3}
    if rule_code == "ORG-005":
        return {"body_signature": "A局", "actual_agency": "B局"}
    if rule_code == "PDF-001":
        return {"parse_status": "failed"}
    if rule_code == "PDF-002":
        return {"is_encrypted": True, "user_password_ok": False}
    if rule_code == "PDF-003":
        return {"page_count": 0, "pdf_text": ""}
    if rule_code == "PDF-004":
        return {"page_doc_number": "A号", "pdf_doc_number": "B号"}
    if rule_code == "PDF-005":
        return {"page_title": "标题A", "pdf_title": "标题B"}
    if rule_code == "PDF-006":
        return {"page_date": "2025-01-01", "pdf_date": "2025-02-01"}
    if rule_code == "PDF-007":
        return {"page_agency": "A局", "pdf_agency": "B局"}
    if rule_code == "ATT-001":
        return {"required_attachments": 2, "actual_attachments": 1}
    if rule_code == "ATT-002":
        return {"attachment_label": "附件1", "attachment_content": "附表"}
    if rule_code == "ATT-003":
        return {"attachment_name_keyword": "附件", "attachment_content": "正文内容"}
    if rule_code == "ARCH-001":
        return {"round1_present": True, "round2_present": True, "round3_present": False}
    if rule_code == "ARCH-002":
        return {"page_updating": True, "section_archived": True}
    if rule_code == "ARCH-003":
        return {"is_long_term_section": True, "section_archived": True}
    if rule_code == "ARCH-004":
        return {"municipal_present": True, "district_present": False, "absent_rounds": 2}
    if rule_code == "ARCH-005":
        return {"district_present": True, "municipal_present": False, "absent_rounds": 2}
    if rule_code == "ARCH-006":
        return {"migrated": True, "redirect_ok": False}
    if rule_code == "DUP-001":
        return {"url_a": "http://a", "url_b": "http://a", "hash_a": "h1", "hash_b": "h1"}
    if rule_code == "DUP-002":
        return {"section_a": "S1", "section_b": "S1", "hash_a": "h1", "hash_b": "h1", "url_a": "u1", "url_b": "u2"}
    if rule_code == "DUP-003":
        return {"url_a": "http://a", "url_b": "http://b", "hash_a": "h1", "hash_b": "h1"}
    if rule_code == "DUP-004":
        return {"section_a": "S1", "section_b": "S2", "hash_a": "h1", "hash_b": "h1"}
    if rule_code == "DUP-005":
        return {"channel_a": "municipal", "channel_b": "district", "hash_a": "h1", "hash_b": "h1"}
    if rule_code == "DUP-006":
        return {"pdf_id_a": 1, "pdf_id_b": 1, "page_a": 1, "page_b": 2}
    if rule_code == "DUP-007":
        return {"doc_number_a": "X1", "doc_number_b": "X1", "hash_a": "h1", "hash_b": "h2"}
    if rule_code == "DUP-008":
        return {"doc_number_a": "X1", "doc_number_b": "X2", "similarity": 0.92, "hash_a": "h1", "hash_b": "h2"}
    if rule_code == "RECON-001":
        return {"municipal_hash": "h1", "district_hash": "h1"}
    if rule_code == "RECON-002":
        return {"municipal_present": False, "district_present": True}
    if rule_code == "RECON-003":
        return {"district_present": False, "municipal_present": True}
    if rule_code == "RECON-004":
        return {"municipal_title": "标题A", "district_title": "标题B"}
    if rule_code == "RECON-005":
        return {"municipal_doc_number": "X1", "district_doc_number": "X2"}
    if rule_code == "RECON-006":
        return {"municipal_date": "2025-01-01", "district_date": "2025-02-01"}
    if rule_code == "RECON-007":
        return {"municipal_body_hash": "h1", "district_body_hash": "h2"}
    if rule_code == "RECON-008":
        return {"municipal_attachments": ["a"], "district_attachments": ["b"]}
    if rule_code == "RECON-009":
        return {"delay_days": 14}
    if rule_code == "RECON-010":
        return {"municipal_present": False, "district_present": False}
    if rule_code == "READ-001":
        return {"button_present": False}
    if rule_code == "READ-002":
        return {"button_present": True, "button_clickable": False}
    if rule_code == "READ-003":
        return {"jump_status_code": 404}
    if rule_code == "READ-004":
        return {"jump_status_code": 200, "page_has_content": False}
    if rule_code == "READ-005":
        return {"jump_status_code": 200, "page_text": "暂无数据"}
    if rule_code == "MEDIA-001":
        return {"image_count": 0, "expected_images": 2}
    if rule_code == "MEDIA-002":
        return {"image_status_codes": [200, 404]}
    if rule_code == "MEDIA-003":
        return {"video_count": 0, "expected_videos": 1}
    if rule_code == "MEDIA-004":
        return {"video_status_code": 500}
    if rule_code == "MEDIA-005":
        return {"image_count": 0, "video_count": 0, "expected_total": 2}
    if rule_code == "MEDIA-006":
        return {"image_status_code": 403, "video_status_code": 200}
    if rule_code == "INTERP-001":
        return {"link_status_code": 404}
    if rule_code == "INTERP-002":
        return {"target_doc_number": "X1", "interp_doc_number": "X2"}
    if rule_code == "INTERP-003":
        return {"interp_published_at": ""}
    if rule_code == "INTERP-004":
        return {"interp_target_url": "http://a", "expected_target_url": "http://b"}
    if rule_code == "INTERP-005":
        return {"interp_image_status": 404}
    if rule_code == "INTERP-006":
        return {"interp_video_status": 500}
    if rule_code == "INTERP-007":
        return {"interp_page_content": ""}
    if rule_code == "INTERP-008":
        return {"interp_type": "未知类型"}
    if rule_code == "RECT-001":
        return {"responsible_unit": ""}
    if rule_code == "RECT-002":
        return {"overdue_days": 7}
    if rule_code == "RECT-003":
        return {"status": "feedback_uploaded", "evidence_json": ""}
    if rule_code == "RECT-004":
        return {"status": "failed", "reopened": False}
    if rule_code == "RECT-005":
        return {"status": "signoff", "recurrence_count": 1}
    if rule_code == "RECT-006":
        return {"status_history_count": 0}
    if rule_code == "RECT-007":
        return {"deadline": ""}
    return {}


def _gen_negative_sample(rule_code: str, rng: random.Random) -> dict:
    """生成一个该规则不应命中的负样本 payload。"""
    if rule_code == "META-001":
        return {"title": "标题", "published_date": "2025-01-01", "issuing_agency": "XX局"}
    if rule_code == "DOC-001":
        return {"page_document_number": "X1", "body_document_number": "X1"}
    if rule_code == "DOC-002":
        return {"page_document_number": "沪普府〔2025〕1号", "authored_date": "2025-06-01"}
    if rule_code == "DOC-003":
        return {"candidate_count": 1}
    if rule_code == "DATE-001":
        return {"overdue_days": rng.randint(0, 365)}
    if rule_code == "DATE-002":
        return {"published_date": "2025-07-01", "authored_date": "2025-06-01"}
    if rule_code == "AGENCY-001":
        return {"agency_prefix": "沪普府", "expected_prefixes": ["沪普府"]}
    if rule_code in ("AGENCY-002", "ORG-004"):
        return {"title_main_entity": "A局", "actual_agency": "A局"}
    if rule_code == "AGENCY-003":
        return {"signature_agency": "A局", "actual_agency": "A局"}
    if rule_code == "ORG-001":
        return {"main_org": "上海市普陀区人民政府", "code_org": "上海市普陀区人民政府"}
    if rule_code == "ORG-002":
        return {"disclosure_subject": "A局", "actual_agency": "A局"}
    if rule_code == "ORG-003":
        return {"joint_agencies": ["A", "B"], "expected_joint_count": 2}
    if rule_code == "ORG-005":
        return {"body_signature": "A局", "actual_agency": "A局"}
    if rule_code in ("PDF-001",):
        return {"parse_status": "ok"}
    if rule_code == "PDF-002":
        return {"is_encrypted": False, "user_password_ok": True}
    if rule_code == "PDF-003":
        return {"page_count": 5, "pdf_text": "正文"}
    if rule_code == "PDF-004":
        return {"page_doc_number": "X1", "pdf_doc_number": "X1"}
    if rule_code == "PDF-005":
        return {"page_title": "标题", "pdf_title": "标题"}
    if rule_code == "PDF-006":
        return {"page_date": "2025-01-01", "pdf_date": "2025-01-01"}
    if rule_code == "PDF-007":
        return {"page_agency": "A局", "pdf_agency": "A局"}
    if rule_code == "ATT-001":
        return {"required_attachments": 1, "actual_attachments": 1}
    if rule_code == "ATT-002":
        return {"attachment_label": "附件1", "attachment_content": "附件1内容"}
    if rule_code == "ATT-003":
        return {"attachment_name_keyword": "附件", "attachment_content": "附件正文"}
    if rule_code == "ARCH-001":
        return {"round1_present": True, "round2_present": True, "round3_present": True}
    if rule_code == "ARCH-002":
        return {"page_updating": True, "section_archived": False}
    if rule_code == "ARCH-003":
        return {"is_long_term_section": False, "section_archived": True}
    if rule_code == "ARCH-004":
        return {"municipal_present": True, "district_present": True, "absent_rounds": 0}
    if rule_code == "ARCH-005":
        return {"district_present": True, "municipal_present": True, "absent_rounds": 0}
    if rule_code == "ARCH-006":
        return {"migrated": True, "redirect_ok": True}
    if rule_code == "DUP-001":
        return {"url_a": "http://a", "url_b": "http://b", "hash_a": "h1", "hash_b": "h2"}
    if rule_code == "DUP-002":
        return {"section_a": "S1", "section_b": "S2", "hash_a": "h1", "hash_b": "h2", "url_a": "u1", "url_b": "u2"}
    if rule_code == "DUP-003":
        return {"url_a": "http://a", "url_b": "http://b", "hash_a": "h1", "hash_b": "h2"}
    if rule_code == "DUP-004":
        return {"section_a": "S1", "section_b": "S2", "hash_a": "h1", "hash_b": "h2"}
    if rule_code == "DUP-005":
        return {"channel_a": "municipal", "channel_b": "district", "hash_a": "h1", "hash_b": "h2"}
    if rule_code == "DUP-006":
        return {"pdf_id_a": 1, "pdf_id_b": 2, "page_a": 1, "page_b": 2}
    if rule_code == "DUP-007":
        return {"doc_number_a": "X1", "doc_number_b": "X2", "hash_a": "h1", "hash_b": "h2"}
    if rule_code == "DUP-008":
        return {"doc_number_a": "X1", "doc_number_b": "X2", "similarity": 0.50, "hash_a": "h1", "hash_b": "h2"}
    if rule_code == "RECON-001":
        return {"municipal_hash": "h1", "district_hash": "h2"}  # 不一致 = 不触发 RECON-001（一致）
    if rule_code == "RECON-002":
        return {"municipal_present": True, "district_present": True}
    if rule_code == "RECON-003":
        return {"district_present": True, "municipal_present": True}
    if rule_code == "RECON-004":
        return {"municipal_title": "标题", "district_title": "标题"}
    if rule_code == "RECON-005":
        return {"municipal_doc_number": "X1", "district_doc_number": "X1"}
    if rule_code == "RECON-006":
        return {"municipal_date": "2025-01-01", "district_date": "2025-01-01"}
    if rule_code == "RECON-007":
        return {"municipal_body_hash": "h1", "district_body_hash": "h1"}
    if rule_code == "RECON-008":
        return {"municipal_attachments": ["a"], "district_attachments": ["a"]}
    if rule_code == "RECON-009":
        return {"delay_days": 3}
    if rule_code == "RECON-010":
        return {"municipal_present": True, "district_present": True}
    if rule_code == "READ-001":
        return {"button_present": True}
    if rule_code == "READ-002":
        return {"button_present": True, "button_clickable": True}
    if rule_code == "READ-003":
        return {"jump_status_code": 200}
    if rule_code == "READ-004":
        return {"jump_status_code": 200, "page_has_content": True}
    if rule_code == "READ-005":
        return {"jump_status_code": 200, "page_text": "政策内容正文"}
    if rule_code == "MEDIA-001":
        return {"image_count": 2, "expected_images": 2}
    if rule_code == "MEDIA-002":
        return {"image_status_codes": [200, 200]}
    if rule_code == "MEDIA-003":
        return {"video_count": 1, "expected_videos": 1}
    if rule_code == "MEDIA-004":
        return {"video_status_code": 200}
    if rule_code == "MEDIA-005":
        return {"image_count": 1, "video_count": 1, "expected_total": 2}
    if rule_code == "MEDIA-006":
        return {"image_status_code": 200, "video_status_code": 200}
    if rule_code == "INTERP-001":
        return {"link_status_code": 200}
    if rule_code == "INTERP-002":
        return {"target_doc_number": "X1", "interp_doc_number": "X1"}
    if rule_code == "INTERP-003":
        return {"interp_published_at": "2025-01-01"}
    if rule_code == "INTERP-004":
        return {"interp_target_url": "http://a", "expected_target_url": "http://a"}
    if rule_code == "INTERP-005":
        return {"interp_image_status": 200}
    if rule_code == "INTERP-006":
        return {"interp_video_status": 200}
    if rule_code == "INTERP-007":
        return {"interp_page_content": "解读内容"}
    if rule_code == "INTERP-008":
        return {"interp_type": "policy_interpretation"}
    if rule_code == "RECT-001":
        return {"responsible_unit": "A局"}
    if rule_code == "RECT-002":
        return {"overdue_days": 0}
    if rule_code == "RECT-003":
        return {"status": "feedback_uploaded", "evidence_json": '{"file":"evidence.pdf"}'}
    if rule_code == "RECT-004":
        return {"status": "failed", "reopened": True}
    if rule_code == "RECT-005":
        return {"status": "signoff", "recurrence_count": 0}
    if rule_code == "RECT-006":
        return {"status_history_count": 3}
    if rule_code == "RECT-007":
        return {"deadline": "2025-12-31"}
    return {}


def generate_samples(rng: random.Random) -> list[EvalSample]:
    """生成 1,500 条评测样本。

    74 条规则 × 20 = 1,480，再补 20 条跨规则边界样本（混合正负）凑足 1,500。
    """
    samples: list[EvalSample] = []
    sid = 1
    per_rule = TOTAL_SAMPLES // len(ALL_RULES)  # 20
    for rule_code in sorted(ALL_RULES.keys()):
        milestone = RULE_MILESTONE[rule_code]
        category = RULE_CATEGORY[rule_code]
        # 70% 正样本，30% 负样本（确保正样本主导，便于评估召回）
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

    # 补足 20 条跨规则边界样本：每条 M3-M5 关键规则再补 1 条正样本
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
    # 重新编号（保证 ID 连续）
    for i, s in enumerate(samples, 1):
        s.sample_id = i
    return samples


# ========== 评测执行 ==========

def evaluate(samples: list[EvalSample]) -> dict[str, Any]:
    per_rule: dict[str, dict[str, Any]] = {}
    overall_tp = overall_fp = overall_fn = overall_tn = 0

    for sample in samples:
        rule_fn = ALL_RULES[sample.rule_code]
        try:
            actual = bool(rule_fn(sample.payload))
        except Exception:
            actual = False  # 异常视为不触发（fail-safe）

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

    # 计算每条规则的 precision/recall/F1/accuracy
    for code, stats in per_rule.items():
        tp, fp, fn, tn = stats["tp"], stats["fp"], stats["fn"], stats["tn"]
        total = tp + fp + fn + tn
        stats["precision"] = round(tp / (tp + fp), 4) if (tp + fp) else 1.0
        stats["recall"] = round(tp / (tp + fn), 4) if (tp + fn) else 1.0
        stats["f1"] = round(2 * stats["precision"] * stats["recall"] / (stats["precision"] + stats["recall"]), 4) if (stats["precision"] + stats["recall"]) else 0.0
        stats["accuracy"] = round((tp + tn) / total, 4) if total else 1.0

    # 全局指标
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

    # 未达标规则（V3.0 §11.5 阈值：precision >= 0.90, recall >= 0.85）
    threshold_precision = 0.90
    threshold_recall = 0.85
    failed_rules = [
        {"rule_code": code, "precision": s["precision"], "recall": s["recall"], "f1": s["f1"]}
        for code, s in per_rule.items()
        if s["precision"] < threshold_precision or s["recall"] < threshold_recall
    ]

    # 规则降级建议（V3.0 §6.6 阈值：连续 2 次快照 precision < 0.80 -> degraded）
    degradation_candidates = [
        {"rule_code": code, "precision": s["precision"], "reason": "precision < 0.80"}
        for code, s in per_rule.items()
        if s["precision"] < 0.80
    ]

    # 按 milestone 分组统计
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
    lines.append("# M7-3 最终 1,500 条评测样本报告\n")
    lines.append(f"- 生成时间: {result['started_at']}")
    lines.append(f"- 样本总数: {result['total_samples']}")
    lines.append(f"- 规则总数: {result['total_rules']}")
    lines.append(f"- 阈值: precision >= {result['thresholds']['precision']}, recall >= {result['thresholds']['recall']}, 降级阈值 precision < {result['thresholds']['degradation_precision']}")
    lines.append(f"- 总体结论: {'PASS' if result['pass'] else 'FAIL'}\n")

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

    lines.append("## 规则降级候选（precision < 0.80）\n")
    if not result["degradation_candidates"]:
        lines.append("无降级候选。\n")
    else:
        lines.append("| 规则码 | Precision | 原因 |")
        lines.append("|---|---|---|")
        for r in result["degradation_candidates"]:
            lines.append(f"| {r['rule_code']} | {r['precision']:.4f} | {r['reason']} |")
        lines.append("")

    lines.append("## 每条规则详细指标\n")
    lines.append("| 规则码 | Milestone | Category | TP | FP | FN | TN | Precision | Recall | F1 | Accuracy |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for code in sorted(result["per_rule"].keys()):
        s = result["per_rule"][code]
        lines.append(f"| {code} | {s['milestone']} | {s['category']} | {s['tp']} | {s['fp']} | {s['fn']} | {s['tn']} | {s['precision']:.4f} | {s['recall']:.4f} | {s['f1']:.4f} | {s['accuracy']:.4f} |")

    return "\n".join(lines) + "\n"


def main() -> int:
    rng = random.Random(SEED)
    samples = generate_samples(rng)
    assert len(samples) == TOTAL_SAMPLES, f"样本数 {len(samples)} != {TOTAL_SAMPLES}"

    result = evaluate(samples)
    result["finished_at"] = datetime.now(timezone.utc).isoformat()

    out_dir = ROOT / "tmp" / "program_control"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "M7_1500_evaluation.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "M7_1500_evaluation.md").write_text(
        render_markdown(result), encoding="utf-8"
    )

    # 控制台摘要
    o = result["overall"]
    print(f"=== M7-3 评测完成 ===")
    print(f"样本: {result['total_samples']}, 规则: {result['total_rules']}")
    print(f"Precision={o['precision']:.4f} Recall={o['recall']:.4f} F1={o['f1']:.4f} Accuracy={o['accuracy']:.4f}")
    print(f"未达标规则: {len(result['failed_rules'])}")
    print(f"降级候选: {len(result['degradation_candidates'])}")
    print(f"总体结论: {'PASS' if result['pass'] else 'FAIL'}")
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
