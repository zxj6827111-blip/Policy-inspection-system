from datetime import date, datetime

from openpyxl import load_workbook

from app.db import Database
from app.domain import Finding, PolicyRecord
from app.exporter import export_job
from app.repository import Repository


def test_database_and_excel_export(tmp_path, monkeypatch):
    db = Database(tmp_path / "test.db")
    db.initialize()
    job_id = db.create_job(["普陀区"], "full", {"min_delay_seconds": 5})
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
    repository.record_job_document(job_id, document_id, "普陀区", 1, 0, "processed")
    monkeypatch.setattr("app.exporter.EXPORT_DIR", tmp_path)
    output = export_job(db, job_id)
    workbook = load_workbook(output)
    assert workbook.sheetnames == ["问题汇总", "超期与日期问题", "文号与机构问题", "外链问题", "全量明细", "扫描运行记录"]
    assert workbook["全量明细"]["D2"].value == "测试文件"
    assert workbook["全量明细"]["X2"].hyperlink.target == "https://example.test/10001.html"
    assert workbook["超期与日期问题"]["E2"].value == datetime(2026, 1, 1)
    assert workbook["超期与日期问题"]["G2"].value == 22
    assert workbook["问题汇总"]["E2"].value == "完整"
    assert workbook["问题汇总"]["F2"].value == 1
    assert workbook["问题汇总"]["G2"].value == 1
