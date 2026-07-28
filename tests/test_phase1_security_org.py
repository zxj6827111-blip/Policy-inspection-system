"""Phase-1 security, finding model, org rules and sample regression tests."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.auth.service import CSRF_HEADER, AuthService
from app.db import Database
from app.domain import Finding, PolicyRecord
from app.findings_model import is_customer_formal_problem
from app.jobs import JobManager
from app.knowledge.organizations import load_knowledge
from app.repository import Repository
from app.rules import WorkdayCalendar, evaluate_record
from tests.auth_helpers import bootstrap_users, login

pytestmark = pytest.mark.real_auth


def _app_client(tmp_path, monkeypatch):
    db = Database(tmp_path / "phase1.db")
    db.initialize()
    manager = JobManager(db)
    monkeypatch.setattr(main_module, "db", db)
    monkeypatch.setattr(main_module, "manager", manager)
    monkeypatch.setenv("POLICY_AUTH_DISABLED", "0")
    bootstrap_users(db)
    return db, TestClient(main_module.app)


def test_unauthenticated_write_is_rejected(tmp_path, monkeypatch):
    _db, client = _app_client(tmp_path, monkeypatch)
    with client:
        response = client.post("/api/site-jobs", json={"site_keys": ["municipal_putuo"]})
    assert response.status_code in {401, 403}


def test_viewer_cannot_call_write_or_review(tmp_path, monkeypatch):
    db, client = _app_client(tmp_path, monkeypatch)
    with client:
        headers = login(client, "viewer", "ViewerPass123!")["headers"]
        denied = client.post(
            "/api/site-jobs",
            json={"site_keys": ["municipal_putuo"]},
            headers=headers,
        )
        assert denied.status_code == 403
        job_id = db.create_job(["普陀区"], "full", {}, 1)
        fid = Repository(db).save_record(
            job_id,
            PolicyRecord("普陀区", "x", "https://example.test/v"),
            [Finding("META-001", "元数据问题", "high", "confirmed", "缺字段", machine_status="confirmed_by_rule", allow_customer_output=True)],
        )
        findings = client.get(f"/api/jobs/{job_id}/findings", headers=headers)
        assert findings.status_code == 200
        review = client.post(
            f"/api/findings/{findings.json()[0]['id']}/review",
            json={"decision": "confirmed", "note": "nope"},
            headers=headers,
        )
        assert review.status_code == 403


def test_reviewer_can_review_but_not_manage_users(tmp_path, monkeypatch):
    db, client = _app_client(tmp_path, monkeypatch)
    with client:
        headers = login(client, "reviewer", "ReviewPass123!")["headers"]
        job_id = db.create_job(["普陀区"], "full", {}, 1)
        Repository(db).save_record(
            job_id,
            PolicyRecord("普陀区", "待复核", "https://example.test/r"),
            [Finding("DOC-001", "文号问题", "high", "confirmed", "不一致", machine_status="confirmed_by_rule", allow_customer_output=True)],
        )
        finding_id = client.get(f"/api/jobs/{job_id}/findings", headers=headers).json()[0]["id"]
        ok = client.post(
            f"/api/findings/{finding_id}/review",
            json={"decision": "human_confirmed", "note": "确认", "reason_code": "other"},
            headers=headers,
        )
        assert ok.status_code == 200
        users = client.post(
            "/api/users",
            json={"username": "x", "password": "Password123!", "roles": ["viewer"]},
            headers=headers,
        )
        assert users.status_code == 403


def test_admin_can_create_user_and_audit_recorded(tmp_path, monkeypatch):
    db, client = _app_client(tmp_path, monkeypatch)
    with client:
        headers = login(client, "admin", "AdminPass123!")["headers"]
        created = client.post(
            "/api/users",
            json={"username": "ops2", "password": "Password123!", "roles": ["operator"]},
            headers=headers,
        )
        assert created.status_code == 200
        audits = client.get("/api/audits", headers=headers)
        assert audits.status_code == 200
        assert any(row["action"] == "user.create" for row in audits.json())


def test_csrf_required_on_cookie_writes(tmp_path, monkeypatch):
    _db, client = _app_client(tmp_path, monkeypatch)
    with client:
        login(client, "admin", "AdminPass123!")
        # cookies present but no CSRF header
        response = client.post("/api/site-jobs", json={"site_keys": ["municipal_putuo"]})
        assert response.status_code == 403
        assert "CSRF" in response.json()["detail"]


def test_health_hides_database_path(tmp_path, monkeypatch):
    _db, client = _app_client(tmp_path, monkeypatch)
    with client:
        payload = client.get("/health").json()
    assert payload["status"] == "ok"
    assert "policy_inspector" not in str(payload.get("database", "")).replace("\\", "/")
    assert payload["database"] == "configured"


def test_agency_001_not_customer_formal(tmp_path, monkeypatch):
    db, _client = _app_client(tmp_path, monkeypatch)
    agencies = db.agency_rules("普陀区")
    record = PolicyRecord(
        "普陀区",
        "上海市普陀区发展和改革委员会关于项目申报的通知",
        "https://example.test/a",
        issuing_agency="上海市普陀区人民政府",
        page_document_number="沪发改〔2026〕1号",
    )
    findings = evaluate_record(record, agencies, WorkdayCalendar(), knowledge=None)
    agency = [f for f in findings if f.rule_code == "AGENCY-001"]
    assert agency
    assert agency[0].machine_status == "suspected"
    assert agency[0].allow_customer_output is False
    assert not is_customer_formal_problem(
        rule_code="AGENCY-001",
        machine_status="suspected",
        human_status="unreviewed",
    )


def test_puweiban_2013_41_joint_forward_no_org001(tmp_path, monkeypatch):
    db, _client = _app_client(tmp_path, monkeypatch)
    with db.connect() as conn:
        knowledge = load_knowledge(conn, "普陀区")
    body = (
        "中共上海市普陀区委办公室 上海市普陀区人民政府办公室转发\n"
        "《关于进一步做好有关工作的意见》的通知\n"
    )
    record = PolicyRecord(
        "普陀区",
        "转发通知",
        "https://example.test/puwei41",
        issuing_agency="上海市普陀区人民政府",
        page_document_number="普委办〔2013〕41号",
        body_text=body,
    )
    findings = evaluate_record(record, db.agency_rules("普陀区"), WorkdayCalendar(), knowledge=knowledge)
    codes = {f.rule_code for f in findings}
    assert "ORG-001" not in codes
    assert "ORG-002" in codes
    org2 = next(f for f in findings if f.rule_code == "ORG-002")
    assert org2.machine_status == "hint"
    assert org2.allow_customer_output is False
    assert org2.rule_version
    assert any(item.get("field_name") == "联合发文机关" for item in org2.evidence_fields)


def test_street_office_and_historical_alias_symbols(tmp_path, monkeypatch):
    db, _client = _app_client(tmp_path, monkeypatch)
    with db.connect() as conn:
        knowledge = load_knowledge(conn, "普陀区")
    record = PolicyRecord(
        "普陀区",
        "宜川路街道通知",
        "https://example.test/yichuan",
        issuing_agency="上海市普陀区宜川路街道办事处",
        page_document_number="普宜办〔2020〕3号",
        body_text="上海市普陀区宜川路街道办事处\n2020年3月1日",
    )
    findings = evaluate_record(record, [], WorkdayCalendar(), knowledge=knowledge)
    assert not any(f.rule_code == "ORG-001" for f in findings)

    record2 = PolicyRecord(
        "普陀区",
        "长征相关",
        "https://example.test/changzheng",
        issuing_agency="长征镇",
        page_document_number="长街办〔2018〕2号",
        body_text="上海市普陀区长征镇人民政府\n2018年1月1日",
    )
    findings2 = evaluate_record(record2, [], WorkdayCalendar(), knowledge=knowledge)
    assert not any(f.rule_code == "ORG-001" for f in findings2)


def test_finding_state_migration_and_review_append(tmp_path, monkeypatch):
    db, client = _app_client(tmp_path, monkeypatch)
    with client:
        headers = login(client, "reviewer", "ReviewPass123!")["headers"]
        job_id = db.create_job(["普陀区"], "full", {}, 1)
        Repository(db).save_record(
            job_id,
            PolicyRecord("普陀区", "状态", "https://example.test/state"),
            [
                Finding(
                    "DATE-001",
                    "日期问题",
                    "high",
                    "confirmed",
                    "超期",
                    machine_status="confirmed_by_rule",
                    allow_customer_output=True,
                    rule_version="1.0.0",
                    evidence="成文到发布",
                )
            ],
        )
        finding = client.get(f"/api/jobs/{job_id}/findings", headers=headers).json()[0]
        assert finding["machine_status"] == "confirmed_by_rule"
        assert finding["human_status"] == "unreviewed"
        client.post(
            f"/api/findings/{finding['id']}/review",
            json={"decision": "human_dismissed", "note": "迁移补录", "reason_code": "platform_migration"},
            headers=headers,
        )
        client.post(
            f"/api/findings/{finding['id']}/review",
            json={"decision": "human_confirmed", "note": "再次确认", "reason_code": "other"},
            headers=headers,
        )
        with db.connect() as conn:
            reviews = conn.execute(
                "SELECT decision FROM finding_reviews WHERE finding_id=? ORDER BY id",
                (finding["id"],),
            ).fetchall()
            decision = conn.execute(
                "SELECT decision FROM review_decisions WHERE finding_id=?",
                (finding["id"],),
            ).fetchone()
        assert len(reviews) == 2
        assert decision["decision"] == "confirmed"
        evidence = client.get(f"/api/findings/{finding['id']}/evidence", headers=headers).json()
        assert evidence["display_mode"] == "dynamic_fields"
        assert evidence["evidence_fields"]


def test_human_conclusion_inherited_on_resave(tmp_path, monkeypatch):
    db, _client = _app_client(tmp_path, monkeypatch)
    job_id = db.create_job(["普陀区"], "full", {}, 1)
    repo = Repository(db)
    record = PolicyRecord("普陀区", "继承", "https://example.test/inherit")
    repo.save_record(
        job_id,
        record,
        [Finding("META-001", "元数据问题", "high", "confirmed", "缺", machine_status="confirmed_by_rule", allow_customer_output=True)],
    )
    with db.connect() as conn:
        fid = conn.execute("SELECT id FROM rule_findings WHERE job_id=?", (job_id,)).fetchone()["id"]
        conn.execute(
            "UPDATE rule_findings SET human_status='human_dismissed', review_status='dismissed' WHERE id=?",
            (fid,),
        )
    repo.save_record(
        job_id,
        record,
        [Finding("META-001", "元数据问题", "high", "confirmed", "缺", machine_status="confirmed_by_rule", allow_customer_output=True)],
    )
    with db.connect() as conn:
        row = conn.execute("SELECT human_status, review_status FROM rule_findings WHERE job_id=?", (job_id,)).fetchone()
    assert row["human_status"] == "human_dismissed"
