from fastapi.testclient import TestClient

import app.main as main_module
from app.db import Database
from app.domain import Finding, PolicyListItem, PolicyRecord
from app.jobs import JobManager
from app.repository import Repository


def test_findings_can_be_reviewed_from_api(tmp_path, monkeypatch):
    db = Database(tmp_path / "api.db")
    manager = JobManager(db)
    monkeypatch.setattr(main_module, "db", db)
    monkeypatch.setattr(main_module, "manager", manager)

    with TestClient(main_module.app) as client:
        job_id = db.create_job(["普陀区"], "full", {}, 1)
        db.update_job(job_id, status="partial", coverage_status="partial")
        document_id = Repository(db).save_record(
            job_id,
            PolicyRecord("普陀区", "待复核文件", "https://example.test/review"),
            [Finding("AGENCY-001", "机构问题", "medium", "pending", "疑似不匹配", evidence="页面证据")],
        )

        response = client.get(f"/api/jobs/{job_id}/findings")
        assert response.status_code == 200
        finding = response.json()[0]
        assert finding["document_id"] == document_id
        assert finding["review_note"] == ""

        saved = client.post(
            f"/api/findings/{finding['id']}/review",
            json={"decision": "confirmed", "note": "人工核对正文后确认"},
        )
        assert saved.status_code == 200
        reviewed = client.get(f"/api/jobs/{job_id}/findings").json()[0]
        assert reviewed["review_status"] == "confirmed"
        assert reviewed["review_note"] == "人工核对正文后确认"


def test_review_note_length_is_bounded(tmp_path, monkeypatch):
    db = Database(tmp_path / "api-limit.db")
    monkeypatch.setattr(main_module, "db", db)
    monkeypatch.setattr(main_module, "manager", JobManager(db))
    with TestClient(main_module.app) as client:
        response = client.post("/api/findings/1/review", json={"decision": "pending", "note": "x" * 501})
    assert response.status_code == 422


def test_finding_evidence_returns_saved_policy_text(tmp_path, monkeypatch):
    db = Database(tmp_path / "evidence.db")
    monkeypatch.setattr(main_module, "db", db)
    monkeypatch.setattr(main_module, "manager", JobManager(db))
    with TestClient(main_module.app) as client:
        job_id = db.create_job(["普陀区"], "full", {}, 1)
        document_id = Repository(db).save_record(
            job_id,
            PolicyRecord(
                "普陀区", "证据定位文件", "https://example.test/evidence",
                issuing_agency="上海市普陀区人民政府", page_document_number="普府〔2026〕1号",
                body_text="上海市普陀区人民政府\n普府〔2026〕1号",
            ),
            [Finding("AGENCY-001", "机构问题", "medium", "pending", "疑似不匹配", "上海市普陀区人民政府", "普府", "机构与文号")],
        )
        finding = client.get(f"/api/jobs/{job_id}/findings").json()[0]
        response = client.get(f"/api/findings/{finding['id']}/evidence")
    assert response.status_code == 200
    payload = response.json()
    assert payload["document_id"] == document_id
    assert "普府〔2026〕1号" in payload["body_text"]


def test_review_queue_filters_and_pages_findings(tmp_path, monkeypatch):
    db = Database(tmp_path / "queue.db")
    monkeypatch.setattr(main_module, "db", db)
    monkeypatch.setattr(main_module, "manager", JobManager(db))
    with TestClient(main_module.app) as client:
        job_id = db.create_job(["普陀区"], "full", {}, 1)
        for index, status in enumerate(["pending", "pending", "confirmed"]):
            Repository(db).save_record(
                job_id, PolicyRecord("普陀区", f"文件{index}", f"https://example.test/queue/{index}"),
                [Finding(f"RULE-{index}", "机构问题", "medium", status, "测试问题")],
            )
        pending = client.get(f"/api/jobs/{job_id}/review-queue?review_status=pending&page=1&page_size=1")
        all_items = client.get(f"/api/jobs/{job_id}/review-queue?review_status=all&page=1&page_size=10")
    assert pending.status_code == 200
    assert pending.json()["total"] == 2
    assert len(pending.json()["items"]) == 1
    assert pending.json()["counts"] == {"pending": 2, "confirmed": 1, "dismissed": 0}
    assert all_items.status_code == 200
    assert len(all_items.json()["items"]) == 3


def test_scan_exceptions_api_only_returns_retest_failures(tmp_path, monkeypatch):
    db = Database(tmp_path / "exceptions-api.db")
    monkeypatch.setattr(main_module, "db", db)
    monkeypatch.setattr(main_module, "manager", JobManager(db))
    with TestClient(main_module.app) as client:
        job_id = db.create_job(["普陀区"], "full", {}, 1)
        repository = Repository(db)
        first = PolicyListItem("普陀区", 2, 3, "待复测政策", "https://example.test/retry")
        second = PolicyListItem("普陀区", 2, 4, "已恢复政策", "https://example.test/resolved")
        repository.record_scan_exception(job_id, first, "detail_metadata", "首次加载超时")
        repository.record_scan_exception(job_id, second, "detail_metadata", "首次加载超时")
        pending = repository.pending_scan_exceptions(job_id, "普陀区")
        repository.fail_scan_exception_retest(pending[0]["id"], "两次加载后仍不完整")
        repository.resolve_scan_exception(pending[1]["id"])
        response = client.get(f"/api/jobs/{job_id}/scan-exceptions")
    assert response.status_code == 200
    payload = response.json()
    assert payload["counts"] == {"pending": 0, "resolved": 1, "review_required": 1}
    assert len(payload["items"]) == 1
    assert payload["items"][0]["title"] == "待复测政策"


def test_baseline_and_item_stats_apis_expose_completed_full_runs_only(tmp_path, monkeypatch):
    db = Database(tmp_path / "baseline-api.db")
    monkeypatch.setattr(main_module, "db", db)
    monkeypatch.setattr(main_module, "manager", JobManager(db))
    with TestClient(main_module.app) as client:
        full_job = db.create_job(["市级平台·普陀区"], "full", {}, 0)
        db.update_job(full_job, status="completed", coverage_status="complete", estimated_total=2, examined_count=2)
        partial_job = db.create_job(["市级平台·普陀区"], "full", {}, 0)
        db.update_job(partial_job, status="completed", coverage_status="partial")
        repository = Repository(db)
        first = PolicyListItem(
            "普陀区", 1, 0, "完整文件", "https://example.test/complete", source_key="municipal_putuo",
            source_site="市级平台·普陀区",
        )
        second = PolicyListItem(
            "普陀区", 1, 1, "无表头文件", "https://example.test/no-header", source_key="municipal_putuo",
            source_site="市级平台·普陀区",
        )
        repository.record_scan_item(full_job, first, detail_status="checked_complete", header_detected=True)
        repository.record_scan_item(full_job, second, detail_status="no_header_pass", header_detected=False)

        baselines = client.get("/api/baselines")
        stats = client.get(f"/api/jobs/{full_job}/item-stats")
        missing_baseline = client.post(
            "/api/jobs", json={"targets": ["municipal_putuo"], "mode": "incremental", "max_documents": 0}
        )

    assert baselines.status_code == 200
    assert [job["id"] for job in baselines.json()] == [full_job]
    assert stats.status_code == 200
    assert stats.json()["complete_header"] == 1
    assert stats.json()["no_header_pass"] == 1
    assert missing_baseline.status_code == 400
    assert "必须选择" in missing_baseline.json()["detail"]


def test_index_groups_five_putuo_sources_into_one_visible_selection(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "db", Database(tmp_path / "index.db"))
    monkeypatch.setattr(main_module, "manager", JobManager(main_module.db))
    with TestClient(main_module.app) as client:
        response = client.get("/")

    assert response.status_code == 200
    html = response.text
    assert 'id="putuo-merged-target"' in html
    assert "政策文件（合并扫描）" in html
    assert html.count("data-putuo-member-target") == 5
    for target_key in (
        "putuo_government",
        "putuo_bureaus",
        "putuo_towns",
        "putuo_normative",
        "putuo_party_government",
    ):
        assert f'value="{target_key}"' in html
