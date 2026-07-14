from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from app.config import BASE_DIR, SCAN_SITES, SafetyConfig, resolve_site, resolve_target, targets_for_site
from app.db import Database, utc_now
from app.exporter import export_job
from app.jobs import JobManager


db = Database()
manager = JobManager(db)
templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.initialize()
    manager.recover_interrupted()
    yield


app = FastAPI(title="上海区级政策文件安全巡检系统", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "static"), name="static")


class CreateJobRequest(BaseModel):
    targets: list[str] = Field(default_factory=list)
    # 兼容已打开的旧页面和历史自动化脚本。
    districts: list[str] = Field(default_factory=list)
    mode: str = "incremental"
    max_documents: int = Field(default=0, ge=0, le=100000)
    baseline_job_id: int | None = Field(default=None, ge=1)


class StartSiteJobsRequest(BaseModel):
    site_keys: list[str] = Field(default_factory=list)
    max_documents: int = Field(default=0, ge=0, le=100000)


class ReviewRequest(BaseModel):
    decision: str
    note: str = Field(default="", max_length=500)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"sites": list(SCAN_SITES.values()), "safety": SafetyConfig()},
    )


@app.get("/health")
async def health():
    return {"status": "ok", "database": str(db.path)}


@app.get("/api/jobs")
async def list_jobs():
    return db.list_jobs()


@app.get("/api/baselines")
async def list_baselines():
    """仅返回可作为增量依据的完整全量扫描任务。"""
    return db.eligible_baselines()


def _site_status(site_key: str) -> dict:
    site = resolve_site(site_key)
    labels = [target.label for target in targets_for_site(site_key)]
    plan = manager.automatic_plan(labels)
    latest = plan["job"]
    baseline = plan["baseline"]
    return {
        "key": site.key,
        "label": site.label,
        "district": site.district,
        "source_level": site.source_level,
        "source_name": site.source_name,
        "host": site.host,
        "target_count": len(site.target_keys),
        "target_labels": labels,
        "next_action": plan["action"],
        "next_mode": plan["mode"],
        "baseline_job_id": int(baseline["id"]) if baseline else None,
        "latest_job": latest,
    }


@app.get("/api/scan-sites")
async def list_scan_sites():
    return [_site_status(site_key) for site_key in SCAN_SITES]


@app.post("/api/site-jobs")
async def start_site_jobs(payload: StartSiteJobsRequest):
    site_keys = list(dict.fromkeys(payload.site_keys))
    if not site_keys:
        raise HTTPException(400, "请至少选择一个扫描站点")
    if len(site_keys) != len(payload.site_keys):
        raise HTTPException(400, "同一扫描站点只能选择一次")
    if len(site_keys) > len(SCAN_SITES):
        raise HTTPException(400, "扫描站点数量超出范围")
    try:
        selected_sites = [(resolve_site(site_key), targets_for_site(site_key)) for site_key in site_keys]
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    results = []
    for site, targets in selected_sites:
        try:
            labels = [target.label for target in targets]
            result = await manager.start_automatic(labels, payload.max_documents)
        except ValueError as exc:
            raise HTTPException(409, f"{site.label}：{exc}") from exc
        results.append({"site_key": site.key, "site_label": site.label, **result})
    return {"jobs": results}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: int):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    return job


@app.post("/api/jobs")
async def create_job(payload: CreateJobRequest):
    selected = payload.targets or payload.districts
    if not selected:
        raise HTTPException(400, "请至少选择一个扫描来源")
    try:
        # 使用展示名称写入任务，历史记录和导出中可直接区分市级/区级来源。
        targets = [resolve_target(value) for value in selected]
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    labels = [target.label for target in targets]
    if len(set(labels)) != len(labels):
        raise HTTPException(400, "同一扫描来源只能选择一次")
    if payload.mode not in {"full", "incremental"}:
        raise HTTPException(400, "扫描模式必须是 full 或 incremental")
    try:
        job_id = await manager.create_and_start(
            labels, payload.mode, payload.max_documents, payload.baseline_job_id,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}/item-stats")
async def item_stats(job_id: int):
    if not db.get_job(job_id):
        raise HTTPException(404, "任务不存在")
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT detail_status,COUNT(*) AS count FROM scan_item_results WHERE job_id=? GROUP BY detail_status",
            (job_id,),
        ).fetchall()
    counts = {row["detail_status"]: int(row["count"]) for row in rows}
    no_header = sum(count for status, count in counts.items() if "no_header" in status)
    reused = sum(count for status, count in counts.items() if status.startswith("reused_"))
    return {
        "total": sum(counts.values()),
        "complete_header": counts.get("checked_complete", 0),
        "incomplete_header": counts.get("checked_incomplete", 0),
        "no_header_pass": no_header,
        "baseline_reused": counts.get("reused_baseline_detail", 0) + counts.get("reused_baseline_no_header", 0),
        "current_url_reused": counts.get("reused_current_detail", 0) + counts.get("reused_current_no_header", 0),
        "exception": counts.get("exception", 0),
        "by_status": counts,
    }


@app.post("/api/jobs/{job_id}/pause")
async def pause_job(job_id: int):
    if not db.get_job(job_id):
        raise HTTPException(404, "任务不存在")
    try:
        await manager.pause(job_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True}


@app.post("/api/jobs/{job_id}/resume")
async def resume_job(job_id: int):
    try:
        await manager.resume(job_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True}


@app.post("/api/jobs/{job_id}/stop")
async def stop_job(job_id: int):
    if not db.get_job(job_id):
        raise HTTPException(404, "任务不存在")
    try:
        await manager.stop(job_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True}


@app.get("/api/jobs/{job_id}/export")
async def download_export(job_id: int):
    try:
        output = export_job(db, job_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return FileResponse(output, filename=output.name, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.get("/api/jobs/{job_id}/findings")
async def list_findings(job_id: int):
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT f.*,d.title,d.url,d.district,COALESCE(r.note,'') review_note
            FROM rule_findings f JOIN policy_documents d ON d.id=f.document_id
            LEFT JOIN review_decisions r ON r.finding_id=f.id
            WHERE f.job_id=? ORDER BY f.id DESC LIMIT 500""", (job_id,)
        ).fetchall()
        return [dict(row) for row in rows]


@app.get("/api/jobs/{job_id}/scan-exceptions")
async def list_scan_exceptions(job_id: int):
    with db.connect() as conn:
        if not conn.execute("SELECT id FROM scan_jobs WHERE id=?", (job_id,)).fetchone():
            raise HTTPException(404, "任务不存在")
        count_rows = conn.execute(
            "SELECT status,COUNT(*) count FROM scan_exceptions WHERE job_id=? GROUP BY status", (job_id,)
        ).fetchall()
        counts = {status: 0 for status in ("pending", "resolved", "review_required")}
        counts.update({row["status"]: int(row["count"]) for row in count_rows})
        rows = conn.execute(
            """SELECT * FROM scan_exceptions WHERE job_id=? AND status='review_required'
            ORDER BY page_number,item_index""",
            (job_id,),
        ).fetchall()
    return {"items": [dict(row) for row in rows], "counts": counts}


@app.get("/api/jobs/{job_id}/review-queue")
async def review_queue(
    job_id: int,
    review_status: str = Query(default="pending"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=50),
):
    if review_status not in {"all", "pending", "confirmed", "dismissed"}:
        raise HTTPException(400, "无效的复核状态筛选条件")
    with db.connect() as conn:
        if not conn.execute("SELECT id FROM scan_jobs WHERE id=?", (job_id,)).fetchone():
            raise HTTPException(404, "任务不存在")
        count_rows = conn.execute(
            "SELECT review_status,COUNT(*) count FROM rule_findings WHERE job_id=? GROUP BY review_status", (job_id,)
        ).fetchall()
        counts = {status: 0 for status in ("pending", "confirmed", "dismissed")}
        counts.update({row["review_status"]: int(row["count"]) for row in count_rows})
        clause = "" if review_status == "all" else "AND f.review_status=?"
        params = [job_id]
        if review_status != "all":
            params.append(review_status)
        total = conn.execute(
            f"SELECT COUNT(*) FROM rule_findings f WHERE f.job_id=? {clause}", params
        ).fetchone()[0]
        rows = conn.execute(
            f"""SELECT f.*,d.title,d.url,d.district,COALESCE(r.note,'') review_note
            FROM rule_findings f JOIN policy_documents d ON d.id=f.document_id
            LEFT JOIN review_decisions r ON r.finding_id=f.id
            WHERE f.job_id=? {clause} ORDER BY f.id DESC LIMIT ? OFFSET ?""",
            [*params, page_size, (page - 1) * page_size],
        ).fetchall()
    return {
        "items": [dict(row) for row in rows], "counts": counts, "filter": review_status,
        "page": page, "page_size": page_size, "total": int(total),
    }


@app.get("/api/findings/{finding_id}/evidence")
async def finding_evidence(finding_id: int):
    """返回本地保存的复核快照，不直接改写第三方政府网站页面。"""
    with db.connect() as conn:
        row = conn.execute(
            """SELECT f.*,d.title,d.url,d.district,d.issuing_agency,d.page_document_number,
            d.published_date,d.authored_date,d.body_text
            FROM rule_findings f JOIN policy_documents d ON d.id=f.document_id WHERE f.id=?""",
            (finding_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "问题记录不存在")
    return dict(row)


@app.post("/api/findings/{finding_id}/review")
async def review_finding(finding_id: int, payload: ReviewRequest):
    if payload.decision not in {"confirmed", "dismissed", "pending"}:
        raise HTTPException(400, "无效的复核结论")
    with db.connect() as conn:
        finding = conn.execute("SELECT id FROM rule_findings WHERE id=?", (finding_id,)).fetchone()
        if not finding:
            raise HTTPException(404, "问题记录不存在")
        conn.execute(
            """INSERT INTO review_decisions(finding_id,decision,note,reviewed_at) VALUES(?,?,?,?)
            ON CONFLICT(finding_id) DO UPDATE SET decision=excluded.decision,note=excluded.note,reviewed_at=excluded.reviewed_at""",
            (finding_id, payload.decision, payload.note, utc_now()),
        )
        conn.execute("UPDATE rule_findings SET review_status=? WHERE id=?", (payload.decision, finding_id))
    return {"ok": True}
