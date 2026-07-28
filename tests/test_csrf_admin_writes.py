"""R-P0-1-TEST: 三段 CSRF 校验覆盖 admin 写接口.

独立总验收 P0 阻断项 1 的回归测试：验证 PUT /api/system-settings/{key} 与
POST /api/backups 在修复前仅使用 require_roles("admin")（无 CSRF），修复后
改为 require_write("users:admin")，强制三段 CSRF（header + cookie + session）。

三段 CSRF 含义（见 app/auth/dependencies.py::require_write）：
  1. header_token  = request.headers["X-CSRF-Token"]
  2. cookie_token  = request.cookies["pis_csrf"]
  3. session_token = auth.session_csrf(session_id)  # 来自 sessions 表
三者均非空且两两相等才放行；否则 403 "CSRF 校验失败" 或 "缺少 CSRF 令牌"。

本测试矩阵：
  - 无 CSRF header                         → 403 含 "CSRF"
  - CSRF header 与 cookie 不匹配           → 403 含 "CSRF"
  - 合法三段 CSRF（admin 登录后默认）       → 通过 CSRF 门禁（200 或 404/业务码）
  - viewer 持合法 CSRF 但无 users:admin    → 403 "权限不足"（证明 RBAC 仍生效，非 CSRF 误判）
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.auth.service import CSRF_HEADER
from app.db import Database, utc_now
from app.jobs import JobManager
from tests.auth_helpers import bootstrap_users, login

pytestmark = pytest.mark.real_auth


def _app_client(tmp_path, monkeypatch):
    db = Database(tmp_path / "csrf_admin.db")
    db.initialize()
    manager = JobManager(db)
    monkeypatch.setattr(main_module, "db", db)
    monkeypatch.setattr(main_module, "manager", manager)
    monkeypatch.setenv("POLICY_AUTH_DISABLED", "0")
    bootstrap_users(db)
    # 预置一条 system_settings 记录，供 PUT 更新（避免 404 干扰 CSRF 断言）
    with db.connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO system_settings(key, value, category, description, updated_at, updated_by) "
            "VALUES(?,?,?,?,?,?)",
            ("test.csrf_key", "v0", "general", "csrf test seed", utc_now(), "system"),
        )
    return db, TestClient(main_module.app)


# ---------- PUT /api/system-settings/{key} ----------


def test_put_system_settings_without_csrf_header_rejected(tmp_path, monkeypatch):
    """无 X-CSRF-Token header → 403 CSRF（即使 admin 已登录、cookie 存在）。"""
    _db, client = _app_client(tmp_path, monkeypatch)
    with client:
        login(client, "admin", "AdminPass123!")  # 建立 session + csrf cookie
        resp = client.put(
            "/api/system-settings/test.csrf_key",
            json={"value": "v1"},
        )
    assert resp.status_code == 403
    assert "CSRF" in resp.json()["detail"]


def test_put_system_settings_mismatched_csrf_rejected(tmp_path, monkeypatch):
    """CSRF header 与 cookie 不匹配 → 403 CSRF。"""
    _db, client = _app_client(tmp_path, monkeypatch)
    with client:
        login(client, "admin", "AdminPass123!")
        resp = client.put(
            "/api/system-settings/test.csrf_key",
            json={"value": "v1"},
            headers={CSRF_HEADER: "tampered-token-does-not-match-cookie"},
        )
    assert resp.status_code == 403
    assert "CSRF" in resp.json()["detail"]


def test_put_system_settings_valid_csrf_passes(tmp_path, monkeypatch):
    """合法三段 CSRF → 通过 CSRF 门禁，返回 200 并完成更新。"""
    db, client = _app_client(tmp_path, monkeypatch)
    with client:
        auth = login(client, "admin", "AdminPass123!")
        resp = client.put(
            "/api/system-settings/test.csrf_key",
            json={"value": "v1", "description": "updated"},
            headers=auth["headers"],
        )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"key": "test.csrf_key", "value": "v1"}
    # 校验落库
    with db.connect() as conn:
        row = conn.execute(
            "SELECT value FROM system_settings WHERE key=?", ("test.csrf_key",)
        ).fetchone()
    assert row["value"] == "v1"


def test_put_system_settings_viewer_with_valid_csrf_still_forbidden(tmp_path, monkeypatch):
    """viewer 持合法 CSRF 但无 users:admin 能力 → 403 权限不足（非 CSRF）。"""
    _db, client = _app_client(tmp_path, monkeypatch)
    with client:
        auth = login(client, "viewer", "ViewerPass123!")
        resp = client.put(
            "/api/system-settings/test.csrf_key",
            json={"value": "v2"},
            headers=auth["headers"],
        )
    assert resp.status_code == 403
    assert "CSRF" not in resp.json()["detail"]  # RBAC 拒绝，非 CSRF


# ---------- POST /api/backups ----------


def test_post_backups_without_csrf_header_rejected(tmp_path, monkeypatch):
    """无 X-CSRF-Token header → 403 CSRF。"""
    _db, client = _app_client(tmp_path, monkeypatch)
    with client:
        login(client, "admin", "AdminPass123!")
        resp = client.post("/api/backups", json={"label": "csrf_test"})
    assert resp.status_code == 403
    assert "CSRF" in resp.json()["detail"]


def test_post_backups_mismatched_csrf_rejected(tmp_path, monkeypatch):
    """CSRF header 与 cookie 不匹配 → 403 CSRF。"""
    _db, client = _app_client(tmp_path, monkeypatch)
    with client:
        login(client, "admin", "AdminPass123!")
        resp = client.post(
            "/api/backups",
            json={"label": "csrf_test"},
            headers={CSRF_HEADER: "tampered-does-not-match"},
        )
    assert resp.status_code == 403
    assert "CSRF" in resp.json()["detail"]


def test_post_backups_valid_csrf_passes(tmp_path, monkeypatch):
    """合法三段 CSRF → 通过 CSRF 门禁，返回 200 并创建备份。"""
    _db, client = _app_client(tmp_path, monkeypatch)
    with client:
        auth = login(client, "admin", "AdminPass123!")
        resp = client.post(
            "/api/backups",
            json={"label": "csrf_test", "prune": False},
            headers=auth["headers"],
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "backup" in body
    assert str(body["backup"]).endswith(".db") or "backup" in str(body["backup"])


def test_post_backups_viewer_with_valid_csrf_still_forbidden(tmp_path, monkeypatch):
    """viewer 持合法 CSRF 但无 users:admin 能力 → 403 权限不足（非 CSRF）。"""
    _db, client = _app_client(tmp_path, monkeypatch)
    with client:
        auth = login(client, "viewer", "ViewerPass123!")
        resp = client.post(
            "/api/backups",
            json={"label": "csrf_test"},
            headers=auth["headers"],
        )
    assert resp.status_code == 403
    assert "CSRF" not in resp.json()["detail"]


# ---------- logout-simple CSRF + failed login audit ----------


def test_logout_simple_without_csrf_rejected(tmp_path, monkeypatch):
    """POST /api/auth/logout-simple 无 CSRF → 403。"""
    _db, client = _app_client(tmp_path, monkeypatch)
    with client:
        login(client, "admin", "AdminPass123!")
        resp = client.post("/api/auth/logout-simple", json={})
    assert resp.status_code == 403
    assert "CSRF" in resp.json()["detail"]


def test_logout_simple_valid_csrf_passes(tmp_path, monkeypatch):
    """POST /api/auth/logout-simple 合法 CSRF → 200。"""
    _db, client = _app_client(tmp_path, monkeypatch)
    with client:
        auth = login(client, "admin", "AdminPass123!")
        resp = client.post(
            "/api/auth/logout-simple",
            json={},
            headers=auth["headers"],
        )
    assert resp.status_code == 200, resp.text
    assert resp.json().get("ok") is True


def test_failed_login_writes_audit_with_actor_username(tmp_path, monkeypatch):
    """失败登录写入 operation_audits，actor_username=尝试登录名。"""
    db, client = _app_client(tmp_path, monkeypatch)
    with client:
        resp = client.post(
            "/api/auth/login",
            json={"username": "no-such-user", "password": "WrongPass999!"},
        )
    assert resp.status_code == 401
    with db.connect() as conn:
        row = conn.execute(
            "SELECT action, actor_username, resource_id FROM operation_audits "
            "WHERE action=? ORDER BY id DESC LIMIT 1",
            ("auth.login_failed",),
        ).fetchone()
    assert row is not None
    assert row["action"] == "auth.login_failed"
    assert row["actor_username"] == "no-such-user"
    assert row["resource_id"] == "no-such-user"
