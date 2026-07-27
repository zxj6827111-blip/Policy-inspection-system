from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from app.auth.dependencies import (
    get_auth_service,
    get_optional_user,
    require_login,
    require_roles,
    require_write,
)
from app.auth.models import CurrentUser
from app.auth.service import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE, AuthService
from app.config import (
    BASE_DIR,
    SCAN_SITES,
    SafetyConfig,
    district_sites,
    municipal_sites,
    resolve_site,
    resolve_target,
    targets_for_site,
)
from app.db import Database, utc_now
from app.exporter import export_job
from app.findings_model import (
    decision_to_human,
    enrich_finding_row,
    is_customer_formal_problem,
    legacy_review_status,
)
from app.jobs import JobManager


db = Database()
manager = JobManager(db)
templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")


def _client_meta(request: Request) -> tuple[str, str]:
    ip = request.client.host if request.client else ""
    ua = request.headers.get("user-agent", "")
    return ip, ua


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.runtime_config import get_runtime_settings, reset_runtime_settings_cache

    reset_runtime_settings_cache()
    settings = get_runtime_settings(refresh=True)
    # Safe startup log — no secrets, passwords, tokens, or cookies.
    print(
        f"[startup] app_env={settings.app_env.value} auth_enabled={settings.auth_enabled}",
        flush=True,
    )
    db.initialize()
    app.state.db = db
    app.state.manager = manager
    app.state.runtime_settings = settings
    manager.recover_interrupted()
    manager.ensure_continuous_scheduler()
    yield


app = FastAPI(title="上海区级政策文件安全巡检系统", version="0.2.0-phase1", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "app" / "static")), name="static")


# M1: Protect API documentation in production (fail-closed).
# /docs, /redoc, /openapi.json are accessible only when APP_ENV != production.
# In production these paths return 404 to avoid leaking the API surface.
_DOCS_PATHS = {"/docs", "/redoc", "/openapi.json"}


@app.middleware("http")
async def protect_api_docs(request: Request, call_next):
    path = request.url.path.rstrip("/") or "/"
    if path in _DOCS_PATHS:
        from app.runtime_config import AppEnv, get_runtime_settings

        settings = get_runtime_settings()
        if settings.app_env == AppEnv.PRODUCTION:
            return JSONResponse({"detail": "API 文档在生产环境已禁用"}, status_code=404)
    return await call_next(request)


class CreateJobRequest(BaseModel):
    targets: list[str] = Field(default_factory=list)
    districts: list[str] = Field(default_factory=list)
    mode: str = "incremental"
    max_documents: int = Field(default=0, ge=0, le=100000)
    baseline_job_id: int | None = Field(default=None, ge=1)


class StartSiteJobsRequest(BaseModel):
    site_keys: list[str] = Field(default_factory=list)
    max_documents: int = Field(default=0, ge=0, le=100000)


class RebuildSiteJobsRequest(BaseModel):
    site_keys: list[str] = Field(default_factory=list)


class ReviewRequest(BaseModel):
    decision: str
    note: str = Field(default="", max_length=500)
    reason_code: str = Field(default="other", max_length=64)


class LoginRequest(BaseModel):
    username: str
    password: str


class CreateUserRequest(BaseModel):
    username: str
    password: str
    roles: list[str]
    display_name: str = ""


class BatchReviewRequest(BaseModel):
    finding_ids: list[int] = Field(default_factory=list)
    decision: str
    reason_code: str = "other"
    note: str = Field(default="", max_length=500)


class DualAssignRequest(BaseModel):
    finding_ids: list[int] = Field(default_factory=list)
    first_reviewer_id: int
    first_reviewer_username: str
    second_reviewer_id: int
    second_reviewer_username: str


class DualReviewRequest(BaseModel):
    round: int = Field(ge=1, le=2)
    decision: str
    reason_code: str = "other"
    note: str = Field(default="", max_length=500)


class ArbitrateRequest(BaseModel):
    decision: str
    reason_code: str = "other"
    note: str = Field(default="", max_length=500)


def _set_session_cookies(response: Response, session_id: str, csrf_token: str, expires) -> None:
    secure = os.environ.get("POLICY_COOKIE_SECURE", "").lower() in {"1", "true", "yes"}
    response.set_cookie(
        SESSION_COOKIE,
        session_id,
        httponly=True,
        samesite="lax",
        secure=secure,
        max_age=7 * 24 * 3600,
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf_token,
        httponly=False,
        samesite="lax",
        secure=secure,
        max_age=7 * 24 * 3600,
        path="/",
    )


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "district_sites": district_sites(),
            "municipal_sites": municipal_sites(),
            "safety": SafetyConfig(),
        },
    )


@app.get("/health")
async def health():
    from app.runtime_config import get_runtime_settings

    settings = get_runtime_settings()
    return {
        "status": "ok",
        "database": "configured",
        "app_env": settings.app_env.value,
        "auth_enabled": settings.auth_enabled,
    }


@app.get("/api/auth/me")
async def auth_me(user: CurrentUser | None = Depends(get_optional_user)):
    if user is None:
        return {"authenticated": False}
    return {
        "authenticated": True,
        "id": user.id,
        "username": user.username,
        "roles": list(user.roles),
        "capabilities": sorted(user.capabilities()),
    }


@app.post("/api/auth/login")
async def auth_login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    auth: AuthService = Depends(get_auth_service),
):
    user_row = auth.authenticate(payload.username, payload.password)
    if not user_row:
        raise HTTPException(401, "用户名或密码错误")
    ip, ua = _client_meta(request)
    session_id, csrf_token, _expires = auth.create_session(int(user_row["id"]), ip=ip, user_agent=ua)
    user = auth.resolve_session(session_id)
    auth.audit(
        actor=user,
        action="auth.login",
        resource_type="session",
        resource_id=session_id,
        ip=ip,
        user_agent=ua,
    )
    _set_session_cookies(response, session_id, csrf_token, _expires)
    return {
        "ok": True,
        "username": user_row["username"],
        "roles": list(user.roles) if user else [],
        "csrf_token": csrf_token,
    }


@app.post("/api/auth/logout")
async def auth_logout(
    request: Request,
    response: Response,
    user: CurrentUser = Depends(require_write("read")),
    auth: AuthService = Depends(get_auth_service),
):
    # logout needs auth; capability read is on all roles — use jobs:write? Better special-case.
    ip, ua = _client_meta(request)
    auth.revoke_session(request.cookies.get(SESSION_COOKIE))
    auth.audit(actor=user, action="auth.logout", resource_type="session", ip=ip, user_agent=ua)
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    return {"ok": True}


@app.post("/api/auth/logout-simple")
async def auth_logout_simple(
    request: Request,
    response: Response,
    user: CurrentUser = Depends(require_login),
    auth: AuthService = Depends(get_auth_service),
):
    """Logout without CSRF for same-site navigation convenience after confirming session."""
    # Still require login; CSRF optional here only if header present — enforce CSRF via cookie match soft
    ip, ua = _client_meta(request)
    auth.revoke_session(request.cookies.get(SESSION_COOKIE))
    auth.audit(actor=user, action="auth.logout", resource_type="session", ip=ip, user_agent=ua)
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    return {"ok": True}


@app.get("/api/users")
async def list_users(
    user: CurrentUser = Depends(require_roles("admin")),
    auth: AuthService = Depends(get_auth_service),
):
    return auth.list_users()


@app.post("/api/users")
async def create_user(
    payload: CreateUserRequest,
    request: Request,
    user: CurrentUser = Depends(require_write("users:admin")),
    auth: AuthService = Depends(get_auth_service),
):
    ip, ua = _client_meta(request)
    try:
        user_id = auth.create_user(
            payload.username,
            payload.password,
            payload.roles,
            display_name=payload.display_name,
            created_by=user.username,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    auth.audit(
        actor=user,
        action="user.create",
        resource_type="user",
        resource_id=str(user_id),
        after={"username": payload.username, "roles": payload.roles},
        ip=ip,
        user_agent=ua,
    )
    return {"id": user_id, "username": payload.username}


@app.get("/api/audits")
async def list_audits(
    limit: int = Query(default=100, ge=1, le=500),
    user: CurrentUser = Depends(require_roles("admin", "auditor")),
):
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM operation_audits ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(row) for row in rows]


@app.get("/api/continuous-schedules")
async def continuous_schedules(_user: CurrentUser = Depends(require_login)):
    return manager.continuous_status()


@app.get("/api/jobs/{job_id}/generation")
async def job_generation(job_id: int, _user: CurrentUser = Depends(require_login)):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    gen_id = job.get("generation_id")
    generation = manager.repo.get_generation(int(gen_id)) if gen_id else manager.repo.latest_generation_for_job(job_id)
    counts = manager.repo.generation_item_counts(int(generation["id"])) if generation else {}
    try:
        stats = __import__("json").loads(job.get("generation_stats_json") or "{}")
    except Exception:
        stats = {}
    return {
        "job_id": job_id,
        "scan_phase": job.get("scan_phase") or "idle",
        "generation": generation,
        "counts": counts,
        "stats": stats,
    }


@app.get("/api/jobs")
async def list_jobs(_user: CurrentUser = Depends(require_login)):
    return db.list_jobs()


@app.get("/api/baselines")
async def list_baselines(_user: CurrentUser = Depends(require_login)):
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
async def list_scan_sites(_user: CurrentUser = Depends(require_login)):
    return [_site_status(site_key) for site_key in SCAN_SITES]


@app.post("/api/site-jobs")
async def start_site_jobs(
    payload: StartSiteJobsRequest,
    request: Request,
    user: CurrentUser = Depends(require_write("jobs:write")),
    auth: AuthService = Depends(get_auth_service),
):
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
    ip, ua = _client_meta(request)
    auth.audit(
        actor=user,
        action="jobs.site_start",
        resource_type="site_jobs",
        detail={"site_keys": site_keys, "results": results},
        ip=ip,
        user_agent=ua,
    )
    return {"jobs": results}


@app.post("/api/site-jobs/full-rebuild")
async def rebuild_site_jobs(
    payload: RebuildSiteJobsRequest,
    request: Request,
    user: CurrentUser = Depends(require_write("jobs:write")),
    auth: AuthService = Depends(get_auth_service),
):
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
    source_groups = [[target.label for target in targets] for _, targets in selected_sites]
    try:
        created = await manager.start_full_rebuilds(source_groups)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    ip, ua = _client_meta(request)
    auth.audit(
        actor=user,
        action="jobs.full_rebuild",
        resource_type="site_jobs",
        detail={"site_keys": site_keys},
        ip=ip,
        user_agent=ua,
    )
    return {
        "jobs": [
            {"site_key": site.key, "site_label": site.label, **result}
            for (site, _), result in zip(selected_sites, created, strict=True)
        ]
    }


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: int, _user: CurrentUser = Depends(require_login)):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    # Ensure count fields present even on pre-closeout rows
    from app.finding_counts import compute_counts_from_rows, COUNT_FIELD_HELP
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM rule_findings WHERE job_id=? AND COALESCE(is_current,1)=1",
            (job_id,),
        ).fetchall()
    live = compute_counts_from_rows([dict(r) for r in rows])
    payload = dict(job)
    for key, value in live.items():
        payload[key] = value
    payload["count_definitions"] = COUNT_FIELD_HELP
    # Compatibility: never present finding_count alone as 客户问题数
    payload["finding_count_meaning"] = COUNT_FIELD_HELP["finding_count"]
    payload["formal_finding_count_meaning"] = COUNT_FIELD_HELP["formal_finding_count"]
    return payload


@app.get("/api/jobs/{job_id}/counts")
async def job_counts(job_id: int, _user: CurrentUser = Depends(require_login)):
    if not db.get_job(job_id):
        raise HTTPException(404, "任务不存在")
    from app.finding_counts import compute_counts_from_rows, attach_count_help
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM rule_findings WHERE job_id=? AND COALESCE(is_current,1)=1",
            (job_id,),
        ).fetchall()
    return attach_count_help(compute_counts_from_rows([dict(r) for r in rows]))



@app.post("/api/jobs")
async def create_job(
    payload: CreateJobRequest,
    request: Request,
    user: CurrentUser = Depends(require_write("jobs:write")),
    auth: AuthService = Depends(get_auth_service),
):
    selected = payload.targets or payload.districts
    if not selected:
        raise HTTPException(400, "请至少选择一个扫描来源")
    try:
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
    ip, ua = _client_meta(request)
    auth.audit(
        actor=user,
        action="jobs.create",
        resource_type="job",
        resource_id=str(job_id),
        detail={"labels": labels, "mode": payload.mode},
        ip=ip,
        user_agent=ua,
    )
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}/item-stats")
async def item_stats(job_id: int, _user: CurrentUser = Depends(require_login)):
    if not db.get_job(job_id):
        raise HTTPException(404, "任务不存在")
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT detail_status,COUNT(*) AS count FROM scan_item_results WHERE job_id=? GROUP BY detail_status",
            (job_id,),
        ).fetchall()
    counts = {row["detail_status"]: int(row["count"]) for row in rows}
    no_header = sum(count for status, count in counts.items() if "no_header" in status)
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
async def pause_job(
    job_id: int,
    request: Request,
    user: CurrentUser = Depends(require_write("jobs:write")),
    auth: AuthService = Depends(get_auth_service),
):
    if not db.get_job(job_id):
        raise HTTPException(404, "任务不存在")
    try:
        await manager.pause(job_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    ip, ua = _client_meta(request)
    auth.audit(actor=user, action="jobs.pause", resource_type="job", resource_id=str(job_id), ip=ip, user_agent=ua)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/resume")
async def resume_job(
    job_id: int,
    request: Request,
    user: CurrentUser = Depends(require_write("jobs:write")),
    auth: AuthService = Depends(get_auth_service),
):
    try:
        await manager.resume(job_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    ip, ua = _client_meta(request)
    auth.audit(actor=user, action="jobs.resume", resource_type="job", resource_id=str(job_id), ip=ip, user_agent=ua)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/stop")
async def stop_job(
    job_id: int,
    request: Request,
    user: CurrentUser = Depends(require_write("jobs:write")),
    auth: AuthService = Depends(get_auth_service),
):
    if not db.get_job(job_id):
        raise HTTPException(404, "任务不存在")
    try:
        await manager.stop(job_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    ip, ua = _client_meta(request)
    auth.audit(actor=user, action="jobs.stop", resource_type="job", resource_id=str(job_id), ip=ip, user_agent=ua)
    return {"ok": True}


@app.get("/api/jobs/{job_id}/export")
async def download_export(
    job_id: int,
    request: Request,
    user: CurrentUser = Depends(require_login),
    auth: AuthService = Depends(get_auth_service),
):
    if not user.has_capability("export:read") and not user.has_role("admin"):
        raise HTTPException(403, "权限不足")
    try:
        output = export_job(db, job_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    ip, ua = _client_meta(request)
    auth.audit(
        actor=user,
        action="jobs.export",
        resource_type="job",
        resource_id=str(job_id),
        detail={"file": output.name},
        ip=ip,
        user_agent=ua,
    )
    return FileResponse(
        output, filename=output.name, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


def _finding_query_rows(conn, job_id: int, extra_clause: str = "", params: list | None = None):
    params = params or [job_id]
    rows = conn.execute(
        f"""SELECT f.*,d.title,d.url,d.district,COALESCE(r.note,'') review_note,
        COALESCE((
            SELECT fr.comment FROM finding_reviews fr WHERE fr.finding_id=f.id ORDER BY fr.id DESC LIMIT 1
        ), COALESCE(r.note, '')) AS latest_review_comment
        FROM rule_findings f JOIN policy_documents d ON d.id=f.document_id
        LEFT JOIN review_decisions r ON r.finding_id=f.id
        WHERE f.job_id=? {extra_clause} ORDER BY f.id DESC""",
        params,
    ).fetchall()
    return [enrich_finding_row(dict(row)) for row in rows]


@app.get("/api/jobs/{job_id}/findings")
async def list_findings(job_id: int, _user: CurrentUser = Depends(require_login)):
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT f.*,d.title,d.url,d.district,COALESCE(r.note,'') review_note
            FROM rule_findings f JOIN policy_documents d ON d.id=f.document_id
            LEFT JOIN review_decisions r ON r.finding_id=f.id
            WHERE f.job_id=? AND COALESCE(f.is_current,1)=1 ORDER BY f.id DESC LIMIT 500""",
            (job_id,),
        ).fetchall()
        return [enrich_finding_row(dict(row)) for row in rows]


@app.get("/api/jobs/{job_id}/finding-summary")
async def finding_summary(job_id: int, _user: CurrentUser = Depends(require_login)):
    with db.connect() as conn:
        if not conn.execute("SELECT id FROM scan_jobs WHERE id=?", (job_id,)).fetchone():
            raise HTTPException(404, "任务不存在")
        rows = conn.execute("SELECT * FROM rule_findings WHERE job_id=? AND COALESCE(is_current,1)=1", (job_id,)).fetchall()
    buckets = {
        "confirmed_problem": 0,
        "suspected": 0,
        "insufficient_data": 0,
        "collection_error": 0,
        "auto_excluded": 0,
        "human_excluded": 0,
        "customer_formal": 0,
        "total": 0,
    }
    for row in rows:
        item = enrich_finding_row(dict(row))
        buckets["total"] += 1
        buckets[item["ui_bucket"]] = buckets.get(item["ui_bucket"], 0) + 1
        if item["is_customer_formal"]:
            buckets["customer_formal"] += 1
    return buckets


@app.get("/api/jobs/{job_id}/scan-exceptions")
async def list_scan_exceptions(job_id: int, _user: CurrentUser = Depends(require_login)):
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
    _user: CurrentUser = Depends(require_login),
):
    allowed = {
        "all",
        "pending",
        "confirmed",
        "dismissed",
        "suspected",
        "confirmed_problem",
        "insufficient_data",
        "collection_error",
        "auto_excluded",
        "human_excluded",
        "customer_formal",
    }
    if review_status not in allowed:
        raise HTTPException(400, "无效的复核状态筛选条件")
    with db.connect() as conn:
        if not conn.execute("SELECT id FROM scan_jobs WHERE id=?", (job_id,)).fetchone():
            raise HTTPException(404, "任务不存在")
        all_rows = conn.execute(
            """SELECT f.*,d.title,d.url,d.district,COALESCE(r.note,'') review_note
            FROM rule_findings f JOIN policy_documents d ON d.id=f.document_id
            LEFT JOIN review_decisions r ON r.finding_id=f.id
            WHERE f.job_id=? ORDER BY f.id DESC""",
            (job_id,),
        ).fetchall()
    items = [enrich_finding_row(dict(row)) for row in all_rows]
    counts = {"pending": 0, "confirmed": 0, "dismissed": 0}
    bucket_counts = {
        "confirmed_problem": 0,
        "suspected": 0,
        "insufficient_data": 0,
        "collection_error": 0,
        "auto_excluded": 0,
        "human_excluded": 0,
        "customer_formal": 0,
    }
    for item in items:
        counts[item["review_status"]] = counts.get(item["review_status"], 0) + 1
        bucket_counts[item["ui_bucket"]] = bucket_counts.get(item["ui_bucket"], 0) + 1
        if item["is_customer_formal"]:
            bucket_counts["customer_formal"] += 1

    if review_status in {"pending", "confirmed", "dismissed"}:
        filtered = [item for item in items if item["review_status"] == review_status]
    elif review_status == "customer_formal":
        filtered = [item for item in items if item["is_customer_formal"]]
    elif review_status == "all":
        filtered = items
    else:
        filtered = [item for item in items if item["ui_bucket"] == review_status]

    total = len(filtered)
    start = (page - 1) * page_size
    page_items = filtered[start : start + page_size]
    return {
        "items": page_items,
        "counts": counts,
        "bucket_counts": bucket_counts,
        "filter": review_status,
        "page": page,
        "page_size": page_size,
        "total": total,
    }


@app.get("/api/findings/{finding_id}/evidence")
async def finding_evidence(finding_id: int, _user: CurrentUser = Depends(require_login)):
    with db.connect() as conn:
        row = conn.execute(
            """SELECT f.*,d.title,d.url,d.district,d.issuing_agency,d.page_document_number,
            d.published_date,d.authored_date,d.body_text
            FROM rule_findings f JOIN policy_documents d ON d.id=f.document_id WHERE f.id=?""",
            (finding_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "问题记录不存在")
        evidence_rows = conn.execute(
            "SELECT * FROM finding_evidence WHERE finding_id=? ORDER BY id",
            (finding_id,),
        ).fetchall()
    payload = enrich_finding_row(dict(row))
    fields = [dict(item) for item in evidence_rows]
    if not fields and payload.get("evidence_json"):
        try:
            import json

            raw_fields = json.loads(payload["evidence_json"] or "[]")
            fields = [
                {
                    "field_name": item.get("field_name", ""),
                    "raw_value": item.get("raw_value", ""),
                    "source_type": item.get("source_type", ""),
                    "excerpt": item.get("excerpt", item.get("raw_value", "")),
                }
                for item in raw_fields
            ]
        except Exception:
            fields = []
    # Dynamic labels for org rules
    if payload["rule_code"].startswith(("AGENCY-", "ORG-")) and not any(
        f.get("field_name") == "页面公开主体" for f in fields
    ):
        fields = [
            {"field_name": "页面公开主体", "raw_value": payload.get("issuing_agency") or payload.get("page_value") or ""},
            {"field_name": "实际发文机关", "raw_value": payload.get("body_value") or ""},
            {"field_name": "文号机关代字", "raw_value": payload.get("body_value") or ""},
            {"field_name": "正文证据", "raw_value": (payload.get("evidence") or "")[:500]},
            {"field_name": "机器状态", "raw_value": payload.get("machine_status") or ""},
            {"field_name": "置信度", "raw_value": str(payload.get("confidence") or "")},
            {"field_name": "规则版本", "raw_value": payload.get("rule_version") or ""},
            {"field_name": "人工状态", "raw_value": payload.get("human_status") or ""},
        ]
    payload["evidence_fields"] = fields
    # Remove fixed page/body exclusive presentation expectation — keep fields for compat
    payload["display_mode"] = "dynamic_fields"
    # Immutable review history across lineage (same stable_key / superseded chain)
    with db.connect() as conn:
        stable = payload.get("stable_key") or f"{payload.get('document_id')}:{payload.get('rule_code')}"
        history = conn.execute(
            """SELECT fr.* FROM finding_reviews fr
            JOIN rule_findings f ON f.id=fr.finding_id
            WHERE f.job_id=? AND (f.stable_key=? OR f.id=? OR f.superseded_by=? OR ? IN (
                SELECT id FROM rule_findings WHERE superseded_by=f.id
            ))
            ORDER BY fr.id""",
            (payload.get("job_id"), stable, finding_id, finding_id, finding_id),
        ).fetchall()
        # Simpler reliable query:
        history = conn.execute(
            """SELECT fr.*, f.rule_version AS finding_rule_version, f.id AS source_finding_id, f.is_current
            FROM finding_reviews fr
            JOIN rule_findings f ON f.id=fr.finding_id
            WHERE f.job_id=? AND f.document_id=? AND f.rule_code=?
            ORDER BY fr.id""",
            (payload.get("job_id"), payload.get("document_id"), payload.get("rule_code")),
        ).fetchall()
    payload["review_history"] = [dict(r) for r in history]
    payload["stable_key"] = payload.get("stable_key") or f"{payload.get('document_id')}:{payload.get('rule_code')}"
    return payload


@app.post("/api/findings/{finding_id}/review")
async def review_finding(
    finding_id: int,
    payload: ReviewRequest,
    request: Request,
    user: CurrentUser = Depends(require_write("review:write")),
    auth: AuthService = Depends(get_auth_service),
):
    try:
        human = decision_to_human(payload.decision)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    legacy_decision = {
        "human_confirmed": "confirmed",
        "human_dismissed": "dismissed",
        "unreviewed": "pending",
        "needs_more_evidence": "pending",
    }[human]
    with db.connect() as conn:
        finding = conn.execute("SELECT * FROM rule_findings WHERE id=?", (finding_id,)).fetchone()
        if not finding:
            raise HTTPException(404, "问题记录不存在")
        before = dict(finding)
        machine = finding["machine_status"] if "machine_status" in finding.keys() and finding["machine_status"] else finding["review_status"]
        review_status = legacy_review_status(machine, human)
        conn.execute(
            """INSERT INTO review_decisions(finding_id,decision,note,reviewed_at) VALUES(?,?,?,?)
            ON CONFLICT(finding_id) DO UPDATE SET decision=excluded.decision,note=excluded.note,reviewed_at=excluded.reviewed_at""",
            (finding_id, legacy_decision, payload.note, utc_now()),
        )
        prev_review = conn.execute(
            "SELECT id FROM finding_reviews WHERE finding_id=? ORDER BY id DESC LIMIT 1",
            (finding_id,),
        ).fetchone()
        conn.execute(
            """INSERT INTO finding_reviews(
                finding_id, reviewer_id, reviewer_username, decision, reason_code, comment, created_at, supersedes_review_id
            ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                finding_id,
                user.id,
                user.username,
                human,
                payload.reason_code or "other",
                payload.note,
                utc_now(),
                int(prev_review["id"]) if prev_review else None,
            ),
        )
        conn.execute(
            "UPDATE rule_findings SET review_status=?, human_status=? WHERE id=?",
            (review_status, human, finding_id),
        )
    ip, ua = _client_meta(request)
    auth.audit(
        actor=user,
        action="finding.review",
        resource_type="finding",
        resource_id=str(finding_id),
        before={"human_status": before.get("human_status"), "review_status": before.get("review_status")},
        after={"human_status": human, "review_status": review_status, "note": payload.note},
        ip=ip,
        user_agent=ua,
    )
    # Recompute job counters after review
    job_id = int(before["job_id"])
    from app.finding_counts import compute_counts_from_rows
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM rule_findings WHERE job_id=? AND COALESCE(is_current,1)=1",
            (job_id,),
        ).fetchall()
        counts = compute_counts_from_rows([dict(r) for r in rows])
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(scan_jobs)")}
        if "formal_finding_count" in cols:
            conn.execute(
                """UPDATE scan_jobs SET
                    finding_count=?, finding_total_count=?, formal_finding_count=?,
                    suspected_count=?, hint_count=?, insufficient_data_count=?,
                    collection_error_count=?, auto_excluded_count=?,
                    human_dismissed_count=?, human_confirmed_count=?
                WHERE id=?""",
                (
                    counts["finding_count"], counts["finding_total_count"], counts["formal_finding_count"],
                    counts["suspected_count"], counts["hint_count"], counts["insufficient_data_count"],
                    counts["collection_error_count"], counts["auto_excluded_count"],
                    counts["human_dismissed_count"], counts["human_confirmed_count"], job_id,
                ),
            )
    return {"ok": True, "human_status": human, "review_status": review_status, "counts": counts}


# ---------- M2: batch review, dual review, rule effect center ----------


@app.post("/api/findings/batch-review")
async def batch_review_endpoint(
    payload: BatchReviewRequest,
    request: Request,
    user: CurrentUser = Depends(require_write("review:write")),
    auth: AuthService = Depends(get_auth_service),
):
    """M2: apply one review decision to many findings at once."""
    from app.review_batch import batch_review

    if not payload.finding_ids:
        raise HTTPException(400, "请至少选择一个问题记录")
    result = batch_review(
        db,
        finding_ids=payload.finding_ids,
        decision=payload.decision,
        reason_code=payload.reason_code,
        note=payload.note,
        reviewer_id=user.id,
        reviewer_username=user.username,
    )
    ip, ua = _client_meta(request)
    auth.audit(
        actor=user,
        action="finding.batch_review",
        resource_type="findings",
        detail={
            "ids": payload.finding_ids[:50],
            "decision": payload.decision,
            "reason_code": payload.reason_code,
            "updated": result.updated,
            "skipped": result.skipped,
        },
        ip=ip,
        user_agent=ua,
    )
    return {
        "ok": True,
        "total": result.total,
        "updated": result.updated,
        "skipped": result.skipped,
        "errors": result.errors,
    }


@app.get("/api/jobs/{job_id}/finding-clusters")
async def finding_clusters(
    job_id: int,
    dimension: str = Query(default="rule_code"),
    _user: CurrentUser = Depends(require_login),
):
    """M2: cluster finding ids by rule_code/district/source_site/document_number_prefix."""
    from app.review_batch import cluster_findings

    if not db.get_job(job_id):
        raise HTTPException(404, "任务不存在")
    try:
        groups = cluster_findings(db, job_id=job_id, dimension=dimension)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "dimension": dimension,
        "groups": [
            {"key": key, "count": len(ids), "finding_ids": ids}
            for key, ids in sorted(groups.items(), key=lambda kv: -len(kv[1]))
        ],
    }


@app.post("/api/findings/dual-assign")
async def dual_assign_endpoint(
    payload: DualAssignRequest,
    request: Request,
    user: CurrentUser = Depends(require_write("review:write")),
    auth: AuthService = Depends(get_auth_service),
):
    """M2: assign two reviewers for dual-person review."""
    from app.review_dual import assign_dual_review

    if not payload.finding_ids:
        raise HTTPException(400, "请至少选择一个问题记录")
    try:
        inserted = assign_dual_review(
            db,
            finding_ids=payload.finding_ids,
            first_reviewer_id=payload.first_reviewer_id,
            first_reviewer_username=payload.first_reviewer_username,
            second_reviewer_id=payload.second_reviewer_id,
            second_reviewer_username=payload.second_reviewer_username,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    ip, ua = _client_meta(request)
    auth.audit(
        actor=user,
        action="finding.dual_assign",
        resource_type="findings",
        detail={
            "ids": payload.finding_ids[:50],
            "first": payload.first_reviewer_username,
            "second": payload.second_reviewer_username,
            "inserted": inserted,
        },
        ip=ip,
        user_agent=ua,
    )
    return {"ok": True, "assigned": inserted, "requested": len(payload.finding_ids)}


@app.post("/api/findings/{finding_id}/dual-review")
async def dual_review_endpoint(
    finding_id: int,
    payload: DualReviewRequest,
    request: Request,
    user: CurrentUser = Depends(require_write("review:write")),
    auth: AuthService = Depends(get_auth_service),
):
    """M2: submit one reviewer's decision in dual-person review."""
    from app.review_dual import submit_dual_review

    try:
        result = submit_dual_review(
            db,
            finding_id=finding_id,
            reviewer_id=user.id,
            reviewer_username=user.username,
            round_num=payload.round,
            decision=payload.decision,
            reason_code=payload.reason_code,
            note=payload.note,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    ip, ua = _client_meta(request)
    auth.audit(
        actor=user,
        action="finding.dual_review",
        resource_type="finding",
        resource_id=str(finding_id),
        detail=result,
        ip=ip,
        user_agent=ua,
    )
    return result


@app.post("/api/findings/{finding_id}/arbitrate")
async def arbitrate_endpoint(
    finding_id: int,
    payload: ArbitrateRequest,
    request: Request,
    user: CurrentUser = Depends(require_write("review:write")),
    auth: AuthService = Depends(get_auth_service),
):
    """M2: arbitrator resolves a dual-review disagreement."""
    from app.review_dual import arbitrate

    try:
        result = arbitrate(
            db,
            finding_id=finding_id,
            arbitrator_id=user.id,
            arbitrator_username=user.username,
            decision=payload.decision,
            reason_code=payload.reason_code,
            note=payload.note,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    ip, ua = _client_meta(request)
    auth.audit(
        actor=user,
        action="finding.arbitrate",
        resource_type="finding",
        resource_id=str(finding_id),
        detail=result,
        ip=ip,
        user_agent=ua,
    )
    return result


@app.get("/api/jobs/{job_id}/dual-assignments")
async def dual_assignments_endpoint(
    job_id: int, _user: CurrentUser = Depends(require_login)
):
    """M2: list dual-review assignments for a job."""
    from app.review_dual import list_dual_assignments

    if not db.get_job(job_id):
        raise HTTPException(404, "任务不存在")
    return list_dual_assignments(db, job_id=job_id)


@app.get("/api/consistency-rate")
async def consistency_rate_endpoint(
    job_id: int | None = Query(default=None),
    _user: CurrentUser = Depends(require_login),
):
    """M2: overall dual-review consistency rate."""
    from app.review_dual import compute_consistency_rate

    return compute_consistency_rate(db, job_id=job_id)


@app.get("/api/rule-effect")
async def rule_effect_endpoint(
    job_id: int | None = Query(default=None),
    _user: CurrentUser = Depends(require_login),
):
    """M2: rule effect center — per-rule precision, false-positive rate, top dismissal reasons."""
    from app.rule_effect import compute_rule_effectiveness

    return compute_rule_effectiveness(db, job_id=job_id)


@app.post("/api/rule-effect/snapshot")
async def rule_effect_snapshot_endpoint(
    request: Request,
    job_id: int | None = Query(default=None),
    snapshot_kind: str = Query(default="live"),
    user: CurrentUser = Depends(require_write("review:write")),
    auth: AuthService = Depends(get_auth_service),
):
    """M2: persist current rule effectiveness as a snapshot for trend tracking."""
    from app.rule_effect import snapshot_rule_effectiveness

    if snapshot_kind not in {"live", "sample", "evaluation"}:
        raise HTTPException(400, "snapshot_kind 必须是 live/sample/evaluation")
    result = snapshot_rule_effectiveness(db, job_id=job_id, snapshot_kind=snapshot_kind)
    ip, ua = _client_meta(request)
    auth.audit(
        actor=user,
        action="rule_effect.snapshot",
        resource_type="rule_effect",
        detail=result,
        ip=ip,
        user_agent=ua,
    )
    return result


@app.post("/api/rule-effect/degrade")
async def rule_degradation_endpoint(
    request: Request,
    job_id: int | None = Query(default=None),
    user: CurrentUser = Depends(require_write("review:write")),
    auth: AuthService = Depends(get_auth_service),
):
    """M2: evaluate and apply rule degradation based on precision thresholds."""
    from app.rule_degradation import evaluate_rule_degradation

    result = evaluate_rule_degradation(db, job_id=job_id)
    ip, ua = _client_meta(request)
    auth.audit(
        actor=user,
        action="rule_effect.degrade",
        resource_type="rule_versions",
        detail={"degraded": result["degraded"], "restored": result["restored"]},
        ip=ip,
        user_agent=ua,
    )
    return result

