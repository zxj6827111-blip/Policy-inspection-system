import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.config import ContinuousScanConfig, targets_for_site
from app.db import Database
from app.jobs import JobManager


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
