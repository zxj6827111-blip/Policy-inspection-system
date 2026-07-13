from __future__ import annotations

import json
import re
from datetime import date, timedelta
from typing import Callable

from app.domain import Finding, PolicyRecord


DOC_NUMBER_RE = re.compile(
    r"([\u4e00-\u9fff]{1,12}(?:办|发|规|字|令)?)\s*[〔\[【(（]\s*(20\d{2}|19\d{2})\s*[〕\]】)）]\s*(\d{1,4})\s*号"
)
DATE_RE = re.compile(r"((?:19|20)\d{2})\s*[年./-]\s*(\d{1,2})\s*[月./-]\s*(\d{1,2})\s*日?")


def _clean_prefix(value: str) -> str:
    # 正文中的“另见、及、依据”等连接词紧邻文号时，不应成为机关代字的一部分。
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
        # 落款日期通常独占一行；不接受夹在“有效期至”“自...起施行”等条款中的日期。
        date_match = DATE_RE.fullmatch(line)
        if not date_match:
            continue
        context = " ".join(lines[max(0, index - 2): index + 1])
        if any(keyword in context for keyword in excluded_keywords):
            continue
        parsed = _valid_date(date_match)
        if not parsed:
            continue

        # 支持多个联合发文机关连续落款；页面元数据只列出其中一个机关时也能命中。
        signature_lines: list[str] = []
        for previous in reversed(lines[max(0, index - 6):index]):
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

    # 没有可信落款时不再使用“正文最后一个日期”的猜测，日期类规则将不会自动确认。
    return None


class WorkdayCalendar:
    def __init__(self, overrides: dict[date, bool] | None = None, provider: Callable[[date], bool] | None = None):
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


def evaluate_record(
    record: PolicyRecord,
    agency_rows: list[dict],
    calendar: WorkdayCalendar,
) -> list[Finding]:
    findings: list[Finding] = []
    if record.header_detected and record.missing_metadata_fields:
        missing = "、".join(record.missing_metadata_fields)
        findings.append(Finding(
            "META-001", "元数据问题", "high", "confirmed",
            f"详情页表头字段缺失或格式无效：{missing}", missing, "", record.url,
        ))
    page_number = normalize_document_number(record.page_document_number)
    body_numbers = record.body_document_numbers or extract_document_numbers(record.body_text)

    if page_number and body_numbers:
        normalized_body = [normalize_document_number(number) for number in body_numbers]
        if len(normalized_body) > 1 and page_number not in normalized_body:
            findings.append(Finding("DOC-003", "文号问题", "medium", "pending", "正文存在多个候选文号，需人工复核", page_number, "、".join(normalized_body), record.body_text[:300]))
        elif page_number not in normalized_body:
            findings.append(Finding("DOC-001", "文号问题", "high", "confirmed", "页面文号与正文文号不一致", page_number, "、".join(normalized_body), record.body_text[:300]))

    authored = record.authored_date
    if authored and page_number:
        match = DOC_NUMBER_RE.search(page_number)
        if match and int(match.group(2)) != authored.year:
            findings.append(Finding("DOC-002", "文号问题", "high", "confirmed", f"文号年份 {match.group(2)} 与成文年份 {authored.year} 不一致", page_number, str(authored), f"{page_number}；成文日期 {authored}"))

    if authored and record.published_date:
        if record.published_date < authored:
            findings.append(Finding("DATE-002", "日期问题", "high", "confirmed", "发布日期早于成文日期", str(record.published_date), str(authored), f"成文日期 {authored}；发布日期 {record.published_date}"))
        else:
            days = calendar.count_between(authored, record.published_date)
            if days > 20:
                findings.append(Finding("DATE-001", "日期问题", "high", "confirmed", f"成文日期至发布日期相隔 {days} 个工作日，超过 20 个工作日", str(record.published_date), str(authored), f"成文日期 {authored}；发布日期 {record.published_date}"))

    if page_number and record.issuing_agency:
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
            findings.append(Finding("AGENCY-001", "机构问题", "medium", "pending", "发文机构与文号机关代字疑似不匹配，需人工复核", record.issuing_agency, prefix, f"发文机构：{record.issuing_agency}；文号：{page_number}"))

        title_matches = [name for name in matched_names if name and name in record.title]
        body_tail = record.body_text[-800:]
        body_matches = [name for name in matched_names if name and name in body_tail]
        if matched_agency and matched_names and not title_matches and any("人民政府" in name for name in matched_names):
            other_subject = re.search(r"上海市[^\s，。；]{2,24}(?:委员会|局|办公室|中心)", record.title)
            if other_subject:
                findings.append(Finding(
                    "AGENCY-002", "机构问题", "medium", "pending",
                    "标题主体与页面发文机构疑似不一致，需人工复核", record.issuing_agency,
                    other_subject.group(0), record.title,
                ))
        if matched_agency and body_matches and not any(name in record.issuing_agency for name in body_matches):
            findings.append(Finding(
                "AGENCY-003", "机构问题", "medium", "pending",
                "正文落款与页面发文机构疑似不一致，需人工复核", record.issuing_agency,
                "、".join(body_matches), body_tail,
            ))

    return findings
