import json

import pytest
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


def test_index_exposes_district_and_sixteen_municipal_sites(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "db", Database(tmp_path / "index.db"))
    monkeypatch.setattr(main_module, "manager", JobManager(main_module.db))
    with TestClient(main_module.app) as client:
        response = client.get("/")

    assert response.status_code == 200
    html = response.text
    assert html.count('name="site_key"') == 17
    assert 'value="putuo_district"' in html
    assert 'value="municipal_putuo"' in html
    assert 'value="municipal_chongming"' in html
    assert 'value="municipal_huangpu"' in html
    assert 'value="municipal_pudong"' in html
    assert "区政府、委办局、街道镇、规范性文件、党政混合信息" in html
    assert "site-group-district" in html
    assert "site-option-chip" in html
    assert "默认折叠" in html
    assert "请自行勾选" in html
    assert 'id="auto-start-btn"' in html
    assert 'id="full-rebuild-btn"' in html
    assert 'name="mode"' not in html
    assert 'name="baseline_job_id"' not in html
    # checkboxes must not be pre-selected
    assert 'name="site_key" checked' not in html
    assert 'checked name="site_key"' not in html



def test_municipal_sites_cover_sixteen_districts_with_site_ids():
    from app.config import DISTRICT_SITE_IDS, SCAN_SITES, municipal_sites, district_sites, resolve_site

    munis = municipal_sites()
    assert len(munis) == 16
    assert len(district_sites()) == 1
    assert district_sites()[0].key == "putuo_district"
    assert len(SCAN_SITES) == 17
    assert set(DISTRICT_SITE_IDS) == {site.district for site in munis}
    assert DISTRICT_SITE_IDS["普陀区"] == "0075"
    assert DISTRICT_SITE_IDS["崇明区"] == "0085"
    assert DISTRICT_SITE_IDS["黄浦区"] == "0071"
    assert DISTRICT_SITE_IDS["浦东新区"] == "0070"
    for site in munis:
        resolved = resolve_site(site.key)
        assert resolved.source_level == "市级"
        assert resolved.host == "www.shanghai.gov.cn"
        assert resolved.target_keys == (site.key,)

def test_site_jobs_create_separate_fixed_jobs_and_choose_mode_automatically(tmp_path, monkeypatch):
    db = Database(tmp_path / "site-jobs.db")
    manager = JobManager(db)
    monkeypatch.setattr(main_module, "db", db)
    monkeypatch.setattr(main_module, "manager", manager)

    async def no_run(_job_id):
        return None

    monkeypatch.setattr(manager, "_run", no_run)
    with TestClient(main_module.app) as client:
        baseline = db.create_job(["市级平台·普陀区"], "full", {}, 0)
        db.update_job(baseline, status="completed", coverage_status="complete", estimated_total=1, examined_count=1)
        Repository(db).record_scan_item(
            baseline,
            PolicyListItem(
                "普陀区", 1, 0, "基线文件", "https://example.test/baseline",
                source_key="municipal_putuo", source_site="市级平台·普陀区",
            ),
            detail_status="checked_complete",
        )

        response = client.post(
            "/api/site-jobs",
            json={"site_keys": ["municipal_putuo", "putuo_district"], "max_documents": 0},
        )
        history = client.get("/api/jobs")

    assert response.status_code == 200
    assert history.status_code == 200
    assert next(job for job in history.json() if job["id"] == baseline)["scan_item_count"] == 1
    results = response.json()["jobs"]
    assert [result["site_key"] for result in results] == ["municipal_putuo", "putuo_district"]
    assert [result["mode"] for result in results] == ["incremental", "full"]
    municipal_job = db.get_job(results[0]["job_id"])
    district_job = db.get_job(results[1]["job_id"])
    assert municipal_job["baseline_job_id"] == baseline
    assert json.loads(municipal_job["districts_json"]) == ["市级平台·普陀区"]
    assert json.loads(district_job["districts_json"]) == [
        "区级网站·普陀区·区政府文件",
        "区级网站·普陀区·委办局",
        "区级网站·普陀区·街道镇",
        "区级网站·普陀区·规范性文件",
        "区级网站·普陀区·党政混合信息",
    ]


def test_site_job_resumes_unfinished_initialization_instead_of_creating_duplicate(tmp_path, monkeypatch):
    db = Database(tmp_path / "site-resume.db")
    manager = JobManager(db)
    monkeypatch.setattr(main_module, "db", db)
    monkeypatch.setattr(main_module, "manager", manager)

    async def no_run(_job_id):
        return None

    monkeypatch.setattr(manager, "_run", no_run)
    with TestClient(main_module.app) as client:
        original = db.create_job(["市级平台·崇明区"], "full", {}, 3)
        db.update_job(original, status="partial", coverage_status="partial", examined_count=3, estimated_total=374)
        response = client.post(
            "/api/site-jobs", json={"site_keys": ["municipal_chongming"], "max_documents": 0}
        )

    assert response.status_code == 200
    result = response.json()["jobs"][0]
    assert result == {
        "site_key": "municipal_chongming",
        "site_label": "市级平台·崇明区",
        "job_id": original,
        "action": "resumed",
        "mode": "full",
    }
    assert len(db.list_jobs()) == 1
    assert db.get_job(original)["status"] == "pending"


def test_full_rebuild_creates_new_unlimited_full_job_without_reusing_history(tmp_path, monkeypatch):
    db = Database(tmp_path / "site-full-rebuild.db")
    manager = JobManager(db)
    monkeypatch.setattr(main_module, "db", db)
    monkeypatch.setattr(main_module, "manager", manager)

    async def no_run(_job_id):
        return None

    monkeypatch.setattr(manager, "_run", no_run)
    with TestClient(main_module.app) as client:
        baseline = db.create_job(["市级平台·普陀区"], "full", {}, 0)
        db.update_job(
            baseline, status="completed", coverage_status="complete",
            completion_kind="full", estimated_total=1, examined_count=1,
        )
        Repository(db).record_scan_item(
            baseline,
            PolicyListItem(
                "普陀区", 1, 0, "基线文件", "https://example.test/baseline",
                source_key="municipal_putuo", source_site="市级平台·普陀区",
            ),
            detail_status="checked_complete",
        )
        unfinished = db.create_job(["市级平台·普陀区"], "incremental", {}, 3, baseline)
        db.update_job(
            unfinished, status="partial", coverage_status="partial",
            completion_kind="limit", estimated_total=1, examined_count=1,
        )

        response = client.post(
            "/api/site-jobs/full-rebuild", json={"site_keys": ["municipal_putuo"]}
        )

    assert response.status_code == 200
    result = response.json()["jobs"][0]
    assert result["site_key"] == "municipal_putuo"
    assert result["action"] == "created_full_rebuild"
    assert result["mode"] == "full"
    assert result["job_id"] not in {baseline, unfinished}
    rebuilt = db.get_job(result["job_id"])
    assert rebuilt["mode"] == "full"
    assert rebuilt["max_documents"] == 0
    assert rebuilt["baseline_job_id"] is None
    assert db.get_job(baseline)["status"] == "completed"
    assert db.get_job(unfinished)["status"] == "partial"


def test_full_rebuild_putuo_district_always_covers_all_five_sources(tmp_path, monkeypatch):
    db = Database(tmp_path / "putuo-district-full-rebuild.db")
    manager = JobManager(db)
    monkeypatch.setattr(main_module, "db", db)
    monkeypatch.setattr(main_module, "manager", manager)

    async def no_run(_job_id):
        return None

    monkeypatch.setattr(manager, "_run", no_run)
    with TestClient(main_module.app) as client:
        response = client.post(
            "/api/site-jobs/full-rebuild", json={"site_keys": ["putuo_district"]}
        )

    assert response.status_code == 200
    rebuilt = db.get_job(response.json()["jobs"][0]["job_id"])
    assert json.loads(rebuilt["districts_json"]) == [
        "区级网站·普陀区·区政府文件",
        "区级网站·普陀区·委办局",
        "区级网站·普陀区·街道镇",
        "区级网站·普陀区·规范性文件",
        "区级网站·普陀区·党政混合信息",
    ]
    assert rebuilt["max_documents"] == 0
    assert rebuilt["baseline_job_id"] is None


@pytest.mark.parametrize("active_status", ["pending", "running", "cooling"])
def test_full_rebuild_rejects_selected_sites_as_one_batch_when_any_site_is_active(
    tmp_path, monkeypatch, active_status,
):
    db = Database(tmp_path / "site-full-rebuild-active.db")
    manager = JobManager(db)
    monkeypatch.setattr(main_module, "db", db)
    monkeypatch.setattr(main_module, "manager", manager)

    with TestClient(main_module.app) as client:
        active = db.create_job(["市级平台·崇明区"], "full", {}, 0)
        db.update_job(active, status=active_status)
        response = client.post(
            "/api/site-jobs/full-rebuild",
            json={"site_keys": ["municipal_putuo", "municipal_chongming"]},
        )

    assert response.status_code == 409
    assert f"活动任务 #{active}" in response.json()["detail"]
    assert "请先停止" in response.json()["detail"]
    assert [job["id"] for job in db.list_jobs()] == [active]


def test_full_rebuild_rejects_unknown_site_as_bad_request(tmp_path, monkeypatch):
    db = Database(tmp_path / "site-full-rebuild-invalid.db")
    monkeypatch.setattr(main_module, "db", db)
    monkeypatch.setattr(main_module, "manager", JobManager(db))

    with TestClient(main_module.app) as client:
        response = client.post(
            "/api/site-jobs/full-rebuild", json={"site_keys": ["unknown-site"]}
        )

    assert response.status_code == 400
    assert "不支持的扫描站点" in response.json()["detail"]
    assert db.list_jobs() == []
