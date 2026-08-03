import json

import pytest

from app.config import SCAN_SITES, resolve_site, resolve_target, targets_for_site
from app.db import Database
from app.jobs import JobManager


def test_province_city_hierarchy_configuration():
    site = resolve_site("jiangsu_huaian")
    assert site.province == "江苏省"
    assert site.city == "淮安市"
    assert site.host == "www.huaian.gov.cn"
    assert site.target_keys == ("huaian_government", "huaian_bureaus", "huaian_counties")
    assert [t.label for t in targets_for_site("jiangsu_huaian")] == [
        "地市级网站·淮安市·市政府文件",
        "地市级网站·淮安市·部门文件",
        "地市级网站·淮安市·县区文件",
    ]

    suqian = resolve_site("jiangsu_suqian")
    assert suqian.province == "江苏省"
    assert suqian.city == "宿迁市"
    assert targets_for_site("jiangsu_suqian")[0].key == "suqian_zcwj"

    putuo = resolve_site("putuo_district")
    assert putuo.province == "上海市"
    assert putuo.city == "上海市"
    municipal = resolve_site("municipal_putuo")
    assert municipal.province == "上海市"
    assert municipal.host == "www.shanghai.gov.cn"


def test_new_targets_resolve_by_key_and_label():
    target = resolve_target("huaian_bureaus")
    assert target.collector_type == "huaian"
    assert target.host == "www.huaian.gov.cn"
    assert target.channel_id == "bm"
    assert resolve_target("地市级网站·淮安市·市政府文件").key == "huaian_government"
    assert resolve_target("地市级网站·宿迁市·政策文件库").key == "suqian_zcwj"


def test_all_sites_carry_province_and_city():
    for key, site in SCAN_SITES.items():
        assert site.province, key
        assert site.city, key


@pytest.mark.asyncio
async def test_new_site_jobs_lock_on_own_host(tmp_path, monkeypatch):
    """淮安/宿迁任务分别按各自域名加同域锁。"""
    db = Database(tmp_path / "new-site-jobs.db")
    db.initialize()
    manager = JobManager(db)
    captured: list[list[str]] = []

    async def no_run(job_id):
        return None

    async def capture_run(job_id):
        job = db.get_job(job_id)
        targets = [resolve_target(value) for value in json.loads(job["districts_json"])]
        captured.append(sorted({t.host for t in targets}))
        await no_run(job_id)

    monkeypatch.setattr(manager, "_run", capture_run)
    job_id = await manager.create_and_start(["地市级网站·淮安市·市政府文件"], "full")
    await manager._tasks[job_id]
    assert captured[-1] == ["www.huaian.gov.cn"]
    job_id = await manager.create_and_start(["地市级网站·宿迁市·政策文件库"], "full")
    await manager._tasks[job_id]
    assert captured[-1] == ["www.suqian.gov.cn"]
    job_id = await manager.create_and_start(["区级网站·普陀区·区政府文件"], "full")
    await manager._tasks[job_id]
    assert captured[-1] == ["www.shpt.gov.cn"]
