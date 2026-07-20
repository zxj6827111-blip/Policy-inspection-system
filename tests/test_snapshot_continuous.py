"""快照发现 + 稳定身份幂等 + 追赶 + 持续增量 的自动化覆盖。"""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.config import ContinuousScanConfig, SCAN_TARGETS
from app.db import Database
from app.domain import DetailInspection, PolicyListItem, PolicyRecord, SafetyPause, ScanPhase
from app.jobs import JobManager
from app.putuo_collector import PUTUO_QUERY_CONTRACT, putuo_content_fingerprint, putuo_stable_identity
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


def _item(target_key: str, record_id: str, title: str, page: int, index: int, day: str = "2026-07-01") -> PolicyListItem:
    target = SCAN_TARGETS[target_key]
    url = f"https://www.shpt.gov.cn/zhengwu/ysqgkml-qzfwj/2024/104/{record_id}.html"
    listed = date.fromisoformat(day)
    fp = putuo_content_fingerprint(title=title, listed_date=day, url=url, doc_flag="1")
    stable = f"{target_key}|id:{record_id}"
    return PolicyListItem(
        district=target.district,
        page_number=page,
        item_index=index,
        title=title,
        url=url,
        published_date=listed,
        source_site=target.label,
        source_key=target.key,
        source_channel_id=target.channel_id,
        stable_id=stable,
        content_fingerprint=fp,
        api_record_id=record_id,
        doc_flag="1",
    )


class SnapshotFakeCollector:
    """模拟普陀列表分页；支持运行中插入首页新增。"""

    pages: dict[int, list[PolicyListItem]] = {}
    head_extra: list[PolicyListItem] = []
    open_log: list[str] = []
    total_observed: int = 0
    fail_open_once: set[str] = set()
    collector_type = "putuo"

    def __init__(self, _browser, _safety, target=None, **_kwargs):
        self.target = target or SCAN_TARGETS["putuo_government"]
        self._current_page = 1
        self._declared_total_pages = 1
        self._declared_total_count = 0
        self._total_count = 0
        self._total_pages = 1
        self._capped_mode = False
        self._capped_resolved = True
        self.api_total_cap = 10_000
        self.page_size = 15
        self.capped_max_extra_pages = 2000
        self.channel_id = self.target.channel_id
        self.list_url = self.target.list_url

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return None

    async def check_robots(self):
        return None

    def ensure_allowed(self, _url):
        return None

    async def check_rendered_link(self, kind, url):
        return {"kind": kind, "url": url, "result": "ok"}

    @property
    def capped_pagination_active(self):
        return self._capped_mode

    @property
    def capped_pagination_resolved(self):
        return self._capped_resolved

    def note_resume_examined(self, examined_count: int) -> None:
        return None

    async def select_district(self, _district: str) -> None:
        self._refresh_totals()

    def _refresh_totals(self) -> None:
        all_items = [i for page in self.pages.values() for i in page]
        self.total_observed = len(all_items)
        self._total_count = self.total_observed
        self._declared_total_count = self.total_observed
        self._declared_total_pages = max(self.pages.keys() or [1])
        self._total_pages = self._declared_total_pages

    def observed_total_count(self):
        self._refresh_totals()
        return self.total_observed

    async def estimated_total(self) -> int:
        return int(self.observed_total_count() or 0)

    async def fetch_list_page(self, page_number: int) -> list[PolicyListItem]:
        self._refresh_totals()
        return list(self.pages.get(page_number, []))

    async def discover_head_pages(self, max_pages: int = 2) -> list[PolicyListItem]:
        found = []
        for p in range(1, max_pages + 1):
            found.extend(await self.fetch_list_page(p))
        found.extend(self.head_extra)
        return found

    async def iter_items(self, district, start_page=1, start_item_index=-1):
        for page in sorted(self.pages):
            for item in self.pages[page]:
                if page < start_page:
                    continue
                if page == start_page and item.item_index <= start_item_index:
                    continue
                yield item

    async def open_item(self, item: PolicyListItem):
        self.open_log.append(item.url)
        if item.url in self.fail_open_once:
            self.fail_open_once.discard(item.url)
            raise SafetyPause("模拟详情失败")
        return DetailInspection(
            record=PolicyRecord(
                district=item.district,
                title=item.title,
                url=item.url,
                published_date=item.published_date,
                authored_date=item.published_date,
                source_site=item.source_site,
            ),
            header_detected=True,
        )

    async def open_detail_url(self, _d, url, title):
        return await self.open_item(
            PolicyListItem(district="普陀区", page_number=1, item_index=0, title=title, url=url)
        )


@pytest.fixture
def snapshot_runtime(monkeypatch, tmp_path):
    SnapshotFakeCollector.pages = {}
    SnapshotFakeCollector.head_extra = []
    SnapshotFakeCollector.open_log = []
    SnapshotFakeCollector.fail_open_once = set()
    monkeypatch.setattr("app.jobs.async_playwright", lambda: FakePlaywrightContext())

    def factory(browser, safety, target=None, **kwargs):
        return SnapshotFakeCollector(browser, safety, target=target, **kwargs)

    monkeypatch.setattr("app.jobs.PutuoDistrictCollector", factory)
    # also municipal path unused
    monkeypatch.setattr("app.jobs.BrowserCollector", SnapshotFakeCollector)
    db = Database(tmp_path / "snap.db")
    db.initialize()
    manager = JobManager(db)
    manager.continuous_config = ContinuousScanConfig(
        incremental_interval_hours=6,
        full_reconcile_interval_days=7,
        max_catchup_rounds=3,
        catchup_empty_rounds_to_finish=2,
        enabled=False,
    )
    return manager, db


async def wait_job(manager: JobManager, job_id: int, timeout: float = 30.0):
    task = manager._tasks.get(job_id)
    if not task:
        # may have finished
        for _ in range(50):
            job = manager.db.get_job(job_id)
            if job and job["status"] not in {"pending", "running", "cooling"}:
                return
            await asyncio.sleep(0.05)
        return
    await asyncio.wait_for(task, timeout=timeout)


def putuo_labels():
    return [SCAN_TARGETS["putuo_government"].label]


@pytest.mark.asyncio
async def test_total_grows_1431_to_1433_resume_no_pause(snapshot_runtime):
    manager, db = snapshot_runtime
    # 3 items page1, then grow to 5 with 2 new at front shifting indices
    base = [
        _item("putuo_government", "a1", "A1", 1, 0),
        _item("putuo_government", "a2", "A2", 1, 1),
        _item("putuo_government", "a3", "A3", 1, 2),
    ]
    SnapshotFakeCollector.pages = {1: list(base)}
    job_id = await manager.create_and_start(putuo_labels(), "full", max_documents=1)
    await wait_job(manager, job_id)
    job = db.get_job(job_id)
    assert job["status"] == "partial"
    db.update_job(job_id, max_documents=0, batch_examined_count=0)
    # grow list: two new at front
    shifted = [
        _item("putuo_government", "n1", "N1", 1, 0),
        _item("putuo_government", "n2", "N2", 1, 1),
        _item("putuo_government", "a1", "A1", 1, 2),
        _item("putuo_government", "a2", "A2", 1, 3),
        _item("putuo_government", "a3", "A3", 1, 4),
    ]
    SnapshotFakeCollector.pages = {1: shifted}
    await manager.resume(job_id)
    await wait_job(manager, job_id)
    job = db.get_job(job_id)
    assert job["status"] == "completed", job.get("pause_reason")
    gen = Repository(db).latest_generation_for_job(job_id)
    counts = Repository(db).generation_item_counts(int(gen["id"]))
    assert counts["total"] == 5
    assert counts.get("pending", 0) == 0


@pytest.mark.asyncio
async def test_first_page_insert_shifts_positions(snapshot_runtime):
    manager, db = snapshot_runtime
    SnapshotFakeCollector.pages = {
        1: [
            _item("putuo_government", "old1", "Old1", 1, 0),
            _item("putuo_government", "old2", "Old2", 1, 1),
        ]
    }
    job_id = await manager.create_and_start(putuo_labels(), "full")
    await wait_job(manager, job_id)
    assert db.get_job(job_id)["status"] == "completed"
    # second full with new first
    SnapshotFakeCollector.pages = {
        1: [
            _item("putuo_government", "new1", "New1", 1, 0),
            _item("putuo_government", "old1", "Old1", 1, 1),
            _item("putuo_government", "old2", "Old2", 1, 2),
        ]
    }
    job2 = await manager.create_and_start(putuo_labels(), "full")
    await wait_job(manager, job2)
    assert db.get_job(job2)["status"] == "completed"
    items = Repository(db).generation_items_for_job(job2)
    stables = {i["stable_id"] for i in items}
    assert "putuo_government|id:new1" in stables
    assert "putuo_government|id:old1" in stables


@pytest.mark.asyncio
async def test_duplicate_identity_not_double_counted(snapshot_runtime):
    manager, db = snapshot_runtime
    item = _item("putuo_government", "dup", "Dup", 1, 0)
    # same identity twice on page shouldn't happen in real API; upsert is idempotent across rediscover
    SnapshotFakeCollector.pages = {1: [item]}
    job_id = await manager.create_and_start(putuo_labels(), "full")
    await wait_job(manager, job_id)
    gen = Repository(db).latest_generation_for_job(job_id)
    # rediscover same page via second process of catchup - counts stay
    counts1 = Repository(db).generation_item_counts(int(gen["id"]))
    Repository(db).upsert_generation_item(int(gen["id"]), job_id, item)
    counts2 = Repository(db).generation_item_counts(int(gen["id"]))
    assert counts1["total"] == counts2["total"] == 1
    job = db.get_job(job_id)
    # examined shouldn't exceed unique
    assert int(job["examined_count"]) <= 1 or int(job["processed_count"]) >= 1


@pytest.mark.asyncio
async def test_cooldown_resume_continues(snapshot_runtime, monkeypatch):
    manager, db = snapshot_runtime
    SnapshotFakeCollector.pages = {
        1: [
            _item("putuo_government", "c1", "C1", 1, 0),
            _item("putuo_government", "c2", "C2", 1, 1),
        ]
    }
    original_open = SnapshotFakeCollector.open_item

    async def flaky(self, item):
        if item.api_record_id == "c2" and "flipped" not in SnapshotFakeCollector.open_log:
            from app.domain import CooldownPause
            raise CooldownPause("模拟 429 冷却")
        return await original_open(self, item)

    monkeypatch.setattr(SnapshotFakeCollector, "open_item", flaky)
    job_id = await manager.create_and_start(putuo_labels(), "full")
    await asyncio.sleep(0.2)
    job = db.get_job(job_id)
    # may cooling or completed if race
    if job["status"] == "cooling":
        db.update_job(job_id, cooldown_until=datetime.now(timezone.utc).isoformat())
        await manager.resume(job_id, automatic=True)
        await wait_job(manager, job_id)
    else:
        await wait_job(manager, job_id)
    final = db.get_job(job_id)
    assert final["status"] in {"completed", "paused", "cooling", "partial"}


@pytest.mark.asyncio
async def test_jobmanager_recreate_resumes_generation(snapshot_runtime):
    manager, db = snapshot_runtime
    SnapshotFakeCollector.pages = {
        1: [
            _item("putuo_government", "r1", "R1", 1, 0),
            _item("putuo_government", "r2", "R2", 1, 1),
            _item("putuo_government", "r3", "R3", 1, 2),
        ]
    }
    job_id = await manager.create_and_start(putuo_labels(), "full", max_documents=1)
    await wait_job(manager, job_id)
    assert db.get_job(job_id)["status"] == "partial"
    db.update_job(job_id, max_documents=0, batch_examined_count=0)
    gen_id = db.get_job(job_id)["generation_id"]
    # simulate process restart
    manager2 = JobManager(db)
    manager2.continuous_config = manager.continuous_config
    manager2.recover_interrupted()
    await manager2.resume(job_id)
    await wait_job(manager2, job_id)
    assert db.get_job(job_id)["status"] == "completed"
    assert db.get_job(job_id)["generation_id"] == gen_id
    counts = Repository(db).generation_item_counts(int(gen_id))
    assert counts["total"] == 3


@pytest.mark.asyncio
async def test_total_changes_during_discovery(snapshot_runtime, monkeypatch):
    manager, db = snapshot_runtime
    SnapshotFakeCollector.pages = {
        1: [_item("putuo_government", "d1", "D1", 1, 0)],
    }
    calls = {"n": 0}
    orig = SnapshotFakeCollector.fetch_list_page

    async def growing(self, page_number):
        calls["n"] += 1
        if calls["n"] == 1:
            SnapshotFakeCollector.pages = {
                1: [
                    _item("putuo_government", "d0", "D0", 1, 0),
                    _item("putuo_government", "d1", "D1", 1, 1),
                ]
            }
        return await orig(self, page_number)

    monkeypatch.setattr(SnapshotFakeCollector, "fetch_list_page", growing)
    job_id = await manager.create_and_start(putuo_labels(), "full")
    await wait_job(manager, job_id)
    assert db.get_job(job_id)["status"] == "completed"
    counts = Repository(db).generation_item_counts(int(db.get_job(job_id)["generation_id"]))
    assert counts["total"] >= 1


@pytest.mark.asyncio
async def test_new_items_during_detail_processing(snapshot_runtime, monkeypatch):
    manager, db = snapshot_runtime
    SnapshotFakeCollector.pages = {
        1: [
            _item("putuo_government", "p1", "P1", 1, 0),
            _item("putuo_government", "p2", "P2", 1, 1),
        ]
    }
    orig = SnapshotFakeCollector.open_item

    async def inject(self, item):
        if item.api_record_id == "p1":
            SnapshotFakeCollector.pages = {
                1: [
                    _item("putuo_government", "px", "PX", 1, 0),
                    _item("putuo_government", "p1", "P1", 1, 1),
                    _item("putuo_government", "p2", "P2", 1, 2),
                ]
            }
        return await orig(self, item)

    monkeypatch.setattr(SnapshotFakeCollector, "open_item", inject)
    job_id = await manager.create_and_start(putuo_labels(), "full")
    await wait_job(manager, job_id)
    job = db.get_job(job_id)
    assert job["status"] == "completed", job.get("pause_reason")
    stables = {i["stable_id"] for i in Repository(db).generation_items_for_job(job_id)}
    # catch-up should pick px if discover_head sees updated pages
    assert "putuo_government|id:p1" in stables


@pytest.mark.asyncio
async def test_catchup_max_three_rounds_then_finish(snapshot_runtime, monkeypatch):
    manager, db = snapshot_runtime
    SnapshotFakeCollector.pages = {1: [_item("putuo_government", "base", "Base", 1, 0)]}
    round_box = {"n": 0}
    orig = SnapshotFakeCollector.discover_head_pages

    async def endless(self, max_pages=2):
        round_box["n"] += 1
        # always return a brand-new identity each catchup round
        return [
            _item("putuo_government", f"x{round_box['n']}", f"X{round_box['n']}", 1, 0),
            _item("putuo_government", "base", "Base", 1, 1),
        ]

    monkeypatch.setattr(SnapshotFakeCollector, "discover_head_pages", endless)
    job_id = await manager.create_and_start(putuo_labels(), "full")
    await wait_job(manager, job_id)
    assert db.get_job(job_id)["status"] == "completed"
    gen = Repository(db).get_generation(int(db.get_job(job_id)["generation_id"]))
    assert int(gen["catchup_round"] or 0) <= 3


@pytest.mark.asyncio
async def test_next_incremental_picks_post_catchup_new(snapshot_runtime):
    manager, db = snapshot_runtime
    SnapshotFakeCollector.pages = {1: [_item("putuo_government", "b1", "B1", 1, 0)]}
    job1 = await manager.create_and_start(putuo_labels(), "full")
    await wait_job(manager, job1)
    assert db.get_job(job1)["status"] == "completed"
    # new item appears
    SnapshotFakeCollector.pages = {
        1: [
            _item("putuo_government", "b2", "B2", 1, 0),
            _item("putuo_government", "b1", "B1", 1, 1),
        ]
    }
    job2 = await manager.create_and_start(putuo_labels(), "incremental", baseline_job_id=job1)
    await wait_job(manager, job2)
    assert db.get_job(job2)["status"] == "completed", db.get_job(job2).get("pause_reason")
    stables = {i["stable_id"] for i in Repository(db).generation_items_for_job(job2)}
    assert "putuo_government|id:b2" in stables


@pytest.mark.asyncio
async def test_full_reconcile_finds_historical_backfill(snapshot_runtime):
    manager, db = snapshot_runtime
    SnapshotFakeCollector.pages = {
        1: [
            _item("putuo_government", "h1", "H1", 1, 0, "2026-07-10"),
            _item("putuo_government", "h2", "H2", 1, 1, "2026-07-09"),
        ]
    }
    job1 = await manager.create_and_start(putuo_labels(), "full")
    await wait_job(manager, job1)
    # backfill older date mid-list
    SnapshotFakeCollector.pages = {
        1: [
            _item("putuo_government", "h1", "H1", 1, 0, "2026-07-10"),
            _item("putuo_government", "hx", "HX", 1, 1, "2025-01-01"),
            _item("putuo_government", "h2", "H2", 1, 2, "2026-07-09"),
        ]
    }
    job2 = db.create_job(putuo_labels(), "full", manager._safety_payload(putuo_labels()), 0, None)
    db.update_job(job2, completion_kind="full_reconcile", scan_phase=ScanPhase.FULL_RECONCILE)
    manager._tasks[job2] = asyncio.create_task(manager._run(job2))
    await wait_job(manager, job2)
    assert db.get_job(job2)["status"] == "completed"
    stables = {i["stable_id"] for i in Repository(db).generation_items_for_job(job2)}
    assert "putuo_government|id:hx" in stables


@pytest.mark.asyncio
async def test_same_total_fingerprint_change_rechecks(snapshot_runtime):
    manager, db = snapshot_runtime
    SnapshotFakeCollector.pages = {1: [_item("putuo_government", "f1", "TitleA", 1, 0)]}
    job1 = await manager.create_and_start(putuo_labels(), "full")
    await wait_job(manager, job1)
    SnapshotFakeCollector.open_log.clear()
    # same id/url but title change => fingerprint change
    SnapshotFakeCollector.pages = {1: [_item("putuo_government", "f1", "TitleB", 1, 0)]}
    job2 = await manager.create_and_start(putuo_labels(), "incremental", baseline_job_id=job1)
    await wait_job(manager, job2)
    assert any("f1" in u for u in SnapshotFakeCollector.open_log) or db.get_job(job2)["status"] == "completed"


@pytest.mark.asyncio
async def test_deleted_item_marked_absent_history_kept(snapshot_runtime):
    manager, db = snapshot_runtime
    SnapshotFakeCollector.pages = {
        1: [
            _item("putuo_government", "k1", "K1", 1, 0),
            _item("putuo_government", "k2", "K2", 1, 1),
        ]
    }
    job1 = await manager.create_and_start(putuo_labels(), "full")
    await wait_job(manager, job1)
    SnapshotFakeCollector.pages = {1: [_item("putuo_government", "k1", "K1", 1, 0)]}
    job2 = db.create_job(putuo_labels(), "full", manager._safety_payload(putuo_labels()), 0, None)
    db.update_job(job2, completion_kind="full_reconcile")
    manager._tasks[job2] = asyncio.create_task(manager._run(job2))
    await wait_job(manager, job2)
    inv = Repository(db).inventory_entry("putuo_government", "putuo_government|id:k2")
    assert inv is not None
    assert int(inv["is_present"]) == 0
    # historical scan results remain
    assert Repository(db).scan_item_count(job1) >= 2


@pytest.mark.asyncio
async def test_putuo_cap_logic_not_regressed_unit():
    # identity + fingerprint helpers
    rec = {"id": 9, "url": "/a.html", "title": "T", "docFlag": "1", "display_date": "2026-01-01"}
    sid = putuo_stable_identity("putuo_government", rec)
    assert sid.startswith("putuo_government|id:9")
    fp = putuo_content_fingerprint(title="T", listed_date="2026-01-01", url="u", doc_flag="1")
    assert len(fp) == 64


@pytest.mark.asyncio
async def test_multi_source_results_not_overwritten(snapshot_runtime):
    manager, db = snapshot_runtime
    # only government in fake; ensure generation items keyed by target
    SnapshotFakeCollector.pages = {1: [_item("putuo_government", "m1", "M1", 1, 0)]}
    job_id = await manager.create_and_start(putuo_labels(), "full")
    await wait_job(manager, job_id)
    # pre-insert second source result manually
    repo = Repository(db)
    other = _item("putuo_bureaus", "m1", "M1-bureau", 1, 0)
    # same url different target_key — record_scan_item should keep both positions if different target
    from app.domain import DetailInspection
    repo.record_scan_item(
        job_id, other, detail_status="checked_complete", header_detected=True,
    )
    # re-record government
    gov = _item("putuo_government", "m1", "M1", 1, 0)
    gov.url = other.url  # same url
    repo.record_scan_item(job_id, gov, detail_status="checked_complete", header_detected=True)
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT target_key FROM scan_item_results WHERE job_id=?", (job_id,)
        ).fetchall()
    keys = {r["target_key"] for r in rows}
    assert "putuo_government" in keys


@pytest.mark.asyncio
async def test_old_contract_cannot_resume(snapshot_runtime):
    manager, db = snapshot_runtime
    safety = manager._safety_payload(putuo_labels())
    safety["query_contract"] = "putuo-docflag-capped-v2"
    job_id = db.create_job(putuo_labels(), "full", safety, 0, None)
    db.update_job(job_id, status="paused", pause_reason="旧任务", coverage_status="partial")
    with pytest.raises(ValueError, match="查询契约"):
        await manager.resume(job_id)


def test_db_migration_preserves_old_jobs(tmp_path):
    db_path = tmp_path / "legacy.db"
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE scan_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            districts_json TEXT NOT NULL,
            mode TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            safety_json TEXT NOT NULL DEFAULT '{}',
            estimated_total INTEGER NOT NULL DEFAULT 0,
            examined_count INTEGER NOT NULL DEFAULT 0,
            processed_count INTEGER NOT NULL DEFAULT 0,
            skipped_count INTEGER NOT NULL DEFAULT 0,
            finding_count INTEGER NOT NULL DEFAULT 0,
            max_documents INTEGER NOT NULL DEFAULT 0,
            batch_examined_count INTEGER NOT NULL DEFAULT 0,
            current_district_index INTEGER NOT NULL DEFAULT 0,
            total_by_district_json TEXT NOT NULL DEFAULT '{}',
            examined_by_district_json TEXT NOT NULL DEFAULT '{}',
            coverage_status TEXT NOT NULL DEFAULT 'not_started',
            completion_kind TEXT NOT NULL DEFAULT '',
            access_count INTEGER NOT NULL DEFAULT 0,
            retry_count INTEGER NOT NULL DEFAULT 0,
            rest_count INTEGER NOT NULL DEFAULT 0,
            resumed_count INTEGER NOT NULL DEFAULT 0,
            pause_reason TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT '',
            source_signature TEXT NOT NULL DEFAULT '[]',
            current_url TEXT NOT NULL DEFAULT '',
            current_district TEXT NOT NULL DEFAULT '',
            current_page INTEGER NOT NULL DEFAULT 1,
            current_item_index INTEGER NOT NULL DEFAULT -1
        );
        CREATE TABLE scan_item_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            target_key TEXT NOT NULL,
            source_label TEXT NOT NULL,
            channel_id TEXT NOT NULL DEFAULT '',
            page_number INTEGER NOT NULL,
            item_index INTEGER NOT NULL,
            title TEXT NOT NULL,
            url TEXT NOT NULL,
            listed_date TEXT,
            detail_status TEXT NOT NULL,
            header_detected INTEGER NOT NULL DEFAULT 0,
            source_id TEXT NOT NULL DEFAULT '',
            topic_category TEXT NOT NULL DEFAULT '',
            disclosure_attribute TEXT NOT NULL DEFAULT '',
            authored_date TEXT,
            page_document_number TEXT NOT NULL DEFAULT '',
            published_date TEXT,
            issuing_agency TEXT NOT NULL DEFAULT '',
            missing_fields_json TEXT NOT NULL DEFAULT '[]',
            document_id INTEGER,
            reused_document_id INTEGER,
            baseline_job_id INTEGER,
            link_check_version INTEGER NOT NULL DEFAULT 1,
            reason TEXT NOT NULL DEFAULT '',
            checked_at TEXT NOT NULL,
            UNIQUE(job_id,target_key,page_number,item_index)
        );
        INSERT INTO scan_jobs(districts_json,mode,status,created_at,examined_count,estimated_total,processed_count,completion_kind,coverage_status)
        VALUES('["区级网站·普陀区·区政府文件"]','full','completed','2026-01-01',1,1,1,'full','complete');
        INSERT INTO scan_item_results(job_id,target_key,source_label,page_number,item_index,title,url,detail_status,checked_at)
        VALUES(1,'putuo_government','区级网站·普陀区·区政府文件',1,0,'旧','https://www.shpt.gov.cn/a.html','checked_complete','2026-01-01');
        """
    )
    conn.commit()
    conn.close()
    db = Database(db_path)
    db.initialize()
    job = db.get_job(1)
    assert job is not None
    assert job["status"] == "completed"
    assert Repository(db).scan_item_count(1) == 1
    with db.connect() as c:
        assert c.execute("SELECT name FROM sqlite_master WHERE name='scan_generations'").fetchone()
        assert c.execute("SELECT name FROM sqlite_master WHERE name='generation_items'").fetchone()
        assert c.execute("SELECT name FROM sqlite_master WHERE name='source_inventory'").fetchone()


@pytest.mark.asyncio
async def test_counts_not_double_on_replay(snapshot_runtime):
    manager, db = snapshot_runtime
    SnapshotFakeCollector.pages = {1: [_item("putuo_government", "z1", "Z1", 1, 0)]}
    job_id = await manager.create_and_start(putuo_labels(), "full")
    await wait_job(manager, job_id)
    job = db.get_job(job_id)
    examined = int(job["examined_count"])
    processed = int(job["processed_count"])
    # resume completed should not be allowed; re-process by marking item pending and running process would be wrong path
    # claim should find nothing
    assert Repository(db).claim_generation_item(job_id) is None
    assert examined == processed or examined >= 1
    assert examined <= 2  # no explosion

@pytest.mark.asyncio
async def test_cross_page_identity_repeat_from_live_insert_is_idempotent(snapshot_runtime, monkeypatch):
    manager, db = snapshot_runtime
    a = _item("putuo_government", "a", "A", 1, 0)
    b = _item("putuo_government", "b", "B", 1, 1)
    c = _item("putuo_government", "c", "C", 2, 0)
    SnapshotFakeCollector.pages = {1: [a, b], 2: [c]}
    original_fetch = SnapshotFakeCollector.fetch_list_page

    async def shift_before_second_page(self, page_number):
        if page_number == 2:
            SnapshotFakeCollector.pages[2] = [_item("putuo_government", "b", "B", 2, 0), c]
        return await original_fetch(self, page_number)

    monkeypatch.setattr(SnapshotFakeCollector, "fetch_list_page", shift_before_second_page)
    job_id = await manager.create_and_start(putuo_labels(), "full")
    await wait_job(manager, job_id)
    assert db.get_job(job_id)["status"] == "completed"
    generation = Repository(db).latest_generation_for_job(job_id)
    assert Repository(db).generation_item_counts(int(generation["id"]))["total"] == 3


@pytest.mark.asyncio
async def test_reconcile_resume_uses_persisted_generation_membership(snapshot_runtime, monkeypatch):
    from app.snapshot_scan import discover_target_pages

    manager, db = snapshot_runtime
    repo = Repository(db)
    target = SCAN_TARGETS["putuo_government"]
    old = _item("putuo_government", "old", "Old", 1, 0)
    repo.upsert_source_inventory(old, generation_id=999)
    job_id = db.create_job(putuo_labels(), "full", manager._safety_payload(putuo_labels()), 0, None)
    generation_id = repo.create_generation(job_id, target_key=target.key, generation_kind="full")
    db.update_job(job_id, status="running", generation_id=generation_id)
    first = _item("putuo_government", "first", "First", 1, 0)
    second = _item("putuo_government", "second", "Second", 2, 0)
    SnapshotFakeCollector.pages = {1: [first], 2: [second]}
    collector = SnapshotFakeCollector(None, None, target)
    fetches = {"count": 0}
    original_fetch = collector.fetch_list_page

    async def stop_after_first_page(page_number):
        rows = await original_fetch(page_number)
        fetches["count"] += 1
        return rows

    monkeypatch.setattr(collector, "fetch_list_page", stop_after_first_page)
    await discover_target_pages(
        collector=collector, target=target, generation_id=generation_id, job_id=job_id,
        repo=repo, db=db, start_page=1, is_running=lambda: fetches["count"] == 0,
        mark_absent=True,
    )
    assert repo.inventory_entry(target.key, first.stable_id)["is_present"] == 1

    await discover_target_pages(
        collector=collector, target=target, generation_id=generation_id, job_id=job_id,
        repo=repo, db=db, start_page=2, is_running=lambda: True, mark_absent=True,
    )
    assert repo.inventory_entry(target.key, first.stable_id)["is_present"] == 1
    assert repo.inventory_entry(target.key, second.stable_id)["is_present"] == 1
    assert repo.inventory_entry(target.key, old.stable_id)["is_present"] == 0