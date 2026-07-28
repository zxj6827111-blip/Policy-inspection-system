"""M3 attachment checks: ATT-001 missing, ATT-002 mismatch, ATT-003 name/content mismatch.

These rules operate on `attachment_records` rows stored in the DB (one per
declared attachment URL on a policy detail page). The collector records the
declared URL + filename + expected kind; this module classifies the status
after the attachment bytes have been fetched (or after fetch failure).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


# File-extension → expected kind
_EXT_KIND = {
    ".pdf": "pdf",
    ".doc": "doc",
    ".docx": "docx",
    ".xls": "xls",
    ".xlsx": "xlsx",
    ".zip": "archive",
    ".rar": "archive",
    ".jpg": "image",
    ".jpeg": "image",
    ".png": "image",
    ".gif": "image",
}

# File-magic bytes → detected kind
_MAGIC = [
    (b"%PDF", "pdf"),
    (b"PK\x03\x04", "docx/xlsx/zip"),  # office open xml or zip
    (b"\xd0\xcf\x11\xe0", "doc/xls"),  # ole2 (legacy office)
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF8", "image/gif"),
    (b"Rar!\x1a\x07", "archive/rar"),
]


@dataclass
class AttachmentClassification:
    status: str  # ok | missing | mismatch | broken | encrypted
    detected_kind: str = ""
    notes: str = ""


def detect_kind_from_payload(payload: bytes | None) -> str:
    if not payload:
        return ""
    for magic, kind in _MAGIC:
        if payload.startswith(magic):
            return kind
    return ""


def detect_kind_from_filename(filename: str) -> str:
    if not filename:
        return ""
    ext = os.path.splitext(filename)[1].lower()
    return _EXT_KIND.get(ext, "")


def classify_attachment(
    *,
    url: str,
    filename: str,
    expected_kind: str,
    payload: bytes | None,
    fetch_error: str = "",
) -> AttachmentClassification:
    """Classify attachment status into one of ATT-001~003 outcomes.

    - ATT-001 (missing): fetch returned 404/no payload and no error override.
    - ATT-002 (mismatch): file extension says PDF but content is HTML/empty.
    - ATT-003 (name/content mismatch): filename says `xx.pdf` but magic bytes say image.
    """
    if fetch_error or payload is None:
        # Network failure to fetch → mark missing (ATT-001).
        return AttachmentClassification(
            status="missing",
            detected_kind="",
            notes=fetch_error or "payload_unavailable",
        )
    if not payload:
        return AttachmentClassification(
            status="broken",
            detected_kind="",
            notes="empty_payload",
        )
    detected = detect_kind_from_payload(payload)
    declared = expected_kind or detect_kind_from_filename(filename)

    # PDF-encrypted detection: PDF magic present but content unreadable.
    if detected == "pdf" and len(payload) > 200 and b"Encrypt" in payload[:2000]:
        # Best-effort detection; PDF-002 will be authoritative.
        return AttachmentClassification(
            status="encrypted",
            detected_kind=detected,
            notes="pdf_encrypted_flag_in_header",
        )

    if not detected:
        return AttachmentClassification(
            status="broken",
            detected_kind="",
            notes="unknown_magic_bytes",
        )

    if declared and detected and declared not in detected and detected not in declared:
        # ATT-003: filename extension and content magic disagree.
        return AttachmentClassification(
            status="mismatch",
            detected_kind=detected,
            notes=f"declared={declared}; detected={detected}",
        )
    return AttachmentClassification(
        status="ok",
        detected_kind=detected,
        notes="",
    )


def attachment_findings(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert classified attachment records to ATT-001~003 findings."""
    findings: list[dict[str, Any]] = []
    for r in records:
        status = r.get("status", "")
        if status == "ok":
            continue
        rule_code = {
            "missing": "ATT-001",
            "mismatch": "ATT-002",
            "broken": "ATT-003",
            "encrypted": "ATT-003",
        }.get(status, "")
        if not rule_code:
            continue
        findings.append({
            "rule_code": rule_code,
            "category": "attachment",
            "severity": "high" if rule_code in ("ATT-001", "ATT-002") else "medium",
            "detail": f"附件 {r.get('url','')} status={status} notes={r.get('notes','')}",
            "page_value": r.get("filename", ""),
            "body_value": r.get("expected_kind", ""),
            "pdf_value": r.get("detected_kind", ""),
            "machine_status": "suspected",
            "allow_customer_output": True,
        })
    return findings
