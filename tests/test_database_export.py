from datetime import date, datetime

from openpyxl import load_workbook

from app.db import Database
from app.domain import Finding, PolicyListItem, PolicyRecord
from app.exporter import export_job
from app.repository import Repository


def test_database_and_excel_export(tmp_path, monkeypatch):
    db = Database(tmp_path / "test.db")
    db.initialize()
    job_id = db.create_job(["putuo_government"], "full", {"min_delay_seconds": 5})
    db.update_job(job_id, status="completed", coverage_status="complete", completion_kind="full",
                  processed_count=1, examined_count=1, estimated_total=1, finding_count=1)
    record = PolicyRecord(
        district="普陀区", source_id="10001", title="测试文件", url="https://example.test/10001.html",
        issuing_agency="上海市普陀区人民政府", page_document_number="普府〔2026〕1号",
        authored_date=date(2026, 1, 1), published_date=date(2026, 2, 1), body_text="测试正文",
    )
    repository = Repository(db)
    document_id = repository.save_record(
        job_id, record, [Finding("DATE-001", "日期问题", "high", "confirmed", "相隔 22 个工作日，超过20个工作日")]
    )
    repository.record_job_document(
        job_id, document_id, "区级网站·普陀区·区政府文件", 1, 0, "processed"
    )
    repository.record_scan_item(
        job_id,
        PolicyListItem(
            district="普陀区", page_number=1, item_index=0, title="测试文件",
            url="https://example.test/10001.html", published_date=date(2026, 2, 1),
            source_site="区级网站·普陀区·区政府文件", source_key="putuo_government", source_channel_id="3",
        ),
        detail_status="checked_complete", header_detected=True, source_id="10001",
        authored_date=date(2026, 1, 1), page_document_number="普府〔2026〕1号",
        published_date=date(2026, 2, 1), issuing_agency="上海市普陀区人民政府", document_id=document_id,
    )
    monkeypatch.setattr("app.exporter.EXPORT_DIR", tmp_path)
    output = export_job(db, job_id)
    workbook = load_workbook(output)
    assert workbook.sheetnames == ["问题汇总", "超期与日期问题", "文号与机构问题", "外链问题", "全量明细", "元数据问题", "扫描运行记录"]
    assert workbook["全量明细"]["F2"].value == "测试文件"
    assert workbook["全量明细"]["AB2"].hyperlink.target == "https://example.test/10001.html"
    assert workbook["超期与日期问题"]["E2"].value == datetime(2026, 1, 1)
    assert workbook["超期与日期问题"]["G2"].value == 22
    assert workbook["问题汇总"]["E2"].value == "完整"
    assert workbook["问题汇总"]["F2"].value == 1
    assert workbook["问题汇总"]["G2"].value == 1
    assert workbook["问题汇总"]["A2"].value == "区级网站·普陀区·区政府文件"
    assert workbook["问题汇总"]["B2"].value == "日期问题"
    assert workbook["问题汇总"]["C2"].value == 1


def test_export_deduplicates_metadata_problem_sheet_by_url(tmp_path, monkeypatch):
    db = Database(tmp_path / "metadata.db")
    db.initialize()
    job_id = db.create_job(["putuo_government"], "full", {})
    db.update_job(
        job_id, status="completed", coverage_status="complete", completion_kind="full",
        processed_count=1, skipped_count=1, examined_count=2, estimated_total=2, finding_count=1,
        total_by_district_json='{"区级网站·普陀区·区政府文件": 2}',
        examined_by_district_json='{"区级网站·普陀区·区政府文件": 2}',
    )
    url = "https://www.shpt.gov.cn/zhengwu/test/1.html"
    source_label = "区级网站·普陀区·区政府文件"
    record = PolicyRecord(
        district="普陀区", title="元数据测试", url=url, source_site=source_label,
        source_id="SY1", published_date=date(2026, 7, 1), authored_date=date(2026, 7, 1),
        header_detected=True, missing_metadata_fields=["主题分类"],
    )
    repository = Repository(db)
    document_id = repository.save_record(
        job_id, record,
        [Finding("META-001", "元数据问题", "high", "confirmed", "详情页表头字段缺失：主题分类")],
    )
    repository.record_job_document(job_id, document_id, source_label, 1, 0, "processed")
    first = PolicyListItem(
        "普陀区", 1, 0, "元数据测试", url, date(2026, 7, 1),
        source_site=source_label, source_key="putuo_government", source_channel_id="3",
    )
    duplicate = PolicyListItem(
        "普陀区", 2, 0, "元数据测试", url, date(2026, 7, 1),
        source_site=source_label, source_key="putuo_government", source_channel_id="3",
    )
    repository.record_scan_item(
        job_id, first, detail_status="checked_incomplete", header_detected=True,
        source_id="SY1", authored_date=date(2026, 7, 1), published_date=date(2026, 7, 1),
        missing_fields=["主题分类"], document_id=document_id, reason="实际详情检查",
    )
    repository.record_scan_item(
        job_id, duplicate, detail_status="reused_current_detail", header_detected=True,
        source_id="SY1", authored_date=date(2026, 7, 1), published_date=date(2026, 7, 1),
        missing_fields=["主题分类"], document_id=document_id, reused_document_id=document_id,
        reason="同一 URL 复用",
    )

    monkeypatch.setattr("app.exporter.EXPORT_DIR", tmp_path)
    workbook = load_workbook(export_job(db, job_id))

    assert workbook["全量明细"].max_row == 3
    assert workbook["元数据问题"].max_row == 2
    assert workbook["元数据问题"]["F2"].value == "checked_incomplete"
    assert workbook["问题汇总"]["A2"].value == source_label
    assert workbook["问题汇总"]["B2"].value == "元数据问题"
    assert workbook["问题汇总"]["C2"].value == 1
