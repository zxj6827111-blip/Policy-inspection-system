"""M2 dual-person review and arbitration service.

Two independent reviewers (first + second) review the same finding. When
their decisions agree the finding is settled. When they disagree an
arbitrator resolves the conflict. Consistency rate is tracked per rule
and per reviewer pair for the rule effect center.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.db import Database, utc_now
from app.findings_model import (
    HUMAN_CONFIRMED,
    HUMAN_DISMISSED,
    HUMAN_NEEDS_MORE,
    decision_to_human,
    legacy_review_status,
)


@dataclass
class DualReviewAssignment:
    finding_id: int
    job_id: int
    first_reviewer_username: str
    second_reviewer_username: str
    arbitration_status: str
    consistency: str
    first_decision: str | None
    second_decision: str | None
    arbitration_decision: str | None


def assign_dual_review(
    db: Database,
    *,
    finding_ids: list[int],
    first_reviewer_id: int,
    first_reviewer_username: str,
    second_reviewer_id: int,
    second_reviewer_username: str,
) -> int:
    """Assign two reviewers to each finding. Returns number newly assigned."""
    if first_reviewer_id == second_reviewer_id:
        raise ValueError("第一复核员与第二复核员不能为同一人")
    now = utc_now()
    inserted = 0
    with db.connect() as conn:
        for fid in finding_ids:
            finding = conn.execute(
                "SELECT id, job_id FROM rule_findings WHERE id=?", (fid,)
            ).fetchone()
            if not finding:
                continue
            existing = conn.execute(
                "SELECT id FROM review_assignments WHERE finding_id=?", (fid,)
            ).fetchone()
            if existing:
                continue
            conn.execute(
                """INSERT INTO review_assignments(
                    finding_id, job_id, first_reviewer_id, first_reviewer_username,
                    second_reviewer_id, second_reviewer_username,
                    arbitration_status, consistency, assigned_at
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    fid,
                    int(finding["job_id"]),
                    first_reviewer_id,
                    first_reviewer_username,
                    second_reviewer_id,
                    second_reviewer_username,
                    "pending",
                    "pending",
                    now,
                ),
            )
            inserted += 1
    return inserted


def submit_dual_review(
    db: Database,
    *,
    finding_id: int,
    reviewer_id: int,
    reviewer_username: str,
    round_num: int,
    decision: str,
    reason_code: str = "other",
    note: str = "",
) -> dict[str, Any]:
    """Submit one reviewer's decision (round 1 or 2). Updates consistency when both done."""
    if round_num not in (1, 2):
        raise ValueError("复核轮次必须是 1 或 2")
    human = decision_to_human(decision)
    now = utc_now()
    with db.connect() as conn:
        assignment = conn.execute(
            "SELECT * FROM review_assignments WHERE finding_id=?", (finding_id,)
        ).fetchone()
        if not assignment:
            raise ValueError("该问题未分配双人复核任务")
        # Validate reviewer matches the round slot
        if round_num == 1 and assignment["first_reviewer_id"] != reviewer_id:
            raise ValueError("当前用户不是该问题的第一复核员")
        if round_num == 2 and assignment["second_reviewer_id"] != reviewer_id:
            raise ValueError("当前用户不是该问题的第二复核员")
        # Append review row
        prev = conn.execute(
            "SELECT id FROM finding_reviews WHERE finding_id=? ORDER BY id DESC LIMIT 1",
            (finding_id,),
        ).fetchone()
        conn.execute(
            """INSERT INTO finding_reviews(
                finding_id, reviewer_id, reviewer_username, decision, reason_code,
                comment, created_at, supersedes_review_id, review_round
            ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                finding_id,
                reviewer_id,
                reviewer_username,
                human,
                reason_code,
                note,
                now,
                int(prev["id"]) if prev else None,
                round_num,
            ),
        )
        # Check if both reviewers have submitted (one row per round)
        round1 = conn.execute(
            "SELECT decision FROM finding_reviews WHERE finding_id=? AND review_round=1 "
            "ORDER BY id DESC LIMIT 1",
            (finding_id,),
        ).fetchone()
        round2 = conn.execute(
            "SELECT decision FROM finding_reviews WHERE finding_id=? AND review_round=2 "
            "ORDER BY id DESC LIMIT 1",
            (finding_id,),
        ).fetchone()
        result = {
            "finding_id": finding_id,
            "round": round_num,
            "decision": human,
            "consistency": "pending",
            "arbitration_status": str(assignment["arbitration_status"]),
        }
        if round1 and round2:
            d1 = round1["decision"]
            d2 = round2["decision"]
            if d1 == d2:
                consistency = "agree"
                arb_status = "single"
                # Apply the agreed decision to the finding
                legacy = {
                    HUMAN_CONFIRMED: "confirmed",
                    HUMAN_DISMISSED: "dismissed",
                    HUMAN_NEEDS_MORE: "pending",
                }[human]
                machine = (
                    conn.execute(
                        "SELECT machine_status FROM rule_findings WHERE id=?", (finding_id,)
                    ).fetchone()["machine_status"]
                    or "suspected"
                )
                review_status = legacy_review_status(machine, human)
                conn.execute(
                    """INSERT INTO review_decisions(finding_id, decision, note, reviewed_at)
                    VALUES(?,?,?,?) ON CONFLICT(finding_id) DO UPDATE SET decision=excluded.decision,
                    note=excluded.note, reviewed_at=excluded.reviewed_at""",
                    (finding_id, legacy, note, now),
                )
                conn.execute(
                    "UPDATE rule_findings SET human_status=?, review_status=? WHERE id=?",
                    (human, review_status, finding_id),
                )
            else:
                consistency = "disagree"
                arb_status = "pending"
            conn.execute(
                "UPDATE review_assignments SET consistency=?, arbitration_status=?, resolved_at=? WHERE finding_id=?",
                (
                    consistency,
                    arb_status if consistency == "agree" else "pending",
                    now if consistency == "agree" else None,
                    finding_id,
                ),
            )
            result["consistency"] = consistency
            result["arbitration_status"] = arb_status if consistency == "agree" else "pending"
        return result


def arbitrate(
    db: Database,
    *,
    finding_id: int,
    arbitrator_id: int,
    arbitrator_username: str,
    decision: str,
    reason_code: str = "other",
    note: str = "",
) -> dict[str, Any]:
    """Arbitrator resolves a disagreement. Final decision applied to the finding."""
    human = decision_to_human(decision)
    now = utc_now()
    with db.connect() as conn:
        assignment = conn.execute(
            "SELECT * FROM review_assignments WHERE finding_id=?", (finding_id,)
        ).fetchone()
        if not assignment:
            raise ValueError("该问题未分配双人复核任务")
        if assignment["consistency"] != "disagree":
            raise ValueError("该问题未处于争议状态，无需仲裁")
        prev = conn.execute(
            "SELECT id FROM finding_reviews WHERE finding_id=? ORDER BY id DESC LIMIT 1",
            (finding_id,),
        ).fetchone()
        conn.execute(
            """INSERT INTO finding_reviews(
                finding_id, reviewer_id, reviewer_username, decision, reason_code,
                comment, created_at, supersedes_review_id, review_round
            ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                finding_id,
                arbitrator_id,
                arbitrator_username,
                human,
                reason_code,
                note,
                now,
                int(prev["id"]) if prev else None,
                3,  # arbitration round
            ),
        )
        legacy = {
            HUMAN_CONFIRMED: "confirmed",
            HUMAN_DISMISSED: "dismissed",
            HUMAN_NEEDS_MORE: "pending",
        }[human]
        machine = (
            conn.execute(
                "SELECT machine_status FROM rule_findings WHERE id=?", (finding_id,)
            ).fetchone()["machine_status"]
            or "suspected"
        )
        review_status = legacy_review_status(machine, human)
        conn.execute(
            """INSERT INTO review_decisions(finding_id, decision, note, reviewed_at)
            VALUES(?,?,?,?) ON CONFLICT(finding_id) DO UPDATE SET decision=excluded.decision,
            note=excluded.note, reviewed_at=excluded.reviewed_at""",
            (finding_id, legacy, note, now),
        )
        conn.execute(
            "UPDATE rule_findings SET human_status=?, review_status=? WHERE id=?",
            (human, review_status, finding_id),
        )
        conn.execute(
            """UPDATE review_assignments
            SET arbitration_status='resolved', arbitrator_id=?, arbitrator_username=?,
                arbitration_decision=?, consistency='arbitrated', resolved_at=?
            WHERE finding_id=?""",
            (arbitrator_id, arbitrator_username, human, now, finding_id),
        )
    return {
        "finding_id": finding_id,
        "decision": human,
        "arbitration_status": "resolved",
        "consistency": "arbitrated",
        "arbitrator": arbitrator_username,
    }


def list_dual_assignments(db: Database, *, job_id: int) -> list[dict[str, Any]]:
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT ra.*,
            (SELECT decision FROM finding_reviews fr
             WHERE fr.finding_id=ra.finding_id AND fr.review_round=1
             ORDER BY fr.id DESC LIMIT 1) AS first_decision,
            (SELECT decision FROM finding_reviews fr
             WHERE fr.finding_id=ra.finding_id AND fr.review_round=2
             ORDER BY fr.id DESC LIMIT 1) AS second_decision
            FROM review_assignments ra WHERE ra.job_id=? ORDER BY ra.id""",
            (job_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def compute_consistency_rate(db: Database, *, job_id: int | None = None) -> dict[str, Any]:
    """Overall consistency: agree / (agree + disagree + arbitrated)."""
    with db.connect() as conn:
        if job_id is not None:
            rows = conn.execute(
                "SELECT consistency, COUNT(*) c FROM review_assignments WHERE job_id=? GROUP BY consistency",
                (job_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT consistency, COUNT(*) c FROM review_assignments GROUP BY consistency"
            ).fetchall()
    counts = {r["consistency"]: int(r["c"]) for r in rows}
    agree = counts.get("agree", 0)
    disagree = counts.get("disagree", 0)
    arbitrated = counts.get("arbitrated", 0)
    settled = agree + disagree + arbitrated
    return {
        "agree": agree,
        "disagree": disagree,
        "arbitrated": arbitrated,
        "pending": counts.get("pending", 0),
        "settled": settled,
        "consistency_rate": (agree / settled) if settled else 0.0,
    }
