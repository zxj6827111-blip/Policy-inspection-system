import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.config import ContinuousScanConfig, targets_for_site
from app.db import Database
from app.jobs import JobManager, JobStatus


@pytest.fixture
def gating_runtime(tmp_path):
    db = Database(tmp_path / "gate.db")
    db.initialize()
    manager = JobManager(db)
    manager.continuous_config = ContinuousScanConfig(
        incremental_interval_hours=6,
        full_reconcile_interval_days=7,
        site_keys=("municipal_putuo",),
        enabled=True,
    )
    return manager, db


@pytest.mark.asyncio
async def test_site_without_baseline_gets_no_scheduled_job(gating_runtime, monkeypatch):
    manager, db = gating_runtime
    monkeypatch.setattr(manager, "eligible_baseline_for", lambda labels: None)
    await manager._tick_continuous_schedules()
    assert db.list_jobs() == []
    # 未建基准的站点也不应产生调度行
    assert manager.repo.list_schedules() == []


@pytest.mark.asyncio
async def test_site_with_baseline_gets_incremental_job(gating_runtime, monkeypatch):
    manager, db = gating_runtime
    labels = [t.label for t in targets_for_site("municipal_putuo")]
    baseline_id = db.create_job(labels, "full", {}, 0, None)
    db.update_job(baseline_id, status="completed", coverage_status="complete")
    monkeypatch.setattr(manager, "eligible_baseline_for", lambda _labels: {"id": baseline_id})

    async def fake_run(job_id):
        return None

    monkeypatch.setattr(manager, "_run", fake_run)
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    manager.repo.get_or_create_schedule("municipal_putuo", labels)
    manager.repo.update_schedule(
        "municipal_putuo", next_incremental_at=past, next_full_reconcile_at=None,
    )
    await manager._tick_continuous_schedules()
    await asyncio.sleep(0)
    incremental = [job for job in db.list_jobs() if job["mode"] == "incremental"]
    assert len(incremental) == 1
    assert int(incremental[0]["baseline_job_id"]) == baseline_id


def test_continuous_status_reports_baseline_gate(gating_runtime, monkeypatch):
    manager, _db = gating_runtime
    monkeypatch.setattr(manager, "eligible_baseline_for", lambda labels: None)
    status = manager.continuous_status()
    assert status["enabled"] is True
    assert status["sites"], "站点列表不应为空"
    assert all(site["has_baseline"] is False for site in status["sites"])
    # 只读接口不得创建调度行
    assert manager.repo.list_schedules() == []


@pytest.mark.asyncio
async def test_stale_full_due_is_skipped_but_incremental_still_runs(gating_runtime, monkeypatch):
    """停摆几个月的调度行，开关一开不能先补跑一个几万篇的全量。"""
    manager, db = gating_runtime
    labels = [t.label for t in targets_for_site("municipal_putuo")]
    baseline_id = db.create_job(labels, "full", {}, 0, None)
    db.update_job(baseline_id, status="completed", coverage_status="complete")
    monkeypatch.setattr(manager, "eligible_baseline_for", lambda _labels: {"id": baseline_id})

    async def fake_run(job_id):
        return None

    monkeypatch.setattr(manager, "_run", fake_run)
    manager.repo.get_or_create_schedule("municipal_putuo", labels)
    now = datetime.now(timezone.utc)
    manager.repo.update_schedule(
        "municipal_putuo",
        next_full_reconcile_at=(now - timedelta(days=90)).isoformat(),
        next_incremental_at=(now - timedelta(minutes=1)).isoformat(),
    )

    await manager._tick_continuous_schedules()
    await asyncio.sleep(0)

    jobs = db.list_jobs()
    assert not [job for job in jobs if job["mode"] == "full" and int(job["id"]) != baseline_id]
    scheduled = [job for job in jobs if job["mode"] == "incremental"]
    assert len(scheduled) == 1, "陈旧的全量要被跳过，本轮应改跑增量"
    row = manager.repo.get_schedule("municipal_putuo")
    assert datetime.fromisoformat(row["next_full_reconcile_at"]) > now + timedelta(days=6)


def test_interval_drift_converges_from_env(gating_runtime):
    """改环境变量里的周期后，表里的间隔和下一轮时间必须跟着收敛（不改代码也能生效）。"""
    manager, _db = gating_runtime
    labels = [t.label for t in targets_for_site("municipal_putuo")]
    manager.repo.get_or_create_schedule(
        "municipal_putuo", labels, incremental_hours=6, reconcile_days=7,
    )
    row = manager.repo.get_or_create_schedule(
        "municipal_putuo", labels, incremental_hours=720, reconcile_days=3650,
    )
    now = datetime.now(timezone.utc)
    assert float(row["incremental_interval_hours"]) == 720
    assert float(row["full_reconcile_interval_days"]) == 3650
    assert datetime.fromisoformat(row["next_incremental_at"]) > now + timedelta(days=29)
    assert datetime.fromisoformat(row["next_full_reconcile_at"]) > now + timedelta(days=3640)


def test_failed_job_backs_off_exponentially_and_caps_at_one_day(gating_runtime):
    """任务持续失败时退避：15 → 30 → 60 …，封顶不超过一天，且绝不停止自动重试。"""
    manager, db = gating_runtime
    labels = [t.label for t in targets_for_site("municipal_putuo")]
    manager.repo.get_or_create_schedule("municipal_putuo", labels)
    job_id = db.create_job(labels, "incremental", {}, 0, None)
    manager.repo.update_schedule("municipal_putuo", last_job_id=job_id, consecutive_failures=0)

    delays = []
    for expected in range(1, 9):
        manager._update_schedule_after_job(job_id, JobStatus.FAILED)
        row = manager.repo.get_schedule("municipal_putuo")
        assert int(row["consecutive_failures"]) == expected
        delays.append(
            (datetime.fromisoformat(row["next_incremental_at"]) - datetime.now(timezone.utc))
            .total_seconds() / 60
        )

    assert 14.5 < delays[0] < 15.5
    assert 29.5 < delays[1] < 30.5
    assert 59.5 < delays[2] < 60.5
    assert max(delays) <= 361, f"封顶不得超过一天/一个周期：{delays}"
    assert delays[-1] > 350, f"封顶后应稳定在 360 分钟（6 小时周期）：{delays}"
    assert all(delay > 0 for delay in delays), "必须继续自动重试，不能停在这里等人"


def test_completed_job_resets_failure_counter(gating_runtime):
    manager, db = gating_runtime
    labels = [t.label for t in targets_for_site("municipal_putuo")]
    manager.repo.get_or_create_schedule("municipal_putuo", labels)
    job_id = db.create_job(labels, "incremental", {}, 0, None)
    manager.repo.update_schedule("municipal_putuo", last_job_id=job_id, consecutive_failures=3)

    manager._update_schedule_after_job(job_id, JobStatus.COMPLETED)

    row = manager.repo.get_schedule("municipal_putuo")
    assert int(row["consecutive_failures"]) == 0
    assert datetime.fromisoformat(row["next_incremental_at"]) > datetime.now(timezone.utc) + timedelta(hours=5)


@pytest.mark.asyncio
async def test_tick_takes_one_backup_and_records_it_on_the_job(gating_runtime, monkeypatch):
    """备份按"要扫描了"触发：一批里只备一次，并写进本轮任务的事件里。"""
    manager, db = gating_runtime
    labels = [t.label for t in targets_for_site("municipal_putuo")]
    baseline_id = db.create_job(labels, "full", {}, 0, None)
    db.update_job(baseline_id, status="completed", coverage_status="complete")
    monkeypatch.setattr(manager, "eligible_baseline_for", lambda _labels: {"id": baseline_id})

    async def fake_run(job_id):
        return None

    monkeypatch.setattr(manager, "_run", fake_run)
    calls = []

    async def fake_backup():
        calls.append(1)
        return "扫描前备份完成：stub.db"

    monkeypatch.setattr(manager, "_backup_before_scan", fake_backup)
    manager.repo.get_or_create_schedule("municipal_putuo", labels)
    manager.repo.update_schedule(
        "municipal_putuo",
        next_incremental_at=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
    )

    await manager._tick_continuous_schedules()
    await asyncio.sleep(0)

    assert len(calls) == 1
    job = [row for row in db.list_jobs() if row["mode"] == "incremental"][0]
    with db.connect() as conn:
        events = conn.execute(
            "SELECT event_type,message FROM job_events WHERE job_id=?", (job["id"],),
        ).fetchall()
    assert [(row["event_type"], row["message"]) for row in events] == [
        ("pre_scan_backup", "扫描前备份完成：stub.db")
    ]


@pytest.mark.asyncio
async def test_scan_backup_happens_once_per_round(gating_runtime, tmp_path, monkeypatch):
    import app.config as config_module

    manager, _db = gating_runtime
    data_dir = tmp_path / "data"
    monkeypatch.setattr(config_module, "DATA_DIR", data_dir)

    first = await manager._backup_before_scan()
    second = await manager._backup_before_scan()

    assert "备份完成" in first, first
    assert "复用" in second, second
    files = list((data_dir / "backups").glob("policy_inspector_*.db"))
    assert len(files) == 1, files


@pytest.mark.asyncio
async def test_scan_backup_skips_when_disk_is_low(gating_runtime, tmp_path, monkeypatch):
    import app.config as config_module
    import app.jobs as jobs_module

    manager, _db = gating_runtime
    monkeypatch.setattr(config_module, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(jobs_module, "SCAN_BACKUP_MIN_FREE_GIB", 10 ** 9)

    message = await manager._backup_before_scan()

    assert "跳过扫描前备份" in message, message
    assert not list((tmp_path / "data" / "backups").glob("policy_inspector_*.db"))
