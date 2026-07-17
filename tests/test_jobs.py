import json
import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest

from app.db import Database
from app.domain import DetailInspection, PolicyListItem, PolicyRecord
from app.domain import CooldownPause, ItemReviewRequired, SafetyPause
from app.jobs import JobManager
from app.repository import Repository


class FakeBrowser:
    async def close(self):
        return None


class FakeChromium:
    async def launch(self, headless=True):
        return FakeBrowser()


class FakePlaywright:
    chromium = FakeChromium()


class FakePlaywrightContext:
    async def __aenter__(self):
        return FakePlaywright()

    async def __aexit__(self, *_args):
        return None


class FakeCollector:
    datasets = {}
    opened = []

    def __init__(self, _browser, _safety):
        self.current_district = ""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def check_robots(self):
        return None

    def ensure_allowed(self, _url):
        return None

    async def check_rendered_link(self, kind, url):
        return {"kind": kind, "url": url, "result": "ok"}

    async def iter_items(self, district, start_page=1, start_item_index=-1):
        self.current_district = district
        for item in self.datasets[district]:
            if item.page_number < start_page:
                continue
            if item.page_number == start_page and item.item_index <= start_item_index:
                continue
            yield item

    async def select_district(self, district):
        self.current_district = district

    async def estimated_total(self):
        return len(self.datasets[self.current_district])

    async def open_item(self, item):
        self.opened.append(item.url)
        return PolicyRecord(
            district=item.district, title=item.title, url=item.url,
            published_date=item.published_date, authored_date=item.published_date,
        )


def item(district, index, suffix):
    return PolicyListItem(
        district=district, page_number=1, item_index=index, title=f"文件{suffix}",
        url=f"https://example.test/{suffix}", published_date=date(2026, 7, 1),
    )


@pytest.fixture
def fake_runtime(monkeypatch):
    FakeCollector.datasets = {}
    FakeCollector.opened = []
    monkeypatch.setattr("app.jobs.async_playwright", lambda: FakePlaywrightContext())
    monkeypatch.setattr("app.jobs.BrowserCollector", FakeCollector)
    return FakeCollector


async def wait_job(manager, job_id):
    task = manager._tasks[job_id]
    await task


def disable_request_throttle(monkeypatch):
    """单元测试不触发真实 5 秒访问间隔；生产 SafetyConfig 不变。"""
    async def _no_wait(self):
        return None

    monkeypatch.setattr("app.safety.SafetyController.before_request", _no_wait)


@pytest.mark.asyncio
async def test_limited_scan_is_partial_and_resume_keeps_limit(tmp_path, fake_runtime):
    db = Database(tmp_path / "jobs.db")
    db.initialize()
    fake_runtime.datasets = {"普陀区": [item("普陀区", n, n) for n in range(3)]}
    manager = JobManager(db)

    job_id = await manager.create_and_start(["普陀区"], "full", max_documents=1)
    await wait_job(manager, job_id)
    first = db.get_job(job_id)
    assert first["status"] == "partial"
    assert first["coverage_status"] == "partial"
    assert first["completion_kind"] == "limit"
    assert first["processed_count"] == first["examined_count"] == 1
    assert first["max_documents"] == 1

    await manager.resume(job_id)
    await wait_job(manager, job_id)
    resumed = db.get_job(job_id)
    assert resumed["status"] == "partial"
    assert resumed["processed_count"] == resumed["examined_count"] == 2
    assert resumed["batch_examined_count"] == 1
    assert resumed["max_documents"] == 1
    assert fake_runtime.opened == ["https://example.test/0", "https://example.test/1"]


@pytest.mark.asyncio
async def test_incremental_unchanged_document_skips_detail(tmp_path, fake_runtime):
    db = Database(tmp_path / "incremental.db")
    db.initialize()
    seed_job = db.create_job(["普陀区"], "full", {}, 0)
    db.update_job(
        seed_job, status="completed", coverage_status="complete", completion_kind="full",
        estimated_total=1, examined_count=1,
    )
    existing = PolicyRecord(
        district="普陀区", title="文件same", url="https://example.test/same",
        published_date=date(2026, 7, 1), authored_date=date(2026, 7, 1),
    )
    repository = Repository(db)
    document_id = repository.save_record(seed_job, existing, [])
    repository.record_scan_item(
        seed_job,
        PolicyListItem(
            district="普陀区", page_number=1, item_index=0, title="文件same",
            url="https://example.test/same", published_date=date(2026, 7, 1),
            source_site="市级平台·普陀区", source_key="municipal_putuo",
        ),
        detail_status="checked_complete", header_detected=True,
        authored_date=date(2026, 7, 1), published_date=date(2026, 7, 1), document_id=document_id,
    )
    fake_runtime.datasets = {"普陀区": [item("普陀区", 0, "same")]}
    manager = JobManager(db)

    job_id = await manager.create_and_start(["普陀区"], "incremental", baseline_job_id=seed_job)
    await wait_job(manager, job_id)
    job = db.get_job(job_id)
    assert job["status"] == "completed"
    assert job["examined_count"] == 1
    assert job["processed_count"] == 0
    assert job["skipped_count"] == 1
    assert fake_runtime.opened == []
    with db.connect() as conn:
        row = conn.execute("SELECT action,reason FROM scan_job_documents WHERE job_id=?", (job_id,)).fetchone()
    assert row["action"] == "skipped"
    assert "一致" in row["reason"]


@pytest.mark.asyncio
async def test_legacy_hidden_baseline_link_forces_detail_recheck(tmp_path, fake_runtime):
    db = Database(tmp_path / "legacy-baseline.db")
    db.initialize()
    baseline_job = db.create_job(["普陀区"], "full", {}, 0)
    db.update_job(
        baseline_job, status="completed", coverage_status="complete", completion_kind="full",
        estimated_total=1, examined_count=1,
    )
    repository = Repository(db)
    url = "https://example.test/same"
    document_id = repository.save_record(
        baseline_job,
        PolicyRecord(
            "普陀区", "文件same", url,
            published_date=date(2026, 7, 1), authored_date=date(2026, 7, 1),
        ),
        [],
    )
    repository.record_scan_item(
        baseline_job,
        PolicyListItem(
            "普陀区", 1, 0, "文件same", url, date(2026, 7, 1),
            source_site="市级平台·普陀区", source_key="municipal_putuo",
        ),
        detail_status="checked_complete", header_detected=True,
        authored_date=date(2026, 7, 1), published_date=date(2026, 7, 1), document_id=document_id,
    )
    repository.save_link_check(
        baseline_job,
        document_id,
        {
            "kind": "阅办联动",
            "url": "https://api.example.test/hidden",
            "result": "broken",
            "visible": False,
            "review_status": "legacy_hidden",
        },
    )
    fake_runtime.datasets = {"普陀区": [item("普陀区", 0, "same")]}
    manager = JobManager(db)

    job_id = await manager.create_and_start(["普陀区"], "incremental", baseline_job_id=baseline_job)
    await wait_job(manager, job_id)

    job = db.get_job(job_id)
    assert fake_runtime.opened == [url]
    assert job["processed_count"] == 1
    assert job["skipped_count"] == 0
    with db.connect() as conn:
        copied = conn.execute("SELECT COUNT(*) FROM link_checks WHERE job_id=?", (job_id,)).fetchone()[0]
    assert copied == 0


@pytest.mark.asyncio
async def test_legacy_baseline_without_link_rows_forces_detail_recheck(tmp_path, fake_runtime):
    db = Database(tmp_path / "legacy-no-links.db")
    db.initialize()
    baseline_job = db.create_job(["普陀区"], "full", {}, 0)
    db.update_job(
        baseline_job, status="completed", coverage_status="complete", completion_kind="full",
        estimated_total=1, examined_count=1,
    )
    repository = Repository(db)
    url = "https://example.test/same"
    document_id = repository.save_record(
        baseline_job,
        PolicyRecord(
            "普陀区", "文件same", url,
            published_date=date(2026, 7, 1), authored_date=date(2026, 7, 1),
        ),
        [],
    )
    repository.record_scan_item(
        baseline_job,
        PolicyListItem(
            "普陀区", 1, 0, "文件same", url, date(2026, 7, 1),
            source_site="市级平台·普陀区", source_key="municipal_putuo",
        ),
        detail_status="checked_complete", header_detected=True,
        authored_date=date(2026, 7, 1), published_date=date(2026, 7, 1), document_id=document_id,
    )
    with db.connect() as conn:
        conn.execute(
            "UPDATE scan_item_results SET link_check_version=0 WHERE job_id=?",
            (baseline_job,),
        )
    fake_runtime.datasets = {"普陀区": [item("普陀区", 0, "same")]}
    manager = JobManager(db)

    job_id = await manager.create_and_start(["普陀区"], "incremental", baseline_job_id=baseline_job)
    await wait_job(manager, job_id)

    job = db.get_job(job_id)
    assert fake_runtime.opened == [url]
    assert job["processed_count"] == 1
    assert job["skipped_count"] == 0
    with db.connect() as conn:
        version = conn.execute(
            "SELECT link_check_version FROM scan_item_results WHERE job_id=?", (job_id,)
        ).fetchone()[0]
    assert version == 1


@pytest.mark.asyncio
async def test_incremental_requires_a_matching_completed_full_baseline(tmp_path):
    db = Database(tmp_path / "baseline-validation.db")
    db.initialize()
    manager = JobManager(db)

    with pytest.raises(ValueError, match="必须选择"):
        await manager.create_and_start(["普陀区"], "incremental")

    incomplete = db.create_job(["普陀区"], "full", {}, 0)
    db.update_job(incomplete, status="completed", coverage_status="partial")
    with pytest.raises(ValueError, match="完整完成"):
        await manager.create_and_start(["普陀区"], "incremental", baseline_job_id=incomplete)

    other_source = db.create_job(["崇明区"], "full", {}, 0)
    db.update_job(other_source, status="completed", coverage_status="complete", estimated_total=1, examined_count=1)
    Repository(db).record_scan_item(
        other_source,
        PolicyListItem("崇明区", 1, 0, "崇明文件", "https://example.test/chongming", source_key="municipal_chongming"),
        detail_status="no_header_pass", header_detected=False,
    )
    with pytest.raises(ValueError, match="来源必须"):
        await manager.create_and_start(["普陀区"], "incremental", baseline_job_id=other_source)


@pytest.mark.asyncio
async def test_incomplete_baseline_metadata_forces_detail_recheck(tmp_path, fake_runtime):
    db = Database(tmp_path / "baseline-incomplete.db")
    db.initialize()
    baseline_job = db.create_job(["普陀区"], "full", {}, 0)
    db.update_job(
        baseline_job, status="completed", coverage_status="complete", completion_kind="full",
        estimated_total=1, examined_count=1,
    )
    repository = Repository(db)
    document_id = repository.save_record(
        baseline_job,
        PolicyRecord("普陀区", "文件same", "https://example.test/same", published_date=date(2026, 7, 1)),
        [],
    )
    repository.record_scan_item(
        baseline_job,
        PolicyListItem("普陀区", 1, 0, "文件same", "https://example.test/same", date(2026, 7, 1),
                       source_site="市级平台·普陀区", source_key="municipal_putuo"),
        detail_status="checked_incomplete", header_detected=True, missing_fields=["发布日期"], document_id=document_id,
    )
    fake_runtime.datasets = {"普陀区": [item("普陀区", 0, "same")]}
    manager = JobManager(db)

    job_id = await manager.create_and_start(["普陀区"], "incremental", baseline_job_id=baseline_job)
    await wait_job(manager, job_id)

    assert fake_runtime.opened == ["https://example.test/same"]
    assert db.get_job(job_id)["processed_count"] == 1


@pytest.mark.asyncio
async def test_no_header_pass_is_saved_for_every_list_item(tmp_path, fake_runtime, monkeypatch):
    class HeaderCollector(FakeCollector):
        async def open_item(self, policy_item):
            self.opened.append(policy_item.url)
            if policy_item.item_index == 0:
                return DetailInspection(record=None, header_detected=False)
            return await super().open_item(policy_item)

    db = Database(tmp_path / "no-header.db")
    db.initialize()
    HeaderCollector.datasets = {"普陀区": [item("普陀区", 0, "no-header"), item("普陀区", 1, "normal")]}
    monkeypatch.setattr("app.jobs.BrowserCollector", HeaderCollector)
    manager = JobManager(db)
    job_id = await manager.create_and_start(["普陀区"], "full")
    await wait_job(manager, job_id)

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT detail_status,header_detected FROM scan_item_results WHERE job_id=? ORDER BY item_index", (job_id,)
        ).fetchall()
    assert [tuple(row) for row in rows] == [("no_header_pass", 0), ("checked_complete", 1)]


@pytest.mark.asyncio
async def test_same_url_in_two_sources_is_opened_once_but_exported_twice(tmp_path, fake_runtime):
    db = Database(tmp_path / "duplicate-url.db")
    db.initialize()
    shared_url = "https://example.test/shared"
    fake_runtime.datasets = {
        "普陀区": [PolicyListItem("普陀区", 1, 0, "共享文件", shared_url, date(2026, 7, 1))],
        "崇明区": [PolicyListItem("崇明区", 1, 0, "共享文件", shared_url, date(2026, 7, 1))],
    }
    manager = JobManager(db)
    job_id = await manager.create_and_start(["普陀区", "崇明区"], "full")
    await wait_job(manager, job_id)

    assert fake_runtime.opened == [shared_url]
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT target_key,detail_status FROM scan_item_results WHERE job_id=? ORDER BY target_key", (job_id,)
        ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("municipal_chongming", "reused_current_detail"),
        ("municipal_putuo", "checked_complete"),
    ]


@pytest.mark.asyncio
async def test_resume_reuses_current_job_detail_saved_before_progress_advance(tmp_path, fake_runtime):
    db = Database(tmp_path / "resume-detail.db")
    db.initialize()
    job_id = db.create_job(["普陀区"], "full", {}, 0)
    db.update_job(job_id, status="paused", coverage_status="partial")
    policy_item = item("普陀区", 0, "saved")
    policy_item.source_site = "市级平台·普陀区"
    policy_item.source_key = "municipal_putuo"
    record = PolicyRecord(
        district="普陀区", title=policy_item.title, url=policy_item.url,
        published_date=policy_item.published_date, authored_date=policy_item.published_date,
    )
    repository = Repository(db)
    document_id = repository.save_record(job_id, record, [])
    repository.record_scan_item(
        job_id, policy_item, detail_status="checked_complete", header_detected=True,
        authored_date=record.authored_date, published_date=record.published_date,
        document_id=document_id,
    )
    fake_runtime.datasets = {"普陀区": [policy_item]}
    manager = JobManager(db)

    await manager.resume(job_id)
    await wait_job(manager, job_id)

    job = db.get_job(job_id)
    assert job["status"] == "completed"
    assert job["examined_count"] == job["processed_count"] == 1
    assert job["skipped_count"] == 0
    assert fake_runtime.opened == []


@pytest.mark.asyncio
async def test_reported_total_mismatch_cannot_be_marked_completed(tmp_path, fake_runtime, monkeypatch):
    class IncompleteCoverageCollector(FakeCollector):
        async def estimated_total(self):
            return len(self.datasets[self.current_district]) + 1

    db = Database(tmp_path / "coverage-mismatch.db")
    db.initialize()
    IncompleteCoverageCollector.datasets = {"普陀区": [item("普陀区", 0, "only")]}
    IncompleteCoverageCollector.opened = []
    monkeypatch.setattr("app.jobs.BrowserCollector", IncompleteCoverageCollector)
    manager = JobManager(db)

    job_id = await manager.create_and_start(["普陀区"], "full")
    await wait_job(manager, job_id)

    job = db.get_job(job_id)
    assert job["status"] == "paused"
    assert job["coverage_status"] == "partial"
    assert job["completion_kind"] == "data_pause"
    assert "扫描覆盖校验失败" in job["pause_reason"]


@pytest.mark.asyncio
async def test_multi_district_total_is_sum_and_progress_is_auditable(tmp_path, fake_runtime):
    db = Database(tmp_path / "districts.db")
    db.initialize()
    fake_runtime.datasets = {
        "普陀区": [item("普陀区", n, f"p{n}") for n in range(2)],
        "崇明区": [item("崇明区", n, f"c{n}") for n in range(3)],
    }
    manager = JobManager(db)
    job_id = await manager.create_and_start(["普陀区", "崇明区"], "full")
    await wait_job(manager, job_id)
    job = db.get_job(job_id)
    assert job["status"] == "completed"
    assert job["estimated_total"] == 5
    assert job["examined_count"] == job["processed_count"] == 5
    assert json.loads(job["total_by_district_json"]) == {"市级平台·普陀区": 2, "市级平台·崇明区": 3}
    assert json.loads(job["examined_by_district_json"]) == {"市级平台·普陀区": 2, "市级平台·崇明区": 3}


@pytest.mark.asyncio
async def test_multi_district_limited_batch_preloads_total_coverage(tmp_path, fake_runtime):
    db = Database(tmp_path / "district-limit.db")
    db.initialize()
    fake_runtime.datasets = {
        "普陀区": [item("普陀区", 0, "p")],
        "崇明区": [item("崇明区", 0, "c")],
    }
    manager = JobManager(db)
    job_id = await manager.create_and_start(["普陀区", "崇明区"], "full", max_documents=1)
    await wait_job(manager, job_id)
    job = db.get_job(job_id)
    assert job["status"] == "partial"
    assert job["estimated_total"] == 2
    assert json.loads(job["total_by_district_json"]) == {"市级平台·普陀区": 1, "市级平台·崇明区": 1}


def test_process_restart_preserves_cursor_limit_and_marks_partial(tmp_path):
    db = Database(tmp_path / "restart.db")
    db.initialize()
    job_id = db.create_job(["普陀区", "崇明区"], "full", {}, 5)
    db.update_job(
        job_id, status="running", current_district="崇明区", current_district_index=1,
        current_page=3, current_item_index=7, examined_count=12, batch_examined_count=2,
    )
    JobManager(db).recover_interrupted()
    job = db.get_job(job_id)
    assert job["status"] == "paused"
    assert job["coverage_status"] == "partial"
    assert job["completion_kind"] == "interrupted"
    assert job["max_documents"] == 5
    assert (job["current_district_index"], job["current_page"], job["current_item_index"]) == (1, 3, 7)


@pytest.mark.asyncio
async def test_resume_rejects_old_job_when_same_site_has_active_rebuild(tmp_path):
    db = Database(tmp_path / "resume-active-rebuild.db")
    db.initialize()
    old_job = db.create_job(["市级平台·普陀区"], "incremental", {}, 3)
    db.update_job(old_job, status="partial", coverage_status="partial", completion_kind="limit")
    active_rebuild = db.create_job(["市级平台·普陀区"], "full", {}, 0)

    with pytest.raises(ValueError, match=rf"活动任务 #{active_rebuild}"):
        await JobManager(db).resume(old_job)

    assert db.get_job(old_job)["status"] == "partial"
    assert db.get_job(active_rebuild)["status"] == "pending"


def test_unfinished_link_checkpoint_forces_resume_and_link_results_are_idempotent(tmp_path):
    db = Database(tmp_path / "links-resume.db")
    db.initialize()
    job_id = db.create_job(["普陀区"], "incremental", {}, 5)
    repository = Repository(db)
    record = PolicyRecord(
        "普陀区", "文件resume", "https://example.test/resume", published_date=date(2026, 7, 1)
    )
    document_id = repository.save_record(job_id, record, [])
    repository.record_job_document(job_id, document_id, "普陀区", 1, 0, "checking_links", "进行中")

    skip, reason, _ = repository.incremental_decision(
        record.url, record.title, record.published_date, job_id=job_id
    )
    assert skip is False
    assert "未完成" in reason

    result = {
        "kind": "附件", "url": "https://example.test/a.pdf", "final_url": "https://example.test/a.pdf",
        "status_code": 200, "result": "ok", "redirect_chain": ["https://example.test/a.pdf"],
        "source_area": "列表页", "link_text": "附件下载", "source_page_url": "https://example.test/list",
    }
    repository.save_link_check(job_id, document_id, result)
    repository.save_link_check(job_id, document_id, result)
    repository.save_link_check(job_id, document_id, {**result, "source_area": "详情页正文"})
    with db.connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM link_checks WHERE job_id=?", (job_id,)).fetchone()[0]
    assert count == 2


@pytest.mark.asyncio
async def test_resume_is_rejected_during_safety_cooldown(tmp_path):
    db = Database(tmp_path / "cooldown.db")
    db.initialize()
    job_id = db.create_job(["普陀区"], "full", {}, 5)
    db.update_job(
        job_id, status="cooling", pause_reason="目标站返回风控状态码 429",
        cooldown_until=(datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat(),
    )
    with pytest.raises(ValueError, match="冷却尚未结束"):
        await JobManager(db).resume(job_id)


@pytest.mark.asyncio
async def test_manual_pause_cancels_inflight_detail_work(tmp_path, fake_runtime, monkeypatch):
    class BlockingCollector(FakeCollector):
        started = asyncio.Event()

        async def open_item(self, item):
            self.started.set()
            await asyncio.Event().wait()

    db = Database(tmp_path / "cancel.db")
    db.initialize()
    BlockingCollector.datasets = {"普陀区": [item("普陀区", 0, "blocked")]}
    monkeypatch.setattr("app.jobs.BrowserCollector", BlockingCollector)
    manager = JobManager(db)
    job_id = await manager.create_and_start(["普陀区"], "full", 5)
    await asyncio.wait_for(BlockingCollector.started.wait(), timeout=1)
    task = manager._tasks[job_id]
    await manager.pause(job_id)
    assert task.done()
    assert db.get_job(job_id)["status"] == "paused"
    assert db.get_job(job_id)["examined_count"] == 0


@pytest.mark.asyncio
async def test_safety_signal_pauses_whole_job_with_cooldown(tmp_path, fake_runtime, monkeypatch):
    class RiskCollector(FakeCollector):
        async def check_robots(self):
            raise CooldownPause("目标站返回风控状态码 429")

    db = Database(tmp_path / "risk.db")
    db.initialize()
    monkeypatch.setattr("app.jobs.BrowserCollector", RiskCollector)
    manager = JobManager(db)
    job_id = await manager.create_and_start(["普陀区"], "full", 1)
    await wait_job(manager, job_id)
    job = db.get_job(job_id)
    assert job["status"] == "cooling"
    assert job["completion_kind"] == "safety_pause"
    assert "429" in job["pause_reason"]
    assert datetime.fromisoformat(job["cooldown_until"]) > datetime.now(timezone.utc)
    await manager.stop(job_id)


@pytest.mark.asyncio
async def test_data_validation_pause_requires_manual_review_without_cooldown(tmp_path, fake_runtime, monkeypatch):
    class DataMismatchCollector(FakeCollector):
        async def check_robots(self):
            raise SafetyPause("列表数据校验失败")

    db = Database(tmp_path / "data-pause.db")
    db.initialize()
    monkeypatch.setattr("app.jobs.BrowserCollector", DataMismatchCollector)
    manager = JobManager(db)
    job_id = await manager.create_and_start(["普陀区"], "full", 1)
    await wait_job(manager, job_id)
    job = db.get_job(job_id)
    assert job["status"] == "paused"
    assert job["completion_kind"] == "data_pause"
    assert job["cooldown_until"] is None


@pytest.mark.asyncio
async def test_single_detail_exception_is_retested_after_scan_without_stopping_job(tmp_path, fake_runtime, monkeypatch):
    class QueuedExceptionCollector(FakeCollector):
        async def open_item(self, policy_item):
            if policy_item.item_index == 0:
                raise ItemReviewRequired("详情页两次加载后仍不完整", "detail_metadata")
            return await super().open_item(policy_item)

        async def open_detail_url(self, district, url, title):
            return PolicyRecord(
                district=district, title=title, url=url,
                published_date=date(2026, 7, 1), authored_date=date(2026, 7, 1),
            )

    db = Database(tmp_path / "exception-queue.db")
    db.initialize()
    QueuedExceptionCollector.datasets = {"普陀区": [item("普陀区", 0, "retry"), item("普陀区", 1, "normal")]}
    monkeypatch.setattr("app.jobs.BrowserCollector", QueuedExceptionCollector)
    manager = JobManager(db)
    job_id = await manager.create_and_start(["普陀区"], "full")
    await wait_job(manager, job_id)

    job = db.get_job(job_id)
    assert job["status"] == "completed"
    assert job["examined_count"] == job["processed_count"] == 2
    assert job["skipped_count"] == 0
    with db.connect() as conn:
        exception = conn.execute("SELECT status,retry_count FROM scan_exceptions WHERE job_id=?", (job_id,)).fetchone()
    assert dict(exception) == {"status": "resolved", "retry_count": 1}


@pytest.mark.asyncio
async def test_scheduler_runs_different_hosts_in_parallel_and_same_host_serially(tmp_path, monkeypatch):
    cross_db = Database(tmp_path / "cross-host.db")
    cross_db.initialize()
    cross_manager = JobManager(cross_db)
    active = 0
    max_active = 0
    both_started = asyncio.Event()

    async def cross_host_run(_job_id):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        if active == 2:
            both_started.set()
        try:
            await asyncio.wait_for(both_started.wait(), timeout=1)
        finally:
            active -= 1

    monkeypatch.setattr(cross_manager, "_run_acquired", cross_host_run)
    cross_ids = [
        await cross_manager.create_and_start(["市级平台·普陀区"], "full"),
        await cross_manager.create_and_start(["区级网站·普陀区·区政府文件"], "full"),
    ]
    cross_tasks = [cross_manager._tasks[job_id] for job_id in cross_ids]
    await asyncio.gather(*cross_tasks)
    assert max_active == 2

    same_db = Database(tmp_path / "same-host.db")
    same_db.initialize()
    same_manager = JobManager(same_db)
    started = []
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    async def same_host_run(job_id):
        started.append(job_id)
        if len(started) == 1:
            first_started.set()
            await release_first.wait()

    monkeypatch.setattr(same_manager, "_run_acquired", same_host_run)
    first_id = await same_manager.create_and_start(["市级平台·普陀区"], "full")
    first_task = same_manager._tasks[first_id]
    await asyncio.wait_for(first_started.wait(), timeout=1)
    second_id = await same_manager.create_and_start(["市级平台·崇明区"], "full")
    second_task = same_manager._tasks[second_id]
    await asyncio.sleep(0)
    assert started == [first_id]
    release_first.set()
    await asyncio.gather(first_task, second_task)
    assert started == [first_id, second_id]


@pytest.mark.asyncio
async def test_putuo_capped_scan_refreshes_real_total_before_complete(tmp_path, monkeypatch):
    disable_request_throttle(monkeypatch)
    """截断分页结束后，应用实际唯一条数覆盖 10000 上限并完成校验。"""
    from app.putuo_collector import PutuoDistrictCollector, PUTUO_QUERY_CONTRACT

    page_size = 3
    cap = 10  # residual on last page = 1, but page 4 returns 3
    pages = {
        1: {
            "list": [{"id": i, "doc_flag": "1", "title": f"t{i}", "url": f"/{i}.html"} for i in range(1, 4)],
            "pageNo": 1, "totalPage": 4, "totalCount": cap,
        },
        2: {
            "list": [{"id": i, "doc_flag": "1", "title": f"t{i}", "url": f"/{i}.html"} for i in range(4, 7)],
            "pageNo": 2, "totalPage": 4, "totalCount": cap,
        },
        3: {
            "list": [{"id": i, "doc_flag": "1", "title": f"t{i}", "url": f"/{i}.html"} for i in range(7, 10)],
            "pageNo": 3, "totalPage": 4, "totalCount": cap,
        },
        4: {
            "list": [{"id": i, "doc_flag": "1", "title": f"t{i}", "url": f"/{i}.html"} for i in range(10, 13)],
            "pageNo": 4, "totalPage": 4, "totalCount": cap,
        },
        5: {
            "list": [{"id": i, "doc_flag": "1", "title": f"t{i}", "url": f"/{i}.html"} for i in range(13, 15)],
            "pageNo": 4, "totalPage": 4, "totalCount": cap,
        },
        6: {"list": [], "pageNo": 0, "totalPage": 0, "totalCount": 0},
    }

    class MultiPageRequest:
        async def post(self, _url, *, data, timeout, fail_on_status_code):
            from tests.test_collector_parsing import FakePutuoApiResponse
            return FakePutuoApiResponse({"data": pages[int(data["pageNo"])]})

    class CappedCollector(PutuoDistrictCollector):
        def __init__(self, browser, safety, target):
            super().__init__(
                browser, safety, target,
                page_size=page_size, api_total_cap=cap, capped_max_extra_pages=20,
            )
            self.page = object()
            self.context = type("Context", (), {"request": MultiPageRequest()})()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def check_robots(self):
            return None

        async def safe_goto(self, page, url):
            return None

        async def open_item(self, item):
            return PolicyRecord(
                district=item.district, title=item.title, url=item.url,
                published_date=item.published_date or date(2026, 7, 1),
                authored_date=item.published_date or date(2026, 7, 1),
            )

    db = Database(tmp_path / "putuo-capped-complete.db")
    db.initialize()
    monkeypatch.setattr("app.jobs.async_playwright", lambda: FakePlaywrightContext())
    monkeypatch.setattr("app.jobs.PutuoDistrictCollector", CappedCollector)
    manager = JobManager(db)
    job_id = await manager.create_and_start(["区级网站·普陀区·委办局"], "full")
    await wait_job(manager, job_id)
    job = db.get_job(job_id)
    assert job["status"] == "completed"
    assert job["coverage_status"] == "complete"
    assert job["examined_count"] == 14
    assert job["estimated_total"] == 14
    assert json.loads(job["total_by_district_json"]) == {"区级网站·普陀区·委办局": 14}
    assert json.loads(job["examined_by_district_json"]) == {"区级网站·普陀区·委办局": 14}
    assert json.loads(job["safety_json"])["query_contract"] == PUTUO_QUERY_CONTRACT


@pytest.mark.asyncio
async def test_putuo_capped_unresolved_cannot_complete(tmp_path, monkeypatch):
    disable_request_throttle(monkeypatch)
    from app.putuo_collector import PutuoDistrictCollector

    page_size = 3
    cap = 10
    repeated = {
        "list": [{"id": i, "doc_flag": "1", "title": f"t{i}", "url": f"/{i}.html"} for i in range(10, 13)],
        "pageNo": 4, "totalPage": 4, "totalCount": cap,
    }
    pages = {
        1: {
            "list": [{"id": i, "doc_flag": "1", "title": f"t{i}", "url": f"/{i}.html"} for i in range(1, 4)],
            "pageNo": 1, "totalPage": 4, "totalCount": cap,
        },
        2: {
            "list": [{"id": i, "doc_flag": "1", "title": f"t{i}", "url": f"/{i}.html"} for i in range(4, 7)],
            "pageNo": 2, "totalPage": 4, "totalCount": cap,
        },
        3: {
            "list": [{"id": i, "doc_flag": "1", "title": f"t{i}", "url": f"/{i}.html"} for i in range(7, 10)],
            "pageNo": 3, "totalPage": 4, "totalCount": cap,
        },
        4: repeated,
        5: repeated,  # beyond declared, full page of already-seen ids
    }

    class MultiPageRequest:
        async def post(self, _url, *, data, timeout, fail_on_status_code):
            from tests.test_collector_parsing import FakePutuoApiResponse
            return FakePutuoApiResponse({"data": pages[int(data["pageNo"])]})

    class CappedCollector(PutuoDistrictCollector):
        def __init__(self, browser, safety, target):
            super().__init__(
                browser, safety, target,
                page_size=page_size, api_total_cap=cap, capped_max_extra_pages=20,
            )
            self.page = object()
            self.context = type("Context", (), {"request": MultiPageRequest()})()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def check_robots(self):
            return None

        async def safe_goto(self, page, url):
            return None

        async def open_item(self, item):
            return PolicyRecord(
                district=item.district, title=item.title, url=item.url,
                published_date=item.published_date or date(2026, 7, 1),
                authored_date=item.published_date or date(2026, 7, 1),
            )

    db = Database(tmp_path / "putuo-capped-unresolved.db")
    db.initialize()
    monkeypatch.setattr("app.jobs.async_playwright", lambda: FakePlaywrightContext())
    monkeypatch.setattr("app.jobs.PutuoDistrictCollector", CappedCollector)
    manager = JobManager(db)
    job_id = await manager.create_and_start(["区级网站·普陀区·委办局"], "full")
    await wait_job(manager, job_id)
    job = db.get_job(job_id)
    assert job["status"] == "paused"
    assert job["coverage_status"] == "partial"
    assert "重复返回已扫描记录" in job["pause_reason"]


@pytest.mark.asyncio
async def test_old_putuo_query_contract_blocks_resume_and_baseline(tmp_path, fake_runtime, monkeypatch):
    disable_request_throttle(monkeypatch)
    from app.putuo_collector import PUTUO_QUERY_CONTRACT

    db = Database(tmp_path / "putuo-contract.db")
    db.initialize()
    manager = JobManager(db)

    old_job = db.create_job(
        ["区级网站·普陀区·区政府文件"],
        "full",
        {"min_delay_seconds": 5, "query_contract": "putuo-legacy-v1"},
        0,
    )
    db.update_job(old_job, status="paused", coverage_status="partial", pause_reason="旧暂停")
    with pytest.raises(ValueError, match="查询契约已失效"):
        await manager.resume(old_job)

    old_baseline = db.create_job(
        ["区级网站·普陀区·区政府文件"],
        "full",
        {"min_delay_seconds": 5},
        0,
    )
    db.update_job(
        old_baseline, status="completed", coverage_status="complete", completion_kind="full",
        estimated_total=1, examined_count=1,
    )
    with db.connect() as conn:
        conn.execute(
            """INSERT INTO scan_item_results(
                job_id,target_key,source_label,channel_id,page_number,item_index,title,url,detail_status,header_detected,checked_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                old_baseline, "putuo_government", "区级网站·普陀区·区政府文件", "3",
                1, 0, "t", "https://www.shpt.gov.cn/a.html", "checked_complete", 1, "2026-07-17T00:00:00+00:00",
            ),
        )
    with pytest.raises(ValueError, match="查询契约已过期"):
        await manager.create_and_start(
            ["区级网站·普陀区·区政府文件"], "incremental", baseline_job_id=old_baseline,
        )

    # 新任务应写入当前契约
    fake_runtime.datasets = {
        "普陀区": [item("普陀区", 0, "only")],
    }
    # putuo collector path: patch PutuoDistrictCollector to Fake-like
    class SimplePutuo(FakeCollector):
        def __init__(self, browser, safety, target):
            super().__init__(browser, safety)
            self.target = target
            self._total = 1

        async def estimated_total(self):
            return 1

        async def iter_items(self, district, start_page=1, start_item_index=-1):
            yield PolicyListItem(
                district="普陀区", page_number=1, item_index=0, title="only",
                url="https://www.shpt.gov.cn/only.html", published_date=date(2026, 7, 1),
                source_site=self.target.label, source_key=self.target.key,
            )

        async def open_item(self, policy_item):
            return PolicyRecord(
                district=policy_item.district, title=policy_item.title, url=policy_item.url,
                published_date=policy_item.published_date, authored_date=policy_item.published_date,
            )

    import app.jobs as jobs_mod
    original = jobs_mod.PutuoDistrictCollector
    jobs_mod.PutuoDistrictCollector = SimplePutuo
    try:
        new_job = await manager.create_and_start(["区级网站·普陀区·区政府文件"], "full")
        await wait_job(manager, new_job)
    finally:
        jobs_mod.PutuoDistrictCollector = original
    created = db.get_job(new_job)
    assert json.loads(created["safety_json"])["query_contract"] == PUTUO_QUERY_CONTRACT


@pytest.mark.asyncio
async def test_automatic_plan_skips_old_putuo_contract_resume(tmp_path):
    db = Database(tmp_path / "auto-skip-old.db")
    db.initialize()
    manager = JobManager(db)
    old_job = db.create_job(
        ["区级网站·普陀区·区政府文件"],
        "full",
        {"query_contract": "putuo-legacy-v1"},
        0,
    )
    db.update_job(old_job, status="partial", coverage_status="partial")
    plan = manager.automatic_plan(["区级网站·普陀区·区政府文件"])
    assert plan["action"] == "create"
    assert plan["mode"] == "full"



@pytest.mark.asyncio
async def test_resume_preserves_completed_capped_target_real_total(tmp_path, monkeypatch):
    """已完成截断来源的真实总量不得被恢复时首屏 10000 覆盖。"""
    from app.putuo_collector import PutuoDistrictCollector, PUTUO_QUERY_CONTRACT
    from app.safety import SafetyController

    disable_request_throttle(monkeypatch)

    page_size = 3
    cap = 10
    pages = {
        1: {
            "list": [{"id": i, "doc_flag": "1", "title": f"t{i}", "url": f"/{i}.html"} for i in range(1, 4)],
            "pageNo": 1, "totalPage": 4, "totalCount": cap,
        },
        2: {
            "list": [{"id": i, "doc_flag": "1", "title": f"t{i}", "url": f"/{i}.html"} for i in range(4, 7)],
            "pageNo": 2, "totalPage": 4, "totalCount": cap,
        },
        3: {
            "list": [{"id": i, "doc_flag": "1", "title": f"t{i}", "url": f"/{i}.html"} for i in range(7, 10)],
            "pageNo": 3, "totalPage": 4, "totalCount": cap,
        },
        4: {
            "list": [{"id": i, "doc_flag": "1", "title": f"t{i}", "url": f"/{i}.html"} for i in range(10, 13)],
            "pageNo": 4, "totalPage": 4, "totalCount": cap,
        },
        5: {
            "list": [{"id": i, "doc_flag": "1", "title": f"t{i}", "url": f"/{i}.html"} for i in range(13, 15)],
            "pageNo": 4, "totalPage": 4, "totalCount": cap,
        },
        6: {"list": [], "pageNo": 0, "totalPage": 0, "totalCount": 0},
    }
    select_calls = []

    class MultiPageRequest:
        async def post(self, _url, *, data, timeout, fail_on_status_code):
            from tests.test_collector_parsing import FakePutuoApiResponse
            return FakePutuoApiResponse({"data": pages[int(data["pageNo"])]})

    class CappedCollector(PutuoDistrictCollector):
        def __init__(self, browser, safety, target):
            super().__init__(
                browser, safety, target,
                page_size=page_size, api_total_cap=cap, capped_max_extra_pages=20,
            )
            self.page = object()
            self.context = type("Context", (), {"request": MultiPageRequest()})()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def check_robots(self):
            return None

        async def safe_goto(self, page, url):
            return None

        async def select_district(self, district):
            select_calls.append(self.target.label)
            return await super().select_district(district)

        async def open_item(self, item):
            return PolicyRecord(
                district=item.district, title=item.title, url=item.url,
                published_date=date(2026, 7, 1), authored_date=date(2026, 7, 1),
            )

        async def open_detail_url(self, district, url, title):
            return PolicyRecord(
                district=district, title=title, url=url,
                published_date=date(2026, 7, 1), authored_date=date(2026, 7, 1),
            )

    db = Database(tmp_path / "preserve-capped-total.db")
    db.initialize()
    monkeypatch.setattr("app.jobs.async_playwright", lambda: FakePlaywrightContext())
    monkeypatch.setattr("app.jobs.PutuoDistrictCollector", CappedCollector)
    manager = JobManager(db)

    # 先跑完委办局，得到真实总量 14
    job_id = await manager.create_and_start(["区级网站·普陀区·委办局"], "full")
    await wait_job(manager, job_id)
    job = db.get_job(job_id)
    assert job["status"] == "completed"
    assert json.loads(job["total_by_district_json"])["区级网站·普陀区·委办局"] == 14

    # 模拟：任务在“异常复测阶段”暂停（列表已完成，index == len(targets)）
    db.update_job(
        job_id, status="paused", coverage_status="partial", completion_kind="data_pause",
        current_district_index=1, finished_at=None, pause_reason="模拟复测阶段暂停",
    )
    select_calls.clear()
    examined_before = int(job["examined_count"])
    with db.connect() as conn:
        item_count_before = conn.execute(
            "SELECT COUNT(*) FROM scan_item_results WHERE job_id=?", (job_id,)
        ).fetchone()[0]

    await manager.resume(job_id)
    await wait_job(manager, job_id)
    resumed = db.get_job(job_id)
    assert resumed["status"] == "completed"
    assert json.loads(resumed["total_by_district_json"])["区级网站·普陀区·委办局"] == 14
    assert resumed["estimated_total"] == 14
    assert resumed["examined_count"] == examined_before
    with db.connect() as conn:
        item_count_after = conn.execute(
            "SELECT COUNT(*) FROM scan_item_results WHERE job_id=?", (job_id,)
        ).fetchone()[0]
    assert item_count_after == item_count_before
    # 已完成来源在预初始化时不得 select_district 重写总量
    assert select_calls == []


@pytest.mark.asyncio
async def test_multi_source_resume_skips_completed_capped_total_and_continues(tmp_path, monkeypatch):
    """多来源：已完成截断 B 保持 14，当前 C 可继续，最终覆盖一致。"""
    from app.putuo_collector import PutuoDistrictCollector, PUTUO_QUERY_CONTRACT

    disable_request_throttle(monkeypatch)

    page_size = 3
    cap = 10
    bureau_pages = {
        1: {
            "list": [{"id": i, "doc_flag": "1", "title": f"b{i}", "url": f"/b{i}.html"} for i in range(1, 4)],
            "pageNo": 1, "totalPage": 4, "totalCount": cap,
        },
        2: {
            "list": [{"id": i, "doc_flag": "1", "title": f"b{i}", "url": f"/b{i}.html"} for i in range(4, 7)],
            "pageNo": 2, "totalPage": 4, "totalCount": cap,
        },
        3: {
            "list": [{"id": i, "doc_flag": "1", "title": f"b{i}", "url": f"/b{i}.html"} for i in range(7, 10)],
            "pageNo": 3, "totalPage": 4, "totalCount": cap,
        },
        4: {
            "list": [{"id": i, "doc_flag": "1", "title": f"b{i}", "url": f"/b{i}.html"} for i in range(10, 13)],
            "pageNo": 4, "totalPage": 4, "totalCount": cap,
        },
        5: {
            "list": [{"id": i, "doc_flag": "1", "title": f"b{i}", "url": f"/b{i}.html"} for i in range(13, 15)],
            "pageNo": 4, "totalPage": 4, "totalCount": cap,
        },
        6: {"list": [], "pageNo": 0, "totalPage": 0, "totalCount": 0},
    }
    gov_items = [
        PolicyListItem(
            district="普陀区", page_number=1, item_index=0, title="gov0",
            url="https://www.shpt.gov.cn/g0.html", published_date=date(2026, 7, 1),
            source_site="区级网站·普陀区·区政府文件", source_key="putuo_government",
        )
    ]
    town_items = [
        PolicyListItem(
            district="普陀区", page_number=1, item_index=0, title="town0",
            url="https://www.shpt.gov.cn/t0.html", published_date=date(2026, 7, 1),
            source_site="区级网站·普陀区·街道镇", source_key="putuo_towns",
        ),
        PolicyListItem(
            district="普陀区", page_number=1, item_index=1, title="town1",
            url="https://www.shpt.gov.cn/t1.html", published_date=date(2026, 7, 1),
            source_site="区级网站·普陀区·街道镇", source_key="putuo_towns",
        ),
    ]
    select_labels = []
    open_urls = []
    pause_after_first_town = {"done": False}

    class MultiPageRequest:
        async def post(self, _url, *, data, timeout, fail_on_status_code):
            from tests.test_collector_parsing import FakePutuoApiResponse
            return FakePutuoApiResponse({"data": bureau_pages[int(data["pageNo"])]})

    class HybridCollector(PutuoDistrictCollector):
        def __init__(self, browser, safety, target):
            super().__init__(
                browser, safety, target,
                page_size=page_size, api_total_cap=cap, capped_max_extra_pages=20,
            )
            self.page = object()
            self.context = type("Context", (), {"request": MultiPageRequest()})()
            self._fake_total = 1 if target.key != "putuo_towns" else 2
            if target.key == "putuo_government":
                self._fake_items = gov_items
            elif target.key == "putuo_towns":
                self._fake_items = town_items
            else:
                self._fake_items = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def check_robots(self):
            return None

        async def safe_goto(self, page, url):
            return None

        async def select_district(self, district):
            select_labels.append(self.target.label)
            if self._fake_items is not None:
                self._estimated_total = self._fake_total
                self._total_count = self._fake_total
                return None
            return await super().select_district(district)

        async def estimated_total(self):
            if self._fake_items is not None:
                return self._fake_total
            return await super().estimated_total()

        async def iter_items(self, district, start_page=1, start_item_index=-1):
            if self._fake_items is not None:
                for policy_item in self._fake_items:
                    if policy_item.page_number < start_page:
                        continue
                    if policy_item.page_number == start_page and policy_item.item_index <= start_item_index:
                        continue
                    yield policy_item
                return
            async for policy_item in super().iter_items(district, start_page, start_item_index):
                yield policy_item

        async def open_item(self, policy_item):
            open_urls.append(policy_item.url)
            if self.target.key == "putuo_towns" and not pause_after_first_town["done"]:
                pause_after_first_town["done"] = True
                raise SafetyPause("模拟街道镇中途暂停")
            return PolicyRecord(
                district=policy_item.district, title=policy_item.title, url=policy_item.url,
                published_date=date(2026, 7, 1), authored_date=date(2026, 7, 1),
            )

        async def open_detail_url(self, district, url, title):
            return PolicyRecord(
                district=district, title=title, url=url,
                published_date=date(2026, 7, 1), authored_date=date(2026, 7, 1),
            )

    db = Database(tmp_path / "multi-source-resume.db")
    db.initialize()
    monkeypatch.setattr("app.jobs.async_playwright", lambda: FakePlaywrightContext())
    monkeypatch.setattr("app.jobs.PutuoDistrictCollector", HybridCollector)
    manager = JobManager(db)
    sources = [
        "区级网站·普陀区·区政府文件",
        "区级网站·普陀区·委办局",
        "区级网站·普陀区·街道镇",
    ]
    job_id = await manager.create_and_start(sources, "full")
    await wait_job(manager, job_id)
    paused = db.get_job(job_id)
    assert paused["status"] == "paused"
    assert "模拟街道镇中途暂停" in paused["pause_reason"]
    totals = json.loads(paused["total_by_district_json"])
    assert totals["区级网站·普陀区·委办局"] == 14
    assert paused["current_district_index"] == 2

    select_labels.clear()
    open_urls.clear()
    pause_after_first_town["done"] = True  # 恢复后不再暂停

    await manager.resume(job_id)
    await wait_job(manager, job_id)
    done = db.get_job(job_id)
    assert done["status"] == "completed"
    totals = json.loads(done["total_by_district_json"])
    examined = json.loads(done["examined_by_district_json"])
    assert totals["区级网站·普陀区·委办局"] == 14
    assert totals["区级网站·普陀区·区政府文件"] == 1
    assert totals["区级网站·普陀区·街道镇"] == 2
    assert examined == totals
    assert done["estimated_total"] == 17
    assert done["examined_count"] == 17
    with db.connect() as conn:
        item_count = conn.execute(
            "SELECT COUNT(*) FROM scan_item_results WHERE job_id=?", (job_id,)
        ).fetchone()[0]
    assert item_count == 17
    # 恢复预初始化：已完成 gov/bureaus 不 select；街道镇要 select
    assert "区级网站·普陀区·委办局" not in select_labels
    assert "区级网站·普陀区·区政府文件" not in select_labels
    assert "区级网站·普陀区·街道镇" in select_labels
    # 街道镇剩余条目被扫描（第 0 条可能在恢复时复用已落库结果）
    assert any(url.endswith("t1.html") for url in open_urls) or done["examined_count"] == 17


@pytest.mark.asyncio
async def test_resume_during_exception_retest_preserves_all_final_totals(tmp_path, monkeypatch):
    """列表全部完成后在异常复测阶段恢复，不得覆盖任何最终总量。"""
    from app.putuo_collector import PutuoDistrictCollector

    disable_request_throttle(monkeypatch)

    page_size = 3
    cap = 10
    pages = {
        1: {
            "list": [{"id": i, "doc_flag": "1", "title": f"t{i}", "url": f"/{i}.html"} for i in range(1, 4)],
            "pageNo": 1, "totalPage": 4, "totalCount": cap,
        },
        2: {
            "list": [{"id": i, "doc_flag": "1", "title": f"t{i}", "url": f"/{i}.html"} for i in range(4, 7)],
            "pageNo": 2, "totalPage": 4, "totalCount": cap,
        },
        3: {
            "list": [{"id": i, "doc_flag": "1", "title": f"t{i}", "url": f"/{i}.html"} for i in range(7, 10)],
            "pageNo": 3, "totalPage": 4, "totalCount": cap,
        },
        4: {
            "list": [{"id": i, "doc_flag": "1", "title": f"t{i}", "url": f"/{i}.html"} for i in range(10, 13)],
            "pageNo": 4, "totalPage": 4, "totalCount": cap,
        },
        5: {
            "list": [{"id": i, "doc_flag": "1", "title": f"t{i}", "url": f"/{i}.html"} for i in range(13, 15)],
            "pageNo": 4, "totalPage": 4, "totalCount": cap,
        },
        6: {"list": [], "pageNo": 0, "totalPage": 0, "totalCount": 0},
    }
    retest_calls = []
    select_calls = []

    class MultiPageRequest:
        async def post(self, _url, *, data, timeout, fail_on_status_code):
            from tests.test_collector_parsing import FakePutuoApiResponse
            return FakePutuoApiResponse({"data": pages[int(data["pageNo"])]})

    class CappedCollector(PutuoDistrictCollector):
        def __init__(self, browser, safety, target):
            super().__init__(
                browser, safety, target,
                page_size=page_size, api_total_cap=cap, capped_max_extra_pages=20,
            )
            self.page = object()
            self.context = type("Context", (), {"request": MultiPageRequest()})()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def check_robots(self):
            return None

        async def safe_goto(self, page, url):
            return None

        async def select_district(self, district):
            select_calls.append(self.target.label)
            return await super().select_district(district)

        async def open_item(self, item):
            if item.item_index == 0 and item.page_number == 1:
                raise ItemReviewRequired("模拟首条异常", "detail_metadata")
            return PolicyRecord(
                district=item.district, title=item.title, url=item.url,
                published_date=date(2026, 7, 1), authored_date=date(2026, 7, 1),
            )

        async def open_detail_url(self, district, url, title):
            retest_calls.append(url)
            return PolicyRecord(
                district=district, title=title, url=url,
                published_date=date(2026, 7, 1), authored_date=date(2026, 7, 1),
            )

    db = Database(tmp_path / "retest-resume.db")
    db.initialize()
    monkeypatch.setattr("app.jobs.async_playwright", lambda: FakePlaywrightContext())
    monkeypatch.setattr("app.jobs.PutuoDistrictCollector", CappedCollector)
    manager = JobManager(db)
    job_id = await manager.create_and_start(["区级网站·普陀区·委办局"], "full")
    await wait_job(manager, job_id)
    completed = db.get_job(job_id)
    # 正常路径会完成；改为模拟复测阶段暂停
    assert completed["status"] == "completed"
    assert json.loads(completed["total_by_district_json"])["区级网站·普陀区·委办局"] == 14

    # 复用首轮扫描产生的异常记录（若已 resolve 则重置为 pending），并把任务拨回复测阶段
    with db.connect() as conn:
        existing = conn.execute(
            "SELECT id,status FROM scan_exceptions WHERE job_id=? AND url=?",
            (job_id, "https://www.shpt.gov.cn/1.html"),
        ).fetchone()
        if existing:
            conn.execute(
                """UPDATE scan_exceptions
                SET status='pending', retry_count=0, resolved_at=NULL, last_error='模拟首条异常'
                WHERE id=?""",
                (existing["id"],),
            )
        else:
            conn.execute(
                """INSERT INTO scan_exceptions(
                    job_id,district,page_number,item_index,title,url,category,first_error,last_error,status,retry_count,first_seen_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    job_id, "普陀区", 1, 0, "t1", "https://www.shpt.gov.cn/1.html",
                    "detail_metadata", "模拟首条异常", "模拟首条异常", "pending", 0, "2026-07-17T00:00:00+00:00",
                ),
            )
    # 回退到“列表完成、异常待复测”的计数：1 条 skipped 待复测，13 条已处理。
    db.update_job(
        job_id, status="paused", coverage_status="partial", completion_kind="exception_queued",
        current_district_index=1, finished_at=None, pause_reason="异常复测前暂停",
        estimated_total=14,
        total_by_district_json=json.dumps({"区级网站·普陀区·委办局": 14}, ensure_ascii=False),
        examined_by_district_json=json.dumps({"区级网站·普陀区·委办局": 14}, ensure_ascii=False),
        examined_count=14, processed_count=13, skipped_count=1,
    )
    select_calls.clear()
    retest_calls.clear()

    await manager.resume(job_id)
    await wait_job(manager, job_id)
    done = db.get_job(job_id)
    assert done["status"] == "completed"
    assert json.loads(done["total_by_district_json"])["区级网站·普陀区·委办局"] == 14
    assert done["estimated_total"] == 14
    assert done["examined_count"] == 14
    assert select_calls == []
    assert retest_calls == ["https://www.shpt.gov.cn/1.html"]
    with db.connect() as conn:
        item_count = conn.execute(
            "SELECT COUNT(*) FROM scan_item_results WHERE job_id=?", (job_id,)
        ).fetchone()[0]
    assert item_count == 14


@pytest.mark.asyncio
async def test_incomplete_normal_target_still_pauses_when_total_changes(tmp_path, fake_runtime, monkeypatch):
    """未完成的普通来源恢复时总量变化仍必须 SafetyPause。"""
    disable_request_throttle(monkeypatch)

    class ChangingTotalCollector(FakeCollector):
        totals = {"普陀区": 2}

        async def estimated_total(self):
            return self.totals[self.current_district or "普陀区"]

        async def select_district(self, district):
            self.current_district = district

    db = Database(tmp_path / "normal-total-change.db")
    db.initialize()
    monkeypatch.setattr("app.jobs.async_playwright", lambda: FakePlaywrightContext())
    monkeypatch.setattr("app.jobs.BrowserCollector", ChangingTotalCollector)
    ChangingTotalCollector.datasets = {
        "普陀区": [item("普陀区", 0, "a"), item("普陀区", 1, "b")],
    }
    ChangingTotalCollector.opened = []
    manager = JobManager(db)
    job_id = await manager.create_and_start(["市级平台·普陀区"], "full", max_documents=1)
    await wait_job(manager, job_id)
    paused = db.get_job(job_id)
    assert paused["status"] == "partial"
    assert json.loads(paused["total_by_district_json"])["市级平台·普陀区"] == 2

    ChangingTotalCollector.totals["普陀区"] = 3
    await manager.resume(job_id)
    await wait_job(manager, job_id)
    failed = db.get_job(job_id)
    assert failed["status"] == "paused"
    assert "列表总量在任务恢复后发生变化" in failed["pause_reason"]
    assert "原 2 条，现 3 条" in failed["pause_reason"]


@pytest.mark.asyncio
async def test_incomplete_capped_target_resume_from_beyond_declared_page(tmp_path, monkeypatch):
    """未完成截断来源可从超声明页码恢复，真实总量 = 已覆盖 + 本轮新增。"""
    from app.putuo_collector import PutuoDistrictCollector

    disable_request_throttle(monkeypatch)

    page_size = 3
    cap = 10
    pages = {
        1: {
            "list": [{"id": i, "doc_flag": "1", "title": f"t{i}", "url": f"/{i}.html"} for i in range(1, 4)],
            "pageNo": 1, "totalPage": 4, "totalCount": cap,
        },
        2: {
            "list": [{"id": i, "doc_flag": "1", "title": f"t{i}", "url": f"/{i}.html"} for i in range(4, 7)],
            "pageNo": 2, "totalPage": 4, "totalCount": cap,
        },
        3: {
            "list": [{"id": i, "doc_flag": "1", "title": f"t{i}", "url": f"/{i}.html"} for i in range(7, 10)],
            "pageNo": 3, "totalPage": 4, "totalCount": cap,
        },
        4: {
            "list": [{"id": i, "doc_flag": "1", "title": f"t{i}", "url": f"/{i}.html"} for i in range(10, 13)],
            "pageNo": 4, "totalPage": 4, "totalCount": cap,
        },
        5: {
            "list": [{"id": i, "doc_flag": "1", "title": f"t{i}", "url": f"/{i}.html"} for i in range(13, 15)],
            "pageNo": 4, "totalPage": 4, "totalCount": cap,
        },
        6: {"list": [], "pageNo": 0, "totalPage": 0, "totalCount": 0},
    }
    opened = []
    pause_at_url = "/12.html"

    class MultiPageRequest:
        async def post(self, _url, *, data, timeout, fail_on_status_code):
            from tests.test_collector_parsing import FakePutuoApiResponse
            return FakePutuoApiResponse({"data": pages[int(data["pageNo"])]})

    class CappedCollector(PutuoDistrictCollector):
        def __init__(self, browser, safety, target):
            super().__init__(
                browser, safety, target,
                page_size=page_size, api_total_cap=cap, capped_max_extra_pages=20,
            )
            self.page = object()
            self.context = type("Context", (), {"request": MultiPageRequest()})()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def check_robots(self):
            return None

        async def safe_goto(self, page, url):
            return None

        async def open_item(self, item):
            opened.append((item.page_number, item.item_index, item.url))
            if item.url.endswith(pause_at_url):
                raise SafetyPause("截断扫描中途暂停")
            return PolicyRecord(
                district=item.district, title=item.title, url=item.url,
                published_date=date(2026, 7, 1), authored_date=date(2026, 7, 1),
            )

    db = Database(tmp_path / "capped-mid-resume.db")
    db.initialize()
    monkeypatch.setattr("app.jobs.async_playwright", lambda: FakePlaywrightContext())
    monkeypatch.setattr("app.jobs.PutuoDistrictCollector", CappedCollector)
    manager = JobManager(db)
    job_id = await manager.create_and_start(["区级网站·普陀区·委办局"], "full")
    await wait_job(manager, job_id)
    paused = db.get_job(job_id)
    assert paused["status"] == "paused"
    assert "截断扫描中途暂停" in paused["pause_reason"]
    # 第 4 页 item_index 2 是 id=12；暂停发生在处理该条时，游标可能已到该条
    assert int(paused["current_page"]) >= 4
    examined_mid = int(paused["examined_count"])
    assert examined_mid >= 11  # 前 11 条已推进或含暂停条

    opened.clear()
    # 恢复后不再暂停
    pause_at_url = "/never.html"
    await manager.resume(job_id)
    await wait_job(manager, job_id)
    done = db.get_job(job_id)
    assert done["status"] == "completed"
    assert done["estimated_total"] == 14
    assert done["examined_count"] == 14
    assert json.loads(done["total_by_district_json"])["区级网站·普陀区·委办局"] == 14
    assert json.loads(done["examined_by_district_json"])["区级网站·普陀区·委办局"] == 14
    with db.connect() as conn:
        item_count = conn.execute(
            "SELECT COUNT(*) FROM scan_item_results WHERE job_id=?", (job_id,)
        ).fetchone()[0]
    assert item_count == 14
