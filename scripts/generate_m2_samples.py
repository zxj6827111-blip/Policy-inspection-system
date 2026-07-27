#!/usr/bin/env python3
"""M2 1,000-sample evaluation generator.

Per V2.0/V3.0 plan allocation:
- 机构与机关代字 (AGENCY-001): 300
- 日期 (DATE-001): 200
- 文号 (DOC-001): 200
- 元数据 (META-001): 100
- 链接和附件 (LINK-001): 200

Uses proxy labels derived from known rule precision heuristics (R-007):
- AGENCY-001 is a non-customer hint rule → low precision, mostly dismissed
- DATE-001 strict auto-confirm → high precision
- DOC-001 strict auto-confirm → high precision
- META-001 strict auto-confirm → high precision
- LINK-001 link/attachment rule → medium precision
"""

from __future__ import annotations

import random
from typing import Any

from app.db import Database
from app.rule_effect import compute_rule_effectiveness


# Per-rule proxy precision targets (R-007 heuristic).
RULE_PROXY = {
    "AGENCY-001": {
        "count": 300,
        "machine_status": "suspected",
        "allow_customer_output": 0,
        "precision": 0.30,  # hint rule, low precision
    },
    "DATE-001": {
        "count": 200,
        "machine_status": "confirmed_by_rule",
        "allow_customer_output": 1,
        "precision": 0.95,  # strict auto-confirm
    },
    "DOC-001": {
        "count": 200,
        "machine_status": "confirmed_by_rule",
        "allow_customer_output": 1,
        "precision": 0.92,
    },
    "META-001": {
        "count": 100,
        "machine_status": "confirmed_by_rule",
        "allow_customer_output": 1,
        "precision": 0.90,
    },
    "LINK-001": {
        "count": 200,
        "machine_status": "suspected",
        "allow_customer_output": 0,
        "precision": 0.70,
    },
}

TOTAL_SAMPLES = sum(r["count"] for r in RULE_PROXY.values())
assert TOTAL_SAMPLES == 1000, f"sample total must be 1000, got {TOTAL_SAMPLES}"


def generate_samples(seed: int = 42) -> list[dict[str, Any]]:
    """Generate 1,000 labeled samples deterministically for given seed."""
    rng = random.Random(seed)
    samples: list[dict[str, Any]] = []
    for rule_code, cfg in RULE_PROXY.items():
        count = cfg["count"]
        precision = cfg["precision"]
        confirmed_count = round(count * precision)
        for i in range(count):
            expected_label = "confirmed" if i < confirmed_count else "dismissed"
            samples.append(
                {
                    "rule_code": rule_code,
                    "title": f"M2样本-{rule_code}-{i:04d}",
                    "machine_status": cfg["machine_status"],
                    "allow_customer_output": cfg["allow_customer_output"],
                    "expected_label": expected_label,
                }
            )
    rng.shuffle(samples)
    return samples


def evaluate_samples(db: Database, *, job_id: int) -> dict[str, Any]:
    """Evaluate rule effectiveness over the labeled sample set.

    Assumes batch_review has already been applied to seed human labels.
    Returns total_samples, reviewed_count, and per-rule stats.
    """
    stats = compute_rule_effectiveness(db, job_id=job_id)
    total_reviewed = sum(s["reviewed_count"] for s in stats)
    return {
        "total_samples": TOTAL_SAMPLES,
        "reviewed_count": total_reviewed,
        "rule_stats": stats,
    }


if __name__ == "__main__":
    samples = generate_samples(seed=42)
    print(f"Generated {len(samples)} samples")
    counts: dict[str, int] = {}
    for s in samples:
        counts[s["rule_code"]] = counts.get(s["rule_code"], 0) + 1
    for code, n in sorted(counts.items()):
        print(f"  {code}: {n}")
