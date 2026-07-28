"""Legacy AGENCY-* rules kept for compatibility; always non-customer / suspected."""

from __future__ import annotations

import json
import re

from app.domain import Finding, PolicyRecord
from app.findings_model import MACHINE_SUSPECTED

DOC_NUMBER_RE = re.compile(
    r"([\u4e00-\u9fff]{1,12}(?:办|发|规|字|令)?)\s*[〔\[【(（]\s*(20\d{2}|19\d{2})\s*[〕\]】)）]\s*(\d{1,4})\s*号"
)
RULE_VERSIONS = {
    "AGENCY-001": "1.0.0",
    "AGENCY-002": "1.0.0",
    "AGENCY-003": "1.0.0",
}


def _normalize_document_number(value: str) -> str:
    value = value.strip().replace("【", "〔").replace("】", "〕")
    value = value.replace("[", "〔").replace("]", "〕").replace("（", "〔").replace("）", "〕")
    value = re.sub(r"\s+", "", value)
    match = DOC_NUMBER_RE.search(value)
    if not match:
        return value
    prefix = re.sub(r"^(?:另见|参见|以及|及|见|根据|依据|文件)", "", match.group(1))
    return f"{prefix}〔{match.group(2)}〕{int(match.group(3))}号"


def legacy_agency_findings(record: PolicyRecord, agency_rows: list[dict]) -> list[Finding]:
    findings: list[Finding] = []
    page_number = _normalize_document_number(record.page_document_number)
    if not (page_number and record.issuing_agency):
        return findings
    prefix_match = DOC_NUMBER_RE.search(page_number)
    prefix = prefix_match.group(1) if prefix_match else ""
    matched_agency = False
    prefix_matches = False
    matched_names: list[str] = []
    for row in agency_rows:
        aliases = json.loads(row["aliases_json"])
        names = [row["agency_name"], *aliases]
        if any(name in record.issuing_agency or record.issuing_agency in name for name in names):
            matched_agency = True
            matched_names = names
            prefixes = json.loads(row["document_prefixes_json"])
            prefix_matches = any(prefix.startswith(item) for item in prefixes)
            break
    if matched_agency and prefix and not prefix_matches:
        findings.append(
            Finding(
                "AGENCY-001",
                "机构问题",
                "medium",
                "pending",
                "发文机构与文号机关代字疑似不匹配，需人工复核",
                record.issuing_agency,
                prefix,
                f"发文机构：{record.issuing_agency}；文号：{page_number}",
                machine_status=MACHINE_SUSPECTED,
                human_status="unreviewed",
                rule_version=RULE_VERSIONS["AGENCY-001"],
                confidence=0.55,
                allow_customer_output=False,
            )
        )

    title_matches = [name for name in matched_names if name and name in record.title]
    body_tail = record.body_text[-800:]
    body_matches = [name for name in matched_names if name and name in body_tail]
    if matched_agency and matched_names and not title_matches and any(
        "人民政府" in name for name in matched_names
    ):
        other_subject = re.search(r"上海市[^\s，。；]{2,24}(?:委员会|局|办公室|中心)", record.title)
        if other_subject:
            findings.append(
                Finding(
                    "AGENCY-002",
                    "机构问题",
                    "medium",
                    "pending",
                    "标题主体与页面发文机构疑似不一致，需人工复核",
                    record.issuing_agency,
                    other_subject.group(0),
                    record.title,
                    machine_status=MACHINE_SUSPECTED,
                    human_status="unreviewed",
                    rule_version=RULE_VERSIONS["AGENCY-002"],
                    confidence=0.5,
                    allow_customer_output=False,
                )
            )
    if matched_agency and body_matches and not any(
        name in record.issuing_agency for name in body_matches
    ):
        findings.append(
            Finding(
                "AGENCY-003",
                "机构问题",
                "medium",
                "pending",
                "正文落款与页面发文机构疑似不一致，需人工复核",
                record.issuing_agency,
                "、".join(body_matches),
                body_tail,
                machine_status=MACHINE_SUSPECTED,
                human_status="unreviewed",
                rule_version=RULE_VERSIONS["AGENCY-003"],
                confidence=0.5,
                allow_customer_output=False,
            )
        )
    return findings
