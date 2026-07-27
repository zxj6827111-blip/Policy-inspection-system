"""M5 图片/视频/附件可用性检查 (Image/video/attachment availability).

Implements V3.0 plan §11.2 (partial: media checks):
- 图片缺失 / 图片破损
- 视频缺失 / 视频失效或无法播放
- 页面资源整体为空
- 图片/视频被 403/429 拦截

Rules:
- MEDIA-001: 图片缺失
- MEDIA-002: 图片破损
- MEDIA-003: 视频缺失
- MEDIA-004: 视频失效或无法播放
- MEDIA-005: 页面资源整体为空
- MEDIA-006: 图片/视频被 403/429 拦截 (transient — not a formal issue)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from app.db import Database, utc_now

# Statuses that are transient (403/429) and must NOT enter confirmed problems.
TRANSIENT_MEDIA_STATUSES = {"blocked", "rate_limited"}


@dataclass
class MediaCheckInput:
    document_id: int
    media_type: str  # 'image' | 'video' | 'attachment'
    media_url: str
    job_id: int | None = None
    source_area: str = ""
    http_status: int | None = None
    status: str = "pending"
    content_type: str = ""
    content_hash: str = ""
    size_bytes: int = 0
    detected_kind: str = ""
    duration_seconds: float | None = None


@dataclass
class MediaCheckResult:
    check_id: int
    document_id: int
    media_type: str
    rule_code: str
    machine_status: str
    allow_customer_output: bool
    evidence: list[dict[str, Any]] = field(default_factory=list)
    notes: str = ""


def record_media_check(db: Database, payload: MediaCheckInput) -> MediaCheckResult:
    """Persist a media check and emit rule code if applicable."""
    rule_code, machine_status, allow_output, evidence, notes = _classify(payload)

    with db.connect() as conn:
        cur = conn.execute(
            """INSERT INTO media_checks(
                document_id, job_id, media_type, media_url, source_area,
                http_status, status, content_type, content_hash, size_bytes,
                detected_kind, duration_seconds,
                rule_code, machine_status, allow_customer_output, evidence_json, checked_at, notes
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                payload.document_id, payload.job_id, payload.media_type, payload.media_url,
                payload.source_area, payload.http_status, payload.status,
                payload.content_type, payload.content_hash, payload.size_bytes,
                payload.detected_kind, payload.duration_seconds,
                rule_code, machine_status, 1 if allow_output else 0,
                json.dumps(evidence, ensure_ascii=False), utc_now(), notes,
            ),
        )
        check_id = int(cur.lastrowid)

    return MediaCheckResult(
        check_id=check_id,
        document_id=payload.document_id,
        media_type=payload.media_type,
        rule_code=rule_code,
        machine_status=machine_status,
        allow_customer_output=allow_output,
        evidence=evidence,
        notes=notes,
    )


def _classify(payload: MediaCheckInput) -> tuple[str, str, bool, list[dict[str, Any]], str]:
    evidence: list[dict[str, Any]] = [{
        "media_type": payload.media_type,
        "media_url": payload.media_url,
        "source_area": payload.source_area,
        "http_status": payload.http_status,
        "status": payload.status,
        "content_type": payload.content_type,
        "size_bytes": payload.size_bytes,
    }]

    # 1. MEDIA-006: 403/429 拦截 (transient — NOT a formal issue).
    if payload.status in TRANSIENT_MEDIA_STATUSES or (
        payload.http_status is not None and payload.http_status in (403, 429)
    ):
        return (
            "MEDIA-006",
            "transient_error",
            False,
            evidence,
            f"Media blocked by 403/429 (status={payload.status}, http={payload.http_status}); transient.",
        )

    # 2. MEDIA-005: 页面资源整体为空 (empty).
    if payload.status == "empty":
        return (
            "MEDIA-005",
            "confirmed",
            True,
            evidence,
            "Media resource is empty.",
        )

    # 3. Branch by media type.
    if payload.media_type == "image":
        if payload.status == "missing":
            return ("MEDIA-001", "confirmed", True, evidence, "Image is missing.")
        if payload.status == "broken":
            return ("MEDIA-002", "confirmed", True, evidence, "Image is broken.")
    elif payload.media_type == "video":
        if payload.status == "missing":
            return ("MEDIA-003", "confirmed", True, evidence, "Video is missing.")
        if payload.status in ("broken", "playback_failed"):
            return (
                "MEDIA-004",
                "confirmed",
                True,
                evidence,
                f"Video is invalid or cannot play (status={payload.status}).",
            )

    # 4. OK — no finding.
    if payload.status == "ok":
        return ("", "ok", False, evidence, "Media OK.")
    return ("", "ok", False, evidence, f"Media status={payload.status} (not classified).")


def list_media_checks(
    db: Database, *, document_id: int | None = None,
    media_type: str | None = None, limit: int = 100,
) -> list[dict[str, Any]]:
    with db.connect() as conn:
        query = "SELECT * FROM media_checks WHERE 1=1"
        params: list[Any] = []
        if document_id is not None:
            query += " AND document_id=?"
            params.append(document_id)
        if media_type is not None:
            query += " AND media_type=?"
            params.append(media_type)
        query += " ORDER BY checked_at DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]
