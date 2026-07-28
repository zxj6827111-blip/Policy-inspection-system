"""P1 remediation: formal definition alignment + UI list semantics."""

from __future__ import annotations

from app.findings_model import (
    FORMAL_DEFINITION,
    is_customer_formal_problem,
    is_non_customer_rule,
)


def test_formal_definition_docstring_mentions_both_paths():
    assert "human_confirmed" in FORMAL_DEFINITION
    assert "STRICT_AUTO_CONFIRM" in FORMAL_DEFINITION or "confirmed_by_rule" in FORMAL_DEFINITION
    assert "AGENCY" in FORMAL_DEFINITION and "ORG" in FORMAL_DEFINITION


def test_all_agency_and_org_never_formal_even_human_confirmed():
    for code in (
        "AGENCY-001",
        "AGENCY-002",
        "AGENCY-003",
        "ORG-001",
        "ORG-002",
        "ORG-003",
        "ORG-004",
        "ORG-005",
        "ORG-999",
        "AGENCY-999",
    ):
        assert is_non_customer_rule(code)
        assert not is_customer_formal_problem(
            rule_code=code,
            machine_status="suspected",
            human_status="human_confirmed",
            allow_customer_output=True,
        )


def test_human_confirmed_not_blocked_by_allow_flag_for_date():
    assert is_customer_formal_problem(
        rule_code="DATE-001",
        machine_status="confirmed_by_rule",
        human_status="human_confirmed",
        allow_customer_output=False,
    )


def test_auto_formal_requires_strict_allow_and_unreviewed():
    assert is_customer_formal_problem(
        rule_code="DATE-001",
        machine_status="confirmed_by_rule",
        human_status="unreviewed",
        allow_customer_output=True,
    )
    assert not is_customer_formal_problem(
        rule_code="DATE-001",
        machine_status="confirmed_by_rule",
        human_status="unreviewed",
        allow_customer_output=False,
    )
    assert not is_customer_formal_problem(
        rule_code="DOC-003",
        machine_status="confirmed_by_rule",
        human_status="unreviewed",
        allow_customer_output=True,
    )


def test_human_dismissed_never_formal():
    assert not is_customer_formal_problem(
        rule_code="DATE-001",
        machine_status="confirmed_by_rule",
        human_status="human_dismissed",
        allow_customer_output=True,
    )


def test_ui_list_uses_formal_not_bare_finding_count_label():
    from pathlib import Path

    html = Path("app/templates/index.html").read_text(encoding="utf-8")
    js = Path("app/static/app.js").read_text(encoding="utf-8")
    assert "<th>问题</th>" not in html
    assert "正式问题" in html
    assert "线索" in html
    # list/history cells must prefer formal_finding_count
    assert "formal_finding_count" in js
    assert js.count('${fmtNum(job.finding_count)}') == 0 or "finding_total_count" in js
    # no bare sole use of finding_count in table cells without formal nearby context
    assert "title=\"客户正式问题 formal_finding_count\"" in js or "formal_finding_count !=" in js
