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


def test_export_endpoint_returns_xlsx_attachment(tmp_path, monkeypatch):
    db = Database(tmp_path / "export-api.db")
    monkeypatch.setattr(main_module, "db", db)
    monkeypatch.setattr(main_module, "manager", JobManager(db))
    monkeypatch.setattr("app.exporter.EXPORT_DIR", tmp_path)

    with TestClient(main_module.app) as client:
        job_id = db.create_job(["普陀区"], "full", {}, 1)
        db.update_job(job_id, status="completed", coverage_status="complete")
        response = client.get(f"/api/jobs/{job_id}/export")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert "attachment" in response.headers["content-disposition"]
    assert response.content.startswith(b"PK")


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


def test_finding_evidence_cleans_legacy_putuo_page_chrome(tmp_path, monkeypatch):
    db = Database(tmp_path / "evidence-polluted.db")
    monkeypatch.setattr(main_module, "db", db)
    monkeypatch.setattr(main_module, "manager", JobManager(db))
    title = "普陀区历史政策"
    polluted = f"""无障碍操作说明 跳转到网站导航区 跳转到主要内容区域
首页 一网通办 新闻 政务
{title}
索引号：
SY310107202600001
主题分类：
其他
公开属性：
主动公开
成文日期：
2026年01月01日
发文字号：
普府〔2026〕1号
发布日期：
2026年01月02日
公开主体：
上海市普陀区人民政府

这是政策正文第一段。

这是政策正文第二段。

分享：
关闭窗口
相关推荐
上海市普陀区人民政府主办"""
    with TestClient(main_module.app) as client:
        job_id = db.create_job(["普陀区"], "full", {}, 1)
        Repository(db).save_record(
            job_id,
            PolicyRecord("普陀区", title, "https://www.shpt.gov.cn/example", body_text=polluted),
            [Finding("AGENCY-001", "机构问题", "medium", "pending", "测试")],
        )
        finding = client.get(f"/api/jobs/{job_id}/findings").json()[0]
        payload = client.get(f"/api/findings/{finding['id']}/evidence").json()

    assert payload["body_text"] == "这是政策正文第一段。\n\n这是政策正文第二段。"
    assert "跳转到网站导航区" not in payload["body_text"]
    assert "分享：" not in payload["body_text"]


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


def test_global_review_queue_aggregates_jobs_and_job_zero_is_compatible(tmp_path, monkeypatch):
    db = Database(tmp_path / "global-queue.db")
    monkeypatch.setattr(main_module, "db", db)
    monkeypatch.setattr(main_module, "manager", JobManager(db))
    with TestClient(main_module.app) as client:
        for index in range(2):
            job_id = db.create_job([f"测试区{index}"], "full", {}, 1)
            Repository(db).save_record(
                job_id,
                PolicyRecord(f"测试区{index}", f"跨任务文件{index}", f"https://example.test/global/{index}"),
                [Finding(f"GLOBAL-{index}", "机构问题", "medium", "pending", "待复核")],
            )
        direct = client.get("/api/review-queue?review_status=pending&page=1&page_size=10")
        compatible = client.get("/api/jobs/0/review-queue?review_status=pending&page=1&page_size=10")
        overview = client.get("/api/overview")

    assert direct.status_code == 200
    assert direct.json()["total"] == 2
    assert {item["title"] for item in direct.json()["items"]} == {"跨任务文件0", "跨任务文件1"}
    assert compatible.status_code == 200
    assert compatible.json() == direct.json()
    assert overview.status_code == 200
    assert overview.json()["finding_buckets"]["total"] == 2


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


def test_global_scan_exceptions_returns_all_statuses_and_job_zero_is_compatible(tmp_path, monkeypatch):
    db = Database(tmp_path / "global-exceptions.db")
    monkeypatch.setattr(main_module, "db", db)
    monkeypatch.setattr(main_module, "manager", JobManager(db))
    with TestClient(main_module.app) as client:
        first_job = db.create_job(["普陀区"], "full", {}, 1)
        second_job = db.create_job(["长宁区"], "full", {}, 1)
        repository = Repository(db)
        repository.record_scan_exception(
            first_job, PolicyListItem("普陀区", 1, 0, "待处理", "https://example.test/pending"),
            "detail_metadata", "首次失败",
        )
        repository.record_scan_exception(
            first_job, PolicyListItem("普陀区", 1, 1, "已恢复", "https://example.test/resolved"),
            "detail_metadata", "首次失败",
        )
        repository.record_scan_exception(
            second_job, PolicyListItem("长宁区", 2, 0, "需人工", "https://example.test/review"),
            "detail_metadata", "首次失败",
        )
        first_pending = repository.pending_scan_exceptions(first_job, "普陀区")
        repository.resolve_scan_exception(next(item["id"] for item in first_pending if item["title"] == "已恢复"))
        second_pending = repository.pending_scan_exceptions(second_job, "长宁区")
        repository.fail_scan_exception_retest(second_pending[0]["id"], "复测仍失败")

        direct = client.get("/api/scan-exceptions")
        compatible = client.get("/api/jobs/0/scan-exceptions")

    assert direct.status_code == 200
    assert direct.json()["counts"] == {"pending": 1, "resolved": 1, "review_required": 1}
    assert {item["status"] for item in direct.json()["items"]} == {"pending", "resolved", "review_required"}
    assert compatible.status_code == 200
    assert compatible.json() == direct.json()


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
    """M6-1 UI rewrite: 21-item navigation + dynamic site-scan page.

    Initial HTML no longer embeds the 17 site cards inline; instead it exposes
    a page-overview / page-site skeleton and the site list is rendered via
    /api/scan-sites. We verify both the skeleton and the API payload.
    """
    monkeypatch.setattr(main_module, "db", Database(tmp_path / "index.db"))
    monkeypatch.setattr(main_module, "manager", JobManager(main_module.db))
    with TestClient(main_module.app) as client:
        response = client.get("/")
        sites_response = client.get("/api/scan-sites")

    assert response.status_code == 200
    html = response.text
    # 21 项导航骨架：sidebar、首页总览、站点详情、批量启动、任务中心入口
    assert 'id="nav-sidebar"' in html
    assert 'id="nav-items"' in html
    assert 'id="page-overview"' in html
    assert 'id="page-site"' in html
    assert 'id="batch-start"' in html
    assert 'id="task-center-btn"' in html
    assert 'id="site-scan-btn"' in html
    assert 'id="site-scan-section"' in html
    assert 'id="ov-grid"' in html
    # 旧的手动模式/基线选择表单不应回归
    assert 'name="mode"' not in html
    assert 'name="baseline_job_id"' not in html

    # 22 个站点通过 /api/scan-sites 暴露（上海区级 1 + 上海市级 16 + 江苏 2 + 鄂尔多斯 3）
    assert sites_response.status_code == 200
    sites = sites_response.json()
    assert len(sites) == 22
    keys = {site["key"] for site in sites}
    assert "putuo_district" in keys
    assert "municipal_putuo" in keys
    assert "municipal_chongming" in keys
    assert "municipal_huangpu" in keys
    assert "municipal_pudong" in keys
    assert "jiangsu_huaian" in keys
    assert "jiangsu_suqian" in keys
    assert {"ordos_municipal", "ordos_departments", "yjhl_banner"} <= keys
    assert {site["province"] for site in sites} == {"上海市", "江苏省", "内蒙古自治区"}
    assert {site["city"] for site in sites if site["key"] == "jiangsu_huaian"} == {"淮安市"}
    ordos_sites = [site for site in sites if site["province"] == "内蒙古自治区"]
    assert {site["city"] for site in ordos_sites} == {"鄂尔多斯市"}
    assert {site["key"] for site in ordos_sites} == {
        "ordos_municipal", "ordos_departments", "yjhl_banner",
    }
    assert len(next(site for site in sites if site["key"] == "ordos_departments")["target_labels"]) == 30


def test_scan_sites_reuses_one_prefetched_job_snapshot(tmp_path, monkeypatch):
    db = Database(tmp_path / "scan-sites-prefetch.db")
    manager = JobManager(db)
    monkeypatch.setattr(main_module, "db", db)
    monkeypatch.setattr(main_module, "manager", manager)
    calls = 0
    original = db.list_jobs

    def counted_list_jobs(limit=30):
        nonlocal calls
        calls += 1
        return original(limit)

    monkeypatch.setattr(db, "list_jobs", counted_list_jobs)
    with TestClient(main_module.app) as client:
        response = client.get("/api/scan-sites")

    assert response.status_code == 200
    assert len(response.json()) == 22
    assert calls == 1


def test_delete_job_hides_terminal_task_and_rejects_active_task(tmp_path, monkeypatch):
    db = Database(tmp_path / "delete-job.db")
    monkeypatch.setattr(main_module, "db", db)
    monkeypatch.setattr(main_module, "manager", JobManager(db))
    with TestClient(main_module.app) as client:
        terminal_job_id = db.create_job(["普陀区"], "full", {}, 1)
        db.update_job(terminal_job_id, status="completed", coverage_status="complete")
        deleted = client.delete(f"/api/jobs/{terminal_job_id}")

        assert deleted.status_code == 200
        assert client.get(f"/api/jobs/{terminal_job_id}").status_code == 404
        assert client.get(f"/api/jobs/{terminal_job_id}/findings").status_code == 404
        assert client.get(f"/api/jobs/{terminal_job_id}/export").status_code == 404
        assert all(job["id"] != terminal_job_id for job in client.get("/api/jobs").json())
        assert all(job["id"] != terminal_job_id for job in client.get("/api/overview").json()["jobs"])

        active_job_id = db.create_job(["长宁区"], "full", {}, 1)
        rejected = client.delete(f"/api/jobs/{active_job_id}")

    assert rejected.status_code == 409
    assert rejected.json()["detail"] == "执行中的任务不能删除，请先停止任务"
    with db.connect() as conn:
        deleted_row = conn.execute(
            "SELECT deleted_at,deleted_by FROM scan_jobs WHERE id=?", (terminal_job_id,)
        ).fetchone()
        audit_row = conn.execute(
            "SELECT action,resource_id FROM operation_audits WHERE action='jobs.delete' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert deleted_row["deleted_at"]
    assert deleted_row["deleted_by"] == "auth-disabled"
    assert dict(audit_row) == {"action": "jobs.delete", "resource_id": str(terminal_job_id)}



def test_municipal_sites_cover_sixteen_districts_with_site_ids():
    from app.config import DISTRICT_SITE_IDS, SCAN_SITES, municipal_sites, district_sites, resolve_site

    munis = municipal_sites()
    assert len(munis) == 16
    # 区级组里除普陀区外，新增鄂尔多斯市下辖的伊金霍洛旗；municipal_sites 仍只含上海市级。
    assert [site.key for site in district_sites()] == ["putuo_district", "yjhl_banner"]
    assert len(SCAN_SITES) == 22
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
    for key in ("jiangsu_huaian", "jiangsu_suqian"):
        site = resolve_site(key)
        assert site.province == "江苏省"
        assert site.host in {"www.huaian.gov.cn", "www.suqian.gov.cn"}

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
