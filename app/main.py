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

# M6: 请求上下文中间件（request_id + 请求日志）
try:
    from app.observability import RequestContextMiddleware
    app.add_middleware(RequestContextMiddleware)
except Exception:  # noqa: BLE001 — 中间件可选，不得阻断启动
    pass


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
        # R-P1-3 / R-RE-P2-1: 失败登录写入 operation_audits，actor_username=尝试登录名
        ip, ua = _client_meta(request)
        try:
            auth.audit(
                actor=None,
                action="auth.login_failed",
                resource_type="session",
                resource_id=payload.username,
                detail={"reason": "用户名或密码错误"},
                ip=ip,
                user_agent=ua,
                actor_username=payload.username,
            )
        except Exception:
            pass
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
    user: CurrentUser = Depends(require_write("read")),
    auth: AuthService = Depends(get_auth_service),
):
    """Logout endpoint (R-P1-3: 纳入三段 CSRF 校验)。

    之前为简化前端导航使用 require_login 无 CSRF，存在会话劫持风险。
    现统一纳入 require_write("read")，所有登录角色均具备 read 能力，
    且强制三段 CSRF（header+cookie+session 绑定）。
    """
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


# ===================== M3: PDF / OCR / Three-way compare =====================

class PdfProcessRequest(BaseModel):
    pdf_payload_b64: str
    page_title: str = ""
    page_document_number: str = ""
    page_issuing_agency: str = ""
    page_authored_date: str | None = None
    body_text: str = ""
    run_ocr: bool = True
    run_compare: bool = True


class AttachmentProcessRequest(BaseModel):
    url: str
    filename: str
    expected_kind: str = ""
    payload_b64: str | None = None
    fetch_error: str = ""


@app.get("/api/pdf/documents/{doc_id}")
async def pdf_document_status(doc_id: int, _user: CurrentUser = Depends(require_login)):
    """Return a document's PDF extraction summary."""
    with db.connect() as conn:
        row = conn.execute(
            """SELECT id, title, pdf_url, pdf_path, pdf_hash, pdf_status,
                pdf_page_count, ocr_status, ocr_confidence,
                pdf_extracted_at, length(pdf_text) AS pdf_text_len,
                length(ocr_text) AS ocr_text_len
            FROM policy_documents WHERE id=?""",
            (doc_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "文档不存在")
    return dict(row)


@app.get("/api/pdf/documents/{doc_id}/pages/{page_no}")
async def pdf_page_text(doc_id: int, page_no: int, _user: CurrentUser = Depends(require_login)):
    """Return per-page PDF text and bbox evidence."""
    if page_no < 1:
        raise HTTPException(400, "page_no 必须 >= 1")
    with db.connect() as conn:
        row = conn.execute(
            """SELECT document_id, page_number, text, bbox_json, source_type,
                confidence, char_count, extracted_at
            FROM pdf_extractions WHERE document_id=? AND page_number=?""",
            (doc_id, page_no),
        ).fetchone()
    if not row:
        raise HTTPException(404, "页面不存在")
    import json as _json
    d = dict(row)
    d["bbox_json"] = _json.loads(d["bbox_json"]) if d["bbox_json"] else []
    return d


@app.get("/api/pdf/documents/{doc_id}/compare")
async def pdf_compare_status(doc_id: int, _user: CurrentUser = Depends(require_login)):
    """Return three-way comparison result (page/body/PDF)."""
    with db.connect() as conn:
        doc = conn.execute(
            """SELECT id, title, page_document_number, issuing_agency,
                authored_date, body_text, pdf_text, pdf_metadata_json
            FROM policy_documents WHERE id=?""",
            (doc_id,),
        ).fetchone()
        if not doc:
            raise HTTPException(404, "文档不存在")
        findings = conn.execute(
            """SELECT id, rule_code, severity, detail, page_value, body_value,
                evidence, machine_status, human_status, created_at
            FROM rule_findings
            WHERE document_id=? AND rule_code LIKE 'PDF-%'
            ORDER BY id DESC""",
            (doc_id,),
        ).fetchall()
        evidence_rows = conn.execute(
            """SELECT fe.* FROM finding_evidence fe
            JOIN rule_findings rf ON rf.id=fe.finding_id
            WHERE rf.document_id=? AND rf.rule_code LIKE 'PDF-%'""",
            (doc_id,),
        ).fetchall()
    import json as _json
    metadata = _json.loads(doc["pdf_metadata_json"]) if doc["pdf_metadata_json"] else {}
    return {
        "document_id": doc_id,
        "page": {
            "title": doc["title"],
            "document_number": doc["page_document_number"],
            "issuing_agency": doc["issuing_agency"],
            "authored_date": doc["authored_date"],
        },
        "pdf_metadata": metadata,
        "pdf_text_excerpt": (doc["pdf_text"] or "")[:500],
        "body_text_excerpt": (doc["body_text"] or "")[:500],
        "findings": [dict(f) for f in findings],
        "evidence": [dict(e) for e in evidence_rows],
    }


@app.get("/api/pdf/documents/{doc_id}/evidence")
async def pdf_evidence(doc_id: int, _user: CurrentUser = Depends(require_login)):
    """Return all PDF finding evidence (page numbers + bbox) for a document."""
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT fe.id, fe.finding_id, fe.page_number, fe.bbox, fe.confidence,
                fe.source_type, fe.raw_value, fe.excerpt, rf.rule_code
            FROM finding_evidence fe
            JOIN rule_findings rf ON rf.id=fe.finding_id
            WHERE rf.document_id=? AND rf.rule_code LIKE 'PDF-%'""",
            (doc_id,),
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/pdf/ocr/status/{doc_id}")
async def ocr_status_endpoint(doc_id: int, _user: CurrentUser = Depends(require_login)):
    """Return OCR status for a document."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT ocr_status, ocr_confidence, length(ocr_text) AS ocr_text_len FROM policy_documents WHERE id=?",
            (doc_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "文档不存在")
    return dict(row)


@app.post("/api/pdf/ocr/retry/{doc_id}")
async def ocr_retry_endpoint(
    doc_id: int,
    request: Request,
    user: CurrentUser = Depends(require_write("jobs:write")),
    auth: AuthService = Depends(get_auth_service),
):
    """Retry OCR for a document (admin/operator only)."""
    import base64
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id, pdf_path FROM policy_documents WHERE id=?",
            (doc_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "文档不存在")
    pdf_path = row["pdf_path"]
    if not pdf_path:
        raise HTTPException(400, "文档无 PDF 缓存，无法重试 OCR")
    from pathlib import Path
    p = Path(pdf_path)
    if not p.exists():
        raise HTTPException(400, "PDF 缓存文件丢失")
    payload = p.read_bytes()
    from app.ocr import ocr_pdf
    result = ocr_pdf(payload)
    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
    with db.connect() as conn:
        conn.execute(
            "UPDATE policy_documents SET ocr_status=?, ocr_confidence=?, ocr_text=? WHERE id=?",
            (result.status, result.avg_confidence, result.full_text, doc_id),
        )
    ip, ua = _client_meta(request)
    auth.audit(
        actor=user, action="pdf.ocr_retry", resource_type="policy_documents",
        resource_id=str(doc_id),
        detail={"status": result.status, "avg_conf": result.avg_confidence},
        ip=ip, user_agent=ua,
    )
    return {"status": result.status, "avg_confidence": result.avg_confidence}


@app.get("/api/attachments/{doc_id}")
async def list_attachments(doc_id: int, _user: CurrentUser = Depends(require_login)):
    """List attachment records and ATT-001~003 findings for a document."""
    with db.connect() as conn:
        atts = conn.execute(
            "SELECT * FROM attachment_records WHERE document_id=? ORDER BY id",
            (doc_id,),
        ).fetchall()
        findings = conn.execute(
            """SELECT id, rule_code, severity, detail, page_value, body_value,
                evidence, machine_status, created_at
            FROM rule_findings
            WHERE document_id=? AND rule_code LIKE 'ATT-%'
            ORDER BY id DESC""",
            (doc_id,),
        ).fetchall()
    return {"attachments": [dict(a) for a in atts], "findings": [dict(f) for f in findings]}


@app.get("/api/findings/pdf")
async def list_pdf_findings(
    rule_code: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    _user: CurrentUser = Depends(require_login),
):
    """List PDF-001~007 findings, optionally filtered by rule_code."""
    if rule_code and not rule_code.startswith("PDF-"):
        raise HTTPException(400, "rule_code must start with PDF-")
    with db.connect() as conn:
        if rule_code:
            rows = conn.execute(
                """SELECT * FROM rule_findings
                WHERE rule_code=? ORDER BY id DESC LIMIT ?""",
                (rule_code, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM rule_findings
                WHERE rule_code LIKE 'PDF-%' ORDER BY id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/pdf/documents/{doc_id}/process")
async def pdf_process_endpoint(
    doc_id: int,
    payload: PdfProcessRequest,
    request: Request,
    user: CurrentUser = Depends(require_write("jobs:write")),
    auth: AuthService = Depends(get_auth_service),
):
    """Trigger M3 PDF processing for a document (admin/operator only).

    Accepts base64-encoded PDF bytes; orchestrates integrity check,
    text extraction, OCR fallback, and three-way comparison.
    """
    import base64
    from app.pdf_service import process_document_pdf
    try:
        pdf_bytes = base64.b64decode(payload.pdf_payload_b64)
    except Exception as exc:
        raise HTTPException(400, f"base64 解码失败: {exc}") from exc
    result = process_document_pdf(
        db,
        document_id=doc_id,
        pdf_payload=pdf_bytes,
        page_title=payload.page_title,
        page_document_number=payload.page_document_number,
        page_issuing_agency=payload.page_issuing_agency,
        page_authored_date=payload.page_authored_date,
        body_text=payload.body_text,
        run_ocr=payload.run_ocr,
        run_compare=payload.run_compare,
    )
    ip, ua = _client_meta(request)
    auth.audit(
        actor=user, action="pdf.process", resource_type="policy_documents",
        resource_id=str(doc_id),
        detail={"pdf_status": result.pdf_status, "ocr_status": result.ocr_status,
                "findings": result.findings_generated, "rules": result.rule_codes},
        ip=ip, user_agent=ua,
    )
    return {
        "document_id": result.document_id,
        "pdf_status": result.pdf_status,
        "ocr_status": result.ocr_status,
        "page_count": result.page_count,
        "findings_generated": result.findings_generated,
        "rule_codes": result.rule_codes,
        "error": result.error,
    }


@app.post("/api/attachments/{doc_id}/process")
async def attachment_process_endpoint(
    doc_id: int,
    payload: AttachmentProcessRequest,
    request: Request,
    user: CurrentUser = Depends(require_write("jobs:write")),
    auth: AuthService = Depends(get_auth_service),
):
    """Classify a single attachment for a document."""
    import base64
    from app.pdf_service import process_attachment
    pdf_bytes = None
    if payload.payload_b64:
        try:
            pdf_bytes = base64.b64decode(payload.payload_b64)
        except Exception as exc:
            raise HTTPException(400, f"base64 解码失败: {exc}") from exc
    result = process_attachment(
        db,
        document_id=doc_id,
        url=payload.url,
        filename=payload.filename,
        expected_kind=payload.expected_kind,
        payload=pdf_bytes,
        fetch_error=payload.fetch_error,
    )
    ip, ua = _client_meta(request)
    auth.audit(
        actor=user, action="attachment.process", resource_type="attachment_records",
        resource_id=str(doc_id),
        detail={"url": payload.url, "status": result.status, "kind": result.detected_kind},
        ip=ip, user_agent=ua,
    )
    return {
        "status": result.status,
        "detected_kind": result.detected_kind,
        "notes": result.notes,
    }


# ===================== M4: Archive / Duplicates / Reconciliation =====================

class ArchiveObservationRequest(BaseModel):
    job_id: int | None = None
    target_key: str = ""
    source_level: str = "district"
    source_site: str = ""
    channel_id: str = ""
    url: str = ""
    present: bool = True
    http_status: int | None = None
    page_status: str = "ok"
    content_hash: str = ""
    notes: str = ""


class FingerprintRequest(BaseModel):
    page_document_number: str = ""
    title: str = ""
    authored_date: str | None = None
    issuing_agency: str = ""
    body_text: str = ""
    pdf_hash: str = ""
    attachment_hashes: list[str] = Field(default_factory=list)


class ExcludeArchiveRequest(BaseModel):
    rule_code: str
    reason: str = Field(max_length=500)
    resolved_by: str = ""


class ExcludeDuplicateRequest(BaseModel):
    document_id_a: int
    document_id_b: int
    duplicate_type: str
    reason: str = Field(max_length=500)
    resolved_by: str = ""


class ExcludeReconRequest(BaseModel):
    municipal_document_id: int
    district_document_id: int | None = None
    reason: str = Field(max_length=500)
    resolved_by: str = ""


@app.post("/api/archive/observations/{doc_id}")
async def record_archive_observation(
    doc_id: int,
    payload: ArchiveObservationRequest,
    request: Request,
    user: CurrentUser = Depends(require_write("jobs:write")),
    auth: AuthService = Depends(get_auth_service),
):
    """Record one round of archive observation for a document (M4)."""
    from app.archive import ArchiveObservationInput, record_observation
    obs = ArchiveObservationInput(
        document_id=doc_id,
        job_id=payload.job_id,
        target_key=payload.target_key,
        source_level=payload.source_level,
        source_site=payload.source_site,
        channel_id=payload.channel_id,
        url=payload.url,
        present=payload.present,
        http_status=payload.http_status,
        page_status=payload.page_status,
        content_hash=payload.content_hash,
        notes=payload.notes,
    )
    observation_id = record_observation(db, obs)
    ip, ua = _client_meta(request)
    auth.audit(
        actor=user, action="archive.observation.record",
        resource_type="archive_observations", resource_id=str(observation_id),
        detail={"document_id": doc_id, "source_level": payload.source_level,
                "present": payload.present},
        ip=ip, user_agent=ua,
    )
    return {"observation_id": observation_id, "document_id": doc_id}


@app.get("/api/archive/observations/{doc_id}")
async def list_archive_observations(
    doc_id: int,
    source_level: str | None = Query(default=None),
    _user: CurrentUser = Depends(require_login),
):
    """List archive observations for a document."""
    from app.archive import list_observations
    return list_observations(db, doc_id, source_level=source_level)


@app.post("/api/archive/evaluate/{doc_id}")
async def evaluate_archive(
    doc_id: int,
    request: Request,
    user: CurrentUser = Depends(require_write("jobs:write")),
    auth: AuthService = Depends(get_auth_service),
    migration_search_urls: list[str] | None = None,
):
    """Evaluate ARCH-001~006 anomalies for a document (M4)."""
    from app.archive import evaluate_archive_anomalies
    findings = evaluate_archive_anomalies(
        db, document_id=doc_id, migration_search_urls=migration_search_urls,
    )
    ip, ua = _client_meta(request)
    auth.audit(
        actor=user, action="archive.evaluate", resource_type="policy_documents",
        resource_id=str(doc_id),
        detail={"findings_count": len(findings),
                "rule_codes": [f.rule_code for f in findings]},
        ip=ip, user_agent=ua,
    )
    return {
        "document_id": doc_id,
        "findings": [
            {
                "rule_code": f.rule_code,
                "severity": f.severity,
                "machine_status": f.machine_status,
                "allow_customer_output": f.allow_customer_output,
                "evidence_count": len(f.evidence),
                "notes": f.notes,
            }
            for f in findings
        ],
    }


@app.get("/api/archive/findings")
async def list_archive_findings(
    status: str | None = Query(default=None),
    _user: CurrentUser = Depends(require_login),
):
    """List ARCH-* findings, optionally filtered by status."""
    from app.archive import list_findings
    return list_findings(db, status=status)


@app.post("/api/archive/findings/{doc_id}/exclude")
async def exclude_archive_finding(
    doc_id: int,
    payload: ExcludeArchiveRequest,
    request: Request,
    user: CurrentUser = Depends(require_write("jobs:write")),
    auth: AuthService = Depends(get_auth_service),
):
    """Exclude an archive finding (normal废止/失效/迁移可排除)."""
    from app.archive import exclude_finding
    exclude_finding(
        db, document_id=doc_id, rule_code=payload.rule_code,
        reason=payload.reason, resolved_by=payload.resolved_by or user.username,
    )
    ip, ua = _client_meta(request)
    auth.audit(
        actor=user, action="archive.finding.exclude",
        resource_type="archive_findings",
        resource_id=f"{doc_id}:{payload.rule_code}",
        detail={"reason": payload.reason}, ip=ip, user_agent=ua,
    )
    return {"ok": True, "document_id": doc_id, "rule_code": payload.rule_code}


@app.post("/api/fingerprints/{doc_id}")
async def compute_document_fingerprint(
    doc_id: int,
    payload: FingerprintRequest,
    request: Request,
    user: CurrentUser = Depends(require_write("jobs:write")),
    auth: AuthService = Depends(get_auth_service),
):
    """Compute and persist a document fingerprint (M4)."""
    from app.duplicates import FingerprintInputs, compute_fingerprint, persist_fingerprint
    inputs = FingerprintInputs(
        document_id=doc_id,
        page_document_number=payload.page_document_number,
        title=payload.title,
        authored_date=payload.authored_date,
        issuing_agency=payload.issuing_agency,
        body_text=payload.body_text,
        pdf_hash=payload.pdf_hash,
        attachment_hashes=payload.attachment_hashes,
    )
    fp = compute_fingerprint(inputs)
    persist_fingerprint(db, fp)
    ip, ua = _client_meta(request)
    auth.audit(
        actor=user, action="fingerprint.compute",
        resource_type="document_fingerprints", resource_id=str(doc_id),
        detail={"fingerprint_hash": fp.fingerprint_hash[:16]},
        ip=ip, user_agent=ua,
    )
    return {
        "document_id": doc_id,
        "normalized_doc_number": fp.normalized_doc_number,
        "normalized_title": fp.normalized_title,
        "body_hash": fp.body_hash,
        "pdf_hash": fp.pdf_hash,
        "fingerprint_hash": fp.fingerprint_hash,
    }


@app.get("/api/fingerprints/{doc_id}")
async def get_document_fingerprint(doc_id: int, _user: CurrentUser = Depends(require_login)):
    """Retrieve a document's stored fingerprint."""
    from app.duplicates import get_fingerprint
    fp = get_fingerprint(db, doc_id)
    if not fp:
        raise HTTPException(404, "未找到指纹")
    return fp.__dict__


@app.post("/api/duplicates/detect/{doc_id}")
async def detect_duplicates_endpoint(
    doc_id: int,
    request: Request,
    user: CurrentUser = Depends(require_write("jobs:write")),
    auth: AuthService = Depends(get_auth_service),
    candidate_ids: list[int] | None = None,
):
    """Detect duplicates for a document (M4)."""
    from app.duplicates import detect_duplicates
    matches = detect_duplicates(db, document_id=doc_id, candidate_ids=candidate_ids)
    ip, ua = _client_meta(request)
    auth.audit(
        actor=user, action="duplicates.detect",
        resource_type="policy_documents", resource_id=str(doc_id),
        detail={"matches_count": len(matches),
                "types": [m.duplicate_type for m in matches]},
        ip=ip, user_agent=ua,
    )
    return {
        "document_id": doc_id,
        "matches": [
            {
                "document_id_a": m.document_id_a,
                "document_id_b": m.document_id_b,
                "duplicate_type": m.duplicate_type,
                "similarity_score": m.similarity_score,
                "is_normal": m.is_normal,
                "machine_status": m.machine_status,
                "notes": m.notes,
            }
            for m in matches
        ],
    }


@app.get("/api/duplicates")
async def list_duplicates_endpoint(
    document_id: int | None = Query(default=None),
    duplicate_type: str | None = Query(default=None),
    is_normal: bool | None = Query(default=None),
    status: str | None = Query(default=None),
    _user: CurrentUser = Depends(require_login),
):
    """List duplicate pairs with optional filters."""
    from app.duplicates import list_duplicates
    return list_duplicates(
        db, document_id=document_id, duplicate_type=duplicate_type,
        is_normal=is_normal, status=status,
    )


@app.post("/api/duplicates/exclude")
async def exclude_duplicate_endpoint(
    payload: ExcludeDuplicateRequest,
    request: Request,
    user: CurrentUser = Depends(require_write("jobs:write")),
    auth: AuthService = Depends(get_auth_service),
):
    """Exclude a duplicate pair (normal cross-channel sync etc.)."""
    from app.duplicates import exclude_duplicate
    exclude_duplicate(
        db, document_id_a=payload.document_id_a,
        document_id_b=payload.document_id_b,
        duplicate_type=payload.duplicate_type,
        reason=payload.reason,
        resolved_by=payload.resolved_by or user.username,
    )
    ip, ua = _client_meta(request)
    auth.audit(
        actor=user, action="duplicates.exclude",
        resource_type="duplicate_pairs",
        resource_id=f"{payload.document_id_a}:{payload.document_id_b}:{payload.duplicate_type}",
        detail={"reason": payload.reason}, ip=ip, user_agent=ua,
    )
    return {"ok": True}


@app.post("/api/reconciliation/reconcile/{municipal_id}")
async def reconcile_document_endpoint(
    municipal_id: int,
    request: Request,
    user: CurrentUser = Depends(require_write("jobs:write")),
    auth: AuthService = Depends(get_auth_service),
):
    """Reconcile one municipal document against district candidates (M4)."""
    from app.reconciliation import reconcile_document
    matches = reconcile_document(db, municipal_id=municipal_id)
    ip, ua = _client_meta(request)
    auth.audit(
        actor=user, action="reconciliation.reconcile",
        resource_type="policy_documents", resource_id=str(municipal_id),
        detail={"matches_count": len(matches),
                "statuses": [m.match_status for m in matches]},
        ip=ip, user_agent=ua,
    )
    return {
        "municipal_document_id": municipal_id,
        "matches": [
            {
                "district_document_id": m.district_document_id,
                "match_status": m.match_status,
                "similarity_score": m.similarity_score,
                "title_match": m.title_match,
                "doc_number_match": m.doc_number_match,
                "date_match": m.date_match,
                "body_match": m.body_match,
                "attachment_match": m.attachment_match,
                "machine_status": m.machine_status,
                "notes": m.notes,
            }
            for m in matches
        ],
    }


@app.post("/api/reconciliation/reconcile-all")
async def reconcile_all_endpoint(
    request: Request,
    user: CurrentUser = Depends(require_write("jobs:write")),
    auth: AuthService = Depends(get_auth_service),
    limit: int | None = Query(default=None, ge=1, le=10000),
):
    """Run reconciliation across all unmatched municipal documents (M4)."""
    from app.reconciliation import reconcile_all
    summary = reconcile_all(db, limit=limit)
    ip, ua = _client_meta(request)
    auth.audit(
        actor=user, action="reconciliation.reconcile_all",
        resource_type="reconciliation_matches",
        detail=summary, ip=ip, user_agent=ua,
    )
    return summary


@app.get("/api/reconciliation/matches")
async def list_recon_matches_endpoint(
    match_status: str | None = Query(default=None),
    status: str | None = Query(default=None),
    document_id: int | None = Query(default=None),
    _user: CurrentUser = Depends(require_login),
):
    """List reconciliation matches with optional filters."""
    from app.reconciliation import list_matches
    return list_matches(
        db, match_status=match_status, status=status, document_id=document_id,
    )


@app.post("/api/reconciliation/exclude")
async def exclude_recon_endpoint(
    payload: ExcludeReconRequest,
    request: Request,
    user: CurrentUser = Depends(require_write("jobs:write")),
    auth: AuthService = Depends(get_auth_service),
):
    """Exclude a reconciliation match."""
    from app.reconciliation import exclude_match
    exclude_match(
        db, municipal_document_id=payload.municipal_document_id,
        district_document_id=payload.district_document_id,
        reason=payload.reason,
        resolved_by=payload.resolved_by or user.username,
    )
    ip, ua = _client_meta(request)
    auth.audit(
        actor=user, action="reconciliation.exclude",
        resource_type="reconciliation_matches",
        resource_id=f"{payload.municipal_document_id}:{payload.district_document_id}",
        detail={"reason": payload.reason}, ip=ip, user_agent=ua,
    )
    return {"ok": True}


# ===================== M5: Reading Linkage / Media / Interpretation / Rectification =====================

class ReadingCheckRequest(BaseModel):
    job_id: int | None = None
    button_label: str = ""
    button_selector: str = ""
    button_present: bool = True
    clickable: bool | None = None
    target_url: str = ""
    http_status: int | None = None
    page_status: str = "unknown"
    page_title: str = ""
    page_body_length: int = 0
    has_no_data_placeholder: bool = False


@app.post("/api/reading-checks/{doc_id}")
async def record_reading_check_endpoint(
    doc_id: int,
    payload: ReadingCheckRequest,
    request: Request,
    user: CurrentUser = Depends(require_write("jobs:write")),
    auth: AuthService = Depends(get_auth_service),
):
    """Record a reading/handling linkage check for a document (M5)."""
    from app.reading_linkage import ReadingLinkageInput, record_reading_check
    result = record_reading_check(db, ReadingLinkageInput(
        document_id=doc_id, job_id=payload.job_id,
        button_label=payload.button_label, button_selector=payload.button_selector,
        button_present=payload.button_present, clickable=payload.clickable,
        target_url=payload.target_url, http_status=payload.http_status,
        page_status=payload.page_status, page_title=payload.page_title,
        page_body_length=payload.page_body_length,
        has_no_data_placeholder=payload.has_no_data_placeholder,
    ))
    ip, ua = _client_meta(request)
    auth.audit(
        actor=user, action="reading.check.record",
        resource_type="reading_linkage_checks", resource_id=str(result.check_id),
        detail={"document_id": doc_id, "rule_code": result.rule_code},
        ip=ip, user_agent=ua,
    )
    return {
        "check_id": result.check_id, "document_id": doc_id,
        "rule_code": result.rule_code, "machine_status": result.machine_status,
        "allow_customer_output": result.allow_customer_output,
    }


@app.get("/api/reading-checks")
async def list_reading_checks_endpoint(
    document_id: int | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    _user: CurrentUser = Depends(require_login),
):
    """List reading linkage checks."""
    from app.reading_linkage import list_reading_checks
    return list_reading_checks(db, document_id=document_id, limit=limit)


class MediaCheckRequest(BaseModel):
    job_id: int | None = None
    media_type: str
    media_url: str
    source_area: str = ""
    http_status: int | None = None
    status: str = "pending"
    content_type: str = ""
    content_hash: str = ""
    size_bytes: int = 0
    detected_kind: str = ""
    duration_seconds: float | None = None


@app.post("/api/media-checks/{doc_id}")
async def record_media_check_endpoint(
    doc_id: int,
    payload: MediaCheckRequest,
    request: Request,
    user: CurrentUser = Depends(require_write("jobs:write")),
    auth: AuthService = Depends(get_auth_service),
):
    """Record an image/video/attachment availability check (M5)."""
    from app.media import MediaCheckInput, record_media_check
    result = record_media_check(db, MediaCheckInput(
        document_id=doc_id, job_id=payload.job_id,
        media_type=payload.media_type, media_url=payload.media_url,
        source_area=payload.source_area, http_status=payload.http_status,
        status=payload.status, content_type=payload.content_type,
        content_hash=payload.content_hash, size_bytes=payload.size_bytes,
        detected_kind=payload.detected_kind, duration_seconds=payload.duration_seconds,
    ))
    ip, ua = _client_meta(request)
    auth.audit(
        actor=user, action="media.check.record",
        resource_type="media_checks", resource_id=str(result.check_id),
        detail={"document_id": doc_id, "rule_code": result.rule_code},
        ip=ip, user_agent=ua,
    )
    return {
        "check_id": result.check_id, "document_id": doc_id,
        "media_type": payload.media_type,
        "rule_code": result.rule_code, "machine_status": result.machine_status,
        "allow_customer_output": result.allow_customer_output,
    }


@app.get("/api/media-checks")
async def list_media_checks_endpoint(
    document_id: int | None = Query(default=None),
    media_type: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    _user: CurrentUser = Depends(require_login),
):
    """List media checks."""
    from app.media import list_media_checks
    return list_media_checks(
        db, document_id=document_id, media_type=media_type, limit=limit,
    )


class InterpretationRequest(BaseModel):
    interpretation_type: str
    interpretation_url: str
    title: str = ""
    published_at: str | None = None
    target_document_id: int | None = None
    target_doc_number: str = ""
    target_title: str = ""


@app.post("/api/interpretations/{doc_id}")
async def create_interpretation_endpoint(
    doc_id: int,
    payload: InterpretationRequest,
    request: Request,
    user: CurrentUser = Depends(require_write("jobs:write")),
    auth: AuthService = Depends(get_auth_service),
):
    """Create a policy interpretation record for a document (M5)."""
    from app.interpretation import InterpretationInput, create_interpretation
    interp_id = create_interpretation(db, InterpretationInput(
        document_id=doc_id, interpretation_type=payload.interpretation_type,
        interpretation_url=payload.interpretation_url, title=payload.title,
        published_at=payload.published_at,
        target_document_id=payload.target_document_id,
        target_doc_number=payload.target_doc_number,
        target_title=payload.target_title,
    ))
    ip, ua = _client_meta(request)
    auth.audit(
        actor=user, action="interpretation.create",
        resource_type="policy_interpretations", resource_id=str(interp_id),
        detail={"document_id": doc_id, "type": payload.interpretation_type},
        ip=ip, user_agent=ua,
    )
    return {"interpretation_id": interp_id, "document_id": doc_id}


class InterpretationCheckRequest(BaseModel):
    job_id: int | None = None
    link_status: str = "unknown"
    http_status: int | None = None
    has_image: bool | None = None
    image_status: str = "unknown"
    has_video: bool | None = None
    video_status: str = "unknown"
    page_body_length: int = 0
    is_empty_page: bool = False
    published_at_present: bool | None = None


@app.post("/api/interpretations/{interp_id}/check")
async def record_interpretation_check_endpoint(
    interp_id: int,
    payload: InterpretationCheckRequest,
    request: Request,
    user: CurrentUser = Depends(require_write("jobs:write")),
    auth: AuthService = Depends(get_auth_service),
):
    """Record a check for a policy interpretation (M5)."""
    from app.interpretation import InterpretationCheckInput, record_interpretation_check
    result = record_interpretation_check(db, InterpretationCheckInput(
        interpretation_id=interp_id, job_id=payload.job_id,
        link_status=payload.link_status, http_status=payload.http_status,
        has_image=payload.has_image, image_status=payload.image_status,
        has_video=payload.has_video, video_status=payload.video_status,
        page_body_length=payload.page_body_length,
        is_empty_page=payload.is_empty_page,
        published_at_present=payload.published_at_present,
    ))
    ip, ua = _client_meta(request)
    auth.audit(
        actor=user, action="interpretation.check.record",
        resource_type="interpretation_checks", resource_id=str(result.check_id),
        detail={"interpretation_id": interp_id, "rule_code": result.rule_code},
        ip=ip, user_agent=ua,
    )
    return {
        "check_id": result.check_id, "interpretation_id": interp_id,
        "rule_code": result.rule_code, "machine_status": result.machine_status,
        "allow_customer_output": result.allow_customer_output,
    }


@app.get("/api/interpretations")
async def list_interpretations_endpoint(
    document_id: int | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    _user: CurrentUser = Depends(require_login),
):
    """List policy interpretations."""
    from app.interpretation import list_interpretations
    return list_interpretations(db, document_id=document_id, limit=limit)


class RectificationTaskRequest(BaseModel):
    finding_id: int | None = None
    document_id: int
    rule_code: str = ""
    title: str = ""
    description: str = ""
    responsible_unit: str = ""
    responsible_person: str = ""
    deadline: str | None = None
    priority: str = "normal"
    parent_task_id: int | None = None
    before_evidence: list[dict] = Field(default_factory=list)


@app.post("/api/rectification/tasks")
async def create_rectification_task_endpoint(
    payload: RectificationTaskRequest,
    request: Request,
    user: CurrentUser = Depends(require_write("jobs:write")),
    auth: AuthService = Depends(get_auth_service),
):
    """Create a rectification task (M5)."""
    from app.rectification import RectificationTaskInput, create_task
    result = create_task(db, RectificationTaskInput(
        finding_id=payload.finding_id, document_id=payload.document_id,
        rule_code=payload.rule_code, title=payload.title, description=payload.description,
        responsible_unit=payload.responsible_unit, responsible_person=payload.responsible_person,
        deadline=payload.deadline, priority=payload.priority,
        parent_task_id=payload.parent_task_id,
        before_evidence_json=payload.before_evidence,
    ), actor=user.username)
    ip, ua = _client_meta(request)
    auth.audit(
        actor=user, action="rectification.task.create",
        resource_type="rectification_tasks", resource_id=str(result.task_id),
        detail={"document_id": payload.document_id, "rule_code": payload.rule_code,
                "rule_codes_emitted": result.rule_codes_emitted},
        ip=ip, user_agent=ua,
    )
    return {
        "task_id": result.task_id, "document_id": result.document_id,
        "rule_codes_emitted": result.rule_codes_emitted,
    }


class RectificationTransitionRequest(BaseModel):
    to_status: str
    actor: str = ""
    reason: str = ""
    feedback_notes: str = ""
    after_evidence: list[dict] = Field(default_factory=list)
    deadline: str | None = None


@app.post("/api/rectification/tasks/{task_id}/transition")
async def transition_rectification_task_endpoint(
    task_id: int,
    payload: RectificationTransitionRequest,
    request: Request,
    user: CurrentUser = Depends(require_write("jobs:write")),
    auth: AuthService = Depends(get_auth_service),
):
    """Transition a rectification task status (M5)."""
    from app.rectification import transition_status
    result = transition_status(
        db, task_id, payload.to_status,
        actor=payload.actor or user.username, reason=payload.reason,
        feedback_notes=payload.feedback_notes,
        after_evidence=payload.after_evidence or None,
        deadline=payload.deadline,
    )
    ip, ua = _client_meta(request)
    auth.audit(
        actor=user, action="rectification.task.transition",
        resource_type="rectification_tasks", resource_id=str(task_id),
        detail=result, ip=ip, user_agent=ua,
    )
    return result


@app.get("/api/rectification/tasks")
async def list_rectification_tasks_endpoint(
    document_id: int | None = Query(default=None),
    status: str | None = Query(default=None),
    overdue_only: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=500),
    _user: CurrentUser = Depends(require_login),
):
    """List rectification tasks with optional filters."""
    from app.rectification import list_tasks
    return list_tasks(
        db, document_id=document_id, status=status,
        overdue_only=overdue_only, limit=limit,
    )


@app.get("/api/rectification/tasks/{task_id}")
async def get_rectification_task_endpoint(
    task_id: int, _user: CurrentUser = Depends(require_login),
):
    """Get a single rectification task with status history."""
    from app.rectification import get_task, get_status_history
    task = get_task(db, task_id)
    if not task:
        raise HTTPException(404, "整改任务不存在")
    task["status_history"] = get_status_history(db, task_id)
    return task


@app.get("/api/rectification/overdue")
async def list_overdue_tasks_endpoint(_user: CurrentUser = Depends(require_login)):
    """List overdue rectification tasks (RECT-002)."""
    from app.rectification import detect_overdue_tasks
    return detect_overdue_tasks(db)


# ===================== M6: 监控 / OCR 队列 / 系统设置 / 备份 / 全局聚合 =====================


@app.get("/metrics")
async def metrics_endpoint(request: Request, user: CurrentUser = Depends(require_login)):
    """Prometheus 风格 metrics 端点。

    R-P1-3: 强制登录（auth_enabled=true 时 require_login 拒绝匿名访问）。
    受 system_settings.metrics_enabled 控制；关闭时返回 404。
    """
    with db.connect() as conn:
        metrics_enabled = conn.execute(
            "SELECT value FROM system_settings WHERE key='metrics_enabled'"
        ).fetchone()
    if metrics_enabled and str(metrics_enabled["value"]).lower() not in {"true", "1", "yes"}:
        raise HTTPException(404, "metrics disabled")
    from app.observability import get_metrics, format_metrics_prometheus
    data = get_metrics(db)
    # JSON 响应优先（便于 API 客户端）；Accept: text/plain 时返回 Prometheus 文本
    accept = request.headers.get("accept", "")
    if "text/plain" in accept or "prometheus" in accept:
        return PlainTextResponse(format_metrics_prometheus(data))
    return data


class PlainTextResponse(JSONResponse):
    media_type = "text/plain; charset=utf-8"

    def __init__(self, content: str):
        super().__init__(content=content, media_type=self.media_type)


@app.get("/health/deep")
async def health_deep_endpoint():
    """深度健康检查（含数据库完整性、磁盘、计数）。公开端点，用于 readiness/liveness 探针。"""
    from app.observability import deep_health_check
    result = deep_health_check(db)
    status_code = 200 if result["status"] != "unhealthy" else 503
    return JSONResponse(content=result, status_code=status_code)


@app.get("/api/ocr-queue")
async def ocr_queue_list_endpoint(
    status: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    _user: CurrentUser = Depends(require_login),
):
    """列出 OCR 队列项（M6-4）。"""
    from app.ocr_queue import list_queue
    return list_queue(db, status=status, limit=limit)


@app.get("/api/ocr-queue/depth")
async def ocr_queue_depth_endpoint(_user: CurrentUser = Depends(require_login)):
    """OCR 队列深度统计（M6-4）。"""
    from app.ocr_queue import queue_depth
    return queue_depth(db)


class OcrEnqueueRequest(BaseModel):
    job_id: int | None = None
    priority: int | None = Field(default=None, ge=1, le=1000)
    max_attempts: int | None = Field(default=None, ge=1, le=10)
    requested_by: str = "api"


@app.post("/api/ocr-queue/enqueue/{doc_id}")
async def ocr_queue_enqueue_endpoint(
    doc_id: int,
    payload: OcrEnqueueRequest,
    request: Request,
    user: CurrentUser = Depends(require_write("jobs:write")),
    auth: AuthService = Depends(get_auth_service),
):
    """将文档入队 OCR 处理（M6-4）。"""
    from app.ocr_queue import enqueue
    item_id = enqueue(
        db, doc_id, job_id=payload.job_id, priority=payload.priority,
        max_attempts=payload.max_attempts, requested_by=user.username,
    )
    ip, ua = _client_meta(request)
    auth.audit(
        actor=user, action="ocr.queue.enqueue",
        resource_type="ocr_queue", resource_id=str(item_id),
        detail={"document_id": doc_id, "priority": payload.priority},
        ip=ip, user_agent=ua,
    )
    return {"item_id": item_id, "document_id": doc_id}


class OcrRunBatchRequest(BaseModel):
    max_items: int = Field(default=10, ge=1, le=100)
    worker_id: str = "api-batch"


@app.post("/api/ocr-queue/run-batch")
async def ocr_queue_run_batch_endpoint(
    payload: OcrRunBatchRequest,
    request: Request,
    user: CurrentUser = Depends(require_write("jobs:write")),
    auth: AuthService = Depends(get_auth_service),
):
    """批量处理 OCR 队列（M6-4）。"""
    from app.ocr_queue import run_batch
    result = run_batch(db, max_items=payload.max_items, worker_id=payload.worker_id)
    ip, ua = _client_meta(request)
    auth.audit(
        actor=user, action="ocr.queue.run_batch",
        resource_type="ocr_queue", resource_id="",
        detail=result, ip=ip, user_agent=ua,
    )
    return result


@app.get("/api/system-settings")
async def system_settings_list_endpoint(
    category: str | None = None,
    _user: CurrentUser = Depends(require_login),
):
    """列出系统设置（M6）。"""
    with db.connect() as conn:
        if category:
            rows = conn.execute(
                "SELECT * FROM system_settings WHERE category=? ORDER BY key",
                (category,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM system_settings ORDER BY category, key"
            ).fetchall()
    return [dict(r) for r in rows]


class UpdateSettingRequest(BaseModel):
    value: str
    description: str | None = None


@app.put("/api/system-settings/{key}")
async def update_system_setting_endpoint(
    key: str,
    payload: UpdateSettingRequest,
    request: Request,
    # R-P0-1: 改为 require_write("users:admin") 强制三段 CSRF；
    # users:admin 仅 admin 角色具备（见 ROLE_CAPABILITIES），与原 require_roles("admin") 等价但增加 CSRF 保护。
    user: CurrentUser = Depends(require_write("users:admin")),
    auth: AuthService = Depends(get_auth_service),
):
    """更新系统设置（仅 admin，M6）。"""
    from app.db import utc_now
    with db.connect() as conn:
        existing = conn.execute(
            "SELECT * FROM system_settings WHERE key=?", (key,)
        ).fetchone()
        if not existing:
            raise HTTPException(404, f"系统设置 {key} 不存在")
        before = dict(existing)
        conn.execute(
            """UPDATE system_settings
               SET value=?, description=?, updated_at=?, updated_by=?
               WHERE key=?""",
            (payload.value, payload.description or existing["description"],
             utc_now(), user.username, key),
        )
    ip, ua = _client_meta(request)
    auth.audit(
        actor=user, action="system.setting.update",
        resource_type="system_settings", resource_id=key,
        before={"value": before["value"]}, after={"value": payload.value},
        ip=ip, user_agent=ua,
    )
    return {"key": key, "value": payload.value}


@app.get("/api/backups")
async def backups_list_endpoint(
    _user: CurrentUser = Depends(require_roles("admin", "auditor")),
):
    """列出数据库备份（admin/auditor，M6-7）。"""
    from app.backup import list_backups
    from app.config import BASE_DIR
    backup_dir = BASE_DIR / "data" / "backups"
    return list_backups(backup_dir)


class CreateBackupRequest(BaseModel):
    label: str = "api"
    prune: bool = False
    retention_days: int = Field(default=30, ge=1, le=365)


@app.post("/api/backups")
async def backups_create_endpoint(
    payload: CreateBackupRequest,
    request: Request,
    # R-P0-1: 改为 require_write("users:admin") 强制三段 CSRF；与原 require_roles("admin") 等价但增加 CSRF 保护。
    user: CurrentUser = Depends(require_write("users:admin")),
    auth: AuthService = Depends(get_auth_service),
):
    """创建数据库在线备份（仅 admin，M6-7）。"""
    from app.backup import backup_database, prune_old_backups
    from app.config import BASE_DIR
    backup_dir = BASE_DIR / "data" / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    path = backup_database(db, backup_dir, label=payload.label)
    pruned = []
    if payload.prune:
        pruned = prune_old_backups(backup_dir, retention_days=payload.retention_days)
    ip, ua = _client_meta(request)
    auth.audit(
        actor=user, action="backup.create",
        resource_type="backup", resource_id=str(path.name),
        detail={"label": payload.label, "pruned": len(pruned)},
        ip=ip, user_agent=ua,
    )
    return {"backup": str(path), "pruned": pruned}


@app.get("/api/overview")
async def overview_endpoint(_user: CurrentUser = Depends(require_login)):
    """全局总览聚合统计（跨任务，M6-1 首页总览数据源）。

    汇总最近 30 个任务 + 全局 findings 分桶 + 队列深度 + 整改概览，
    供「首页总览」导航项渲染仪表盘。
    """
    with db.connect() as conn:
        jobs = conn.execute(
            "SELECT * FROM scan_jobs ORDER BY id DESC LIMIT 30"
        ).fetchall()
        finding_buckets = {"confirmed_problem": 0, "suspected": 0, "insufficient_data": 0,
                            "collection_error": 0, "auto_excluded": 0, "human_excluded": 0,
                            "customer_formal": 0, "total": 0}
        for bucket_row in conn.execute(
            """SELECT
                SUM(CASE WHEN machine_status='confirmed_by_rule' AND human_status='human_confirmed' THEN 1 ELSE 0 END) AS confirmed_problem,
                SUM(CASE WHEN machine_status='suspected' AND human_status='unreviewed' THEN 1 ELSE 0 END) AS suspected,
                SUM(CASE WHEN machine_status='insufficient_data' THEN 1 ELSE 0 END) AS insufficient_data,
                SUM(CASE WHEN machine_status='auto_excluded' THEN 1 ELSE 0 END) AS auto_excluded,
                SUM(CASE WHEN human_status='human_dismissed' THEN 1 ELSE 0 END) AS human_excluded,
                SUM(CASE WHEN is_customer_formal=1 THEN 1 ELSE 0 END) AS customer_formal,
                COUNT(*) AS total
               FROM rule_findings WHERE COALESCE(is_current,1)=1"""
        ).fetchall():
            finding_buckets.update({k: int(v or 0) for k, v in dict(bucket_row).items()})
        jobs_active = conn.execute(
            "SELECT COUNT(*) FROM scan_jobs WHERE status IN ('pending','running','cooling')"
        ).fetchone()[0]
        sites_total = len(SCAN_SITES)
        overdue_count = conn.execute(
            """SELECT COUNT(*) FROM rectification_tasks
               WHERE status NOT IN ('signoff','failed','cancelled')
               AND deadline IS NOT NULL AND deadline < ?""",
            (utc_now(),),
        ).fetchone()[0] if _table_exists(conn, "rectification_tasks") else 0
        archive_findings = conn.execute(
            "SELECT COUNT(*) FROM archive_findings WHERE COALESCE(is_current,1)=1"
        ).fetchone()[0] if _table_exists(conn, "archive_findings") else 0
        duplicate_pairs = conn.execute(
            "SELECT COUNT(*) FROM duplicate_pairs"
        ).fetchone()[0] if _table_exists(conn, "duplicate_pairs") else 0
        recon_matches = conn.execute(
            "SELECT COUNT(*) FROM reconciliation_matches"
        ).fetchone()[0] if _table_exists(conn, "reconciliation_matches") else 0
    return {
        "jobs": [dict(j) for j in jobs],
        "jobs_active": int(jobs_active),
        "sites_total": sites_total,
        "finding_buckets": finding_buckets,
        "rectification_overdue": int(overdue_count),
        "archive_findings": int(archive_findings),
        "duplicate_pairs": int(duplicate_pairs),
        "recon_matches": int(recon_matches),
    }


@app.get("/api/evidence-completeness")
async def evidence_completeness_endpoint(_user: CurrentUser = Depends(require_login)):
    """R-P1-2 证据完整率 KPI（只读）。

    返回全局 finding 证据完整率度量字典，供独立验收门禁与仪表盘使用。
    """
    from app.evidence_completeness import compute_evidence_completeness
    return compute_evidence_completeness(db)


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


NAV_ITEMS = [
    {"key": "overview", "label": "首页总览", "icon": "home", "group": "main", "requires_login": True,
     "api": "/api/overview", "description": "全局仪表盘与最近任务"},
    {"key": "scan-jobs", "label": "扫描任务", "icon": "job", "group": "main", "requires_login": True,
     "api": "/api/jobs", "description": "扫描任务列表与状态"},
    {"key": "confirmed-problems", "label": "确认问题", "icon": "issue", "group": "findings", "requires_login": True,
     "api": "/api/findings/pdf?bucket=confirmed_problem", "description": "客户正式问题"},
    {"key": "review-queue", "label": "待复核线索", "icon": "review", "group": "findings", "requires_login": True,
     "api": "/api/jobs/0/review-queue", "description": "待人工复核的疑似线索"},
    {"key": "insufficient-data", "label": "数据不足", "icon": "data", "group": "findings", "requires_login": True,
     "api": "/api/findings/pdf?bucket=insufficient_data", "description": "证据不足以判定"},
    {"key": "collection-errors", "label": "采集异常", "icon": "alert", "group": "findings", "requires_login": True,
     "api": "/api/jobs/0/scan-exceptions", "description": "采集阶段异常"},
    {"key": "auto-excluded", "label": "已自动排除", "icon": "auto", "group": "excluded", "requires_login": True,
     "api": "/api/findings/pdf?bucket=auto_excluded", "description": "规则自动排除"},
    {"key": "human-excluded", "label": "已人工排除", "icon": "human", "group": "excluded", "requires_login": True,
     "api": "/api/findings/pdf?bucket=human_excluded", "description": "人工排除"},
    {"key": "archive", "label": "异常归档", "icon": "archive", "group": "quality", "requires_login": True,
     "api": "/api/archive/findings", "description": "ARCH-001~006 归档问题"},
    {"key": "duplicates", "label": "重复数据", "icon": "duplicate", "group": "quality", "requires_login": True,
     "api": "/api/duplicates", "description": "DUP-001~008 重复对"},
    {"key": "reconciliation", "label": "双渠道对账", "icon": "recon", "group": "quality", "requires_login": True,
     "api": "/api/reconciliation/matches", "description": "市区双渠道匹配与差异"},
    {"key": "pdf-attachments", "label": "PDF 与附件", "icon": "pdf", "group": "documents", "requires_login": True,
     "api": "/api/findings/pdf", "description": "PDF 与附件检查结果"},
    {"key": "interpretations", "label": "政策解读", "icon": "interp", "group": "documents", "requires_login": True,
     "api": "/api/interpretations", "description": "INTERP-001~008 解读检查"},
    {"key": "reading-linkage", "label": "阅办联动", "icon": "read", "group": "documents", "requires_login": True,
     "api": "/api/reading-checks", "description": "READ-001~005 阅办检查"},
    {"key": "rectification", "label": "整改任务", "icon": "rect", "group": "closure", "requires_login": True,
     "api": "/api/rectification/tasks", "description": "整改任务列表"},
    {"key": "signoff", "label": "复查销号", "icon": "signoff", "group": "closure", "requires_login": True,
     "api": "/api/rectification/tasks?status=signoff", "description": "已销号任务"},
    {"key": "rule-effect", "label": "规则效果", "icon": "rule", "group": "governance", "requires_login": True,
     "api": "/api/rule-effect", "description": "规则精度与降级"},
    {"key": "knowledge", "label": "机构知识库", "icon": "kb", "group": "governance", "requires_login": True,
     "api": "/api/organizations", "description": "机构与机关代字"},
    {"key": "users", "label": "用户与权限", "icon": "user", "group": "system", "requires_login": True,
     "roles": ["admin"], "api": "/api/users", "description": "用户管理"},
    {"key": "audits", "label": "操作审计", "icon": "audit", "group": "system", "requires_login": True,
     "roles": ["admin", "auditor"], "api": "/api/audits", "description": "审计日志"},
    {"key": "settings", "label": "系统设置", "icon": "settings", "group": "system", "requires_login": True,
     "roles": ["admin"], "api": "/api/system-settings", "description": "系统配置"},
]


@app.get("/api/nav-items")
async def nav_items_endpoint(user: CurrentUser | None = Depends(get_optional_user)):
    """返回 21 项导航结构，按当前用户角色过滤可见项（M6-1）。

    未登录用户只能看到无需登录的项（目前全部需登录，故未登录返回空列表）。
    """
    if not user:
        return []
    user_roles = set(user.roles)
    visible = []
    for item in NAV_ITEMS:
        required_roles = item.get("roles")
        if required_roles and not (user_roles & set(required_roles)):
            continue
        visible.append({
            "key": item["key"], "label": item["label"], "icon": item["icon"],
            "group": item["group"], "api": item["api"],
            "description": item["description"],
        })
    return visible


@app.get("/api/organizations")
async def organizations_endpoint(
    district: str | None = None,
    _user: CurrentUser = Depends(require_login),
):
    """机构知识库列表（M6-1 机构知识库导航项数据源）。"""
    from app.knowledge import load_knowledge
    with db.connect() as conn:
        records = load_knowledge(conn, district=district)
    return [
        {
            "id": r.id, "district": r.district, "name": r.name,
            "symbol": r.symbol, "aliases": list(r.aliases),
            "valid_from": r.valid_from, "valid_to": r.valid_to,
            "source": r.source,
        }
        for r in records
    ]

