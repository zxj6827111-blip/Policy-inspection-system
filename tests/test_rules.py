from datetime import date

from app.domain import PolicyRecord
from app.rules import (
    WorkdayCalendar,
    evaluate_record,
    extract_authored_date,
    extract_document_numbers,
    normalize_document_number,
)


AGENCIES = [{
    "agency_name": "上海市普陀区人民政府",
    "aliases_json": '["普陀区人民政府", "普陀区政府"]',
    "document_prefixes_json": '["普府", "普府办"]',
}]


def test_normalize_document_number_variants():
    assert normalize_document_number("普府【2026】 05 号") == "普府〔2026〕5号"
    assert normalize_document_number("普府 [ 2026 ] 5号") == "普府〔2026〕5号"


def test_extract_unique_document_numbers():
    text = "普府〔2026〕5号，另见普府〔2026〕5号及普府办〔2026〕2号。"
    assert extract_document_numbers(text) == ["普府〔2026〕5号", "普府办〔2026〕2号"]


def test_document_number_and_year_findings():
    record = PolicyRecord(
        district="普陀区", title="测试", url="https://example.test/1", issuing_agency="上海市普陀区人民政府",
        page_document_number="普府〔2025〕8号", body_document_numbers=["普府〔2024〕8号"],
        authored_date=date(2024, 1, 2), published_date=date(2024, 1, 2),
    )
    findings = evaluate_record(record, [], WorkdayCalendar())
    assert {finding.rule_code for finding in findings} == {"DOC-001", "DOC-002"}


def test_date_boundary_20_and_21_workdays():
    calendar = WorkdayCalendar()
    authored = date(2026, 3, 2)
    record_20 = PolicyRecord("普陀区", "20天", "https://example.test/20", authored_date=authored, published_date=date(2026, 3, 30))
    record_21 = PolicyRecord("普陀区", "21天", "https://example.test/21", authored_date=authored, published_date=date(2026, 3, 31))
    assert not any(f.rule_code == "DATE-001" for f in evaluate_record(record_20, [], calendar))
    assert any(f.rule_code == "DATE-001" for f in evaluate_record(record_21, [], calendar))


def test_adjusted_workday_override_and_date_reversal():
    calendar = WorkdayCalendar({date(2026, 3, 7): True, date(2026, 3, 9): False})
    assert calendar.count_between(date(2026, 3, 6), date(2026, 3, 9)) == 1
    record = PolicyRecord("崇明区", "倒置", "https://example.test/reverse", authored_date=date(2026, 3, 9), published_date=date(2026, 3, 8))
    assert any(f.rule_code == "DATE-002" for f in evaluate_record(record, [], calendar))


def test_agency_prefix_mismatch_and_title_subject_are_pending_review():
    record = PolicyRecord(
        "普陀区", "上海市普陀区发展和改革委员会关于项目申报的通知", "https://example.test/a",
        issuing_agency="上海市普陀区人民政府", page_document_number="沪发改〔2026〕1号",
    )
    findings = evaluate_record(record, AGENCIES, WorkdayCalendar())
    assert {finding.rule_code for finding in findings} == {"AGENCY-001", "AGENCY-002"}
    assert all(finding.status == "pending" for finding in findings)
    assert all(finding.evidence for finding in findings)


def test_joint_issuing_text_with_valid_prefix_is_not_confirmed_as_error():
    record = PolicyRecord(
        "普陀区", "关于联合开展专项工作的通知", "https://example.test/joint",
        issuing_agency="上海市普陀区人民政府、上海市有关部门", page_document_number="普府〔2026〕2号",
    )
    assert not evaluate_record(record, AGENCIES, WorkdayCalendar())


def test_extract_authored_date_prefers_joint_signature_over_effective_period_dates():
    text = """上海市普陀区市场监督管理局
上海市普陀区发展和改革委员会
上海市普陀区财政局
上海市普陀区投资促进办公室
2025年3月18日

普陀区促进质量提升实施意见
本意见自2025年4月21日起施行，有效期至2026年12月31日。"""
    authored = extract_authored_date(text, "上海市普陀区市场监督管理局")
    assert authored == date(2025, 3, 18)

    record = PolicyRecord(
        "普陀区", "关于印发《普陀区促进质量提升实施意见》的通知", "https://example.test/joint-date",
        issuing_agency="上海市普陀区市场监督管理局", page_document_number="普市监规〔2025〕1号",
        published_date=date(2025, 3, 21), authored_date=authored,
    )
    assert not {finding.rule_code for finding in evaluate_record(record, [], WorkdayCalendar())} & {"DOC-002", "DATE-002"}


def test_extract_authored_date_returns_none_when_only_effective_period_dates_exist():
    text = "本意见自2025年4月21日起施行，有效期至2026年12月31日。"
    assert extract_authored_date(text, "上海市普陀区市场监督管理局") is None


def test_incomplete_detected_header_creates_one_metadata_finding():
    record = PolicyRecord(
        "普陀区", "缺字段文件", "https://example.test/meta", header_detected=True,
        missing_metadata_fields=["发布日期", "成文日期（格式无效）"],
    )
    findings = evaluate_record(record, [], WorkdayCalendar())
    metadata = [finding for finding in findings if finding.rule_code == "META-001"]
    assert len(metadata) == 1
    assert "发布日期" in metadata[0].detail
    assert "成文日期（格式无效）" in metadata[0].detail
