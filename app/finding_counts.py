"""Finding count semantics shared by API, UI and Excel."""

from __future__ import annotations

from typing import Any, Mapping

from app.findings_model import (
    MACHINE_AUTO_EXCLUDED,
    MACHINE_COLLECTION_ERROR,
    MACHINE_CONFIRMED_BY_RULE,
    MACHINE_HINT,
    MACHINE_INSUFFICIENT,
    MACHINE_SUSPECTED,
    HUMAN_CONFIRMED,
    HUMAN_DISMISSED,
    HUMAN_UNREVIEWED,
    enrich_finding_row,
    is_customer_formal_problem,
)


COUNT_FIELD_HELP = {
    "finding_count": (
        "兼容字段：当前有效 finding 总数（含线索），不等于客户正式问题数。"
        "页面与 API 展示正式问题时必须使用 formal_finding_count。"
    ),
    "finding_total_count": "当前有效 finding 总数（is_current=1），含全部机器/人工状态与机构线索。",
    "formal_finding_count": (
        "客户正式问题数：human_confirmed（AGENCY-*/ORG-* 除外）；"
        "或 machine_status=confirmed_by_rule 且规则属于 "
        "META/DOC/DATE 严格自动确认白名单且 allow_customer_output=1。"
    ),
    "suspected_count": "机器状态为 suspected 的线索数。",
    "hint_count": "机器状态为 hint 的提示数。",
    "insufficient_data_count": "机器状态为 insufficient_data 的条数。",
    "collection_error_count": "机器状态为 collection_error 的条数。",
    "auto_excluded_count": "机器状态为 auto_excluded 的条数。",
    "human_dismissed_count": "人工状态为 human_dismissed 的条数。",
    "human_confirmed_count": "人工状态为 human_confirmed 的条数（含机构类人工确认，不等于 formal）。",
}


EMPTY_COUNTS: dict[str, int] = {
    "finding_count": 0,
    "finding_total_count": 0,
    "formal_finding_count": 0,
    "suspected_count": 0,
    "hint_count": 0,
    "insufficient_data_count": 0,
    "collection_error_count": 0,
    "auto_excluded_count": 0,
    "human_dismissed_count": 0,
    "human_confirmed_count": 0,
}


def compute_counts_from_rows(rows: list[Mapping[str, Any]]) -> dict[str, int]:
    counts = dict(EMPTY_COUNTS)
    for raw in rows:
        item = enrich_finding_row(dict(raw))
        # Skip superseded findings if flag present
        if "is_current" in item and item.get("is_current") is not None:
            if int(item.get("is_current") or 0) != 1:
                continue
        counts["finding_total_count"] += 1
        machine = item.get("machine_status") or ""
        human = item.get("human_status") or HUMAN_UNREVIEWED
        if human == HUMAN_DISMISSED:
            counts["human_dismissed_count"] += 1
        if human == HUMAN_CONFIRMED:
            counts["human_confirmed_count"] += 1
        if machine == MACHINE_HINT:
            counts["hint_count"] += 1
        elif machine == MACHINE_INSUFFICIENT:
            counts["insufficient_data_count"] += 1
        elif machine == MACHINE_COLLECTION_ERROR:
            counts["collection_error_count"] += 1
        elif machine == MACHINE_AUTO_EXCLUDED:
            counts["auto_excluded_count"] += 1
        elif machine == MACHINE_SUSPECTED:
            counts["suspected_count"] += 1
        elif machine == MACHINE_CONFIRMED_BY_RULE and human == HUMAN_UNREVIEWED:
            # counted in formal below; still part of total
            pass

        if is_customer_formal_problem(
            rule_code=item.get("rule_code", ""),
            machine_status=machine,
            human_status=human,
            allow_customer_output=bool(item.get("allow_customer_output", 0)),
        ):
            counts["formal_finding_count"] += 1

    counts["finding_count"] = counts["finding_total_count"]
    return counts


def attach_count_help(counts: dict[str, int]) -> dict[str, Any]:
    return {
        "counts": counts,
        "definitions": COUNT_FIELD_HELP,
    }
