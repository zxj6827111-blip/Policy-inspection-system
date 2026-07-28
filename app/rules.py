from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any, Callable

from app.domain import Finding, PolicyRecord
from app.extractors.document_roles import extract_document_roles
from app.findings_model import (
    MACHINE_CONFIRMED_BY_RULE,
    MACHINE_HINT,
    MACHINE_SUSPECTED,
)
from app.knowledge.organizations import (
    OrgRecord,
    match_organization_by_name,
    match_organization_by_symbol,
)
from app.rules_legacy_agency import legacy_agency_findings


DOC_NUMBER_RE = re.compile(
    r"([\u4e00-\u9fff]{1,12}(?:办|发|规|字|令)?)\s*[〔\[【(（]\s*(20\d{2}|19\d{2})\s*[〕\]】)）]\s*(\d{1,4})\s*号"
)
DATE_RE = re.compile(r"((?:19|20)\d{2})\s*[年./-]\s*(\d{1,2})\s*[月./-]\s*(\d{1,2})\s*日?")

RULE_VERSIONS = {
    "META-001": "1.0.0",
    "DOC-001": "1.0.0",
    "DOC-002": "1.0.0",
    "DOC-003": "1.0.0",
    "DATE-001": "1.0.0",
    "DATE-002": "1.0.0",
    "AGENCY-001": "1.0.0",
    "AGENCY-002": "1.0.0",
    "AGENCY-003": "1.0.0",
    "ORG-001": "1.0.0",
    "ORG-002": "1.0.0",
    "ORG-003": "1.0.0",
    "ORG-004": "1.0.0",
    "ORG-005": "1.0.0",
}


def _clean_prefix(value: str) -> str:
    return re.sub(r"^(?:另见|参见|以及|及|见|根据|依据|文件)", "", value)


def normalize_document_number(value: str) -> str:
    value = value.strip().replace("【", "〔").replace("】", "〕")
    value = value.replace("[", "〔").replace("]", "〕").replace("（", "〔").replace("）", "〕")
    value = re.sub(r"\s+", "", value)
    match = DOC_NUMBER_RE.search(value)
    if not match:
        return value
    return f"{_clean_prefix(match.group(1))}〔{match.group(2)}〕{int(match.group(3))}号"


def extract_document_numbers(text: str) -> list[str]:
    found: list[str] = []
    for match in DOC_NUMBER_RE.finditer(text or ""):
        number = f"{_clean_prefix(match.group(1))}〔{match.group(2)}〕{int(match.group(3))}号"
        if number not in found:
            found.append(number)
    return found


def _valid_date(match: re.Match) -> date | None:
    try:
        parsed = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        return parsed if date(1990, 1, 1) <= parsed <= date.today() + timedelta(days=366) else None
    except ValueError:
        return None


def extract_authored_date(text: str, issuing_agency: str = "") -> date | None:
    """提取正文落款日期，宁缺毋滥，避免把施行期或有效期当成成文日期。"""
    lines = [line.strip() for line in (text or "").splitlines()]
    agency_keywords = ("人民政府", "委员会", "办公室", "管理局", "监管局", "局", "中心", "部门")
    excluded_keywords = ("有效期", "有效期限", "起施行", "施行日期", "执行日期", "废止")

    for index, line in enumerate(lines):
        date_match = DATE_RE.fullmatch(line)
        if not date_match:
            continue
        context = " ".join(lines[max(0, index - 2) : index + 1])
        if any(keyword in context for keyword in excluded_keywords):
            continue
        parsed = _valid_date(date_match)
        if not parsed:
            continue

        signature_lines: list[str] = []
        for previous in reversed(lines[max(0, index - 6) : index]):
            if not previous:
                if signature_lines:
                    break
                continue
            signature_lines.append(previous)
        signature_block = " ".join(signature_lines)
        if issuing_agency and issuing_agency in signature_block:
            return parsed
        if any(keyword in signature_block for keyword in agency_keywords):
            return parsed
    return None


class WorkdayCalendar:
    def __init__(
        self,
        overrides: dict[date, bool] | None = None,
        provider: Callable[[date], bool] | None = None,
    ):
        self.overrides = overrides or {}
        self.provider = provider

    def is_workday(self, day: date) -> bool:
        if day in self.overrides:
            return self.overrides[day]
        if self.provider:
            return self.provider(day)
        return day.weekday() < 5

    def count_between(self, authored: date, published: date) -> int:
        if published <= authored:
            return 0
        count = 0
        cursor = authored + timedelta(days=1)
        while cursor <= published:
            if self.is_workday(cursor):
                count += 1
            cursor += timedelta(days=1)
        return count


def _finding(
    rule_code: str,
    category: str,
    severity: str,
    machine_status: str,
    detail: str,
    *,
    page_value: str = "",
    body_value: str = "",
    evidence: str = "",
    confidence: float = 0.5,
    allow_customer_output: bool = False,
    evidence_fields: list[dict] | None = None,
) -> Finding:
    return Finding(
        rule_code=rule_code,
        category=category,
        severity=severity,
        status=machine_status if machine_status == "confirmed_by_rule" else (
            "pending" if machine_status in {MACHINE_SUSPECTED, MACHINE_HINT} else machine_status
        ),
        detail=detail,
        page_value=page_value,
        body_value=body_value,
        evidence=evidence,
        machine_status=machine_status,
        human_status="unreviewed",
        rule_version=RULE_VERSIONS.get(rule_code, "1.0.0"),
        confidence=confidence,
        allow_customer_output=allow_customer_output,
        evidence_fields=evidence_fields or [],
    )


def _org_evidence(
    *,
    public_body: str,
    primary_issuer: str,
    joint_issuers: list[str],
    symbol: str,
    match_reason: str,
    body_excerpt: str,
    machine_status: str,
    confidence: float,
    rule_version: str,
) -> list[dict]:
    return [
        {"field_name": "页面公开主体", "raw_value": public_body, "source_type": "page"},
        {"field_name": "实际发文机关", "raw_value": primary_issuer, "source_type": "body"},
        {"field_name": "联合发文机关", "raw_value": "、".join(joint_issuers), "source_type": "body"},
        {"field_name": "文号机关代字", "raw_value": symbol, "source_type": "page"},
        {"field_name": "知识库匹配", "raw_value": match_reason, "source_type": "knowledge"},
        {"field_name": "正文证据", "raw_value": body_excerpt[:300], "source_type": "body"},
        {"field_name": "机器状态", "raw_value": machine_status, "source_type": "system"},
        {"field_name": "置信度", "raw_value": str(confidence), "source_type": "system"},
        {"field_name": "规则版本", "raw_value": rule_version, "source_type": "system"},
        {"field_name": "人工状态", "raw_value": "unreviewed", "source_type": "system"},
    ]


def evaluate_organization_rules(
    record: PolicyRecord,
    knowledge: list[OrgRecord],
) -> list[Finding]:
    findings: list[Finding] = []
    page_number = normalize_document_number(record.page_document_number)
    prefix_match = DOC_NUMBER_RE.search(page_number) if page_number else None
    symbol = prefix_match.group(1) if prefix_match else ""

    roles = extract_document_roles(
        public_body=record.issuing_agency,
        title=record.title,
        body_text=record.body_text,
    )
    primary = roles.primary_issuer or record.issuing_agency
    joint = roles.joint_issuers

    symbol_orgs = match_organization_by_symbol(knowledge, symbol) if symbol else []
    primary_org = match_organization_by_name(knowledge, primary)
    public_org = match_organization_by_name(knowledge, record.issuing_agency)

    # Symbol matches any joint issuer or primary => OK for ORG-001
    symbol_matched_names = [org.standard_name for org in symbol_orgs]
    joint_and_primary = [primary, *joint, record.issuing_agency]
    symbol_ok = False
    match_reason = ""
    if symbol_orgs:
        for org in symbol_orgs:
            names = {org.standard_name, *org.aliases}
            for candidate in joint_and_primary:
                if not candidate:
                    continue
                if any(name and (name in candidate or candidate in name) for name in names):
                    symbol_ok = True
                    match_reason = f"机关代字 {symbol} 匹配 {org.standard_name}（候选机关含 {candidate}）"
                    break
            if symbol_ok:
                break
        if not symbol_ok and roles.relation_type in {"forward", "joint_forward", "joint"}:
            # Forwarding offices: symbol belongs to forwarding body in joint list
            for org in symbol_orgs:
                for name in joint:
                    if org.standard_name in name or any(a in name for a in org.aliases if len(a) >= 2):
                        symbol_ok = True
                        match_reason = f"转发/联合关系下机关代字 {symbol} 匹配联合机关 {name}"
                        break
                if symbol_ok:
                    break

    body_excerpt = roles.opening_excerpt or record.body_text[:300]

    if symbol and record.issuing_agency and not symbol_ok and symbol_orgs:
        # Only emit ORG-001 when we know the symbol but cannot link to issuer roles
        conf = 0.7
        fields = _org_evidence(
            public_body=record.issuing_agency,
            primary_issuer=primary,
            joint_issuers=joint,
            symbol=symbol,
            match_reason="未匹配到主发文/联合/转发机关",
            body_excerpt=body_excerpt,
            machine_status=MACHINE_SUSPECTED,
            confidence=conf,
            rule_version=RULE_VERSIONS["ORG-001"],
        )
        findings.append(
            _finding(
                "ORG-001",
                "机构问题",
                "medium",
                MACHINE_SUSPECTED,
                "实际主发文机关与文号机关代字不匹配，需人工复核",
                page_value=record.issuing_agency,
                body_value=symbol,
                evidence=f"主发文:{primary}；代字:{symbol}；关系:{roles.relation_type}",
                confidence=conf,
                allow_customer_output=False,
                evidence_fields=fields,
            )
        )
    elif symbol_ok:
        match_reason = match_reason or f"机关代字 {symbol} 已匹配知识库: {', '.join(symbol_matched_names)}"

    # ORG-002: public body differs from primary issuer — hint only
    if (
        record.issuing_agency
        and primary
        and record.issuing_agency != primary
        and record.issuing_agency not in primary
        and primary not in record.issuing_agency
    ):
        # If joint forward and public body is government while primary is party office, hint
        conf = 0.6
        fields = _org_evidence(
            public_body=record.issuing_agency,
            primary_issuer=primary,
            joint_issuers=joint,
            symbol=symbol,
            match_reason=match_reason or "公开主体与解析主发文机关不同",
            body_excerpt=body_excerpt,
            machine_status=MACHINE_HINT,
            confidence=conf,
            rule_version=RULE_VERSIONS["ORG-002"],
        )
        findings.append(
            _finding(
                "ORG-002",
                "机构问题",
                "low",
                MACHINE_HINT,
                "页面公开主体与实际发文机关不同（提示，不进入正式问题）",
                page_value=record.issuing_agency,
                body_value=primary,
                evidence=roles.relation_evidence or body_excerpt[:200],
                confidence=conf,
                allow_customer_output=False,
                evidence_fields=fields,
            )
        )

    # ORG-003 joint inconsistency: declared joint but only one org resolvable — weak
    if roles.relation_type in {"joint", "joint_forward"} and len(joint) >= 2:
        resolved = [match_organization_by_name(knowledge, name) for name in joint]
        if any(item is None for item in resolved) and any(item is not None for item in resolved):
            conf = 0.45
            findings.append(
                _finding(
                    "ORG-003",
                    "机构问题",
                    "low",
                    MACHINE_SUSPECTED,
                    "联合发文机关证据不完整或不一致，需人工复核",
                    page_value=record.issuing_agency,
                    body_value="、".join(joint),
                    evidence=roles.relation_evidence,
                    confidence=conf,
                    allow_customer_output=False,
                    evidence_fields=_org_evidence(
                        public_body=record.issuing_agency,
                        primary_issuer=primary,
                        joint_issuers=joint,
                        symbol=symbol,
                        match_reason="联合机关未能全部映射到知识库",
                        body_excerpt=body_excerpt,
                        machine_status=MACHINE_SUSPECTED,
                        confidence=conf,
                        rule_version=RULE_VERSIONS["ORG-003"],
                    ),
                )
            )

    # ORG-004 title subject conflict (similar to AGENCY-002, suspected only)
    if public_org and any("人民政府" in n for n in [public_org.standard_name]):
        other_subject = re.search(r"上海市[^\s，。；]{2,24}(?:委员会|局|办公室|中心)", record.title or "")
        title_hit = public_org.standard_name in (record.title or "") or any(
            a in (record.title or "") for a in public_org.aliases if len(a) >= 4
        )
        if other_subject and not title_hit:
            conf = 0.5
            findings.append(
                _finding(
                    "ORG-004",
                    "机构问题",
                    "medium",
                    MACHINE_SUSPECTED,
                    "标题主体与实际发文机关冲突，需人工复核",
                    page_value=record.issuing_agency,
                    body_value=other_subject.group(0),
                    evidence=record.title,
                    confidence=conf,
                    allow_customer_output=False,
                    evidence_fields=_org_evidence(
                        public_body=record.issuing_agency,
                        primary_issuer=primary,
                        joint_issuers=joint,
                        symbol=symbol,
                        match_reason="标题出现其他主体",
                        body_excerpt=record.title,
                        machine_status=MACHINE_SUSPECTED,
                        confidence=conf,
                        rule_version=RULE_VERSIONS["ORG-004"],
                    ),
                )
            )

    # ORG-005 signature vs primary
    if roles.signature_bodies and primary:
        if not any(primary in sig or sig in primary for sig in roles.signature_bodies):
            # skip if signatures are empty noise
            if len(roles.signature_bodies) >= 1 and roles.relation_type == "direct":
                conf = 0.5
                findings.append(
                    _finding(
                        "ORG-005",
                        "机构问题",
                        "medium",
                        MACHINE_SUSPECTED,
                        "正文落款与实际发文机关冲突，需人工复核",
                        page_value=primary,
                        body_value="、".join(roles.signature_bodies),
                        evidence=roles.closing_excerpt[:300],
                        confidence=conf,
                        allow_customer_output=False,
                        evidence_fields=_org_evidence(
                            public_body=record.issuing_agency,
                            primary_issuer=primary,
                            joint_issuers=joint,
                            symbol=symbol,
                            match_reason="落款未覆盖主发文机关",
                            body_excerpt=roles.closing_excerpt,
                            machine_status=MACHINE_SUSPECTED,
                            confidence=conf,
                            rule_version=RULE_VERSIONS["ORG-005"],
                        ),
                    )
                )

    return findings


def evaluate_record(
    record: PolicyRecord,
    agency_rows: list[dict],
    calendar: WorkdayCalendar,
    knowledge: list[OrgRecord] | None = None,
    *,
    include_legacy_agency: bool = True,
) -> list[Finding]:
    findings: list[Finding] = []
    if record.header_detected and record.missing_metadata_fields:
        missing = "、".join(record.missing_metadata_fields)
        findings.append(
            _finding(
                "META-001",
                "元数据问题",
                "high",
                MACHINE_CONFIRMED_BY_RULE,
                f"详情页表头字段缺失或格式无效：{missing}",
                page_value=missing,
                evidence=record.url,
                confidence=0.95,
                allow_customer_output=True,
                evidence_fields=[
                    {"field_name": "缺失字段", "raw_value": missing, "source_type": "page"},
                    {"field_name": "URL", "raw_value": record.url, "source_type": "page"},
                ],
            )
        )

    page_number = normalize_document_number(record.page_document_number)
    body_numbers = record.body_document_numbers or extract_document_numbers(record.body_text)

    if page_number and body_numbers:
        normalized_body = [normalize_document_number(number) for number in body_numbers]
        if len(normalized_body) > 1 and page_number not in normalized_body:
            findings.append(
                _finding(
                    "DOC-003",
                    "文号问题",
                    "medium",
                    MACHINE_SUSPECTED,
                    "正文存在多个候选文号，需人工复核",
                    page_value=page_number,
                    body_value="、".join(normalized_body),
                    evidence=record.body_text[:300],
                    confidence=0.6,
                    allow_customer_output=False,
                )
            )
        elif page_number not in normalized_body:
            findings.append(
                _finding(
                    "DOC-001",
                    "文号问题",
                    "high",
                    MACHINE_CONFIRMED_BY_RULE,
                    "页面文号与正文文号不一致",
                    page_value=page_number,
                    body_value="、".join(normalized_body),
                    evidence=record.body_text[:300],
                    confidence=0.9,
                    allow_customer_output=True,
                )
            )

    authored = record.authored_date
    if authored and page_number:
        match = DOC_NUMBER_RE.search(page_number)
        if match and int(match.group(2)) != authored.year:
            findings.append(
                _finding(
                    "DOC-002",
                    "文号问题",
                    "high",
                    MACHINE_CONFIRMED_BY_RULE,
                    f"文号年份 {match.group(2)} 与成文年份 {authored.year} 不一致",
                    page_value=page_number,
                    body_value=str(authored),
                    evidence=f"{page_number}；成文日期 {authored}",
                    confidence=0.9,
                    allow_customer_output=True,
                )
            )

    if authored and record.published_date:
        if record.published_date < authored:
            findings.append(
                _finding(
                    "DATE-002",
                    "日期问题",
                    "high",
                    MACHINE_CONFIRMED_BY_RULE,
                    "发布日期早于成文日期",
                    page_value=str(record.published_date),
                    body_value=str(authored),
                    evidence=f"成文日期 {authored}；发布日期 {record.published_date}",
                    confidence=0.95,
                    allow_customer_output=True,
                )
            )
        else:
            days = calendar.count_between(authored, record.published_date)
            if days > 20:
                findings.append(
                    _finding(
                        "DATE-001",
                        "日期问题",
                        "high",
                        MACHINE_CONFIRMED_BY_RULE,
                        f"成文日期至发布日期相隔 {days} 个工作日，超过 20 个工作日",
                        page_value=str(record.published_date),
                        body_value=str(authored),
                        evidence=f"成文日期 {authored}；发布日期 {record.published_date}",
                        confidence=0.85,
                        allow_customer_output=True,
                    )
                )

    # New organization rules (knowledge-based)
    if knowledge:
        findings.extend(evaluate_organization_rules(record, knowledge))
    elif include_legacy_agency:
        # Fall back to legacy agency_rows path for unit tests without knowledge
        findings.extend(legacy_agency_findings(record, agency_rows))

    # Always attach legacy AGENCY-* as compatibility suspected signals when agency_rows provided
    # and knowledge path did not already cover — tests expect AGENCY-001 without knowledge.
    # When knowledge is provided, still emit legacy AGENCY-001 only if ORG rules didn't run symbol match?
    # Spec: keep AGENCY-001 compatible but not customer formal. For knowledge path, emit degraded
    # legacy only when no ORG finding and old logic would fire — skip to avoid double noise on sample.
    if include_legacy_agency and not knowledge and agency_rows:
        pass  # already added via legacy_agency_findings
    elif include_legacy_agency and knowledge is None and agency_rows:
        pass

    return findings
