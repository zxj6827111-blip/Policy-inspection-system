"""M5 阅办联动深度检查 (Reading/handling linkage deep checks).

Implements V3.0 plan §11.1 + §11.3 + §11.5 guarantees:
- Button presence, clickability, jump success, page validity, no-data placeholder.
- 403/429/captcha/temporary network issues do NOT enter confirmed problems
  (machine_status='transient_error', allow_customer_output=0).
- 200 空页面可识别 (READ-004).
- 200 但显示"暂无数据"可识别 (READ-005).

Rules:
- READ-001: 按钮不存在
- READ-002: 按钮存在但不可点击
- READ-003: 跳转失败（HTTP 错误或页面失效）
- READ-004: 跳转成功但页面空白或错误
- READ-005: 200 但显示暂无数据
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.db import Database, utc_now

# Statuses that are transient network issues and MUST NOT enter confirmed
# problems (V3.0 §11.3). These get machine_status='transient_error' and
# allow_customer_output=0.
TRANSIENT_STATUSES = {"blocked", "captcha", "rate_limited"}

# Statuses considered "page failed" for READ-003.
PAGE_FAILED_STATUSES = {"missing", "error", "not_found"}


@dataclass
class ReadingLinkageInput:
    document_id: int
    job_id: int | None = None
    button_label: str = ""
    button_selector: str = ""
    button_present: bool = True
    clickable: bool | None = None
    target_url: str = ""
    http_status: int | None = None
    page_status: str = "unknown"
    page_title: str = ""
    page_body_length: int = 0
    has_no_data_placeholder: bool = False


@dataclass
class ReadingLinkageResult:
    check_id: int
    document_id: int
    rule_code: str
    machine_status: str
    allow_customer_output: bool
    evidence: list[dict[str, Any]] = field(default_factory=list)
    notes: str = ""


def record_reading_check(db: Database, payload: ReadingLinkageInput) -> ReadingLinkageResult:
    """Persist a reading linkage check and emit rule code if applicable."""
    rule_code, machine_status, allow_output, evidence, notes = _classify(payload)

    with db.connect() as conn:
        cur = conn.execute(
            """INSERT INTO reading_linkage_checks(
                document_id, job_id, button_label, button_selector,
                button_present, clickable, target_url, http_status, page_status,
                page_title, page_body_length, has_no_data_placeholder,
                rule_code, machine_status, allow_customer_output, evidence_json, checked_at, notes
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                payload.document_id, payload.job_id, payload.button_label,
                payload.button_selector,
                1 if payload.button_present else 0,
                _bool_or_none(payload.clickable),
                payload.target_url, payload.http_status, payload.page_status,
                payload.page_title, payload.page_body_length,
                1 if payload.has_no_data_placeholder else 0,
                rule_code, machine_status, 1 if allow_output else 0,
                _evidence_json(evidence), utc_now(), notes,
            ),
        )
        check_id = int(cur.lastrowid)

    # Update document-level reading_check_status.
    doc_status = "ok"
    if rule_code:
        if machine_status == "transient_error":
            doc_status = "failed"  # transient — needs recheck, not formal issue
        else:
            doc_status = "has_issues"
    elif payload.page_status in TRANSIENT_STATUSES:
        doc_status = "failed"
    with db.connect() as conn:
        conn.execute(
            "UPDATE policy_documents SET reading_check_status=? WHERE id=?",
            (doc_status, payload.document_id),
        )

    return ReadingLinkageResult(
        check_id=check_id,
        document_id=payload.document_id,
        rule_code=rule_code,
        machine_status=machine_status,
        allow_customer_output=allow_output,
        evidence=evidence,
        notes=notes,
    )


def _classify(payload: ReadingLinkageInput) -> tuple[str, str, bool, list[dict[str, Any]], str]:
    """Return (rule_code, machine_status, allow_customer_output, evidence, notes).

    V3.0 §11.5 guarantees:
    - 正常重定向不误报 → redirect is OK.
    - 403/429 不进入正式问题 → transient_error, allow_customer_output=0.
    - 200 空页面可识别 → READ-004.
    - 200 但显示"暂无数据"可识别 → READ-005.
    """
    evidence: list[dict[str, Any]] = [{
        "button_label": payload.button_label,
        "button_selector": payload.button_selector,
        "target_url": payload.target_url,
        "http_status": payload.http_status,
        "page_status": payload.page_status,
        "page_body_length": payload.page_body_length,
        "has_no_data_placeholder": payload.has_no_data_placeholder,
    }]

    # 1. Transient network issues (403/429/captcha) — NOT formal issues.
    if payload.page_status in TRANSIENT_STATUSES:
        return (
            "",  # No rule code — transient, not a formal problem.
            "transient_error",
            False,  # allow_customer_output
            evidence,
            f"Transient network issue (page_status={payload.page_status}); not a confirmed problem.",
        )

    # 2. READ-001: 按钮不存在.
    if not payload.button_present:
        return (
            "READ-001",
            "confirmed",
            True,
            evidence,
            "Reading/handling button is missing on the document page.",
        )

    # 3. READ-002: 按钮存在但不可点击.
    if payload.button_present and payload.clickable is False:
        return (
            "READ-002",
            "confirmed",
            True,
            evidence,
            "Reading/handling button is present but not clickable.",
        )

    # 4. READ-003: 跳转失败（HTTP 错误或页面失效）.
    # Note: redirect is OK (正常重定向不误报), so page_status='redirect' is NOT a failure.
    if payload.page_status in PAGE_FAILED_STATUSES:
        return (
            "READ-003",
            "confirmed",
            True,
            evidence,
            f"Jump failed: page_status={payload.page_status}.",
        )
    if payload.http_status is not None and payload.http_status >= 400:
        return (
            "READ-003",
            "confirmed",
            True,
            evidence,
            f"Jump failed: HTTP {payload.http_status}.",
        )

    # 5. READ-005: 200 但显示"暂无数据".
    if payload.has_no_data_placeholder:
        return (
            "READ-005",
            "confirmed",
            True,
            evidence,
            "Page returns 200 but displays '暂无数据' placeholder; business cannot proceed.",
        )

    # 6. READ-004: 跳转成功但页面空白.
    if payload.page_status == "blank" or (
        payload.page_status == "ok"
        and payload.page_body_length == 0
        and not payload.has_no_data_placeholder
    ):
        return (
            "READ-004",
            "confirmed",
            True,
            evidence,
            "Jump succeeded but target page is blank or empty.",
        )

    # 7. OK — no finding.
    return (
        "",
        "ok",
        False,
        evidence,
        "Reading linkage OK.",
    )


def list_reading_checks(
    db: Database, *, document_id: int | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    """List reading linkage checks."""
    with db.connect() as conn:
        if document_id is not None:
            rows = conn.execute(
                """SELECT * FROM reading_linkage_checks WHERE document_id=?
                ORDER BY checked_at DESC LIMIT ?""",
                (document_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM reading_linkage_checks ORDER BY checked_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [dict(r) for r in rows]


def _bool_or_none(value: bool | None) -> int | None:
    if value is None:
        return None
    return 1 if value else 0


def _evidence_json(evidence: list[dict[str, Any]]) -> str:
    import json
    return json.dumps(evidence, ensure_ascii=False)
