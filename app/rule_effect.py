"""M2 rule effect center: per-rule precision, false positive rate, top dismissal reasons.

Computes effectiveness statistics from rule_findings + finding_reviews.
Optionally persists a snapshot for trend tracking.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from app.db import Database, utc_now
from app.findings_model import (
    HUMAN_CONFIRMED,
    HUMAN_DISMISSED,
    HUMAN_NEEDS_MORE,
    HUMAN_UNREVIEWED,
)


def compute_rule_effectiveness(db: Database, *, job_id: int | None = None) -> list[dict[str, Any]]:
    """Compute per-rule effectiveness statistics.

    precision = confirmed / reviewed (reviewed = confirmed + dismissed)
    false_positive_rate = dismissed / reviewed
    A finding is "reviewed" when human_status != unreviewed.
    """
    with db.connect() as conn:
        params: tuple[Any, ...] = ()
        where = "WHERE COALESCE(f.is_current,1)=1"
        if job_id is not None:
            where += " AND f.job_id=?"
            params = (job_id,)
        rows = conn.execute(
            f"""SELECT f.rule_code, f.machine_status, f.human_status, f.rule_version,
            f.allow_customer_output,
            (SELECT fr.reason_code FROM finding_reviews fr
             WHERE fr.finding_id=f.id ORDER BY fr.id DESC LIMIT 1) AS last_reason_code
            FROM rule_findings f {where}""",
            params,
        ).fetchall()
        # Group by rule_code
        per_rule: dict[str, dict[str, Any]] = {}
        for r in rows:
            code = r["rule_code"]
            bucket = per_rule.setdefault(
                code,
                {
                    "rule_code": code,
                    "rule_version": r["rule_version"] or "",
                    "trigger_count": 0,
                    "reviewed_count": 0,
                    "confirmed_count": 0,
                    "dismissed_count": 0,
                    "needs_more_count": 0,
                    "unreviewed_count": 0,
                    "dismiss_reasons": Counter(),
                    "allow_customer_output": int(r["allow_customer_output"] or 0),
                },
            )
            bucket["trigger_count"] += 1
            human = r["human_status"] or HUMAN_UNREVIEWED
            if human == HUMAN_UNREVIEWED:
                bucket["unreviewed_count"] += 1
                continue
            bucket["reviewed_count"] += 1
            if human == HUMAN_CONFIRMED:
                bucket["confirmed_count"] += 1
            elif human == HUMAN_DISMISSED:
                bucket["dismissed_count"] += 1
                if r["last_reason_code"]:
                    bucket["dismiss_reasons"][r["last_reason_code"]] += 1
            elif human == HUMAN_NEEDS_MORE:
                bucket["needs_more_count"] += 1
        # Finalize
        results: list[dict[str, Any]] = []
        for code, b in sorted(per_rule.items()):
            reviewed = b["confirmed_count"] + b["dismissed_count"]
            precision = (b["confirmed_count"] / reviewed) if reviewed else 0.0
            fpr = (b["dismissed_count"] / reviewed) if reviewed else 0.0
            top_reasons = [
                {"reason_code": rc, "count": cnt}
                for rc, cnt in b["dismiss_reasons"].most_common(5)
            ]
            results.append(
                {
                    "rule_code": code,
                    "rule_version": b["rule_version"],
                    "trigger_count": b["trigger_count"],
                    "reviewed_count": reviewed + b["needs_more_count"],
                    "confirmed_count": b["confirmed_count"],
                    "dismissed_count": b["dismissed_count"],
                    "needs_more_count": b["needs_more_count"],
                    "unreviewed_count": b["unreviewed_count"],
                    "precision": round(precision, 4),
                    "false_positive_rate": round(fpr, 4),
                    "top_dismiss_reasons": top_reasons,
                    "allow_customer_output": b["allow_customer_output"],
                }
            )
    return results


def snapshot_rule_effectiveness(
    db: Database, *, job_id: int | None = None, snapshot_kind: str = "live"
) -> dict[str, Any]:
    """Persist current effectiveness into rule_effect_snapshots."""
    stats = compute_rule_effectiveness(db, job_id=job_id)
    now = utc_now()
    with db.connect() as conn:
        for s in stats:
            conn.execute(
                """INSERT OR REPLACE INTO rule_effect_snapshots(
                    snapshot_at, job_id, rule_code, rule_version,
                    trigger_count, reviewed_count, confirmed_count, dismissed_count, needs_more_count,
                    precision_value, false_positive_rate, top_dismiss_reasons_json,
                    auto_confirm_allowed, customer_output_allowed, degraded, sample_size, snapshot_kind
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    now,
                    job_id,
                    s["rule_code"],
                    s["rule_version"],
                    s["trigger_count"],
                    s["reviewed_count"],
                    s["confirmed_count"],
                    s["dismissed_count"],
                    s["needs_more_count"],
                    s["precision"],
                    s["false_positive_rate"],
                    json.dumps(s["top_dismiss_reasons"], ensure_ascii=False),
                    0,
                    s["allow_customer_output"],
                    0,
                    s["reviewed_count"],
                    snapshot_kind,
                ),
            )
            # Pull auto_confirm_allowed/degraded from rule_versions for accuracy
            rv = conn.execute(
                "SELECT auto_confirm_allowed, degraded FROM rule_versions WHERE rule_id=? ORDER BY id DESC LIMIT 1",
                (s["rule_code"],),
            ).fetchone()
            if rv:
                conn.execute(
                    """UPDATE rule_effect_snapshots SET auto_confirm_allowed=?, degraded=?
                    WHERE snapshot_at=? AND rule_code=? AND snapshot_kind=?""",
                    (int(rv["auto_confirm_allowed"]), int(rv["degraded"]), now, s["rule_code"], snapshot_kind),
                )
    return {"snapshot_at": now, "rules": len(stats), "kind": snapshot_kind}
