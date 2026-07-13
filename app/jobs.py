from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone

from chinese_calendar import is_workday
from playwright.async_api import async_playwright

from app.collector import BrowserCollector, LinkChecker
from app.config import SafetyConfig, ScanTarget, resolve_target
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
)
from app.putuo_collector import PutuoDistrictCollector
from app.repository import Repository
from app.rules import WorkdayCalendar, evaluate_record
from app.safety import SafetyController


class JobManager:
    def __init__(self, db: Database):
        self.db = db
        self.repo = Repository(db)
        self._tasks: dict[int, asyncio.Task] = {}
        self._guard = asyncio.Lock()

    async def create_and_start(
        self, districts: list[str], mode: str, max_documents: int = 0, baseline_job_id: int | None = None,
    ) -> int:
        async with self._guard:
            active = self.db.active_job()
            if active:
                return int(active["id"])
            if mode == "incremental":
                self._validate_incremental_baseline(districts, baseline_job_id)
            elif baseline_job_id is not None:
                raise ValueError("全量扫描不能指定基准任务")
            safety = SafetyConfig()
            job_id = self.db.create_job(districts, mode, asdict(safety), max_documents, baseline_job_id)
            self._tasks[job_id] = asyncio.create_task(self._run(job_id))
            return job_id

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
            active = self.db.active_job()
            if active and int(active["id"]) != job_id:
                raise ValueError("已有扫描任务正在运行")
            job = self.db.get_job(job_id)
            if not job or job["status"] not in {JobStatus.PAUSED, JobStatus.PARTIAL, JobStatus.COOLING, JobStatus.FAILED}:
                raise ValueError("该任务当前不能恢复")
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
        job = self.db.get_job(job_id)
        if not job:
            return
        safety_config = SafetyConfig(**json.loads(job["safety_json"]))
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
                        # 先在同一全局限速器下读取每个已选来源的总量，进度不会把未扫描范围误报为完成。
                        for target in targets:
                            collector = self._collector_for_target(browser, safety, target)
                            await collector.__aenter__()
                            collectors.append((target, collector))
                            await collector.check_robots()
                            await collector.select_district(target.district)
                            await self._record_target_total(job_id, target.label, await collector.estimated_total())
                        start_index = int(job["current_district_index"])
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
            completion_kind = "incremental" if job["mode"] == "incremental" else "full"
            self.db.update_job(
                job_id, status=JobStatus.COMPLETED, finished_at=utc_now(), current_url="",
                coverage_status="complete", completion_kind=completion_kind, pause_reason="",
            )
            self.db.add_job_event(job_id, "completed", f"{completion_kind} 扫描完成")
        except CooldownPause as exc:
            cooldown = datetime.now(timezone.utc) + timedelta(seconds=safety_config.cooldown_seconds)
            self.db.update_job(
                job_id, status=JobStatus.COOLING, pause_reason=str(exc), cooldown_until=cooldown.isoformat(),
                coverage_status="partial", completion_kind="safety_pause",
            )
            self.db.add_job_event(job_id, "cooling", str(exc), self.db.get_job(job_id)["current_url"])
            self._tasks[job_id] = asyncio.create_task(self._resume_after_cooldown(job_id, cooldown))
        except SafetyPause as exc:
            self.db.update_job(
                job_id, status=JobStatus.PAUSED, pause_reason=str(exc), cooldown_until=None,
                coverage_status="partial", completion_kind="data_pause",
            )
            self.db.add_job_event(job_id, "safety_pause", str(exc), self.db.get_job(job_id)["current_url"])
        except Exception as exc:
            self.db.update_job(
                job_id, status=JobStatus.FAILED, last_error=f"{type(exc).__name__}: {exc}",
                finished_at=utc_now(), coverage_status="partial", completion_kind="failed",
            )
            self.db.add_job_event(job_id, "failed", f"{type(exc).__name__}: {exc}")
        finally:
            if self._tasks.get(job_id) is asyncio.current_task():
                self._tasks.pop(job_id, None)

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

    async def _record_target_total(self, job_id: int, label: str, total: int) -> None:
        current = self.db.get_job(job_id)
        totals = json.loads(current["total_by_district_json"] or "{}")
        totals[label] = total
        self.db.update_job(
            job_id,
            estimated_total=sum(int(value) for value in totals.values()),
            total_by_district_json=json.dumps(totals, ensure_ascii=False),
        )

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
        if row["detail_status"] in {"exception", "checked_incomplete"}:
            return False, "基线详情异常或表头字段不完整"
        if row["detail_status"] in {"no_header_pass", "reused_current_no_header", "reused_baseline_no_header"}:
            return True, "基线无表头 PASS，列表信息未变化"
        if self._all_missing_fields(self._inspection_from_baseline(item, row)):
            return False, "基线表头字段不完整"
        if not self._baseline_document_id(row):
            return False, "基线没有可复用的详情文档"
        return True, "与基线的 URL、标题和列表发布日期一致"

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
                link_result = await checker.check(related.kind, related.url)
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
                    self.repo.save_link_check(job_id, document_id, await checker.check(related.kind, related.url))
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
