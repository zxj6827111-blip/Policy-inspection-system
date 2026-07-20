from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack, suppress
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone

from chinese_calendar import is_workday
from playwright.async_api import async_playwright

from app.collector import BrowserCollector, LinkChecker
from app.config import ContinuousScanConfig, SafetyConfig, ScanTarget, resolve_target, SCAN_SITES
from app.db import Database, utc_now
from app.domain import (
    CooldownPause,
    DetailInspection,
    Finding,
    ItemReviewRequired,
    JobStatus,
    PolicyListItem,
    PolicyRecord,
    SafetyPause,
    ScanPhase,
)
from app.putuo_collector import PUTUO_QUERY_CONTRACT, PutuoDistrictCollector
from app.repository import PAGE_LINK_CHECK_VERSION, Repository
from app.rules import WorkdayCalendar, evaluate_record
from app.safety import SafetyController
from app.snapshot_scan import (
    catchup_head,
    coverage_from_generation,
    discover_target_pages,
    list_item_from_generation_row,
    refresh_generation_stats,
)


class JobManager:
    def __init__(self, db: Database):
        self.db = db
        self.repo = Repository(db)
        self._tasks: dict[int, asyncio.Task] = {}
        self._guard = asyncio.Lock()
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._run_slots = asyncio.Semaphore(2)
        self.continuous_config = ContinuousScanConfig.from_env()
        self._scheduler_task: asyncio.Task | None = None

    async def create_and_start(
        self, districts: list[str], mode: str, max_documents: int = 0, baseline_job_id: int | None = None,
    ) -> int:
        async with self._guard:
            active = next((
                job for job in self.db.list_jobs(1000)
                if job["status"] in {JobStatus.PENDING, JobStatus.RUNNING, JobStatus.COOLING}
                and self._job_sources(job) == sorted(districts)
            ), None)
            if active:
                return int(active["id"])
            if mode == "incremental":
                self._validate_incremental_baseline(districts, baseline_job_id)
            elif baseline_job_id is not None:
                raise ValueError("全量扫描不能指定基准任务")
            safety = self._safety_payload(districts)
            job_id = self.db.create_job(districts, mode, safety, max_documents, baseline_job_id)
            self._tasks[job_id] = asyncio.create_task(self._run(job_id))
            return job_id


    @staticmethod
    def _job_sources(job: dict) -> list[str]:
        try:
            sources = json.loads(job.get("source_signature") or "[]")
        except (TypeError, json.JSONDecodeError):
            sources = []
        if not sources:
            try:
                sources = json.loads(job["districts_json"])
            except (KeyError, TypeError, json.JSONDecodeError):
                sources = []
        return sorted(str(source) for source in sources)

    @staticmethod
    def _targets_include_putuo(districts: list[str]) -> bool:
        for value in districts:
            try:
                if resolve_target(value).collector_type == "putuo":
                    return True
            except ValueError:
                continue
        return False

    @classmethod
    def _safety_payload(cls, districts: list[str], safety: dict | None = None) -> dict:
        payload = dict(safety or asdict(SafetyConfig()))
        if cls._targets_include_putuo(districts):
            payload["query_contract"] = PUTUO_QUERY_CONTRACT
        return payload

    @classmethod
    def _job_query_contract(cls, job: dict) -> str:
        try:
            safety = json.loads(job.get("safety_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            safety = {}
        return str(safety.get("query_contract") or "")

    @classmethod
    def _assert_putuo_query_contract(cls, job: dict, *, action: str) -> None:
        districts = cls._job_sources(job)
        if not cls._targets_include_putuo(districts):
            return
        contract = cls._job_query_contract(job)
        if contract != PUTUO_QUERY_CONTRACT:
            raise ValueError(
                f"旧普陀区任务的列表查询契约已失效（当前需要 {PUTUO_QUERY_CONTRACT}），"
                f"不能{action}。请通过“完整全量重建”创建新任务，勿恢复修复前的 paused/partial 任务"
            )

    def _matching_jobs(self, districts: list[str]) -> list[dict]:
        signature = sorted(districts)
        return [job for job in self.db.list_jobs(1000) if self._job_sources(job) == signature]

    def _active_job_for(self, districts: list[str]) -> dict | None:
        return next((
            job for job in self._matching_jobs(districts)
            if job["status"] in {JobStatus.PENDING, JobStatus.RUNNING, JobStatus.COOLING}
        ), None)

    def eligible_baseline_for(self, districts: list[str]) -> dict | None:
        for job in self.db.eligible_baselines():
            if self._job_sources(job) != sorted(districts):
                continue
            try:
                self._validate_incremental_baseline(districts, int(job["id"]))
            except ValueError:
                continue
            return job
        return None

    def automatic_plan(self, districts: list[str]) -> dict:
        baseline = self.eligible_baseline_for(districts)
        matching = self._matching_jobs(districts)
        latest = matching[0] if matching else None
        resumable = {
            JobStatus.PENDING, JobStatus.RUNNING, JobStatus.COOLING,
            JobStatus.PAUSED, JobStatus.PARTIAL, JobStatus.FAILED,
        }
        if (
            latest
            and latest["status"] in resumable
            and (not baseline or int(latest["id"]) > int(baseline["id"]))
        ):
            try:
                self._assert_putuo_query_contract(latest, action="恢复")
            except ValueError:
                # 查询契约已变化的旧任务不可自动恢复，改为创建新任务。
                latest = None
            if latest is not None:
                action = "existing" if latest["status"] in {
                    JobStatus.PENDING, JobStatus.RUNNING, JobStatus.COOLING,
                } else "resume"
                return {
                    "action": action,
                    "mode": latest["mode"],
                    "baseline_job_id": latest["baseline_job_id"],
                    "job": latest,
                    "baseline": baseline,
                }
        return {
            "action": "create",
            "mode": "incremental" if baseline else "full",
            "baseline_job_id": int(baseline["id"]) if baseline else None,
            "job": latest,
            "baseline": baseline,
        }

    async def start_automatic(self, districts: list[str], max_documents: int = 0) -> dict:
        async with self._guard:
            plan = self.automatic_plan(districts)
            if plan["action"] == "existing":
                return {"job_id": int(plan["job"]["id"]), "action": "existing", "mode": plan["mode"]}
            if plan["action"] == "resume":
                self._resume_locked(plan["job"], automatic=False)
                return {"job_id": int(plan["job"]["id"]), "action": "resumed", "mode": plan["mode"]}
            mode = plan["mode"]
            baseline_job_id = plan["baseline_job_id"]
            if mode == "incremental":
                self._validate_incremental_baseline(districts, baseline_job_id)
            safety = self._safety_payload(districts)
            job_id = self.db.create_job(districts, mode, safety, max_documents, baseline_job_id)
            self._tasks[job_id] = asyncio.create_task(self._run(job_id))
            return {"job_id": job_id, "action": f"created_{mode}", "mode": mode}

    async def start_full_rebuilds(self, source_groups: list[list[str]]) -> list[dict]:
        """为选定站点创建新的完整全量任务，不复用或恢复历史任务。"""
        async with self._guard:
            for districts in source_groups:
                active = self._active_job_for(districts)
                if active:
                    raise ValueError(
                        f"{'、'.join(districts)}已有活动任务 #{active['id']}，"
                        "请先停止该任务，再重新全量扫描"
                    )

            results = []
            for districts in source_groups:
                job_id = self.db.create_job(districts, "full", self._safety_payload(districts), 0, None)
                self._tasks[job_id] = asyncio.create_task(self._run(job_id))
                results.append({
                    "job_id": job_id,
                    "action": "created_full_rebuild",
                    "mode": "full",
                })
            return results

    def _validate_incremental_baseline(self, districts: list[str], baseline_job_id: int | None) -> None:
        if baseline_job_id is None:
            raise ValueError("增量扫描必须选择一条已完成的全量扫描记录作为基准")
        baseline = self.db.get_job(baseline_job_id)
        if not baseline:
            raise ValueError("所选基准扫描记录不存在")
        if not (
            baseline["mode"] == "full"
            and baseline["status"] == JobStatus.COMPLETED
            and baseline["coverage_status"] == "complete"
        ):
            raise ValueError("所选基准必须是已完整完成的全量扫描任务")
        with self.db.connect() as conn:
            item_count = int(conn.execute(
                "SELECT COUNT(*) FROM scan_item_results WHERE job_id=?", (baseline_job_id,)
            ).fetchone()[0])
        if (
            item_count == 0
            or item_count != int(baseline["estimated_total"])
            or item_count != int(baseline["examined_count"])
        ):
            raise ValueError("所选全量任务缺少完整的逐条扫描结果，不能作为增量基准")
        try:
            baseline_sources = sorted(json.loads(baseline["source_signature"] or "[]"))
        except json.JSONDecodeError:
            baseline_sources = []
        if not baseline_sources:
            baseline_sources = sorted(json.loads(baseline["districts_json"]))
        if baseline_sources != sorted(districts):
            raise ValueError("增量扫描来源必须与所选基准全量扫描完全一致")
        if self._targets_include_putuo(districts):
            baseline_contract = self._job_query_contract(baseline)
            if baseline_contract != PUTUO_QUERY_CONTRACT:
                raise ValueError(
                    f"所选基准任务的普陀区查询契约已过期（需要 {PUTUO_QUERY_CONTRACT}），"
                    "不能作为增量基线。请先对普陀区站点执行完整全量重建"
                )

    async def pause(self, job_id: int, reason: str = "用户手动暂停") -> None:
        job = self.db.get_job(job_id)
        if not job or job["status"] not in {JobStatus.PENDING, JobStatus.RUNNING, JobStatus.COOLING}:
            raise ValueError("该任务当前不能暂停")
        self.db.update_job(job_id, status=JobStatus.PAUSED, pause_reason=reason, coverage_status="partial")
        self.db.add_job_event(job_id, "paused", reason)
        await self._cancel_task(job_id)

    async def stop(self, job_id: int) -> None:
        job = self.db.get_job(job_id)
        if not job or job["status"] not in {
            JobStatus.PENDING, JobStatus.RUNNING, JobStatus.PAUSED, JobStatus.PARTIAL,
            JobStatus.COOLING, JobStatus.FAILED,
        }:
            raise ValueError("该任务当前不能停止")
        self.db.update_job(
            job_id, status=JobStatus.STOPPED, finished_at=utc_now(), pause_reason="用户停止",
            coverage_status="partial", completion_kind="stopped",
        )
        self.db.add_job_event(job_id, "stopped", "用户停止")
        await self._cancel_task(job_id)

    async def _cancel_task(self, job_id: int) -> None:
        task = self._tasks.get(job_id)
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def resume(self, job_id: int, *, automatic: bool = False) -> None:
        async with self._guard:
            job = self.db.get_job(job_id)
            if not job or job["status"] not in {JobStatus.PAUSED, JobStatus.PARTIAL, JobStatus.COOLING, JobStatus.FAILED}:
                raise ValueError("该任务当前不能恢复")
            self._assert_putuo_query_contract(job, action="恢复")
            active = self._active_job_for(self._job_sources(job))
            if active and int(active["id"]) != job_id:
                raise ValueError(f"同站点已有活动任务 #{active['id']}，不能同时恢复旧任务")
            self._resume_locked(job, automatic=automatic)

    def _resume_locked(self, job: dict, *, automatic: bool) -> None:
        job_id = int(job["id"])
        if job["cooldown_until"] and not automatic:
            cooldown_until = datetime.fromisoformat(job["cooldown_until"])
            manual_allowed = job["status"] == JobStatus.PAUSED and not self._is_cooldown_reason(job["pause_reason"])
            if datetime.now(timezone.utc) < cooldown_until and not manual_allowed:
                raise ValueError(f"安全冷却尚未结束，请在 {cooldown_until.astimezone().isoformat()} 后恢复")
        self.db.update_job(
            job_id, status=JobStatus.PENDING, pause_reason="", last_error="", cooldown_until=None,
            finished_at=None, batch_examined_count=0, completion_kind="",
            resumed_count=int(job["resumed_count"]) + 1,
        )
        self.db.add_job_event(job_id, "resumed", "冷却结束自动恢复" if automatic else "用户手动恢复")
        self._tasks[job_id] = asyncio.create_task(self._run(job_id))

    def recover_interrupted(self) -> None:
        # 中断时 generation_items.checking 回退为 retry，避免卡死
        with self.db.connect() as conn:
            interrupted_ids = [
                int(r["id"]) for r in conn.execute(
                    "SELECT id FROM scan_jobs WHERE status IN ('pending','running')"
                ).fetchall()
            ]
        for iid in interrupted_ids:
            self.repo.mark_checking_as_retry(iid)
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT id FROM scan_jobs WHERE status IN ('pending','running')"
            ).fetchall()
            conn.execute(
                """UPDATE scan_jobs SET status='paused',pause_reason='程序上次退出，等待手动恢复',
                coverage_status='partial',completion_kind='interrupted'
                WHERE status IN ('pending','running')"""
            )
        for row in rows:
            self.db.add_job_event(int(row["id"]), "interrupted", "程序上次退出，等待手动恢复")
        with self.db.connect() as conn:
            legacy_pauses = conn.execute(
                """SELECT id,pause_reason FROM scan_jobs
                WHERE status='paused' AND completion_kind='safety_pause' AND cooldown_until IS NOT NULL"""
            ).fetchall()
            cooling_rows = conn.execute(
                "SELECT id,cooldown_until FROM scan_jobs WHERE status='cooling' AND cooldown_until IS NOT NULL"
            ).fetchall()
        for row in legacy_pauses:
            if not self._is_cooldown_reason(row["pause_reason"]):
                self.db.update_job(
                    int(row["id"]), cooldown_until=None, completion_kind="data_pause",
                    coverage_status="partial",
                )
                self.db.add_job_event(int(row["id"]), "pause_reclassified", "旧任务的数据校验暂停已清除无效冷却")
        legacy_title_message = "列表页面标题与公开接口不一致，采集器需要更新"
        with self.db.connect() as conn:
            old_title_rows = conn.execute(
                "SELECT id FROM scan_jobs WHERE status='paused' AND pause_reason=?",
                (legacy_title_message,),
            ).fetchall()
        for row in old_title_rows:
            message = "历史记录：旧版本严格标题比对时暂停；已升级为标准化比对，恢复后会按新版规则重新检查当前条目"
            self.db.update_job(int(row["id"]), pause_reason=message, cooldown_until=None, completion_kind="data_pause")
            self.db.add_job_event(int(row["id"]), "pause_message_upgraded", message)
        legacy_detail_message = "详情页动态内容未完整加载，已停止解析以避免空数据覆盖"
        with self.db.connect() as conn:
            old_detail_rows = conn.execute(
                "SELECT id FROM scan_jobs WHERE status='paused' AND pause_reason=?",
                (legacy_detail_message,),
            ).fetchall()
        for row in old_detail_rows:
            message = "历史记录：详情页首次加载超时；已升级为受限重载一次后再判断，恢复后会按新版规则重新检查当前条目"
            self.db.update_job(int(row["id"]), pause_reason=message, cooldown_until=None, completion_kind="data_pause")
            self.db.add_job_event(int(row["id"]), "pause_message_upgraded", message)
        with self.db.connect() as conn:
            legacy_exception_rows = conn.execute(
                """SELECT id,current_district,current_page,current_item_index,current_url,pause_reason
                FROM scan_jobs WHERE status='paused' AND completion_kind='data_pause'
                AND pause_reason LIKE '历史记录：详情页首次加载超时%' AND current_url<>''"""
            ).fetchall()
        for row in legacy_exception_rows:
            try:
                district = resolve_target(row["current_district"]).district
            except ValueError:
                district = row["current_district"]
            item = PolicyListItem(
                district=district, page_number=int(row["current_page"]),
                item_index=int(row["current_item_index"]),
                title=f"第 {row['current_page']} 页第 {int(row['current_item_index']) + 1} 条政策",
                url=row["current_url"],
            )
            self.repo.record_scan_exception(int(row["id"]), item, "detail_metadata", row["pause_reason"])
            message = "单条详情页异常已加入收尾复测队列；可恢复继续扫描，其余条目不会再被它阻塞"
            self.db.update_job(int(row["id"]), pause_reason=message, completion_kind="exception_queued")
            self.db.add_job_event(int(row["id"]), "exception_queued", row["pause_reason"], row["current_url"], category="detail_metadata")
        legacy_item_errors = {
            "详情页动态内容未完整加载，已停止解析以避免空数据覆盖": "detail_metadata",
            "政策条目点击后未打开详情页，采集器需要更新": "detail_open",
        }
        placeholders = ",".join("?" for _ in legacy_item_errors)
        with self.db.connect() as conn:
            historical_item_errors = conn.execute(
                f"""SELECT e.job_id,e.message,e.url,j.current_district,j.current_page,j.current_item_index
                FROM job_events e JOIN scan_jobs j ON j.id=e.job_id
                WHERE j.status='paused' AND e.event_type='safety_pause' AND e.message IN ({placeholders})
                AND e.url<>''""",
                list(legacy_item_errors),
            ).fetchall()
        migrated_jobs: set[int] = set()
        for row in historical_item_errors:
            try:
                district = resolve_target(row["current_district"]).district
            except ValueError:
                district = row["current_district"]
            item = PolicyListItem(
                district=district, page_number=int(row["current_page"]), item_index=int(row["current_item_index"]),
                title=f"历史扫描异常政策（{row['url'].rsplit('=', 1)[-1]}）", url=row["url"],
            )
            self.repo.record_scan_exception(int(row["job_id"]), item, legacy_item_errors[row["message"]], row["message"])
            migrated_jobs.add(int(row["job_id"]))
        for job_id in migrated_jobs:
            message = "历史详情页异常已加入收尾复测队列；可恢复继续扫描，其余条目不会再被它们阻塞"
            self.db.update_job(job_id, pause_reason=message, completion_kind="exception_queued", cooldown_until=None)
            self.db.add_job_event(job_id, "exception_queued", message)
        for row in cooling_rows:
            cooldown_until = datetime.fromisoformat(row["cooldown_until"])
            self._tasks[int(row["id"])] = asyncio.create_task(
                self._resume_after_cooldown(int(row["id"]), cooldown_until)
            )

    async def _run(self, job_id: int) -> None:
        try:
            job = self.db.get_job(job_id)
            if not job:
                return
            targets = [resolve_target(value) for value in json.loads(job["districts_json"])]
            hosts = sorted({
                "www.shpt.gov.cn" if target.collector_type == "putuo" else "www.shanghai.gov.cn"
                for target in targets
            })
            waiting_hosts = [host for host in hosts if self._host_locks.setdefault(host, asyncio.Lock()).locked()]
            if waiting_hosts:
                self.db.update_job(job_id, pause_reason=f"等待同域任务释放：{'、'.join(waiting_hosts)}")
                self.db.add_job_event(job_id, "queued", f"等待同域任务：{'、'.join(waiting_hosts)}")
            async with AsyncExitStack() as stack:
                for host in hosts:
                    await stack.enter_async_context(self._host_locks.setdefault(host, asyncio.Lock()))
                async with self._run_slots:
                    latest = self.db.get_job(job_id)
                    if not latest or latest["status"] != JobStatus.PENDING:
                        return
                    self.db.update_job(job_id, pause_reason="")
                    await self._run_acquired(job_id)
        finally:
            if self._tasks.get(job_id) is asyncio.current_task():
                self._tasks.pop(job_id, None)

    async def _run_acquired(self, job_id: int) -> None:
        job = self.db.get_job(job_id)
        if not job:
            return
        safety_payload = json.loads(job["safety_json"] or "{}")
        safety_fields = {
            key: value for key, value in safety_payload.items()
            if key in SafetyConfig.__dataclass_fields__
        }
        safety_config = SafetyConfig(**safety_fields)
        safety = SafetyController(
            safety_config, event_hook=lambda kind, details: self._record_safety_event(job_id, kind, details),
            initial_pages=int(job["access_count"]),
        )
        calendar = WorkdayCalendar(provider=is_workday)
        target_values = json.loads(job["districts_json"])
        targets = [resolve_target(value) for value in target_values]
        max_documents = int(job["max_documents"])
        self.db.update_job(job_id, status=JobStatus.RUNNING, started_at=job["started_at"] or utc_now())
        self.db.add_job_event(job_id, "started", "扫描批次开始或继续")
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                try:
                    collectors = []
                    detail_cache: dict[str, tuple[DetailInspection, int | None]] = {}
                    try:
                        # 先初始化全部 collector；仅对“当前/未开始”来源刷新列表总量。
                        # 已完成来源保留数据库中的最终真实总量，避免截断栏目首屏 10000 覆盖 10234。
                        start_index = int(job["current_district_index"])
                        for target_index, target in enumerate(targets):
                            collector = self._collector_for_target(browser, safety, target)
                            await collector.__aenter__()
                            collectors.append((target, collector))
                            await collector.check_robots()
                            if target_index < start_index:
                                # 列表扫描已完成：只保留 collector 供异常复测，不修订总量。
                                continue
                            await collector.select_district(target.district)
                            await self._record_target_total(
                                job_id, target.label, await collector.estimated_total(),
                            )
                        for target_index, (target, collector) in enumerate(collectors[start_index:], start=start_index):
                            latest = self.db.get_job(job_id)
                            if not latest or latest["status"] != JobStatus.RUNNING:
                                return
                            async with LinkChecker(
                                safety,
                                collector.ensure_allowed,
                                rendered_checker=collector.check_rendered_link,
                                rendered_hosts=getattr(collector, "rendered_hosts", None),
                            ) as checker:
                                if target.collector_type == "putuo" and self._job_uses_snapshot_contract(job):
                                    generation_id = await self._ensure_generation(
                                        job, kind=job["mode"], target_key=target.key,
                                    )
                                    job = self.db.get_job(job_id) or job
                                    full_reconcile = job.get("mode") == "full" and str(job.get("completion_kind") or "") == "full_reconcile"
                                    await self._scan_target_snapshot(
                                        job_id, job, target, target_index, max_documents, collector, checker, calendar,
                                        detail_cache, generation_id, full_reconcile=full_reconcile,
                                    )
                                else:
                                    await self._scan_target(
                                        job_id, job, target, target_index, max_documents, collector, checker, calendar,
                                        detail_cache,
                                    )
                        for target, collector in collectors:
                            latest = self.db.get_job(job_id)
                            if not latest or latest["status"] != JobStatus.RUNNING:
                                return
                            async with LinkChecker(
                                safety,
                                collector.ensure_allowed,
                                rendered_checker=collector.check_rendered_link,
                                rendered_hosts=getattr(collector, "rendered_hosts", None),
                            ) as checker:
                                await self._retest_target_exceptions(job_id, target, collector, checker, calendar)
                    finally:
                        for _target, collector in reversed(collectors):
                            await collector.__aexit__(None, None, None)
                finally:
                    await browser.close()
            latest = self.db.get_job(job_id)
            if not latest or latest["status"] != JobStatus.RUNNING:
                return
            self._validate_complete_coverage(job_id)
            completion_kind = "incremental" if job["mode"] == "incremental" else (job.get("completion_kind") or "full")
            self.db.update_job(
                job_id, status=JobStatus.COMPLETED, finished_at=utc_now(), current_url="",
                coverage_status="complete", completion_kind=completion_kind, pause_reason="",
            )
            self._update_schedule_after_job(job_id, JobStatus.COMPLETED)
            self.db.add_job_event(job_id, "completed", f"{completion_kind} 扫描完成")
        except CooldownPause as exc:
            cooldown = datetime.now(timezone.utc) + timedelta(seconds=safety_config.cooldown_seconds)
            self.db.update_job(
                job_id, status=JobStatus.COOLING, pause_reason=str(exc), cooldown_until=cooldown.isoformat(),
                coverage_status="partial", completion_kind="safety_pause",
            )
            self.db.add_job_event(job_id, "cooling", str(exc), self.db.get_job(job_id)["current_url"])
            self._update_schedule_after_job(job_id, JobStatus.COOLING)
            self._tasks[job_id] = asyncio.create_task(self._resume_after_cooldown(job_id, cooldown))
        except SafetyPause as exc:
            self.db.update_job(
                job_id, status=JobStatus.PAUSED, pause_reason=str(exc), cooldown_until=None,
                coverage_status="partial", completion_kind="data_pause",
            )
            self.db.add_job_event(job_id, "safety_pause", str(exc), self.db.get_job(job_id)["current_url"])
            self._update_schedule_after_job(job_id, JobStatus.PAUSED)
        except Exception as exc:
            self.db.update_job(
                job_id, status=JobStatus.FAILED, last_error=f"{type(exc).__name__}: {exc}",
                finished_at=utc_now(), coverage_status="partial", completion_kind="failed",
            )
            self.db.add_job_event(job_id, "failed", f"{type(exc).__name__}: {exc}")
            self._update_schedule_after_job(job_id, JobStatus.FAILED)

    @staticmethod
    def _is_cooldown_reason(reason: str) -> bool:
        markers = ("403", "429", "验证码", "访问频率", "连续网络失败", "连续运行上限")
        return any(marker in (reason or "") for marker in markers)

    async def _resume_after_cooldown(self, job_id: int, cooldown_until: datetime) -> None:
        delay = max(0.0, (cooldown_until - datetime.now(timezone.utc)).total_seconds())
        await asyncio.sleep(delay)
        job = self.db.get_job(job_id)
        if not job or job["status"] != JobStatus.COOLING:
            return
        try:
            await self.resume(job_id, automatic=True)
        except ValueError as exc:
            self.db.update_job(
                job_id, status=JobStatus.PAUSED, pause_reason=f"自动恢复未执行：{exc}",
                coverage_status="partial", completion_kind="auto_resume_blocked",
            )
            self.db.add_job_event(job_id, "auto_resume_blocked", str(exc))

    def _collector_for_target(self, browser, safety, target: ScanTarget):
        if target.collector_type == "putuo":
            return PutuoDistrictCollector(browser, safety, target)
        return BrowserCollector(browser, safety)

    async def _record_target_total(
        self, job_id: int, label: str, total: int, *, allow_revision: bool = False,
    ) -> None:
        current = self.db.get_job(job_id)
        totals = json.loads(current["total_by_district_json"] or "{}")
        examined = json.loads(current["examined_by_district_json"] or "{}")
        previous_total = totals.get(label)
        examined_count = int(examined.get(label, 0))
        uses_snapshot = bool(current.get("generation_id")) or self._job_uses_snapshot_contract(current)
        if (
            previous_total is not None
            and examined_count > 0
            and int(previous_total) != int(total)
        ):
            if uses_snapshot:
                # 快照任务以稳定身份为准；totalCount 仅观测，不触发暂停。
                allow_revision = True
            elif not allow_revision:
                raise SafetyPause(
                    f"{label} 列表总量在任务恢复后发生变化：原 {previous_total} 条，现 {total} 条"
                )
            if not uses_snapshot and int(total) < examined_count:
                raise SafetyPause(
                    f"{label} 修订后的列表总量 {total} 小于已覆盖 {examined_count} 条"
                )
        totals[label] = int(total)
        examined.setdefault(label, 0)
        self.db.update_job(
            job_id,
            estimated_total=sum(int(value) for value in totals.values()),
            total_by_district_json=json.dumps(totals, ensure_ascii=False),
            examined_by_district_json=json.dumps(examined, ensure_ascii=False),
        )

    @classmethod
    def _job_uses_snapshot_contract(cls, job: dict) -> bool:
        contract = cls._job_query_contract(job)
        return contract == PUTUO_QUERY_CONTRACT or contract.endswith("-v3") or "snapshot" in contract

    def _validate_complete_coverage(self, job_id: int) -> None:
        """完成校验：快照任务看 generation 状态；旧任务仍比 total/examined/results。"""
        current = self.db.get_job(job_id)
        if not current:
            raise SafetyPause("扫描任务在完成校验时不存在")
        if self._job_uses_snapshot_contract(current):
            with self.db.connect() as conn:
                gen_rows = conn.execute(
                    "SELECT id FROM scan_generations WHERE job_id=?", (job_id,),
                ).fetchall()
            if gen_rows:
                mismatches = []
                open_total = 0
                unique_total = 0
                terminal_total = 0
                for grow in gen_rows:
                    report = coverage_from_generation(self.repo, job_id, int(grow["id"]))
                    open_total += report["open"]
                    unique_total += report["unique_stable"]
                    terminal_total += report["terminal"]
                    if report["terminal"] != report["unique_stable"]:
                        mismatches.append(
                            f"generation {grow['id']} terminal {report['terminal']} / stable {report['unique_stable']}"
                        )
                    if report["observed_total"] and report["observed_total"] != report["unique_stable"]:
                        mismatches.append(
                            f"generation {grow['id']} observed {report['observed_total']} / stable {report['unique_stable']}"
                        )
                if open_total > 0:
                    mismatches.append(f"仍有未终态条目 pending/checking/retry={open_total}")
                if unique_total <= 0:
                    mismatches.append("generation 中没有稳定身份条目")
                if terminal_total > 0 and self.repo.scan_item_count(job_id) == 0:
                    mismatches.append("终态条目已完成但没有任何详情结果")
                if mismatches:
                    raise SafetyPause("扫描覆盖校验失败，任务未标记完成：" + "；".join(mismatches))
                return
        estimated = int(current["estimated_total"])
        examined = int(current["examined_count"])
        item_count = self.repo.scan_item_count(job_id)
        processed = int(current["processed_count"])
        skipped = int(current["skipped_count"])
        try:
            totals = {key: int(value) for key, value in json.loads(
                current["total_by_district_json"] or "{}"
            ).items()}
            examined_by_source = {key: int(value) for key, value in json.loads(
                current["examined_by_district_json"] or "{}"
            ).items()}
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SafetyPause("扫描覆盖校验失败：分来源计数格式无效") from exc
        mismatches = []
        if examined != estimated:
            mismatches.append(f"已覆盖 {examined} / 预计 {estimated}")
        if item_count != examined:
            mismatches.append(f"逐条结果 {item_count} / 已覆盖 {examined}")
        if processed + skipped != examined:
            mismatches.append(f"详情 {processed} + 复用/跳过 {skipped} != 已覆盖 {examined}")
        if totals != examined_by_source:
            mismatches.append("分来源已覆盖数量与接口总量不一致")
        if mismatches:
            raise SafetyPause("扫描覆盖校验失败，任务未标记完成：" + "；".join(mismatches))

    @staticmethod
    def _as_date(value: str | None) -> date | None:
        if not value:
            return None
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None

    @staticmethod
    def _coerce_inspection(value: PolicyRecord | DetailInspection) -> DetailInspection:
        if isinstance(value, DetailInspection):
            return value
        if isinstance(value, PolicyRecord):
            # 市级采集器仍返回 PolicyRecord；它没有普陀七项表头的适用前提，视为已解析详情。
            return DetailInspection(record=value, header_detected=True)
        raise TypeError(f"详情采集器返回了不支持的数据类型：{type(value).__name__}")

    @staticmethod
    def _prepare_item_source(item: PolicyListItem, target: ScanTarget) -> None:
        item.source_key = item.source_key or target.key
        item.source_site = item.source_site or target.label
        item.source_channel_id = item.source_channel_id or target.channel_id

    @staticmethod
    def _all_missing_fields(inspection: DetailInspection) -> list[str]:
        return list(dict.fromkeys([*inspection.missing_fields, *inspection.invalid_fields]))

    def _record_item_result(
        self,
        job_id: int,
        item: PolicyListItem,
        inspection: DetailInspection,
        *,
        detail_status: str,
        document_id: int | None = None,
        reused_document_id: int | None = None,
        baseline_job_id: int | None = None,
        reason: str = "",
    ) -> None:
        record = inspection.record
        self.repo.record_scan_item(
            job_id,
            item,
            detail_status=detail_status,
            header_detected=inspection.header_detected,
            source_id=record.source_id if record else "",
            topic_category=record.topic_category if record else "",
            disclosure_attribute=record.disclosure_attribute if record else "",
            authored_date=record.authored_date if record else None,
            page_document_number=record.page_document_number if record else "",
            published_date=record.published_date if record else None,
            issuing_agency=record.issuing_agency if record else "",
            missing_fields=self._all_missing_fields(inspection),
            document_id=document_id,
            reused_document_id=reused_document_id,
            baseline_job_id=baseline_job_id,
            reason=reason,
        )

    def _inspection_from_baseline(self, item: PolicyListItem, row: dict) -> DetailInspection:
        status = row["detail_status"]
        header_detected = bool(row["header_detected"])
        if status in {"no_header_pass", "reused_current_no_header", "reused_baseline_no_header"}:
            return DetailInspection(record=None, header_detected=False)
        try:
            missing_fields = json.loads(row["missing_fields_json"] or "[]")
        except json.JSONDecodeError:
            missing_fields = []
        record = PolicyRecord(
            district=item.district,
            title=item.title,
            url=item.url,
            source_id=row["source_id"] or "",
            issuing_agency=row["issuing_agency"] or "",
            page_document_number=row["page_document_number"] or "",
            published_date=self._as_date(row["published_date"]),
            authored_date=self._as_date(row["authored_date"]),
            source_site=item.source_site,
            topic_category=row["topic_category"] or "",
            disclosure_attribute=row["disclosure_attribute"] or "",
            header_detected=header_detected,
            missing_metadata_fields=missing_fields,
        )
        return DetailInspection(record=record, header_detected=header_detected, missing_fields=missing_fields)

    @staticmethod
    def _baseline_document_id(row: dict) -> int | None:
        value = row.get("document_id") or row.get("reused_document_id")
        return int(value) if value else None

    def _can_reuse_baseline_item(self, row: dict, item: PolicyListItem) -> tuple[bool, str]:
        if row["title"].strip() != item.title.strip():
            return False, "列表标题发生变化"
        listed_date = item.published_date.isoformat() if item.published_date else None
        if (row["listed_date"] or None) != listed_date:
            return False, "列表发布日期发生变化"
        if int(row.get("link_check_version") or 0) < PAGE_LINK_CHECK_VERSION:
            return False, "基线尚未按页面可见链接规则检查，必须重新打开详情核验"
        if row["detail_status"] in {"exception", "checked_incomplete"}:
            return False, "基线详情异常或表头字段不完整"
        if row.get("has_legacy_hidden_links"):
            return False, "基线含无页面位置证据的历史链接，必须重新打开详情核验"
        if row["detail_status"] in {"no_header_pass", "reused_current_no_header", "reused_baseline_no_header"}:
            return True, "基线无表头 PASS，列表信息未变化"
        if self._all_missing_fields(self._inspection_from_baseline(item, row)):
            return False, "基线表头字段不完整"
        if not self._baseline_document_id(row):
            return False, "基线没有可复用的详情文档"
        return True, "与基线的 URL、标题和列表发布日期一致"


    async def _ensure_generation(self, job: dict, *, kind: str = "full", target_key: str = "") -> int:
        job_id = int(job["id"])
        # 每个 target 独立 generation，避免 list_cursor / 条目串源
        if target_key:
            existing = None
            with self.db.connect() as conn:
                row = conn.execute(
                    """SELECT * FROM scan_generations WHERE job_id=? AND target_key=?
                    ORDER BY id DESC LIMIT 1""",
                    (job_id, target_key),
                ).fetchone()
                if row:
                    existing = dict(row)
            if existing:
                self.db.update_job(job_id, generation_id=int(existing["id"]), scan_phase=ScanPhase.DISCOVERING)
                return int(existing["id"])
        elif job.get("generation_id"):
            return int(job["generation_id"])
        contract = self._job_query_contract(job) or PUTUO_QUERY_CONTRACT
        gen_id = self.repo.create_generation(
            job_id, target_key=target_key, generation_kind=kind, query_contract=contract,
            phase=ScanPhase.DISCOVERING,
        )
        self.db.update_job(job_id, generation_id=gen_id, scan_phase=ScanPhase.DISCOVERING)
        return gen_id

    async def _process_generation_queue(
        self, job_id, job, target, target_index, max_documents, collector, checker, calendar,
        detail_cache, generation_id: int,
    ) -> None:
        """阶段 B：从 generation_items 领取 pending/retry 并幂等处理。"""
        self.db.update_job(job_id, scan_phase=ScanPhase.PROCESSING, current_district=target.label)
        self.repo.update_generation(generation_id, phase=ScanPhase.PROCESSING)
        while True:
            latest = self.db.get_job(job_id)
            if not latest or latest["status"] != JobStatus.RUNNING:
                return
            if max_documents and int(latest["batch_examined_count"]) >= max_documents:
                self._mark_partial_limit(job_id, latest)
                return
            row = self.repo.claim_generation_item(job_id, target.key)
            if not row:
                break
            item = list_item_from_generation_row(row, target)
            self._prepare_item_source(item, target)
            try:
                await self._process_list_item(
                    job_id, job, target, item, collector, checker, calendar, detail_cache,
                    generation_item_id=int(row["id"]),
                )
            except SafetyPause:
                # 数据/安全暂停必须向上抛出；条目标为 retry 以便恢复后续处理
                self.repo.complete_generation_item(
                    int(row["id"]), status="retry", error="safety pause during detail",
                )
                raise
            except Exception as exc:
                self.repo.complete_generation_item(
                    int(row["id"]), status="retry", error=f"{type(exc).__name__}: {exc}",
                )
                raise
            refresh_generation_stats(self.repo, generation_id)
            self._sync_job_stats_from_generation(job_id, generation_id)

    def _sync_job_stats_from_generation(self, job_id: int, generation_id: int) -> None:
        counts = self.repo.generation_item_counts(generation_id)
        gen = self.repo.get_generation(generation_id) or {}
        # 聚合全任务所有 generation，避免多来源时被最后一个来源覆盖
        with self.db.connect() as conn:
            all_gens = conn.execute(
                "SELECT id FROM scan_generations WHERE job_id=?", (job_id,),
            ).fetchall()
        discovered = 0
        completed = 0
        reused = 0
        retry = 0
        review = 0
        pending = 0
        for grow in all_gens:
            c = self.repo.generation_item_counts(int(grow["id"]))
            discovered += c["total"]
            completed += c.get("completed", 0)
            reused += c.get("reused", 0)
            retry += c.get("retry", 0)
            review += c.get("review", 0)
            pending += c.get("pending", 0)
        stats = {
            "discovered": discovered,
            "completed": completed,
            "reused": reused,
            "new": int(gen.get("new_count") or 0),
            "retry": retry,
            "review": review,
            "pending": pending,
            "catchup_round": int(gen.get("catchup_round") or 0),
            "observed_total": int(gen.get("observed_total") or 0),
            "active_generation_id": generation_id,
        }
        terminal = completed + reused + review
        job = self.db.get_job(job_id) or {}
        totals = json.loads(job.get("total_by_district_json") or "{}")
        estimated = sum(int(v) for v in totals.values()) if totals else discovered
        self.db.update_job(
            job_id,
            estimated_total=estimated or discovered,
            examined_count=terminal,
            generation_stats_json=json.dumps(stats, ensure_ascii=False),
        )

    async def _scan_target_snapshot(
        self, job_id, job, target, target_index, max_documents, collector, checker, calendar,
        detail_cache, generation_id: int, *, full_reconcile: bool = False,
    ) -> None:
        latest = self.db.get_job(job_id)
        gen = self.repo.get_generation(generation_id) or {}
        start_page = int(gen.get("list_cursor_page") or 1)
        if latest and int(latest.get("current_district_index") or 0) == target_index:
            # resume mid discovery
            start_page = max(start_page, int(latest.get("current_page") or 1))

        def is_running() -> bool:
            cur = self.db.get_job(job_id)
            return bool(cur and cur["status"] == JobStatus.RUNNING)

        phase = str(gen.get("phase") or ScanPhase.DISCOVERING)
        if phase in {ScanPhase.DISCOVERING, ScanPhase.FULL_RECONCILE, ScanPhase.INCREMENTAL_DISCOVERY, ""}:
            if full_reconcile or phase == ScanPhase.FULL_RECONCILE:
                self.db.update_job(job_id, scan_phase=ScanPhase.FULL_RECONCILE)
                self.repo.update_generation(generation_id, phase=ScanPhase.FULL_RECONCILE)
            await discover_target_pages(
                collector=collector, target=target, generation_id=generation_id, job_id=job_id,
                repo=self.repo, db=self.db, start_page=start_page, is_running=is_running,
                mark_absent=full_reconcile,
            )
            if not is_running():
                return

        async def process_pending() -> None:
            await self._process_generation_queue(
                job_id, job, target, target_index, max_documents, collector, checker, calendar,
                detail_cache, generation_id,
            )

        await process_pending()
        if not is_running():
            return
        # catch-up
        await catchup_head(
            collector=collector, target=target, generation_id=generation_id, job_id=job_id,
            repo=self.repo, db=self.db, config=self.continuous_config, is_running=is_running,
            process_pending=process_pending,
        )
        if not is_running():
            return
        await process_pending()
        # observed total bookkeeping
        counts = self.repo.generation_item_counts(generation_id)
        # 快照任务：分来源总量与已覆盖以 generation 唯一身份为准；collector 观测值仅写入 generation.observed_total
        real_total = counts["total"]
        if hasattr(collector, "estimated_total"):
            try:
                est = int(await collector.estimated_total())
                if est > real_total:
                    real_total = est
            except Exception:
                pass
        # Discovery plus bounded catch-up has reached the final stable identity set.
        # The remote total is only a diagnostic signal; completion is gated by this set.
        self.repo.update_generation(generation_id, observed_total=counts["total"])
        await self._record_target_total(job_id, target.label, real_total, allow_revision=True)
        examined_by = json.loads((self.db.get_job(job_id) or {}).get("examined_by_district_json") or "{}")
        examined_by[target.label] = counts["total"]
        totals = json.loads((self.db.get_job(job_id) or {}).get("total_by_district_json") or "{}")
        totals[target.label] = real_total
        self.db.update_job(
            job_id,
            total_by_district_json=json.dumps(totals, ensure_ascii=False),
            examined_by_district_json=json.dumps(examined_by, ensure_ascii=False),
            estimated_total=sum(int(v) for v in totals.values()),
        )
        self._sync_job_stats_from_generation(job_id, generation_id)
        self.db.update_job(
            job_id, current_district_index=target_index + 1, current_district="",
            current_page=1, current_item_index=-1, scan_phase=ScanPhase.IDLE,
        )

    async def _process_list_item(
        self, job_id, job, target, item, collector, checker, calendar, detail_cache,
        *, generation_item_id: int | None = None,
    ) -> None:
        """处理单条列表项；generation_item_id 非空时走幂等计数。"""
        baseline_job_id = job.get("baseline_job_id") if (
            job["mode"] == "incremental" or str(job.get("completion_kind") or "") == "full_reconcile"
        ) else None
        counted_before = False
        if generation_item_id is not None:
            # 已完成的 generation 项不会再次 claim；此处 claim 后才进入
            pass

        cached = detail_cache.get(item.url)
        if cached:
            inspection, document_id = cached
            detail_status = "reused_current_no_header" if inspection.record is None else "reused_current_detail"
            reason = "同一任务中该 URL 已在另一来源完成详情检查，复用当前任务结果"
            self._record_item_result(
                job_id, item, inspection, detail_status=detail_status, document_id=document_id,
                reused_document_id=document_id, baseline_job_id=baseline_job_id, reason=reason,
            )
            if generation_item_id is not None:
                self.repo.complete_generation_item(
                    generation_item_id, status="reused", detail_status=detail_status, document_id=document_id,
                )
                self._advance_progress_once(
                    job_id, target.label, item, skipped=True, generation_item_id=generation_item_id,
                )
            else:
                self._advance_progress(job_id, target.label, item.page_number, item.item_index, skipped=True)
            return

        current_item = self.repo.current_job_item(job_id, item.url) if item.url else None
        if current_item and int(current_item.get("link_check_version") or 0) >= PAGE_LINK_CHECK_VERSION:
            inspection = self._inspection_from_baseline(item, current_item)
            document_id = self._baseline_document_id(current_item)
            same_position = (
                current_item["target_key"] == item.source_key
                and int(current_item["page_number"]) == item.page_number
                and int(current_item["item_index"]) == item.item_index
            )
            performed_statuses = {"checked_complete", "checked_incomplete", "no_header_pass"}
            already_performed_here = same_position and current_item["detail_status"] in performed_statuses
            if already_performed_here:
                detail_status = current_item["detail_status"]
                reason = "任务恢复时发现该条详情结果已落库，直接恢复进度"
            else:
                detail_status = (
                    "reused_current_no_header" if inspection.record is None else "reused_current_detail"
                )
                reason = "同一任务中该 URL 已完成详情检查，复用数据库中的当前任务结果"
            self._record_item_result(
                job_id, item, inspection, detail_status=detail_status, document_id=document_id,
                reused_document_id=None if already_performed_here else document_id,
                baseline_job_id=baseline_job_id, reason=reason,
            )
            detail_cache[item.url] = (inspection, document_id)
            if generation_item_id is not None:
                self.repo.complete_generation_item(
                    generation_item_id, status="reused" if not already_performed_here else "completed",
                    detail_status=detail_status, document_id=document_id,
                )
                self._advance_progress_once(
                    job_id, target.label, item, skipped=not already_performed_here,
                    generation_item_id=generation_item_id,
                )
            else:
                self._advance_progress(
                    job_id, target.label, item.page_number, item.item_index,
                    skipped=not already_performed_here,
                )
            return

        if baseline_job_id and item.url:
            baseline = self.repo.baseline_item(int(baseline_job_id), item)
            if baseline:
                can_reuse, reason = self._can_reuse_baseline_item(baseline, item)
                if can_reuse:
                    inspection = self._inspection_from_baseline(item, baseline)
                    document_id = self._baseline_document_id(baseline)
                    finding_delta = 0
                    if document_id is not None:
                        copied_findings = self.repo.copy_baseline_findings(int(baseline_job_id), job_id, document_id)
                        finding_delta = len(copied_findings)
                        self.repo.copy_baseline_link_checks(int(baseline_job_id), job_id, document_id)
                        self.repo.record_job_document(
                            job_id, document_id, target.label, item.page_number, item.item_index,
                            "skipped", reason,
                        )
                    detail_status = "reused_baseline_no_header" if inspection.record is None else "reused_baseline_detail"
                    self._record_item_result(
                        job_id, item, inspection, detail_status=detail_status, document_id=document_id,
                        reused_document_id=document_id, baseline_job_id=int(baseline_job_id), reason=reason,
                    )
                    detail_cache[item.url] = (inspection, document_id)
                    if generation_item_id is not None:
                        self.repo.complete_generation_item(
                            generation_item_id, status="reused", detail_status=detail_status, document_id=document_id,
                        )
                        self.repo.update_inventory_detail(
                            target.key, item.stable_id, detail_status=detail_status, document_id=document_id,
                        )
                        self._advance_progress_once(
                            job_id, target.label, item, skipped=True, finding_delta=finding_delta,
                            generation_item_id=generation_item_id,
                        )
                    else:
                        self._advance_progress(
                            job_id, target.label, item.page_number, item.item_index,
                            skipped=True, finding_delta=finding_delta,
                        )
                    return
        try:
            inspection = self._coerce_inspection(await collector.open_item(item))
        except ItemReviewRequired as exc:
            self.repo.record_scan_exception(job_id, item, exc.category, str(exc))
            self._record_item_result(
                job_id, item, DetailInspection(record=None, header_detected=False),
                detail_status="exception", reason=str(exc), baseline_job_id=baseline_job_id,
            )
            self.db.add_job_event(job_id, "exception_queued", str(exc), item.url, category=exc.category)
            if generation_item_id is not None:
                self.repo.complete_generation_item(
                    generation_item_id, status="review", detail_status="exception", error=str(exc),
                )
                self._advance_progress_once(
                    job_id, target.label, item, skipped=True, current_url=item.url,
                    generation_item_id=generation_item_id,
                )
            else:
                self._advance_progress(
                    job_id, target.label, item.page_number, item.item_index, skipped=True, current_url=item.url,
                )
            return
        if inspection.record is None:
            reason = "未发现七项政策表头，按规则 PASS，不进行字段问题检查"
            self._record_item_result(
                job_id, item, inspection, detail_status="no_header_pass", baseline_job_id=baseline_job_id,
                reason=reason,
            )
            detail_cache[item.url] = (inspection, None)
            if generation_item_id is not None:
                self.repo.complete_generation_item(
                    generation_item_id, status="completed", detail_status="no_header_pass",
                )
                self.repo.update_inventory_detail(
                    target.key, item.stable_id, detail_status="no_header_pass", document_id=None,
                )
                self._advance_progress_once(
                    job_id, target.label, item, skipped=False, current_url=item.url,
                    generation_item_id=generation_item_id,
                )
            else:
                self._advance_progress(
                    job_id, target.label, item.page_number, item.item_index,
                    skipped=False, current_url=item.url,
                )
            return
        record = inspection.record
        record.district = target.district
        record.source_site = record.source_site or target.label
        record.header_detected = inspection.header_detected
        record.missing_metadata_fields = self._all_missing_fields(inspection)
        agency_rows = self.db.agency_rules(target.district)
        findings = evaluate_record(record, agency_rows, calendar)
        document_id = self.repo.save_record(job_id, record, findings)
        detail_status = "checked_incomplete" if record.missing_metadata_fields else "checked_complete"
        reason = (
            "详情页表头字段不完整，已记录 META-001 并继续既有检查"
            if record.missing_metadata_fields else "详情页表头完整"
        )
        self.repo.record_job_document(
            job_id, document_id, target.label, item.page_number, item.item_index,
            "checking_links", "关联链接检查进行中",
        )
        self.db.update_job(job_id, current_url=record.url)
        for related in record.related_links:
            link_result = await checker.check(related.kind, related.url, related)
            self.repo.save_link_check(job_id, document_id, link_result)
        self.repo.record_job_document(
            job_id, document_id, target.label, item.page_number, item.item_index, "processed", reason
        )
        self._record_item_result(
            job_id, item, inspection, detail_status=detail_status, document_id=document_id,
            baseline_job_id=baseline_job_id, reason=reason,
        )
        detail_cache[item.url] = (inspection, document_id)
        if generation_item_id is not None:
            self.repo.complete_generation_item(
                generation_item_id, status="completed", detail_status=detail_status, document_id=document_id,
            )
            self.repo.update_inventory_detail(
                target.key, item.stable_id, detail_status=detail_status, document_id=document_id,
            )
            self._advance_progress_once(
                job_id, target.label, item, skipped=False, finding_delta=len(findings),
                current_url=record.url, generation_item_id=generation_item_id,
            )
        else:
            self._advance_progress(
                job_id, target.label, item.page_number, item.item_index,
                skipped=False, finding_delta=len(findings), current_url=record.url,
            )

    def _advance_progress_once(
        self, job_id: int, district: str, item: PolicyListItem, *, skipped: bool,
        finding_delta: int = 0, current_url: str = "", generation_item_id: int | None = None,
    ) -> None:
        """generation 路径：每个 generation_item 只累加一次计数。"""
        # 使用 job_events 记录已计数的 item id，简单用 generation_items.completed 状态保证幂等
        # 这里直接累加；claim 保证每条只处理一次成功路径
        self._advance_progress(
            job_id, district, item.page_number, item.item_index,
            skipped=skipped, finding_delta=finding_delta, current_url=current_url or item.url,
        )

    async def _scan_target(
        self, job_id, job, target, target_index, max_documents, collector, checker, calendar,
        detail_cache: dict[str, tuple[DetailInspection, int | None]],
    ) -> None:
        latest = self.db.get_job(job_id)
        same_target = target_index == int(latest["current_district_index"])
        start_page = int(latest["current_page"]) if same_target else 1
        item_index = int(latest["current_item_index"]) if same_target else -1
        self.db.update_job(
            job_id, current_district=target.label, current_district_index=target_index,
            current_page=start_page, current_item_index=item_index,
        )
        if hasattr(collector, "note_resume_examined"):
            examined_by_source = json.loads(latest["examined_by_district_json"] or "{}")
            collector.note_resume_examined(int(examined_by_source.get(target.label, 0)))
        async for item in collector.iter_items(target.district, start_page, item_index):
            self._prepare_item_source(item, target)
            latest = self.db.get_job(job_id)
            if not latest or latest["status"] != JobStatus.RUNNING:
                return
            if max_documents and int(latest["batch_examined_count"]) >= max_documents:
                self._mark_partial_limit(job_id, latest)
                return
            cached = detail_cache.get(item.url)
            if cached:
                inspection, document_id = cached
                detail_status = "reused_current_no_header" if inspection.record is None else "reused_current_detail"
                reason = "同一任务中该 URL 已在另一来源完成详情检查，复用当前任务结果"
                self._record_item_result(
                    job_id, item, inspection, detail_status=detail_status, document_id=document_id,
                    reused_document_id=document_id, baseline_job_id=job.get("baseline_job_id"), reason=reason,
                )
                self._advance_progress(job_id, target.label, item.page_number, item.item_index, skipped=True)
                continue

            current_item = self.repo.current_job_item(job_id, item.url) if item.url else None
            if current_item and int(current_item.get("link_check_version") or 0) >= PAGE_LINK_CHECK_VERSION:
                inspection = self._inspection_from_baseline(item, current_item)
                document_id = self._baseline_document_id(current_item)
                same_position = (
                    current_item["target_key"] == item.source_key
                    and int(current_item["page_number"]) == item.page_number
                    and int(current_item["item_index"]) == item.item_index
                )
                performed_statuses = {"checked_complete", "checked_incomplete", "no_header_pass"}
                already_performed_here = same_position and current_item["detail_status"] in performed_statuses
                if already_performed_here:
                    detail_status = current_item["detail_status"]
                    reason = "任务恢复时发现该条详情结果已落库，直接恢复进度"
                else:
                    detail_status = (
                        "reused_current_no_header" if inspection.record is None else "reused_current_detail"
                    )
                    reason = "同一任务中该 URL 已完成详情检查，复用数据库中的当前任务结果"
                self._record_item_result(
                    job_id, item, inspection, detail_status=detail_status, document_id=document_id,
                    reused_document_id=None if already_performed_here else document_id,
                    baseline_job_id=job.get("baseline_job_id"), reason=reason,
                )
                detail_cache[item.url] = (inspection, document_id)
                self._advance_progress(
                    job_id, target.label, item.page_number, item.item_index,
                    skipped=not already_performed_here,
                )
                continue

            baseline_job_id = job.get("baseline_job_id") if job["mode"] == "incremental" else None
            if baseline_job_id and item.url:
                baseline = self.repo.baseline_item(int(baseline_job_id), item)
                if baseline:
                    can_reuse, reason = self._can_reuse_baseline_item(baseline, item)
                    if can_reuse:
                        inspection = self._inspection_from_baseline(item, baseline)
                        document_id = self._baseline_document_id(baseline)
                        finding_delta = 0
                        if document_id is not None:
                            copied_findings = self.repo.copy_baseline_findings(int(baseline_job_id), job_id, document_id)
                            finding_delta = len(copied_findings)
                            self.repo.copy_baseline_link_checks(int(baseline_job_id), job_id, document_id)
                            self.repo.record_job_document(
                                job_id, document_id, target.label, item.page_number, item.item_index,
                                "skipped", reason,
                            )
                        detail_status = "reused_baseline_no_header" if inspection.record is None else "reused_baseline_detail"
                        self._record_item_result(
                            job_id, item, inspection, detail_status=detail_status, document_id=document_id,
                            reused_document_id=document_id, baseline_job_id=int(baseline_job_id), reason=reason,
                        )
                        detail_cache[item.url] = (inspection, document_id)
                        self._advance_progress(
                            job_id, target.label, item.page_number, item.item_index,
                            skipped=True, finding_delta=finding_delta,
                        )
                        continue
            try:
                inspection = self._coerce_inspection(await collector.open_item(item))
            except ItemReviewRequired as exc:
                self.repo.record_scan_exception(job_id, item, exc.category, str(exc))
                self._record_item_result(
                    job_id, item, DetailInspection(record=None, header_detected=False),
                    detail_status="exception", reason=str(exc), baseline_job_id=baseline_job_id,
                )
                self.db.add_job_event(job_id, "exception_queued", str(exc), item.url, category=exc.category)
                self._advance_progress(
                    job_id, target.label, item.page_number, item.item_index, skipped=True, current_url=item.url,
                )
                continue
            if inspection.record is None:
                reason = "未发现七项政策表头，按规则 PASS，不进行字段问题检查"
                self._record_item_result(
                    job_id, item, inspection, detail_status="no_header_pass", baseline_job_id=baseline_job_id,
                    reason=reason,
                )
                detail_cache[item.url] = (inspection, None)
                self._advance_progress(
                    job_id, target.label, item.page_number, item.item_index,
                    skipped=False, current_url=item.url,
                )
                continue
            record = inspection.record
            record.district = target.district
            record.source_site = record.source_site or target.label
            record.header_detected = inspection.header_detected
            record.missing_metadata_fields = self._all_missing_fields(inspection)
            agency_rows = self.db.agency_rules(target.district)
            findings = evaluate_record(record, agency_rows, calendar)
            document_id = self.repo.save_record(job_id, record, findings)
            detail_status = "checked_incomplete" if record.missing_metadata_fields else "checked_complete"
            reason = (
                "详情页表头字段不完整，已记录 META-001 并继续既有检查"
                if record.missing_metadata_fields else "详情页表头完整"
            )
            self.repo.record_job_document(
                job_id, document_id, target.label, item.page_number, item.item_index,
                "checking_links", "关联链接检查进行中",
            )
            self.db.update_job(job_id, current_url=record.url)
            for related in record.related_links:
                link_result = await checker.check(related.kind, related.url, related)
                self.repo.save_link_check(job_id, document_id, link_result)
            self.repo.record_job_document(
                job_id, document_id, target.label, item.page_number, item.item_index, "processed", reason
            )
            self._record_item_result(
                job_id, item, inspection, detail_status=detail_status, document_id=document_id,
                baseline_job_id=baseline_job_id, reason=reason,
            )
            detail_cache[item.url] = (inspection, document_id)
            self._advance_progress(
                job_id, target.label, item.page_number, item.item_index,
                skipped=False, finding_delta=len(findings), current_url=record.url,
            )
        latest = self.db.get_job(job_id)
        if latest and latest["status"] == JobStatus.RUNNING:
            if getattr(collector, "capped_pagination_active", False) and not getattr(
                collector, "capped_pagination_resolved", False
            ):
                raise SafetyPause(
                    f"{target.label} 列表截断分页未确认真实终点，任务不能标记完成"
                )
            await self._record_target_total(
                job_id, target.label, await collector.estimated_total(), allow_revision=True,
            )
        self.db.update_job(
            job_id, current_district_index=target_index + 1, current_district="",
            current_page=1, current_item_index=-1,
        )

    async def _retest_target_exceptions(self, job_id, target, collector, checker, calendar) -> None:
        for exception in self.repo.pending_scan_exceptions(job_id, target.district):
            latest = self.db.get_job(job_id)
            if not latest or latest["status"] != JobStatus.RUNNING:
                return
            try:
                inspection = self._coerce_inspection(await collector.open_detail_url(
                    target.district, exception["url"], exception["title"],
                ))
                item = PolicyListItem(
                    district=target.district, page_number=int(exception["page_number"]),
                    item_index=int(exception["item_index"]), title=exception["title"], url=exception["url"],
                    source_site=target.label, source_key=target.key, source_channel_id=target.channel_id,
                )
                if inspection.record is None:
                    self._record_item_result(
                        job_id, item, inspection, detail_status="no_header_pass",
                        reason="收尾复测未发现七项政策表头，按规则 PASS",
                    )
                    self.repo.resolve_scan_exception(int(exception["id"]))
                    self._mark_exception_retest_success(job_id, 0)
                    self.db.add_job_event(job_id, "exception_resolved", "收尾复测无表头 PASS", exception["url"])
                    continue
                record = inspection.record
                record.district = target.district
                record.source_site = record.source_site or target.label
                record.header_detected = inspection.header_detected
                record.missing_metadata_fields = self._all_missing_fields(inspection)
                findings = evaluate_record(record, self.db.agency_rules(target.district), calendar)
                document_id = self.repo.save_record(job_id, record, findings)
                self.repo.record_job_document(
                    job_id, document_id, target.label, int(exception["page_number"]),
                    int(exception["item_index"]), "retest_checking_links", "收尾复测成功，补充关联链接检查",
                )
                for related in record.related_links:
                    self.repo.save_link_check(
                        job_id, document_id, await checker.check(related.kind, related.url, related)
                    )
                self.repo.record_job_document(
                    job_id, document_id, target.label, int(exception["page_number"]),
                    int(exception["item_index"]), "retest_processed", "收尾复测成功",
                )
                self._record_item_result(
                    job_id, item, inspection,
                    detail_status="checked_incomplete" if record.missing_metadata_fields else "checked_complete",
                    document_id=document_id,
                    reason="收尾复测成功",
                )
                self.repo.resolve_scan_exception(int(exception["id"]))
                self._mark_exception_retest_success(job_id, len(findings))
                self.db.add_job_event(job_id, "exception_resolved", "收尾复测成功", exception["url"])
            except ItemReviewRequired as exc:
                self.repo.fail_scan_exception_retest(int(exception["id"]), str(exc))
                self.db.add_job_event(job_id, "exception_review_required", str(exc), exception["url"], category=exc.category)
            except SafetyPause:
                raise
            except Exception as exc:
                message = f"复测异常：{type(exc).__name__}: {exc}"
                self.repo.fail_scan_exception_retest(int(exception["id"]), message)
                self.db.add_job_event(job_id, "exception_review_required", message, exception["url"])

    def _mark_exception_retest_success(self, job_id: int, finding_delta: int) -> None:
        current = self.db.get_job(job_id)
        if not current:
            return
        self.db.update_job(
            job_id,
            processed_count=int(current["processed_count"]) + 1,
            skipped_count=max(0, int(current["skipped_count"]) - 1),
            finding_count=int(current["finding_count"]) + finding_delta,
        )

    def _advance_progress(
        self, job_id: int, district: str, page_number: int, item_index: int,
        *, skipped: bool, finding_delta: int = 0, current_url: str = ""
    ) -> None:
        current = self.db.get_job(job_id)
        examined_by_district = json.loads(current["examined_by_district_json"] or "{}")
        examined_by_district[district] = int(examined_by_district.get(district, 0)) + 1
        self.db.update_job(
            job_id, current_url=current_url, current_page=page_number, current_item_index=item_index,
            examined_count=int(current["examined_count"]) + 1,
            batch_examined_count=int(current["batch_examined_count"]) + 1,
            skipped_count=int(current["skipped_count"]) + (1 if skipped else 0),
            processed_count=int(current["processed_count"]) + (0 if skipped else 1),
            finding_count=int(current["finding_count"]) + finding_delta,
            examined_by_district_json=json.dumps(examined_by_district, ensure_ascii=False),
            coverage_status="partial",
        )

    def _mark_partial_limit(self, job_id: int, current: dict) -> None:
        remaining = max(int(current["estimated_total"]) - int(current["examined_count"]), 0)
        reason = f"已达到本批次安全试扫上限，预计尚余 {remaining} 条；可手动恢复下一批"
        self.db.update_job(
            job_id, status=JobStatus.PARTIAL, coverage_status="partial", completion_kind="limit",
            pause_reason=reason, current_url="",
        )
        self.db.add_job_event(job_id, "partial_limit", reason)

    def _record_safety_event(self, job_id: int, event_type: str, details: dict) -> None:
        current = self.db.get_job(job_id)
        if not current:
            return
        updates = {}
        if event_type == "access":
            updates["access_count"] = int(current["access_count"]) + 1
        elif event_type == "retry":
            updates["retry_count"] = int(current["retry_count"]) + 1
        elif event_type == "rest":
            updates["rest_count"] = int(current["rest_count"]) + 1
        if updates:
            self.db.update_job(job_id, **updates)
        if event_type in {"retry", "rest"} or details.get("status_code") in {403, 429} or details.get("error"):
            self.db.add_job_event(
                job_id, event_type, json.dumps(details, ensure_ascii=False), current.get("current_url", ""), **details
            )

    def _update_schedule_after_job(self, job_id: int, status: JobStatus) -> None:
        """Advance cadence only after a terminal result, never when a job merely starts."""
        schedule = self.repo.schedule_for_last_job(job_id)
        job = self.db.get_job(job_id)
        if not schedule or not job:
            return
        now = datetime.now(timezone.utc)
        cfg = self.continuous_config
        values: dict[str, object] = {"last_status": str(status)}
        if status == JobStatus.COMPLETED:
            if job["mode"] == "full":
                values.update(
                    last_full_reconcile_at=now.isoformat(),
                    next_full_reconcile_at=(now + timedelta(days=cfg.full_reconcile_interval_days)).isoformat(),
                    next_incremental_at=(now + timedelta(hours=cfg.incremental_interval_hours)).isoformat(),
                )
            else:
                values.update(
                    last_incremental_at=now.isoformat(),
                    next_incremental_at=(now + timedelta(hours=cfg.incremental_interval_hours)).isoformat(),
                )
        else:
            retry_at = (now + timedelta(minutes=cfg.failure_retry_minutes)).isoformat()
            values["next_incremental_at"] = retry_at
            if job["mode"] == "full":
                values["next_full_reconcile_at"] = retry_at
        self.repo.update_schedule(str(schedule["site_key"]), **values)
    def ensure_continuous_scheduler(self) -> None:
        if not self.continuous_config.enabled:
            return
        if self._scheduler_task and not self._scheduler_task.done():
            return
        self._scheduler_task = asyncio.create_task(self._continuous_scheduler_loop())

    async def _continuous_scheduler_loop(self) -> None:
        while True:
            try:
                await self._tick_continuous_schedules()
            except Exception as exc:
                # 调度失败不拖垮主服务
                print(f"continuous scheduler error: {type(exc).__name__}: {exc}")
            await asyncio.sleep(60)

    async def _tick_continuous_schedules(self) -> None:
        cfg = self.continuous_config
        if not cfg.enabled:
            return
        now = datetime.now(timezone.utc)
        from app.config import targets_for_site
        for site_key, _site in SCAN_SITES.items():
            if cfg.site_keys and site_key not in cfg.site_keys:
                continue
            labels = [target.label for target in targets_for_site(site_key)]
            schedule = self.repo.get_or_create_schedule(
                site_key, labels,
                incremental_hours=cfg.incremental_interval_hours,
                reconcile_days=cfg.full_reconcile_interval_days,
            )
            if not schedule.get("enabled") or self._active_job_for(labels):
                continue
            due_full = schedule.get("next_full_reconcile_at") and datetime.fromisoformat(
                schedule["next_full_reconcile_at"]
            ) <= now
            due_incremental = schedule.get("next_incremental_at") and datetime.fromisoformat(
                schedule["next_incremental_at"]
            ) <= now
            if not due_full and not due_incremental:
                continue
            baseline = self.eligible_baseline_for(labels)
            if due_full:
                job_id = self.db.create_job(
                    labels, "full", self._safety_payload(labels), 0,
                    int(baseline["id"]) if baseline else None,
                )
                self.db.update_job(job_id, completion_kind="full_reconcile", scan_phase=ScanPhase.FULL_RECONCILE)
                status = "running_full_reconcile"
            elif baseline:
                job_id = self.db.create_job(
                    labels, "incremental", self._safety_payload(labels), 0, int(baseline["id"]),
                )
                self.db.update_job(job_id, scan_phase=ScanPhase.INCREMENTAL_DISCOVERY)
                status = "running_incremental"
            else:
                # An opted-in site needs one baseline before incremental reuse is possible.
                job_id = self.db.create_job(labels, "full", self._safety_payload(labels), 0, None)
                self.db.update_job(job_id, completion_kind="initial_baseline", scan_phase=ScanPhase.DISCOVERING)
                status = "running_initial_baseline"
            self.repo.update_schedule(site_key, last_job_id=job_id, last_status=status)
            self._tasks[job_id] = asyncio.create_task(self._run(job_id))
    def continuous_status(self) -> list[dict]:
        from app.config import targets_for_site
        rows = []
        for site_key, site in SCAN_SITES.items():
            labels = [t.label for t in targets_for_site(site_key)]
            schedule = self.repo.get_or_create_schedule(
                site_key, labels,
                incremental_hours=self.continuous_config.incremental_interval_hours,
                reconcile_days=self.continuous_config.full_reconcile_interval_days,
            )
            rows.append({
                "site_key": site_key,
                "site_label": site.label,
                "last_incremental_at": schedule.get("last_incremental_at"),
                "last_full_reconcile_at": schedule.get("last_full_reconcile_at"),
                "next_incremental_at": schedule.get("next_incremental_at"),
                "next_full_reconcile_at": schedule.get("next_full_reconcile_at"),
                "last_job_id": schedule.get("last_job_id"),
                "last_status": schedule.get("last_status"),
                "enabled": bool(schedule.get("enabled")),
            })
        return rows
