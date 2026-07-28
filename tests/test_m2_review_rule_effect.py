"""M2 tests: batch review, dual-person review, arbitration, rule effect center, rule degradation."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.db import Database
from app.jobs import JobManager
from app.domain import Finding, PolicyRecord
from app.repository import Repository
from app.review_batch import batch_review, cluster_findings
from app.review_dual import (
    assign_dual_review,
    arbitrate,
    compute_consistency_rate,
    submit_dual_review,
)
from app.rule_effect import compute_rule_effectiveness, snapshot_rule_effectiveness
from app.rule_degradation import evaluate_rule_degradation
from tests.auth_helpers import bootstrap_users, login

pytestmark = pytest.mark.real_auth


def _app_client(tmp_path, monkeypatch):
    db = Database(tmp_path / "m2.db")
    db.initialize()
    manager = JobManager(db)
    monkeypatch.setattr(main_module, "db", db)
    monkeypatch.setattr(main_module, "manager", manager)
    monkeypatch.setenv("POLICY_AUTH_DISABLED", "0")
    bootstrap_users(db)
    return db, TestClient(main_module.app)


def _seed_findings(db, count=10, rule_code="META-001", district="普陀区", source_site="putuo"):
    """Seed N findings of one rule for batch/dual/review tests."""
    job_id = db.create_job([district], "full", {}, 1)
    repo = Repository(db)
    fids = []
    for i in range(count):
        fids.append(
            repo.save_record(
                job_id,
                PolicyRecord(district, f"标题{i}", f"https://example.test/m2-{i}"),
                [Finding(rule_code, "问题", "high", "confirmed", "证据",
                         machine_status="confirmed_by_rule", allow_customer_output=True,
                         rule_version="1.0.0")],
            )
        )
    return job_id, fids


def _user_id(db, username: str) -> int:
    with db.connect() as conn:
        row = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
    return int(row["id"])


# ---------- Batch review ----------


def test_batch_review_confirms_many_findings(tmp_path, monkeypatch):
    db, client = _app_client(tmp_path, monkeypatch)
    job_id, fids = _seed_findings(db, count=5)
    with client:
        headers = login(client, "reviewer", "ReviewPass123!")["headers"]
        resp = client.post(
            "/api/findings/batch-review",
            json={
                "finding_ids": fids,
                "decision": "human_confirmed",
                "reason_code": "other",
                "note": "批量确认",
            },
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["updated"] == 5
        assert data["skipped"] == 0
        # Each finding should be confirmed
        findings = client.get(f"/api/jobs/{job_id}/findings", headers=headers).json()
        assert all(f["human_status"] == "human_confirmed" for f in findings)


def test_batch_review_skips_unknown_ids(tmp_path, monkeypatch):
    db, client = _app_client(tmp_path, monkeypatch)
    job_id, fids = _seed_findings(db, count=3)
    with client:
        headers = login(client, "reviewer", "ReviewPass123!")["headers"]
        resp = client.post(
            "/api/findings/batch-review",
            json={
                "finding_ids": fids + [999999],
                "decision": "human_dismissed",
                "reason_code": "joint_issuance",
                "note": "联合发文",
            },
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["updated"] == 3
        assert data["skipped"] == 1


def test_batch_review_viewer_forbidden(tmp_path, monkeypatch):
    db, client = _app_client(tmp_path, monkeypatch)
    _job_id, fids = _seed_findings(db, count=2)
    with client:
        headers = login(client, "viewer", "ViewerPass123!")["headers"]
        resp = client.post(
            "/api/findings/batch-review",
            json={"finding_ids": fids, "decision": "human_confirmed"},
            headers=headers,
        )
        assert resp.status_code == 403


def test_cluster_findings_by_rule(tmp_path, monkeypatch):
    db, _client = _app_client(tmp_path, monkeypatch)
    job_id, _ = _seed_findings(db, count=3, rule_code="META-001")
    # add different rule
    repo = Repository(db)
    repo.save_record(
        job_id,
        PolicyRecord("普陀区", "doc", "https://example.test/doc"),
        [Finding("DOC-001", "文号", "high", "confirmed", "x", machine_status="confirmed_by_rule", allow_customer_output=True)],
    )
    groups = cluster_findings(db, job_id=job_id, dimension="rule_code")
    assert "META-001" in groups
    assert "DOC-001" in groups
    assert len(groups["META-001"]) == 3
    assert len(groups["DOC-001"]) == 1


def test_cluster_invalid_dimension(tmp_path, monkeypatch):
    db, _client = _app_client(tmp_path, monkeypatch)
    job_id, _ = _seed_findings(db, count=2)
    with pytest.raises(ValueError):
        cluster_findings(db, job_id=job_id, dimension="invalid")


# ---------- Dual-person review ----------


def _switch_user(client, username: str, password: str) -> dict:
    """Re-login as a different user and return fresh headers (CSRF matches new cookie)."""
    return login(client, username, password)["headers"]


def test_dual_review_agree(tmp_path, monkeypatch):
    db, client = _app_client(tmp_path, monkeypatch)
    job_id, fids = _seed_findings(db, count=1)
    fid = fids[0]
    reviewer_id = _user_id(db, "reviewer")
    admin_id = _user_id(db, "admin")
    with client:
        # Assign dual reviewers (reviewer + admin) as reviewer
        h_r = _switch_user(client, "reviewer", "ReviewPass123!")
        resp = client.post(
            "/api/findings/dual-assign",
            json={
                "finding_ids": [fid],
                "first_reviewer_id": reviewer_id,
                "first_reviewer_username": "reviewer",
                "second_reviewer_id": admin_id,
                "second_reviewer_username": "admin",
            },
            headers=h_r,
        )
        assert resp.status_code == 200
        assert resp.json()["assigned"] == 1
        # First reviewer (round 1) confirms
        h_r = _switch_user(client, "reviewer", "ReviewPass123!")
        r1 = client.post(
            f"/api/findings/{fid}/dual-review",
            json={"round": 1, "decision": "human_confirmed", "reason_code": "other"},
            headers=h_r,
        )
        assert r1.status_code == 200, r1.text
        assert r1.json()["consistency"] == "pending"
        # Second reviewer (round 2) confirms → agree
        h_a = _switch_user(client, "admin", "AdminPass123!")
        r2 = client.post(
            f"/api/findings/{fid}/dual-review",
            json={"round": 2, "decision": "human_confirmed", "reason_code": "other"},
            headers=h_a,
        )
        assert r2.status_code == 200, r2.text
        assert r2.json()["consistency"] == "agree"
        assert r2.json()["arbitration_status"] == "single"
        # Finding should be confirmed
        h_r = _switch_user(client, "reviewer", "ReviewPass123!")
        finding = client.get(f"/api/findings/{fid}/evidence", headers=h_r).json()
        assert finding["human_status"] == "human_confirmed"


def test_dual_review_disagree_then_arbitrate(tmp_path, monkeypatch):
    db, client = _app_client(tmp_path, monkeypatch)
    job_id, fids = _seed_findings(db, count=1)
    fid = fids[0]
    reviewer_id = _user_id(db, "reviewer")
    admin_id = _user_id(db, "admin")
    with client:
        h_r = _switch_user(client, "reviewer", "ReviewPass123!")
        client.post(
            "/api/findings/dual-assign",
            json={
                "finding_ids": [fid],
                "first_reviewer_id": reviewer_id,
                "first_reviewer_username": "reviewer",
                "second_reviewer_id": admin_id,
                "second_reviewer_username": "admin",
            },
            headers=h_r,
        )
        # Round 1: reviewer confirms
        h_r = _switch_user(client, "reviewer", "ReviewPass123!")
        client.post(
            f"/api/findings/{fid}/dual-review",
            json={"round": 1, "decision": "human_confirmed"},
            headers=h_r,
        )
        # Round 2: admin disagrees → disagree
        h_a = _switch_user(client, "admin", "AdminPass123!")
        r2 = client.post(
            f"/api/findings/{fid}/dual-review",
            json={"round": 2, "decision": "human_dismissed", "reason_code": "joint_issuance"},
            headers=h_a,
        )
        assert r2.status_code == 200, r2.text
        assert r2.json()["consistency"] == "disagree"
        assert r2.json()["arbitration_status"] == "pending"
        # Arbitrate (admin as arbitrator)
        h_a = _switch_user(client, "admin", "AdminPass123!")
        arb = client.post(
            f"/api/findings/{fid}/arbitrate",
            json={"decision": "human_confirmed", "reason_code": "other", "note": "仲裁确认"},
            headers=h_a,
        )
        assert arb.status_code == 200, arb.text
        assert arb.json()["arbitration_status"] == "resolved"
        assert arb.json()["consistency"] == "arbitrated"
        # Consistency rate
        rate = client.get(f"/api/consistency-rate?job_id={job_id}").json()
        assert rate["disagree"] == 0  # disagreement was arbitrated
        assert rate["arbitrated"] == 1


def test_dual_review_same_reviewer_rejected(tmp_path, monkeypatch):
    db, _client = _app_client(tmp_path, monkeypatch)
    _job_id, fids = _seed_findings(db, count=1)
    with pytest.raises(ValueError, match="不能为同一人"):
        assign_dual_review(
            db,
            finding_ids=fids,
            first_reviewer_id=1,
            first_reviewer_username="admin",
            second_reviewer_id=1,
            second_reviewer_username="admin",
        )


def test_arbitrate_without_disagreement_fails(tmp_path, monkeypatch):
    db, _client = _app_client(tmp_path, monkeypatch)
    job_id, fids = _seed_findings(db, count=1)
    fid = fids[0]
    reviewer_id = _user_id(db, "reviewer")
    admin_id = _user_id(db, "admin")
    assign_dual_review(
        db,
        finding_ids=[fid],
        first_reviewer_id=reviewer_id,
        first_reviewer_username="reviewer",
        second_reviewer_id=admin_id,
        second_reviewer_username="admin",
    )
    submit_dual_review(
        db, finding_id=fid, reviewer_id=reviewer_id, reviewer_username="reviewer",
        round_num=1, decision="human_confirmed",
    )
    # Only one reviewer submitted, not in disagreement
    with pytest.raises(ValueError, match="未处于争议状态"):
        arbitrate(
            db, finding_id=fid, arbitrator_id=admin_id, arbitrator_username="admin",
            decision="human_confirmed",
        )


# ---------- Rule effect center ----------


def test_rule_effect_computes_precision(tmp_path, monkeypatch):
    db, _client = _app_client(tmp_path, monkeypatch)
    job_id, fids = _seed_findings(db, count=10, rule_code="META-001")
    # Confirm 8, dismiss 2
    batch_review(
        db, finding_ids=fids[:8], decision="human_confirmed",
        reviewer_id=1, reviewer_username="admin",
    )
    batch_review(
        db, finding_ids=fids[8:], decision="human_dismissed",
        reason_code="rule_not_applicable",
        reviewer_id=1, reviewer_username="admin",
    )
    stats = compute_rule_effectiveness(db, job_id=job_id)
    rule = [s for s in stats if s["rule_code"] == "META-001"][0]
    assert rule["trigger_count"] == 10
    assert rule["reviewed_count"] == 10
    assert rule["confirmed_count"] == 8
    assert rule["dismissed_count"] == 2
    assert rule["precision"] == 0.8
    assert rule["false_positive_rate"] == 0.2
    assert any(r["reason_code"] == "rule_not_applicable" for r in rule["top_dismiss_reasons"])


def test_rule_effect_endpoint_requires_login(tmp_path, monkeypatch):
    _db, client = _app_client(tmp_path, monkeypatch)
    with client:
        resp = client.get("/api/rule-effect")
        assert resp.status_code == 401


def test_rule_effect_snapshot(tmp_path, monkeypatch):
    db, client = _app_client(tmp_path, monkeypatch)
    job_id, fids = _seed_findings(db, count=5, rule_code="DATE-001")
    batch_review(db, finding_ids=fids[:4], decision="human_confirmed",
                 reviewer_id=1, reviewer_username="admin")
    with client:
        headers = login(client, "reviewer", "ReviewPass123!")["headers"]
        resp = client.post(f"/api/rule-effect/snapshot?job_id={job_id}&snapshot_kind=live", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["rules"] >= 1


# ---------- Rule degradation ----------


def test_rule_degradation_marks_low_precision(tmp_path, monkeypatch):
    db, _client = _app_client(tmp_path, monkeypatch)
    job_id, fids = _seed_findings(db, count=20, rule_code="META-001")
    # Confirm 5, dismiss 15 → precision 0.25 < 0.95 → degrade
    batch_review(db, finding_ids=fids[:5], decision="human_confirmed",
                 reviewer_id=1, reviewer_username="admin")
    batch_review(db, finding_ids=fids[5:], decision="human_dismissed",
                 reason_code="rule_not_applicable",
                 reviewer_id=1, reviewer_username="admin")
    result = evaluate_rule_degradation(db, job_id=job_id)
    meta_verdict = [v for v in result["verdicts"] if v["rule_code"] == "META-001"][0]
    assert meta_verdict["action"] == "degraded"
    assert meta_verdict["degraded"] is True
    # Verify rule_versions row updated
    with db.connect() as conn:
        rv = conn.execute(
            "SELECT degraded, auto_confirm_allowed, allow_customer_output FROM rule_versions "
            "WHERE rule_id=? ORDER BY id DESC LIMIT 1",
            ("META-001",),
        ).fetchone()
    assert int(rv["degraded"]) == 1
    assert int(rv["auto_confirm_allowed"]) == 0
    assert int(rv["allow_customer_output"]) == 0


def test_rule_degradation_restores_high_precision(tmp_path, monkeypatch):
    db, _client = _app_client(tmp_path, monkeypatch)
    job_id, fids = _seed_findings(db, count=20, rule_code="DATE-001")
    # Confirm 19, dismiss 1 → precision 0.95 → not degraded
    batch_review(db, finding_ids=fids[:19], decision="human_confirmed",
                 reviewer_id=1, reviewer_username="admin")
    batch_review(db, finding_ids=fids[19:], decision="human_dismissed",
                 reason_code="other",
                 reviewer_id=1, reviewer_username="admin")
    result = evaluate_rule_degradation(db, job_id=job_id)
    date_verdict = [v for v in result["verdicts"] if v["rule_code"] == "DATE-001"][0]
    assert date_verdict["degraded"] is False
    assert date_verdict["action"] == "no_change"


# ---------- 1,000 sample evaluation ----------


def test_generate_1000_sample_evaluation(tmp_path, monkeypatch):
    """M2 gate: generate 1,000 labeled samples and compute rule effectiveness.

    Per V2.0/V3.0 sample allocation:
    - 机构与机关代字 300, 日期 200, 文号 200, 元数据 100, 链接和附件 200
    Uses proxy labels derived from known rule precision heuristics (R-007).
    """
    from scripts.generate_m2_samples import generate_samples, evaluate_samples

    db, _client = _app_client(tmp_path, monkeypatch)
    job_id = db.create_job(["普陀区"], "full", {}, 1)
    repo = Repository(db)
    samples = generate_samples(seed=42)
    assert len(samples) == 1000
    counts: dict[str, int] = {}
    for s in samples:
        counts[s["rule_code"]] = counts.get(s["rule_code"], 0) + 1
    assert counts["AGENCY-001"] == 300
    assert counts["DATE-001"] == 200
    assert counts["DOC-001"] == 200
    assert counts["META-001"] == 100
    assert counts["LINK-001"] == 200
    # Save one finding per sample, track expected label
    confirmed_ids: list[int] = []
    dismissed_ids: list[int] = []
    for idx, s in enumerate(samples):
        fid = repo.save_record(
            job_id,
            PolicyRecord("普陀区", s["title"], f"https://example.test/sample-{idx}"),
            [Finding(s["rule_code"], "样本", "high", "confirmed", "证据",
                     machine_status=s["machine_status"], allow_customer_output=s["allow_customer_output"],
                     rule_version="1.0.0")],
        )
        if s["expected_label"] == "confirmed":
            confirmed_ids.append(fid)
        else:
            dismissed_ids.append(fid)
    # Apply proxy labels as human reviews
    batch_review(db, finding_ids=confirmed_ids, decision="human_confirmed",
                 reviewer_id=1, reviewer_username="admin")
    batch_review(db, finding_ids=dismissed_ids, decision="human_dismissed",
                 reason_code="rule_not_applicable",
                 reviewer_id=1, reviewer_username="admin")
    # Evaluate
    result = evaluate_samples(db, job_id=job_id)
    assert result["total_samples"] == 1000
    assert result["reviewed_count"] == 1000
    for rule_stat in result["rule_stats"]:
        assert 0.0 <= rule_stat["precision"] <= 1.0
    snapshot = snapshot_rule_effectiveness(db, job_id=job_id, snapshot_kind="evaluation")
    assert snapshot["rules"] >= 5
