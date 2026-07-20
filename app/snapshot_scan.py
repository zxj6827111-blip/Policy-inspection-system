"""列表快照 + 稳定身份幂等详情处理 + 结束前追赶。

与 JobManager 协作：把“页码游标”从恢复正确性路径中剥离。
"""
from __future__ import annotations

import json
from datetime import date
from typing import Any, Callable, Awaitable

from app.config import ContinuousScanConfig, ScanTarget
from app.domain import (
    DetailInspection,
    GenerationItemStatus,
    ItemReviewRequired,
    JobStatus,
    PolicyListItem,
    PolicyRecord,
    SafetyPause,
    ScanPhase,
)
from app.putuo_collector import PUTUO_QUERY_CONTRACT


def list_item_from_generation_row(row: dict, target: ScanTarget) -> PolicyListItem:
    listed = None
    if row.get("listed_date"):
        try:
            listed = date.fromisoformat(row["listed_date"])
        except ValueError:
            listed = None
    return PolicyListItem(
        district=target.district,
        page_number=int(row["page_number"] or 1),
        item_index=int(row["item_index"] or 0),
        title=row["title"] or "",
        url=row["url"] or "",
        published_date=listed,
        source_site=target.label,
        source_key=target.key,
        source_channel_id=target.channel_id,
        stable_id=row["stable_id"] or "",
        content_fingerprint=row.get("content_fingerprint") or "",
        api_record_id=row.get("api_record_id") or "",
        doc_flag=row.get("doc_flag") or "",
    )


def refresh_generation_stats(repo, generation_id: int) -> dict[str, int]:
    counts = repo.generation_item_counts(generation_id)
    repo.update_generation(
        generation_id,
        discovered_count=counts["total"],
        completed_count=counts.get("completed", 0) + counts.get("reused", 0),
        reused_count=counts.get("reused", 0),
        retry_count=counts.get("retry", 0),
        review_count=counts.get("review", 0),
    )
    return counts


def coverage_from_generation(repo, job_id: int, generation_id: int) -> dict[str, Any]:
    counts = repo.generation_item_counts(generation_id)
    terminal = (
        counts.get("completed", 0)
        + counts.get("reused", 0)
        + counts.get("review", 0)
    )
    open_count = counts.get("pending", 0) + counts.get("checking", 0) + counts.get("retry", 0)
    item_results = repo.scan_item_count(job_id)
    unique_stable = counts["total"]
    observed_total = int((repo.get_generation(generation_id) or {}).get("observed_total") or 0)
    return {
        "counts": counts,
        "terminal": terminal,
        "open": open_count,
        "item_results": item_results,
        "unique_stable": unique_stable,
        "observed_total": observed_total,
        "ok": (
            open_count == 0
            and unique_stable > 0
            and terminal == unique_stable
            and item_results >= terminal
            and (observed_total == 0 or observed_total == unique_stable)
        ),
    }


async def discover_target_pages(
    *,
    collector,
    target: ScanTarget,
    generation_id: int,
    job_id: int,
    repo,
    db,
    start_page: int = 1,
    is_running: Callable[[], bool],
    on_progress: Callable[[int, int, int], None] | None = None,
    mark_absent: bool = False,
) -> dict[str, int]:
    """阶段 A：逐页发现，按 stable_id 幂等写入 generation_items。"""
    db.update_job(job_id, scan_phase=ScanPhase.DISCOVERING, current_district=target.label)
    await collector.select_district(target.district)
    # 优先复用 iter_items：保留普陀 10000 截断与真实终点逻辑
    use_iter = hasattr(collector, "iter_items") and not hasattr(collector, "fetch_list_page") and not mark_absent
    if use_iter and start_page == 1:
        new_count = 0
        reused_discovery = 0
        present_ids: set[str] = set()
        last_page = 1
        last_index = -1
        async for item in collector.iter_items(target.district, start_page, -1):
            if not is_running():
                return {"new": new_count, "seen": reused_discovery, "pages": last_page}
            if not item.source_key:
                item.source_key = target.key
                item.source_site = target.label
                item.source_channel_id = target.channel_id
            if not item.stable_id:
                item.stable_id = f"{target.key}|url:{item.url}"
            present_ids.add(item.stable_id)
            _id, created = repo.upsert_generation_item(generation_id, job_id, item)
            repo.upsert_source_inventory(item, generation_id)
            if created:
                new_count += 1
            else:
                reused_discovery += 1
            last_page = item.page_number
            last_index = item.item_index
            repo.update_generation(
                generation_id,
                list_cursor_page=last_page,
                list_cursor_item=last_index,
                new_count=new_count,
            )
            db.update_job(
                job_id,
                current_page=last_page,
                current_item_index=last_index,
            )
            if on_progress:
                on_progress(last_page, new_count, reused_discovery)
        if hasattr(collector, "estimated_total"):
            try:
                observed = int(await collector.estimated_total())
                repo.update_generation(generation_id, observed_total=observed)
            except Exception:
                pass
        refresh_generation_stats(repo, generation_id)
        # 同步 job 分来源观测总量，便于中途暂停后审计
        try:
            job_row = db.get_job(job_id) or {}
            totals = json.loads(job_row.get("total_by_district_json") or "{}")
            examined_by = json.loads(job_row.get("examined_by_district_json") or "{}")
            totals[target.label] = max(int(totals.get(target.label) or 0), new_count + reused_discovery, len(present_ids))
            # examined stays based on completed processing elsewhere
            db.update_job(
                job_id,
                total_by_district_json=json.dumps(totals, ensure_ascii=False),
                estimated_total=sum(int(v) for v in totals.values()),
            )
        except Exception:
            pass
        return {"new": new_count, "seen": reused_discovery, "pages": last_page, "present": len(present_ids)}

    observed = 0
    if hasattr(collector, "observed_total_count"):
        observed = int(collector.observed_total_count() or 0)
    elif hasattr(collector, "estimated_total"):
        observed = int(await collector.estimated_total())
    repo.update_generation(generation_id, observed_total=observed, target_key=target.key)

    new_count = 0
    reused_discovery = 0
    present_ids: set[str] = set()
    page = max(1, start_page)
    empty_pages = 0
    max_empty = 1
    declared_pages = getattr(collector, "_declared_total_pages", 0) or 0
    capped = bool(getattr(collector, "capped_pagination_active", False))
    max_extra = int(getattr(collector, "capped_max_extra_pages", 2000) or 2000)
    api_cap = int(getattr(collector, "api_total_cap", 10_000) or 10_000)

    while True:
        if not is_running():
            return {"new": new_count, "seen": reused_discovery, "pages": page}
        if hasattr(collector, "fetch_list_page"):
            items = await collector.fetch_list_page(page)
        else:
            # municipal fallback: single-shot iter not used here
            items = []
            async for item in collector.iter_items(target.district, page, -1):
                items.append(item)
            # one page dump then break
            for item in items:
                if not item.source_key:
                    item.source_key = target.key
                    item.source_site = target.label
                    item.source_channel_id = target.channel_id
                if not item.stable_id:
                    item.stable_id = f"{target.key}|url:{item.url}"
                present_ids.add(item.stable_id)
                _id, created = repo.upsert_generation_item(generation_id, job_id, item)
                repo.upsert_source_inventory(item, generation_id)
                if created:
                    new_count += 1
                else:
                    reused_discovery += 1
            break

        if not items:
            empty_pages += 1
            if page <= declared_pages and not capped:
                raise SafetyPause(f"{target.label} 列表第 {page} 页在声明范围内为空")
            if empty_pages >= max_empty:
                if hasattr(collector, "capped_pagination_active") and collector.capped_pagination_active:
                    # mark resolved if collector tracks it
                    if hasattr(collector, "_capped_resolved"):
                        collector._capped_resolved = True
                break
            page += 1
            continue

        empty_pages = 0
        page_stable_ids: set[str] = set()
        new_on_page = 0
        for item in items:
            if not item.source_key:
                item.source_key = target.key
            if not item.source_site:
                item.source_site = target.label
                item.source_channel_id = target.channel_id
            if not item.stable_id:
                item.stable_id = f"{target.key}|url:{item.url}"
            if item.stable_id in page_stable_ids:
                raise SafetyPause(f"duplicate stable identity on page {page}: {item.stable_id}")
            page_stable_ids.add(item.stable_id)
            present_ids.add(item.stable_id)
            _id, created = repo.upsert_generation_item(generation_id, job_id, item)
            repo.upsert_source_inventory(item, generation_id)
            if created:
                new_count += 1
                new_on_page += 1
            else:
                reused_discovery += 1

        if capped and declared_pages and page > declared_pages:
            if new_on_page == 0:
                raise SafetyPause(f"普陀区官网列表截断分页在第 {page} 页重复返回已扫描记录，无法确认真实终点")
            if new_on_page != len(items):
                raise SafetyPause(f"普陀区官网列表截断分页在第 {page} 页出现部分重复记录，已暂停")

        if on_progress:
            on_progress(page, new_count, reused_discovery)
        repo.update_generation(
            generation_id,
            list_cursor_page=page,
            list_cursor_item=items[-1].item_index if items else -1,
            new_count=new_count,
        )
        db.update_job(
            job_id,
            current_page=page,
            current_item_index=items[-1].item_index if items else -1,
            current_url="",
        )
        # advance page logic for putuo cap
        declared_pages = getattr(collector, "_declared_total_pages", declared_pages) or declared_pages
        capped = bool(getattr(collector, "capped_pagination_active", False)) or capped
        observed = int(getattr(collector, "observed_total_count", lambda: observed)() or observed)
        if hasattr(collector, "observed_total_count"):
            ot = collector.observed_total_count()
            if ot is not None:
                observed = int(ot)
                repo.update_generation(generation_id, observed_total=observed)

        if not capped and declared_pages and page >= declared_pages:
            # probe beyond if at cap
            if observed == api_cap or (getattr(collector, "_declared_total_count", None) == api_cap):
                page += 1
                continue
            break
        if capped and declared_pages and page > declared_pages + max_extra:
            raise SafetyPause(f"{target.label} 截断分页超过安全上限")
        if capped and 0 < len(items) < int(getattr(collector, "page_size", 15) or 15) and page >= declared_pages:
            # try one more; empty will end
            page += 1
            continue
        page += 1

    if mark_absent:
        # Persisted generation membership survives a pause/restart. An in-memory set does not.
        repo.mark_inventory_absent_for_generation(target.key, generation_id)
    if capped or mark_absent:
        repo.update_generation(
            generation_id,
            observed_total=repo.generation_target_item_count(generation_id, target.key),
        )
    refresh_generation_stats(repo, generation_id)
    return {"new": new_count, "seen": reused_discovery, "pages": page, "present": len(present_ids)}


async def catchup_head(
    *,
    collector,
    target: ScanTarget,
    generation_id: int,
    job_id: int,
    repo,
    db,
    config: ContinuousScanConfig,
    is_running: Callable[[], bool],
    process_pending: Callable[[], Awaitable[None]],
) -> list[dict[str, int]]:
    """详情队列清空后头部追赶，最多 max_catchup_rounds 轮。"""
    rounds: list[dict[str, int]] = []
    empty_streak = 0
    for round_no in range(1, config.max_catchup_rounds + 1):
        if not is_running():
            break
        db.update_job(job_id, scan_phase=ScanPhase.CATCHING_UP)
        repo.update_generation(generation_id, phase=ScanPhase.CATCHING_UP, catchup_round=round_no)
        if hasattr(collector, "discover_head_pages"):
            items = await collector.discover_head_pages(2)
        else:
            items = await collector.fetch_list_page(1) if hasattr(collector, "fetch_list_page") else []
        added = 0
        for item in items:
            if not item.source_key:
                item.source_key = target.key
            _id, created = repo.upsert_generation_item(generation_id, job_id, item)
            repo.upsert_source_inventory(item, generation_id)
            if created:
                added += 1
        stats = refresh_generation_stats(repo, generation_id)
        rounds.append({"round": round_no, "new": added, "discovered": stats["total"]})
        if added == 0:
            empty_streak += 1
            if empty_streak >= config.catchup_empty_rounds_to_finish:
                break
        else:
            empty_streak = 0
            await process_pending()
            empty_streak = 0
    return rounds
