"""Finding machine/human status helpers and customer-formal gate."""

from __future__ import annotations

from typing import Any

# 跨任务视图（全局问题清单、总览计数）必须按 (文档,规则) 只留最新一条：
# is_current 的语义是"该任务内未被后续覆盖"，同一缺陷被多个任务重复命中时每个任务都留一条
# current，按任务导出要保留各自的历史，但全局视图直接累加就会把同一个问题数成好几份。
# 别名必须是 f，因为片段里要用 f.document_id / f.rule_code 关联外层。
LATEST_PER_DOCUMENT_RULE = """
    f.id = (SELECT f2.id FROM rule_findings f2
            WHERE f2.document_id = f.document_id AND f2.rule_code = f.rule_code
              AND COALESCE(f2.is_current, 1) = 1
            ORDER BY COALESCE(f2.created_at, '') DESC, f2.id DESC LIMIT 1)
"""

# Machine conclusions
MACHINE_CONFIRMED_BY_RULE = "confirmed_by_rule"
MACHINE_SUSPECTED = "suspected"
MACHINE_HINT = "hint"
MACHINE_INSUFFICIENT = "insufficient_data"
MACHINE_COLLECTION_ERROR = "collection_error"
MACHINE_AUTO_EXCLUDED = "auto_excluded"

MACHINE_STATES = {
    MACHINE_CONFIRMED_BY_RULE,
    MACHINE_SUSPECTED,
    MACHINE_HINT,
    MACHINE_INSUFFICIENT,
    MACHINE_COLLECTION_ERROR,
    MACHINE_AUTO_EXCLUDED,
}

# Human review states
HUMAN_UNREVIEWED = "unreviewed"
HUMAN_CONFIRMED = "human_confirmed"
HUMAN_DISMISSED = "human_dismissed"
HUMAN_NEEDS_MORE = "needs_more_evidence"

HUMAN_STATES = {
    HUMAN_UNREVIEWED,
    HUMAN_CONFIRMED,
    HUMAN_DISMISSED,
    HUMAN_NEEDS_MORE,
}

# Legacy review_status values still accepted on API responses
LEGACY_PENDING = "pending"
LEGACY_CONFIRMED = "confirmed"
LEGACY_DISMISSED = "dismissed"

# Phase-1: never customer-formal, even after human_confirmed.
# Includes entire AGENCY-* and ORG-* families (not only ORG-002).
NON_CUSTOMER_RULE_PREFIXES = ("AGENCY-", "ORG-")
NON_CUSTOMER_RULES = frozenset(
    {
        "AGENCY-001",
        "AGENCY-002",
        "AGENCY-003",
        "ORG-001",
        "ORG-002",
        "ORG-003",
        "ORG-004",
        "ORG-005",
    }
)

# Strict auto-confirm whitelist: only these may become formal without human_confirmed.
STRICT_AUTO_CONFIRM_RULES = frozenset(
    {
        "META-001",
        "DOC-001",
        "DOC-002",
        "DATE-001",
        "DATE-002",
    }
)

FORMAL_DEFINITION = (
    "客户正式问题 = "
    "(1) human_confirmed，且规则不属于 AGENCY-*/ORG-* 家族；或 "
    "(2) machine_status=confirmed_by_rule 且 rule 属于 STRICT_AUTO_CONFIRM_RULES "
    "且 allow_customer_output=1，且人工未排除/未要求补证。"
    " allow_customer_output 仅约束自动确认路径，不拦截人工确认（机构类除外）。"
)


def is_non_customer_rule(rule_code: str) -> bool:
    code = (rule_code or "").strip()
    if code in NON_CUSTOMER_RULES:
        return True
    return any(code.startswith(prefix) for prefix in NON_CUSTOMER_RULE_PREFIXES)


def map_legacy_status_to_machine(legacy: str, rule_code: str = "") -> str:
    legacy = (legacy or "").strip()
    if is_non_customer_rule(rule_code):
        if legacy == LEGACY_DISMISSED:
            return MACHINE_AUTO_EXCLUDED
        return MACHINE_SUSPECTED if legacy != MACHINE_HINT else MACHINE_HINT
    if legacy == LEGACY_CONFIRMED:
        if rule_code in STRICT_AUTO_CONFIRM_RULES:
            return MACHINE_CONFIRMED_BY_RULE
        return MACHINE_SUSPECTED
    if legacy == LEGACY_DISMISSED:
        return MACHINE_AUTO_EXCLUDED
    if legacy == LEGACY_PENDING:
        return MACHINE_SUSPECTED
    if legacy in MACHINE_STATES:
        return legacy
    return MACHINE_SUSPECTED


def map_legacy_status_to_human(legacy: str, has_review_decision: bool) -> str:
    legacy = (legacy or "").strip()
    if not has_review_decision:
        return HUMAN_UNREVIEWED
    if legacy == LEGACY_CONFIRMED:
        return HUMAN_CONFIRMED
    if legacy == LEGACY_DISMISSED:
        return HUMAN_DISMISSED
    if legacy == LEGACY_PENDING:
        return HUMAN_UNREVIEWED
    if legacy in HUMAN_STATES:
        return legacy
    return HUMAN_UNREVIEWED


def legacy_review_status(machine_status: str, human_status: str) -> str:
    """Compatibility projection for old UI/API filters."""
    if human_status == HUMAN_CONFIRMED:
        return LEGACY_CONFIRMED
    if human_status == HUMAN_DISMISSED:
        return LEGACY_DISMISSED
    if human_status == HUMAN_NEEDS_MORE:
        return LEGACY_PENDING
    if machine_status == MACHINE_CONFIRMED_BY_RULE and human_status == HUMAN_UNREVIEWED:
        return LEGACY_CONFIRMED
    if machine_status == MACHINE_AUTO_EXCLUDED and human_status == HUMAN_UNREVIEWED:
        return LEGACY_DISMISSED
    return LEGACY_PENDING


def is_customer_formal_problem(
    *,
    rule_code: str,
    machine_status: str,
    human_status: str,
    allow_customer_output: bool = True,
) -> bool:
    """Single source of truth for customer-formal problems.

    Aligned with phase1/closeout design:

    - AGENCY-* / ORG-* never formal (even human_confirmed).
    - human_confirmed is formal for all other rules (allow_customer_output does not block).
    - Auto formal only for STRICT_AUTO_CONFIRM_RULES with confirmed_by_rule and allow flag.
    - human_dismissed / needs_more_evidence never formal.
    """
    if is_non_customer_rule(rule_code):
        return False
    if human_status in {HUMAN_DISMISSED, HUMAN_NEEDS_MORE}:
        return False
    if human_status == HUMAN_CONFIRMED:
        return True
    if (
        machine_status == MACHINE_CONFIRMED_BY_RULE
        and rule_code in STRICT_AUTO_CONFIRM_RULES
        and allow_customer_output
        and human_status == HUMAN_UNREVIEWED
    ):
        return True
    return False


def bucket_for_finding(
    *,
    machine_status: str,
    human_status: str,
) -> str:
    """UI classification buckets."""
    if human_status == HUMAN_DISMISSED:
        return "human_excluded"
    if human_status == HUMAN_CONFIRMED:
        return "confirmed_problem"
    if machine_status == MACHINE_AUTO_EXCLUDED:
        return "auto_excluded"
    if machine_status == MACHINE_INSUFFICIENT:
        return "insufficient_data"
    if machine_status == MACHINE_COLLECTION_ERROR:
        return "collection_error"
    if machine_status == MACHINE_CONFIRMED_BY_RULE and human_status == HUMAN_UNREVIEWED:
        return "confirmed_problem"
    if machine_status in {MACHINE_SUSPECTED, MACHINE_HINT} or human_status in {
        HUMAN_UNREVIEWED,
        HUMAN_NEEDS_MORE,
    }:
        return "suspected"
    return "suspected"


def decision_to_human(decision: str) -> str:
    mapping = {
        "confirmed": HUMAN_CONFIRMED,
        "dismissed": HUMAN_DISMISSED,
        "pending": HUMAN_UNREVIEWED,
        "human_confirmed": HUMAN_CONFIRMED,
        "human_dismissed": HUMAN_DISMISSED,
        "unreviewed": HUMAN_UNREVIEWED,
        "needs_more_evidence": HUMAN_NEEDS_MORE,
    }
    if decision not in mapping:
        raise ValueError(f"无效的复核结论: {decision}")
    return mapping[decision]


def enrich_finding_row(row: dict[str, Any]) -> dict[str, Any]:
    """Ensure response dict has machine/human fields and legacy review_status."""
    item = dict(row)
    machine = item.get("machine_status") or map_legacy_status_to_machine(
        item.get("review_status", ""), item.get("rule_code", "")
    )
    human = item.get("human_status") or HUMAN_UNREVIEWED
    item["machine_status"] = machine
    item["human_status"] = human
    if not item.get("review_status"):
        item["review_status"] = legacy_review_status(machine, human)
    else:
        item["review_status"] = item["review_status"] or legacy_review_status(machine, human)
    # Default allow_customer_output to 0 when missing (fail closed for auto path).
    if "allow_customer_output" not in item or item.get("allow_customer_output") is None:
        allow = 0
    else:
        allow = bool(item.get("allow_customer_output"))
    item["allow_customer_output"] = int(allow)
    item["is_customer_formal"] = is_customer_formal_problem(
        rule_code=item.get("rule_code", ""),
        machine_status=machine,
        human_status=human,
        allow_customer_output=allow,
    )
    item["ui_bucket"] = bucket_for_finding(machine_status=machine, human_status=human)
    return item
