"""M2 batch review service: batch confirm/dismiss with structured reasons and clustering."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.db import Database, utc_now
from app.findings_model import (
    HUMAN_CONFIRMED,
    HUMAN_DISMISSED,
    legacy_review_status,
)
from app.repository import Repository


@dataclass
class BatchReviewResult:
    total: int
    updated: int
    skipped: int
    errors: list[dict[str, Any]]


def batch_review(
    db: Database,
    *,
    finding_ids: list[int],
    decision: str,
    reason_code: str = "other",
    note: str = "",
    reviewer_id: int,
    reviewer_username: str,
    review_round: int = 1,
) -> BatchReviewResult:
    """Apply the same review decision to many findings.

    - decision: human_confirmed | human_dismissed | needs_more_evidence
    - Appends a finding_reviews row per finding (append-only).
    - Updates review_decisions + rule_findings.human_status/review_status.
    - Skips ids that do not exist.
    """
    from app.findings_model import decision_to_human

    human = decision_to_human(decision)
    legacy = {
        HUMAN_CONFIRMED: "confirmed",
        HUMAN_DISMISSED: "dismissed",
        "needs_more_evidence": "pending",
        "unreviewed": "pending",
    }[human]
    now = utc_now()
    updated = 0
    skipped = 0
    errors: list[dict[str, Any]] = []
    with db.connect() as conn:
        for fid in finding_ids:
            row = conn.execute(
                "SELECT id, machine_status, human_status, rule_code FROM rule_findings WHERE id=?",
                (fid,),
            ).fetchone()
            if not row:
                skipped += 1
                errors.append({"finding_id": fid, "reason": "not_found"})
                continue
            machine = row["machine_status"] or "suspected"
            review_status = legacy_review_status(machine, human)
            conn.execute(
                """INSERT INTO review_decisions(finding_id, decision, note, reviewed_at)
                VALUES(?,?,?,?)
                ON CONFLICT(finding_id) DO UPDATE SET decision=excluded.decision,
                note=excluded.note, reviewed_at=excluded.reviewed_at""",
                (fid, legacy, note, now),
            )
            prev = conn.execute(
                "SELECT id FROM finding_reviews WHERE finding_id=? ORDER BY id DESC LIMIT 1",
                (fid,),
            ).fetchone()
            conn.execute(
                """INSERT INTO finding_reviews(
                    finding_id, reviewer_id, reviewer_username, decision, reason_code,
                    comment, created_at, supersedes_review_id, review_round
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    fid,
                    reviewer_id,
                    reviewer_username,
                    human,
                    reason_code,
                    note,
                    now,
                    int(prev["id"]) if prev else None,
                    review_round,
                ),
            )
            conn.execute(
                "UPDATE rule_findings SET human_status=?, review_status=? WHERE id=?",
                (human, review_status, fid),
            )
            updated += 1
    return BatchReviewResult(
        total=len(finding_ids), updated=updated, skipped=skipped, errors=errors
    )


def cluster_findings(
    db: Database,
    *,
    job_id: int,
    dimension: str = "rule_code",
) -> dict[str, list[int]]:
    """Cluster finding ids by the given dimension.

    dimension ∈ {rule_code, district, source_site, document_number_prefix}
    Used by batch review UI to select groups of similar findings.
    """
    valid = {"rule_code", "district", "source_site", "document_number_prefix"}
    if dimension not in valid:
        raise ValueError(f"无效聚类维度: {dimension}; 可选: {sorted(valid)}")
    groups: dict[str, list[int]] = {}
    with db.connect() as conn:
        if dimension == "rule_code":
            rows = conn.execute(
                """SELECT f.id, f.rule_code FROM rule_findings f
                WHERE f.job_id=? AND COALESCE(f.is_current,1)=1 ORDER BY f.rule_code""",
                (job_id,),
            ).fetchall()
            for r in rows:
                groups.setdefault(r["rule_code"] or "unknown", []).append(int(r["id"]))
        elif dimension == "district":
            rows = conn.execute(
                """SELECT f.id, d.district FROM rule_findings f
                JOIN policy_documents d ON d.id=f.document_id
                WHERE f.job_id=? AND COALESCE(f.is_current,1)=1 ORDER BY d.district""",
                (job_id,),
            ).fetchall()
            for r in rows:
                groups.setdefault(r["district"] or "unknown", []).append(int(r["id"]))
        elif dimension == "source_site":
            rows = conn.execute(
                """SELECT f.id, d.source_site FROM rule_findings f
                JOIN policy_documents d ON d.id=f.document_id
                WHERE f.job_id=? AND COALESCE(f.is_current,1)=1 ORDER BY d.source_site""",
                (job_id,),
            ).fetchall()
            for r in rows:
                groups.setdefault(r["source_site"] or "unknown", []).append(int(r["id"]))
        else:  # document_number_prefix
            rows = conn.execute(
                """SELECT f.id, d.page_document_number FROM rule_findings f
                JOIN policy_documents d ON d.id=f.document_id
                WHERE f.job_id=? AND COALESCE(f.is_current,1)=1""",
                (job_id,),
            ).fetchall()
            for r in rows:
                num = (r["page_document_number"] or "")
                prefix = num[: num.find("〔") + 1] if "〔" in num else (num[:6] if num else "unknown")
                groups.setdefault(prefix or "unknown", []).append(int(r["id"]))
    return groups
