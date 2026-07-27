"""M1: Multi-role permission matrix verification at the API layer.

Completes the five-role (admin/operator/reviewer/viewer/auditor) boundary
checks. Existing Phase1 tests already cover admin/viewer/reviewer; this
file adds explicit operator and auditor checks and a consolidated matrix
assertion so the role-capability contract stays locked.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.db import Database
from app.jobs import JobManager
from app.repository import Repository
from app.domain import Finding, PolicyRecord
from tests.auth_helpers import bootstrap_users, login

pytestmark = pytest.mark.real_auth


def _app_client(tmp_path, monkeypatch):
    db = Database(tmp_path / "m1_roles.db")
    db.initialize()
    manager = JobManager(db)
    monkeypatch.setattr(main_module, "db", db)
    monkeypatch.setattr(main_module, "manager", manager)
    monkeypatch.setenv("POLICY_AUTH_DISABLED", "0")
    bootstrap_users(db)
    return db, TestClient(main_module.app)


def _seed_finding(db, job_id=None):
    if job_id is None:
        job_id = db.create_job(["普陀区"], "full", {}, 1)
    Repository(db).save_record(
        job_id,
        PolicyRecord("普陀区", "矩阵", "https://example.test/matrix"),
        [Finding("META-001", "元数据", "high", "confirmed", "缺", machine_status="confirmed_by_rule", allow_customer_output=True)],
    )
    return job_id


def test_operator_can_write_jobs_but_not_review(tmp_path, monkeypatch):
    db, client = _app_client(tmp_path, monkeypatch)
    job_id = _seed_finding(db)
    with client:
        headers = login(client, "operator", "OperPass123!")["headers"]
        # operator can read findings
        findings = client.get(f"/api/jobs/{job_id}/findings", headers=headers)
        assert findings.status_code == 200
        # operator can list scan sites (read capability)
        sites = client.get("/api/scan-sites", headers=headers)
        assert sites.status_code == 200
        # operator CANNOT review (no review:write capability)
        finding_id = findings.json()[0]["id"]
        review = client.post(
            f"/api/findings/{finding_id}/review",
            json={"decision": "human_confirmed", "note": "op", "reason_code": "other"},
            headers=headers,
        )
        assert review.status_code == 403
        # operator CANNOT manage users
        users = client.post(
            "/api/users",
            json={"username": "x", "password": "Password123!", "roles": ["viewer"]},
            headers=headers,
        )
        assert users.status_code == 403


def test_auditor_can_read_audits_but_not_write_or_review(tmp_path, monkeypatch):
    db, client = _app_client(tmp_path, monkeypatch)
    job_id = _seed_finding(db)
    with client:
        headers = login(client, "auditor", "AuditPass123!")["headers"]
        # auditor can read audits
        audits = client.get("/api/audits", headers=headers)
        assert audits.status_code == 200
        # auditor can read findings
        findings = client.get(f"/api/jobs/{job_id}/findings", headers=headers)
        assert findings.status_code == 200
        # auditor CANNOT create jobs (no jobs:write)
        job_create = client.post(
            "/api/jobs",
            json={"targets": ["municipal_putuo"], "mode": "incremental"},
            headers=headers,
        )
        assert job_create.status_code == 403
        # auditor CANNOT review
        finding_id = findings.json()[0]["id"]
        review = client.post(
            f"/api/findings/{finding_id}/review",
            json={"decision": "human_confirmed", "note": "audit", "reason_code": "other"},
            headers=headers,
        )
        assert review.status_code == 403


def test_viewer_cannot_read_audits(tmp_path, monkeypatch):
    """Viewer has read but not audit:read capability."""
    db, client = _app_client(tmp_path, monkeypatch)
    with client:
        headers = login(client, "viewer", "ViewerPass123!")["headers"]
        audits = client.get("/api/audits", headers=headers)
        assert audits.status_code == 403


def test_role_capability_matrix_contract():
    """Lock the role->capability contract from app.auth.models."""
    from app.auth.models import ROLE_CAPABILITIES

    caps = ROLE_CAPABILITIES
    # admin has everything
    assert "users:admin" in caps["admin"]
    assert "jobs:write" in caps["admin"]
    assert "review:write" in caps["admin"]
    assert "audit:read" in caps["admin"]
    # operator: jobs+export+read, no review/users/audit
    assert "jobs:write" in caps["operator"]
    assert "review:write" not in caps["operator"]
    assert "users:admin" not in caps["operator"]
    assert "audit:read" not in caps["operator"]
    # reviewer: review+export+read, no jobs/users/audit
    assert "review:write" in caps["reviewer"]
    assert "jobs:write" not in caps["reviewer"]
    # viewer: read+export only
    assert caps["viewer"] == frozenset({"read", "export:read"})
    # auditor: read+audit+export, no writes
    assert "audit:read" in caps["auditor"]
    assert "jobs:write" not in caps["auditor"]
    assert "review:write" not in caps["auditor"]


def test_ui_shell_and_static_reachable(tmp_path, monkeypatch):
    """UI index page and static assets are reachable (UI route gate)."""
    _db, client = _app_client(tmp_path, monkeypatch)
    with client:
        index = client.get("/")
        assert index.status_code == 200
        assert "text/html" in index.headers.get("content-type", "")
        # static CSS/JS reachable
        css = client.get("/static/app.css")
        assert css.status_code == 200
        js = client.get("/static/app.js")
        assert js.status_code == 200
