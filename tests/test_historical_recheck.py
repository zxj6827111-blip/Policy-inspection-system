import csv
from datetime import date

from app.db import Database
from app.domain import Finding, PolicyRecord
from app.historical_recheck import apply_recheck_result, recheck_jobs, write_recheck_report
from app.repository import Repository


def _add_document(
    db: Database,
    repository: Repository,
    job_id: int,
    record: PolicyRecord,
    findings: list[Finding],
) -> int:
    document_id = repository.save_record(job_id, record, findings)
    repository.record_job_document(
        job_id,
        document_id,
        record.district,
        1,
        document_id,
        "processed",
        "seed",
    )
    return document_id


def test_historical_recheck_is_read_only_and_classifies_changes(tmp_path):
    db = Database(tmp_path / "recheck.db")
    db.initialize()
    repository = Repository(db)
    job_id = db.create_job(["测试区"], "full", {}, 0)

    _add_document(
        db,
        repository,
        job_id,
        PolicyRecord(
            district="测试区",
            title="引用其他文件",
            url="https://example.test/reference",
            page_document_number="测试发〔2026〕8号",
            body_text="有关单位：\n根据其他发〔2026〕3号文，现通知如下。",
        ),
        [
            Finding(
                "DOC-001",
                "文号问题",
                "high",
                "confirmed",
                "旧规则误报",
                machine_status="confirmed_by_rule",
                allow_customer_output=True,
            )
        ],
    )
    _add_document(
        db,
        repository,
        job_id,
        PolicyRecord(
            district="测试区",
            title="公开超期",
            url="https://example.test/date",
            authored_date=date(2024, 1, 2),
            published_date=date(2024, 3, 1),
        ),
        [
            Finding(
                "DATE-001",
                "日期问题",
                "high",
                "confirmed",
                "旧结果",
                machine_status="confirmed_by_rule",
                allow_customer_output=True,
            )
        ],
    )
    _add_document(
        db,
        repository,
        job_id,
        PolicyRecord(
            district="测试区",
            title="年份不一致",
            url="https://example.test/year",
            page_document_number="测试发〔2025〕2号",
            authored_date=date(2024, 2, 1),
            published_date=date(2024, 2, 2),
        ),
        [],
    )

    with db.connect() as conn:
        before = conn.execute("SELECT COUNT(*) FROM rule_findings").fetchone()[0]
    result = recheck_jobs(db.path, [job_id])
    with db.connect() as conn:
        after = conn.execute("SELECT COUNT(*) FROM rule_findings").fetchone()[0]

    assert before == after == 2
    assert {(row["rule_code"], row["document_id"]) for row in result["removed"]} == {
        ("DOC-001", 1)
    }
    assert {row["rule_code"] for row in result["kept"]} == {"DATE-001"}
    assert {row["rule_code"] for row in result["added"]} == {"DOC-002"}
    assert result["summary"]["totals"]["new_customer_candidates"] == 2
    assert result["summary"]["totals"]["kept_customer_candidates"] == 1
    assert result["summary"]["totals"]["added_customer_candidates"] == 1
    assert result["summary"]["totals"]["removed_customer_candidates"] == 1

    output = write_recheck_report(result, tmp_path / "report")
    assert (output / "summary.json").is_file()
    assert (output / "kept.csv").is_file()
    assert (output / "added.csv").is_file()
    assert (output / "removed.csv").is_file()
    with (output / "kept.csv").open(encoding="utf-8-sig", newline="") as source:
        fieldnames = csv.DictReader(source).fieldnames
    assert fieldnames is not None
    assert {"old_page_value", "new_page_value", "old_body_value", "new_body_value"} <= set(fieldnames)


def test_apply_recheck_result_preserves_lineage_and_updates_counts(tmp_path):
    db = Database(tmp_path / "apply.db")
    db.initialize()
    repository = Repository(db)
    job_id = db.create_job(["测试区"], "full", {}, 0)
    _add_document(
        db,
        repository,
        job_id,
        PolicyRecord(
            district="测试区",
            title="引用文号",
            url="https://example.test/apply-reference",
            page_document_number="测试发〔2026〕8号",
            body_text="有关单位：\n根据其他发〔2026〕3号文，现通知如下。",
        ),
        [Finding("DOC-001", "文号问题", "high", "confirmed", "旧误报")],
    )
    _add_document(
        db,
        repository,
        job_id,
        PolicyRecord(
            district="测试区",
            title="日期超期",
            url="https://example.test/apply-date",
            authored_date=date(2024, 1, 2),
            published_date=date(2024, 3, 1),
        ),
        [Finding("DATE-001", "日期问题", "high", "confirmed", "旧结果")],
    )

    backup = tmp_path / "before.db"
    applied = apply_recheck_result(db.path, [job_id], backup)

    assert backup.is_file()
    assert applied["backup_integrity"] == "ok"
    assert applied["applied_counts"][str(job_id)]["formal_finding_count"] == 1
    with db.connect() as conn:
        old_rows = conn.execute(
            "SELECT rule_code,is_current,superseded_by FROM rule_findings WHERE id IN (1,2) ORDER BY id"
        ).fetchall()
        current = conn.execute(
            "SELECT rule_code FROM rule_findings WHERE job_id=? AND is_current=1", (job_id,)
        ).fetchall()
        job = conn.execute("SELECT * FROM scan_jobs WHERE id=?", (job_id,)).fetchone()
    assert old_rows[0]["is_current"] == 0 and old_rows[0]["superseded_by"] is None
    assert old_rows[1]["is_current"] == 0 and old_rows[1]["superseded_by"] is not None
    assert [row["rule_code"] for row in current] == ["DATE-001"]
    assert job["finding_total_count"] == 1
    assert job["formal_finding_count"] == 1
