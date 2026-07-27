"""R-P1-2 证据完整率 KPI：全局度量 finding 证据完整性。

依据独立总验整改要求，方案要求"证据完整率 100%"，但代码中此前无全局度量。
本模块遍历生产 `rule_findings` 表，按规则类别校验每条 finding 的证据完整性，
返回 JSON 可序列化的 KPI 字典，供 `/api/evidence-completeness` 端点暴露。

证据完整标准（按规则类别）：
- 必填字段（所有 finding）：`evidence` 非空、`page_value` 或 `body_value` 至少一个非空、
  `rule_version` 非空。
- PDF/ATT 类（PDF-001~007、ATT-001~003）：`evidence_json` 中须含 `source_type=pdf`
  或 `source_type=attachment` 的证据项。
- ARCH/DUP/RECON 类：`evidence_json` 中须含 `source_type=archive_observation`、
  `source_type=fingerprint` 或 `source_type=reconciliation` 的证据项。
- READ/MEDIA/INTERP/RECT 类：`evidence_json` 中须含 `source_type=check`、
  `source_type=media`、`source_type=interpretation` 或 `source_type=rectification`
  的证据项。

数据库访问约定：`from app.db import Database`，`with db.connect() as conn:`。
"""

from __future__ import annotations

import json
from typing import Any

from app.db import Database

__all__ = ["compute_evidence_completeness"]

# 规则类别 → 必须命中的 source_type 集合（命中其一即视为该类别证据齐备）。
_PDF_ATT_REQUIRED_SOURCE_TYPES = {"pdf", "attachment"}
_ARCH_DUP_RECON_REQUIRED_SOURCE_TYPES = {
    "archive_observation",
    "fingerprint",
    "reconciliation",
}
_READ_MEDIA_INTERP_RECT_REQUIRED_SOURCE_TYPES = {
    "check",
    "media",
    "interpretation",
    "rectification",
}


def _rule_category(rule_code: str) -> str:
    """根据 rule_code 前缀归类。

    返回值：'pdf_att' | 'arch_dup_recon' | 'read_media_interp_rect' | 'other'。
    """
    code = (rule_code or "").strip()
    if code.startswith("PDF-") or code.startswith("ATT-"):
        return "pdf_att"
    if code.startswith("ARCH-") or code.startswith("DUP-") or code.startswith("RECON-"):
        return "arch_dup_recon"
    if code.startswith("READ-") or code.startswith("MEDIA-") or \
            code.startswith("INTERP-") or code.startswith("RECT-"):
        return "read_media_interp_rect"
    return "other"


def _parse_evidence_items(raw: str) -> list[dict[str, Any]]:
    """解析 evidence_json 列；空或非法时返回空列表。"""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _evidence_source_types(items: list[dict[str, Any]]) -> set[str]:
    """提取证据项的 source_type 集合（小写化）。"""
    types: set[str] = set()
    for item in items:
        st = item.get("source_type")
        if st is None:
            continue
        s = str(st).strip().lower()
        if s:
            types.add(s)
    return types


def _check_finding(row: sqlite3_Row_like) -> tuple[bool, list[str]]:
    """检查单条 finding 是否证据完整。

    返回 (is_complete, missing_fields)。missing_fields 为缺失字段/证据项的描述列表。
    """
    missing: list[str] = []
    rule_code = str(row["rule_code"]) if "rule_code" in row.keys() else ""

    # --- 必填字段（所有 finding）---
    evidence = str(row["evidence"]) if "evidence" in row.keys() and row["evidence"] else ""
    if not evidence.strip():
        missing.append("evidence")

    page_value = str(row["page_value"]) if "page_value" in row.keys() and row["page_value"] else ""
    body_value = str(row["body_value"]) if "body_value" in row.keys() and row["body_value"] else ""
    if not (page_value.strip() or body_value.strip()):
        missing.append("page_value_or_body_value")

    rule_version = str(row["rule_version"]) if "rule_version" in row.keys() and row["rule_version"] else ""
    if not rule_version.strip():
        missing.append("rule_version")

    # --- 按规则类别的额外 source_type 要求 ---
    category = _rule_category(rule_code)
    if category != "other":
        raw_json = str(row["evidence_json"]) if "evidence_json" in row.keys() and row["evidence_json"] else ""
        items = _parse_evidence_items(raw_json)
        source_types = _evidence_source_types(items)

        if category == "pdf_att":
            required = _PDF_ATT_REQUIRED_SOURCE_TYPES
            label = "pdf_or_attachment_evidence"
        elif category == "arch_dup_recon":
            required = _ARCH_DUP_RECON_REQUIRED_SOURCE_TYPES
            label = "archive_dup_recon_evidence"
        else:  # read_media_interp_rect
            required = _READ_MEDIA_INTERP_RECT_REQUIRED_SOURCE_TYPES
            label = "read_media_interp_rect_evidence"

        if not (source_types & required):
            missing.append(label)

    return (len(missing) == 0, missing)


# sqlite3.Row 没有类型注定的基类，这里仅作类型提示占位。
sqlite3_Row_like = Any


def compute_evidence_completeness(db: Database) -> dict[str, Any]:
    """计算全局证据完整率 KPI。

    遍历 `rule_findings` 表的全部 finding，按规则类别校验证据完整性，返回：

    - total_findings: 总 finding 数。
    - complete_findings: 满足"证据完整"标准的 finding 数。
    - incomplete_findings: 不完整 finding 列表，每项含
      finding_id / rule_code / missing_fields。
    - completeness_rate: complete_findings / total_findings；
      total_findings 为 0 时返回 1.0（空库视为完整）。

    返回值 JSON 可序列化。
    """
    total = 0
    complete = 0
    incomplete: list[dict[str, Any]] = []

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, rule_code, evidence, page_value, body_value, "
            "rule_version, evidence_json FROM rule_findings ORDER BY id"
        ).fetchall()

    for row in rows:
        total += 1
        is_complete, missing_fields = _check_finding(row)
        if is_complete:
            complete += 1
        else:
            incomplete.append({
                "finding_id": int(row["id"]),
                "rule_code": str(row["rule_code"]),
                "missing_fields": missing_fields,
            })

    rate = (complete / total) if total > 0 else 1.0
    return {
        "total_findings": total,
        "complete_findings": complete,
        "incomplete_findings": incomplete,
        "completeness_rate": rate,
        "empty_database": total == 0,
        "sample_adequate": total >= 100,
        "note": (
            "empty rule_findings; rate defined as 1.0 with empty_database=true"
            if total == 0
            else (
                "sample size below 100; treat rate as directional only"
                if total < 100
                else ""
            )
        ),
    }
