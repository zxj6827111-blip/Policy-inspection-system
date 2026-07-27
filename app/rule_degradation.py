"""M2 rule degradation: downgrade rules whose precision fails thresholds.

Per V2.0/V3.0 plan gates:
- auto-confirm rules need precision >= 95%
- suspected rules need precision >= 80%
Rules that fail are marked degraded and lose auto_confirm_allowed + allow_customer_output.
"""

from __future__ import annotations

from typing import Any

from app.db import Database, utc_now
from app.migrations.m2 import (
    AUTO_CONFIRM_PRECISION_THRESHOLD,
    SUSPECTED_PRECISION_THRESHOLD,
)
from app.rule_effect import compute_rule_effectiveness


def evaluate_rule_degradation(db: Database, *, job_id: int | None = None) -> dict[str, Any]:
    """Evaluate and apply degradation. Returns per-rule verdicts.

    Rules are auto-confirm-eligible when allow_customer_output=1 and the rule
    is in STRICT_AUTO_CONFIRM_RULES. Suspected rules are the rest with
    machine_status starting with 'suspected' or 'hint'.
    """
    from app.findings_model import STRICT_AUTO_CONFIRM_RULES

    stats = compute_rule_effectiveness(db, job_id=job_id)
    now = utc_now()
    verdicts: list[dict[str, Any]] = []
    degraded_count = 0
    restored_count = 0
    with db.connect() as conn:
        for s in stats:
            code = s["rule_code"]
            reviewed = s["reviewed_count"]
            precision = s["precision"]
            is_auto = code in STRICT_AUTO_CONFIRM_RULES
            threshold = AUTO_CONFIRM_PRECISION_THRESHOLD if is_auto else SUSPECTED_PRECISION_THRESHOLD
            # Only grade rules that have enough reviews to be meaningful (>=10 reviewed)
            meets_threshold = precision >= threshold if reviewed >= 10 else True
            rv = conn.execute(
                "SELECT id, auto_confirm_allowed, degraded, allow_customer_output FROM rule_versions "
                "WHERE rule_id=? ORDER BY id DESC LIMIT 1",
                (code,),
            ).fetchone()
            if not rv:
                continue
            currently_degraded = int(rv["degraded"])
            currently_auto = int(rv["auto_confirm_allowed"])
            should_degrade = (not meets_threshold) and reviewed >= 10
            action = "no_change"
            if should_degrade and not currently_degraded:
                conn.execute(
                    "UPDATE rule_versions SET degraded=1, degraded_reason=?, degraded_at=?, "
                    "auto_confirm_allowed=0, allow_customer_output=0 WHERE id=?",
                    (
                        f"precision={precision:.2f} below threshold {threshold:.2f} (reviewed={reviewed})",
                        now,
                        int(rv["id"]),
                    ),
                )
                action = "degraded"
                degraded_count += 1
            elif (not should_degrade) and currently_degraded and reviewed >= 10:
                conn.execute(
                    "UPDATE rule_versions SET degraded=0, degraded_reason='', degraded_at=NULL WHERE id=?",
                    (int(rv["id"]),),
                )
                action = "restored"
                restored_count += 1
            verdicts.append(
                {
                    "rule_code": code,
                    "is_auto_confirm_rule": is_auto,
                    "threshold": threshold,
                    "precision": precision,
                    "reviewed_count": reviewed,
                    "meets_threshold": meets_threshold,
                    "degraded": should_degrade,
                    "action": action,
                }
            )
    return {
        "evaluated": len(verdicts),
        "degraded": degraded_count,
        "restored": restored_count,
        "verdicts": verdicts,
        "thresholds": {
            "auto_confirm": AUTO_CONFIRM_PRECISION_THRESHOLD,
            "suspected": SUSPECTED_PRECISION_THRESHOLD,
        },
    }
