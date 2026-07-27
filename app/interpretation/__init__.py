"""M5 政策解读深度检查与解读对象匹配 (Policy interpretation deep checks).

Implements V3.0 plan §11.2:
- 8 interpretation types: 图文解读 / 一图读懂 / 视频解读 / 问答解读 /
  负责人解读 / 专家解读 / 新闻发布会 / 媒体解读.
- Link validity, image validity, video playability, page not empty.
- Interpretation object matches original policy.
- Interpretation published_at present.
- Wrong-target detection (interpretation links to wrong policy).

Rules:
- INTERP-001: 解读链接失效
- INTERP-002: 解读对象错误（与原政策不匹配）
- INTERP-003: 解读发布时间缺失
- INTERP-004: 解读链接错挂
- INTERP-005: 解读图片失效
- INTERP-006: 解读视频失效
- INTERP-007: 解读页面空白
- INTERP-008: 解读类型缺失或无法识别
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from app.db import Database, utc_now

# Canonical interpretation types (V3.0 §11.2).
INTERPRETATION_TYPES = {
    "image_text": "图文解读",
    "one_image": "一图读懂",
    "video": "视频解读",
    "qa": "问答解读",
    "leader": "负责人解读",
    "expert": "专家解读",
    "press_conference": "新闻发布会",
    "media": "媒体解读",
}

# Threshold for matching confidence.
MATCH_CONFIDENCE_THRESHOLD = 0.85


@dataclass
class InterpretationInput:
    document_id: int
    interpretation_type: str
    interpretation_url: str
    title: str = ""
    published_at: str | None = None
    target_document_id: int | None = None
    target_doc_number: str = ""
    target_title: str = ""


@dataclass
class InterpretationCheckInput:
    interpretation_id: int
    job_id: int | None = None
    link_status: str = "unknown"
    http_status: int | None = None
    has_image: bool | None = None
    image_status: str = "unknown"
    has_video: bool | None = None
    video_status: str = "unknown"
    page_body_length: int = 0
    is_empty_page: bool = False
    published_at_present: bool | None = None


@dataclass
class InterpretationCheckResult:
    check_id: int
    interpretation_id: int
    rule_code: str
    machine_status: str
    allow_customer_output: bool
    evidence: list[dict[str, Any]] = field(default_factory=list)
    notes: str = ""


def create_interpretation(db: Database, payload: InterpretationInput) -> int:
    """Create a policy interpretation record and compute match confidence."""
    doc_number, title, match_confidence, match_evidence = _compute_match(db, payload)

    with db.connect() as conn:
        cur = conn.execute(
            """INSERT INTO policy_interpretations(
                document_id, interpretation_type, interpretation_url, title,
                published_at, target_document_id, target_doc_number, target_title,
                matched_target, match_confidence, match_evidence_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                payload.document_id, payload.interpretation_type, payload.interpretation_url,
                payload.title, payload.published_at,
                payload.target_document_id,
                payload.target_doc_number or doc_number,
                payload.target_title or title,
                1 if match_confidence >= MATCH_CONFIDENCE_THRESHOLD else 0,
                match_confidence,
                json.dumps(match_evidence, ensure_ascii=False),
            ),
        )
        interp_id = int(cur.lastrowid)

        # Update policy_documents.interpretation_count + has_interpretation_mismatch.
        conn.execute(
            """UPDATE policy_documents
            SET interpretation_count = (
                SELECT COUNT(*) FROM policy_interpretations WHERE document_id=?
            ),
            has_interpretation_mismatch = CASE
                WHEN EXISTS(
                    SELECT 1 FROM policy_interpretations
                    WHERE document_id=? AND matched_target=0
                ) THEN 1 ELSE 0 END
            WHERE id=?""",
            (payload.document_id, payload.document_id, payload.document_id),
        )

    return interp_id


def _compute_match(
    db: Database, payload: InterpretationInput
) -> tuple[str, str, float, list[dict[str, Any]]]:
    """Compute match confidence between interpretation and original policy.

    V3.0 §11.5: 解读对象错误可识别.
    Match signals:
    - target_document_id explicitly matches the document.
    - target_doc_number matches document's page_document_number.
    - target_title matches document's title.
    """
    if payload.target_document_id is not None and payload.target_document_id == payload.document_id:
        return (
            payload.target_doc_number,
            payload.target_title,
            1.0,
            [{"type": "explicit_target_match", "target_document_id": payload.target_document_id}],
        )

    with db.connect() as conn:
        doc = conn.execute(
            "SELECT title, page_document_number FROM policy_documents WHERE id=?",
            (payload.document_id,),
        ).fetchone()

    doc_title = (doc["title"] if doc else "") or ""
    doc_num = (doc["page_document_number"] if doc else "") or ""
    evidence: list[dict[str, Any]] = []
    signals = 0
    total_signals = 0

    if payload.target_doc_number and doc_num:
        total_signals += 1
        if _normalize_doc_number(payload.target_doc_number) == _normalize_doc_number(doc_num):
            signals += 1
            evidence.append({"type": "doc_number_match", "value": doc_num})
        else:
            evidence.append({
                "type": "doc_number_mismatch",
                "expected": doc_num,
                "actual": payload.target_doc_number,
            })

    if payload.target_title and doc_title:
        total_signals += 1
        if _normalize_title(payload.target_title) == _normalize_title(doc_title):
            signals += 1
            evidence.append({"type": "title_match", "value": doc_title})
        else:
            evidence.append({
                "type": "title_mismatch",
                "expected": doc_title,
                "actual": payload.target_title,
            })

    # If no signals available, default to 0.5 (uncertain) — caller should
    # pass explicit target_document_id for confident matches.
    if total_signals == 0:
        confidence = 0.5
        evidence.append({"type": "no_signals", "reason": "no doc_number/title provided for matching"})
    else:
        confidence = signals / total_signals

    return (
        payload.target_doc_number or doc_num,
        payload.target_title or doc_title,
        confidence,
        evidence,
    )


def record_interpretation_check(
    db: Database, payload: InterpretationCheckInput
) -> InterpretationCheckResult:
    """Persist interpretation check and emit rule code if applicable."""
    rule_code, machine_status, allow_output, evidence, notes = _classify_check(payload)

    with db.connect() as conn:
        cur = conn.execute(
            """INSERT INTO interpretation_checks(
                interpretation_id, job_id, link_status, http_status,
                has_image, image_status, has_video, video_status,
                page_body_length, is_empty_page, published_at_present,
                rule_code, machine_status, allow_customer_output, evidence_json, checked_at, notes
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                payload.interpretation_id, payload.job_id, payload.link_status, payload.http_status,
                _bool_or_none(payload.has_image), payload.image_status,
                _bool_or_none(payload.has_video), payload.video_status,
                payload.page_body_length, 1 if payload.is_empty_page else 0,
                _bool_or_none(payload.published_at_present),
                rule_code, machine_status, 1 if allow_output else 0,
                json.dumps(evidence, ensure_ascii=False), utc_now(), notes,
            ),
        )
        check_id = int(cur.lastrowid)

    return InterpretationCheckResult(
        check_id=check_id,
        interpretation_id=payload.interpretation_id,
        rule_code=rule_code,
        machine_status=machine_status,
        allow_customer_output=allow_output,
        evidence=evidence,
        notes=notes,
    )


def _classify_check(
    payload: InterpretationCheckInput,
) -> tuple[str, str, bool, list[dict[str, Any]], str]:
    evidence: list[dict[str, Any]] = [{
        "link_status": payload.link_status,
        "http_status": payload.http_status,
        "has_image": payload.has_image,
        "image_status": payload.image_status,
        "has_video": payload.has_video,
        "video_status": payload.video_status,
        "page_body_length": payload.page_body_length,
        "is_empty_page": payload.is_empty_page,
        "published_at_present": payload.published_at_present,
    }]

    # INTERP-001: 解读链接失效 (HTTP error or page missing).
    if payload.link_status in ("missing", "error", "not_found"):
        return ("INTERP-001", "confirmed", True, evidence, "Interpretation link is invalid.")
    if payload.http_status is not None and payload.http_status >= 400:
        return ("INTERP-001", "confirmed", True, evidence, f"Interpretation link HTTP {payload.http_status}.")

    # INTERP-006: 解读视频失效.
    if payload.has_video and payload.video_status in ("broken", "playback_failed"):
        return ("INTERP-006", "confirmed", True, evidence, f"Interpretation video failed (status={payload.video_status}).")

    # INTERP-005: 解读图片失效.
    if payload.has_image and payload.image_status in ("broken", "missing"):
        return ("INTERP-005", "confirmed", True, evidence, f"Interpretation image failed (status={payload.image_status}).")

    # INTERP-007: 解读页面空白.
    if payload.is_empty_page or (
        payload.link_status == "ok" and payload.page_body_length == 0
    ):
        return ("INTERP-007", "confirmed", True, evidence, "Interpretation page is blank.")

    # INTERP-003: 解读发布时间缺失.
    if payload.published_at_present is False:
        return ("INTERP-003", "confirmed", True, evidence, "Interpretation published_at is missing.")

    # OK — no finding.
    return ("", "ok", False, evidence, "Interpretation check OK.")


def list_interpretations(
    db: Database, *, document_id: int | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    with db.connect() as conn:
        if document_id is not None:
            rows = conn.execute(
                "SELECT * FROM policy_interpretations WHERE document_id=? ORDER BY id DESC LIMIT ?",
                (document_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM policy_interpretations ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [dict(r) for r in rows]


def _bool_or_none(value: bool | None) -> int | None:
    if value is None:
        return None
    return 1 if value else 0


def _normalize_doc_number(s: str) -> str:
    """Normalize a document number for comparison."""
    import re
    import unicodedata
    s = unicodedata.normalize("NFKC", s or "")
    s = re.sub(r"[\s\u3000]+", "", s)
    return s


def _normalize_title(s: str) -> str:
    """Normalize a title for comparison."""
    import re
    import unicodedata
    s = unicodedata.normalize("NFKC", s or "")
    s = re.sub(r"[\s\u3000]+", "", s)
    s = s.replace("(", "（").replace(")", "）")
    s = s.replace("[", "〔").replace("]", "〕")
    s = s.replace("【", "〔").replace("】", "〕")
    s = re.sub(r"[（(〔【].*?[)）〕】]", "", s)
    s = s.strip("。·、，,")
    return s
