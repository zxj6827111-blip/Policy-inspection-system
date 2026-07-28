"""Closeout tests: review lineage, count semantics, auth fail-closed."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.db import Database
from app.domain import Finding, PolicyRecord
from app.finding_counts import compute_counts_from_rows
from app.findings_model import is_customer_formal_problem
from app.jobs import JobManager
from app.knowledge.organizations import load_knowledge
from app.repository import Repository
from app.rules import WorkdayCalendar, evaluate_record
from app.runtime_config import (
    ConfigurationError,
    load_runtime_settings,
    reset_runtime_settings_cache,
)
from tests.auth_helpers import bootstrap_users, login

pytestmark = pytest.mark.real_auth


def _client(tmp_path, monkeypatch):
    reset_runtime_settings_cache()
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("POLICY_AUTH_DISABLED", "0")
    reset_runtime_settings_cache()
    db = Database(tmp_path / "closeout.db")
    db.initialize()
    manager = JobManager(db)
    monkeypatch.setattr(main_module, "db", db)
    monkeypatch.setattr(main_module, "manager", manager)
    bootstrap_users(db)
    return db, TestClient(main_module.app)


def test_rescan_keeps_review_history_and_inherits_human_status(tmp_path, monkeypatch):
    db, client = _client(tmp_path, monkeypatch)
    with client:
        headers = login(client, "reviewer", "ReviewPass123!")["headers"]
        job_id = db.create_job(["普陀区"], "full", {}, 1)
        record = PolicyRecord("普陀区", "历史保留", "https://example.test/hist")
        repo = Repository(db)
        repo.save_record(
            job_id,
            record,
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
                    evidence="e1",
                )
            ],
        )
        finding = client.get(f"/api/jobs/{job_id}/findings", headers=headers).json()[0]
        old_id = finding["id"]
        client.post(
            f"/api/findings/{old_id}/review",
            json={"decision": "human_dismissed", "note": "平台迁移", "reason_code": "platform_migration"},
            headers=headers,
        )
        client.post(
            f"/api/findings/{old_id}/review",
            json={"decision": "human_confirmed", "note": "再次确认", "reason_code": "other"},
            headers=headers,
        )
        with db.connect() as conn:
            before_reviews = conn.execute(
                "SELECT COUNT(*) FROM finding_reviews WHERE finding_id=?", (old_id,)
            ).fetchone()[0]
            assert before_reviews == 2

        # Rescan / rebuild findings for same document
        repo.save_record(
            job_id,
            record,
            [
                Finding(
                    "DATE-001",
                    "日期问题",
                    "high",
                    "confirmed",
                    "超期-重算",
                    machine_status="confirmed_by_rule",
                    allow_customer_output=True,
                    rule_version="1.1.0",
                    evidence="e2",
                )
            ],
        )
        with db.connect() as conn:
            # Old reviews still attached to original finding id
            still = conn.execute(
                "SELECT COUNT(*) FROM finding_reviews WHERE finding_id=?", (old_id,)
            ).fetchone()[0]
            assert still == 2
            old_row = conn.execute("SELECT * FROM rule_findings WHERE id=?", (old_id,)).fetchone()
            assert int(old_row["is_current"]) == 0
            assert old_row["superseded_by"] is not None
            new_row = conn.execute(
                "SELECT * FROM rule_findings WHERE id=?", (old_row["superseded_by"],)
            ).fetchone()
            assert int(new_row["is_current"]) == 1
            assert new_row["human_status"] == "human_confirmed"
            assert new_row["rule_version"] == "1.1.0"
            # Total review rows for document+rule still 2 (immutable, not copied/deleted)
            total_hist = conn.execute(
                """SELECT COUNT(*) FROM finding_reviews fr
                JOIN rule_findings f ON f.id=fr.finding_id
                WHERE f.job_id=? AND f.document_id=? AND f.rule_code=?""",
                (job_id, new_row["document_id"], "DATE-001"),
            ).fetchone()[0]
            assert total_hist == 2

        evidence = client.get(
            f"/api/findings/{new_row['id']}/evidence", headers=headers
        ).json()
        assert len(evidence["review_history"]) == 2
        comments = [r.get("comment") or "" for r in evidence["review_history"]]
        assert "平台迁移" in comments
        assert "再次确认" in comments


def test_dismissed_finding_reappears_with_inherited_status(tmp_path, monkeypatch):
    db, _c = _client(tmp_path, monkeypatch)
    job_id = db.create_job(["普陀区"], "full", {}, 1)
    repo = Repository(db)
    record = PolicyRecord("普陀区", "复发", "https://example.test/reappear")
    repo.save_record(
        job_id,
        record,
        [Finding("META-001", "元数据问题", "high", "confirmed", "缺", machine_status="confirmed_by_rule", allow_customer_output=True)],
    )
    with db.connect() as conn:
        fid = conn.execute("SELECT id FROM rule_findings WHERE job_id=? AND COALESCE(is_current,1)=1", (job_id,)).fetchone()["id"]
        conn.execute(
            "UPDATE rule_findings SET human_status='human_dismissed', review_status='dismissed' WHERE id=?",
            (fid,),
        )
        conn.execute(
            """INSERT INTO finding_reviews(finding_id, reviewer_username, decision, reason_code, comment, created_at)
            VALUES(?,?,?,?,?,?)""",
            (fid, "reviewer", "human_dismissed", "other", "已排除", "2026-01-01T00:00:00+00:00"),
        )
    repo.save_record(
        job_id,
        record,
        [Finding("META-001", "元数据问题", "high", "confirmed", "缺", machine_status="confirmed_by_rule", allow_customer_output=True)],
    )
    with db.connect() as conn:
        current = conn.execute(
            "SELECT * FROM rule_findings WHERE job_id=? AND COALESCE(is_current,1)=1",
            (job_id,),
        ).fetchone()
        assert current["human_status"] == "human_dismissed"
        hist = conn.execute("SELECT COUNT(*) FROM finding_reviews").fetchone()[0]
        assert hist >= 1
        old = conn.execute("SELECT is_current FROM rule_findings WHERE id=?", (fid,)).fetchone()
        assert int(old["is_current"]) == 0


def test_count_semantics_agency_and_formal(tmp_path, monkeypatch):
    db, client = _client(tmp_path, monkeypatch)
    with client:
        headers = login(client, "reviewer", "ReviewPass123!")["headers"]
        job_id = db.create_job(["普陀区"], "full", {}, 1)
        repo = Repository(db)
        repo.save_record(
            job_id,
            PolicyRecord("普陀区", "a", "https://example.test/c1"),
            [
                Finding("AGENCY-001", "机构问题", "medium", "pending", "疑似", machine_status="suspected", allow_customer_output=False),
                Finding("ORG-002", "机构问题", "low", "pending", "提示", machine_status="hint", allow_customer_output=False),
                Finding("DATE-001", "日期问题", "high", "confirmed", "超期", machine_status="confirmed_by_rule", allow_customer_output=True),
            ],
        )
        # save_record inserts one document per call — call thrice for three docs
    # Actually one save overwrites same? different urls:
    db2 = db
    job_id = db2.create_job(["普陀区"], "full", {}, 1) if False else job_id
    # recount from API
    # Re-seed clean job
    job_id = db.create_job(["普陀区"], "full", {}, 1)
    repo = Repository(db)
    for idx, finding in enumerate(
        [
            Finding("AGENCY-001", "机构问题", "medium", "pending", "疑似", machine_status="suspected", allow_customer_output=False),
            Finding("ORG-002", "机构问题", "low", "pending", "提示", machine_status="hint", allow_customer_output=False),
            Finding("DATE-001", "日期问题", "high", "confirmed", "超期", machine_status="confirmed_by_rule", allow_customer_output=True),
        ]
    ):
        repo.save_record(
            job_id,
            PolicyRecord("普陀区", f"t{idx}", f"https://example.test/count/{idx}"),
            [finding],
        )
    with client:
        headers = login(client, "reviewer", "ReviewPass123!")["headers"]
        counts = client.get(f"/api/jobs/{job_id}/counts", headers=headers).json()["counts"]
        assert counts["finding_total_count"] == 3
        assert counts["formal_finding_count"] == 1
        assert counts["suspected_count"] == 1
        assert counts["hint_count"] == 1
        assert not is_customer_formal_problem(
            rule_code="AGENCY-001", machine_status="suspected", human_status="unreviewed"
        )
        job = client.get(f"/api/jobs/{job_id}", headers=headers).json()
        assert job["formal_finding_count"] == 1
        assert "客户" in job["finding_count_meaning"] or "兼容" in job["finding_count_meaning"]

        finding = client.get(f"/api/jobs/{job_id}/findings", headers=headers).json()
        date_f = next(f for f in finding if f["rule_code"] == "DATE-001")
        client.post(
            f"/api/findings/{date_f['id']}/review",
            json={"decision": "human_dismissed", "note": "排除", "reason_code": "other"},
            headers=headers,
        )
        counts2 = client.get(f"/api/jobs/{job_id}/counts", headers=headers).json()["counts"]
        assert counts2["formal_finding_count"] == 0
        assert counts2["human_dismissed_count"] == 1

        client.post(
            f"/api/findings/{date_f['id']}/review",
            json={"decision": "human_confirmed", "note": "确认", "reason_code": "other"},
            headers=headers,
        )
        counts3 = client.get(f"/api/jobs/{job_id}/counts", headers=headers).json()["counts"]
        assert counts3["formal_finding_count"] == 1
        assert counts3["human_confirmed_count"] == 1


def test_auth_fail_closed_production_forbids_disabled(monkeypatch):
    reset_runtime_settings_cache()
    with pytest.raises(ConfigurationError):
        load_runtime_settings(
            environ={"APP_ENV": "production", "POLICY_AUTH_DISABLED": "1"},
            validate=True,
        )
    with pytest.raises(ConfigurationError):
        load_runtime_settings(
            environ={"APP_ENV": "development", "POLICY_AUTH_DISABLED": "1"},
            validate=True,
        )
    settings = load_runtime_settings(
        environ={"APP_ENV": "test", "POLICY_AUTH_DISABLED": "1"},
        validate=True,
    )
    assert settings.auth_disabled is True
    # Unset APP_ENV => production, auth enabled
    settings2 = load_runtime_settings(environ={"POLICY_AUTH_DISABLED": "0"}, validate=True)
    assert settings2.app_env.value == "production"
    assert settings2.auth_enabled is True
    # production normal start
    settings3 = load_runtime_settings(
        environ={"APP_ENV": "production", "POLICY_AUTH_DISABLED": "0"},
        validate=True,
    )
    assert settings3.auth_enabled is True


def test_production_misconfig_refuses_app_startup(tmp_path, monkeypatch):
    reset_runtime_settings_cache()
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("POLICY_AUTH_DISABLED", "1")
    reset_runtime_settings_cache()
    db = Database(tmp_path / "prod-deny.db")
    monkeypatch.setattr(main_module, "db", db)
    monkeypatch.setattr(main_module, "manager", JobManager(db))
    with pytest.raises(ConfigurationError):
        with TestClient(main_module.app):
            pass


def test_puweiban_and_street_still_pass(tmp_path, monkeypatch):
    db, _ = _client(tmp_path, monkeypatch)
    with db.connect() as conn:
        knowledge = load_knowledge(conn, "普陀区")
    body = "中共上海市普陀区委办公室 上海市普陀区人民政府办公室转发\n通知\n"
    findings = evaluate_record(
        PolicyRecord(
            "普陀区",
            "转发",
            "https://example.test/pw",
            issuing_agency="上海市普陀区人民政府",
            page_document_number="普委办〔2013〕41号",
            body_text=body,
        ),
        [],
        WorkdayCalendar(),
        knowledge=knowledge,
    )
    assert "ORG-001" not in {f.rule_code for f in findings}
    assert all(not f.allow_customer_output for f in findings if f.rule_code.startswith("ORG-"))
