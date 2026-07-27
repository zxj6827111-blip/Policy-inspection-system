"""M3 service layer: orchestrates PDF extraction, OCR, comparison, and persistence.

This module provides a single entry point `process_document_pdf` that:
1. Loads PDF bytes (from cache or caller-supplied).
2. Runs integrity check (PDF-001~003).
3. Extracts text layer + metadata.
4. Falls back to OCR for blank/scanned pages.
5. Persists results to policy_documents + pdf_extractions tables.
6. Runs three-way comparison and produces PDF-004~007 findings.

OCR-failure policy (V3.0 §9.5): OCR failures do NOT auto-flag findings;
instead the document is marked ocr_status='failed' and findings are deferred
to human review.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.db import Database, utc_now
from app.pdf import (
    PdfExtractionResult,
    compute_pdf_hash,
    detect_pdf_integrity,
    extract_pdf_metadata,
    extract_pdf_text,
    save_pdf_to_cache,
)
from app.pdf.compare import compare_three_way, comparison_to_findings
from app.pdf.attachments import AttachmentClassification, classify_attachment

logger = logging.getLogger(__name__)


@dataclass
class M3ProcessingResult:
    document_id: int
    pdf_status: str  # ok | corrupted | encrypted | blank | failed
    ocr_status: str  # ok | failed | skipped | not_needed
    page_count: int
    findings_generated: int = 0
    rule_codes: list[str] = field(default_factory=list)
    error: str = ""


def process_document_pdf(
    db: Database,
    *,
    document_id: int,
    pdf_payload: bytes,
    cache_dir: Path | None = None,
    page_title: str = "",
    page_document_number: str = "",
    page_issuing_agency: str = "",
    page_authored_date: str | None = None,
    body_text: str = "",
    run_ocr: bool = True,
    run_compare: bool = True,
) -> M3ProcessingResult:
    """End-to-end PDF processing for one document.

    Steps:
    1. Compute hash, cache PDF (if cache_dir supplied).
    2. Integrity check → PDF-001/002/003 findings.
    3. Text-layer extraction → persist to policy_documents.pdf_text.
    4. OCR fallback when text layer is empty.
    5. Three-way comparison → PDF-004~007 findings.
    """
    # 1. Hash + cache
    pdf_hash = compute_pdf_hash(pdf_payload)
    pdf_path = ""
    if cache_dir is not None:
        try:
            pdf_path = str(save_pdf_to_cache(pdf_payload, cache_dir, pdf_hash))
        except Exception as exc:
            logger.warning("save_pdf_to_cache failed: %s", exc)

    # 2. Integrity
    integrity = detect_pdf_integrity(pdf_payload)
    pdf_status = integrity if integrity in ("ok", "corrupted", "encrypted", "blank") else "failed"

    findings_to_emit: list[dict[str, Any]] = []
    rule_codes: list[str] = []

    if integrity in ("corrupted", "encrypted", "blank", "not_pdf", "failed"):
        # Map to PDF-001/002/003
        rule_map = {"corrupted": "PDF-001", "encrypted": "PDF-002", "blank": "PDF-003"}
        if integrity in rule_map:
            rule_codes.append(rule_map[integrity])
            findings_to_emit.append({
                "rule_code": rule_map[integrity],
                "category": "pdf_integrity",
                "severity": "high",
                "detail": f"PDF integrity={integrity}",
                "page_value": "", "body_value": "", "pdf_value": integrity,
                "page_number": 1,
                "machine_status": "suspected",
                "allow_customer_output": True,
            })
        # Persist status and short-circuit (no text extraction for corrupted/encrypted).
        _persist_pdf_status(db, document_id, pdf_status, pdf_hash, pdf_path, error=integrity)
        _emit_findings(db, document_id, findings_to_emit)
        return M3ProcessingResult(
            document_id=document_id, pdf_status=pdf_status, ocr_status="skipped",
            page_count=0, findings_generated=len(findings_to_emit),
            rule_codes=rule_codes, error=integrity,
        )

    # 3. Text-layer extraction
    extraction = extract_pdf_text(pdf_payload)
    full_text = extraction.full_text
    metadata = extraction.metadata
    page_count = extraction.page_count

    # 4. OCR fallback when text layer is empty/blank
    ocr_status = "not_needed"
    ocr_text = ""
    ocr_confidence = 0.0
    if extraction.status == "blank" and run_ocr:
        try:
            from app.ocr import ocr_pdf
            ocr_result = ocr_pdf(pdf_payload, max_pages=page_count)
            if ocr_result.status == "ok":
                ocr_status = "ok"
                ocr_text = ocr_result.full_text
                ocr_confidence = ocr_result.avg_confidence
                full_text = full_text or ocr_text
            else:
                # OCR failed — per V3.0 §9.5 do NOT auto-flag as finding.
                ocr_status = "failed"
        except Exception as exc:
            logger.warning("OCR failed for doc %s: %s", document_id, exc)
            ocr_status = "failed"

    # Persist pdf_extractions per page
    _persist_pdf_extractions(db, document_id, extraction, ocr_text=ocr_text)

    # 5. Three-way comparison → PDF-004~007 findings
    if run_compare:
        result = compare_three_way(
            document_id=document_id,
            page_title=page_title,
            page_document_number=page_document_number,
            page_issuing_agency=page_issuing_agency,
            page_authored_date=page_authored_date,
            body_text=body_text,
            pdf_full_text=full_text,
            pdf_metadata=metadata,
        )
        new_findings = comparison_to_findings(result)
        for f in new_findings:
            rule_codes.append(f["rule_code"])
        findings_to_emit.extend(new_findings)

    # Update policy_documents row
    _persist_pdf_full(
        db, document_id=document_id,
        pdf_hash=pdf_hash, pdf_path=pdf_path,
        pdf_text=full_text, ocr_text=ocr_text,
        ocr_status=ocr_status, ocr_confidence=ocr_confidence,
        page_count=page_count, metadata=metadata,
        pdf_status=pdf_status,
    )
    _emit_findings(db, document_id, findings_to_emit)

    return M3ProcessingResult(
        document_id=document_id,
        pdf_status=pdf_status,
        ocr_status=ocr_status,
        page_count=page_count,
        findings_generated=len(findings_to_emit),
        rule_codes=rule_codes,
    )


def _persist_pdf_status(
    db: Database, document_id: int, pdf_status: str,
    pdf_hash: str, pdf_path: str, error: str = "",
) -> None:
    now = utc_now()
    with db.connect() as conn:
        conn.execute(
            """UPDATE policy_documents
            SET pdf_status=?, pdf_hash=?, pdf_path=?, pdf_extracted_at=?
            WHERE id=?""",
            (pdf_status, pdf_hash, pdf_path, now, document_id),
        )


def _persist_pdf_full(
    db: Database, *, document_id: int, pdf_hash: str, pdf_path: str,
    pdf_text: str, ocr_text: str, ocr_status: str, ocr_confidence: float,
    page_count: int, metadata: dict[str, Any], pdf_status: str,
) -> None:
    now = utc_now()
    with db.connect() as conn:
        conn.execute(
            """UPDATE policy_documents
            SET pdf_hash=?, pdf_path=?, pdf_text=?, ocr_text=?, ocr_status=?,
                ocr_confidence=?, pdf_page_count=?, pdf_metadata_json=?,
                pdf_status=?, pdf_extracted_at=?
            WHERE id=?""",
            (
                pdf_hash, pdf_path, pdf_text, ocr_text, ocr_status,
                ocr_confidence, page_count,
                json.dumps(metadata, ensure_ascii=False),
                pdf_status, now, document_id,
            ),
        )


def _persist_pdf_extractions(
    db: Database, document_id: int, extraction: PdfExtractionResult,
    *, ocr_text: str = "",
) -> None:
    now = utc_now()
    with db.connect() as conn:
        for page in extraction.pages:
            source_type = page.source_type
            text = page.text
            # If page text was empty but OCR produced text, mix in OCR text per page.
            if not text.strip() and ocr_text:
                # Approximate: distribute OCR text across pages is not feasible here.
                # Leave per-page OCR text empty; full_text is stored at doc level.
                source_type = "ocr" if ocr_text else "empty"
            conn.execute(
                """INSERT OR REPLACE INTO pdf_extractions(
                    document_id, page_number, text, bbox_json, source_type,
                    confidence, char_count, extracted_at
                ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    document_id, page.page_number, text,
                    json.dumps(page.bbox_items, ensure_ascii=False),
                    source_type, page.confidence, page.char_count, now,
                ),
            )


def _emit_findings(db: Database, document_id: int, findings: list[dict[str, Any]]) -> None:
    """Persist findings to rule_findings + finding_evidence (with page evidence)."""
    if not findings:
        return
    now = utc_now()
    # Use a synthetic job_id = 0 for M3 standalone processing (or latest job).
    with db.connect() as conn:
        job_row = conn.execute(
            "SELECT id FROM scan_jobs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        job_id = int(job_row["id"]) if job_row else 0
        for f in findings:
            cur = conn.execute(
                """INSERT INTO rule_findings(
                    job_id, document_id, rule_code, category, severity, review_status,
                    detail, page_value, body_value, evidence, created_at,
                    machine_status, allow_customer_output, rule_version
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    job_id, document_id, f["rule_code"], f["category"], f["severity"],
                    "confirmed", f["detail"], f.get("page_value", ""),
                    f.get("body_value", ""), f.get("pdf_value", ""),
                    now, f.get("machine_status", "suspected"),
                    1 if f.get("allow_customer_output") else 0, "1.0.0",
                ),
            )
            finding_id = int(cur.lastrowid)
            page_number = f.get("page_number") or 1
            conn.execute(
                """INSERT INTO finding_evidence(
                    finding_id, evidence_type, source_type, source_url, field_name,
                    raw_value, normalized_value, excerpt, page_number, bbox,
                    confidence, created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    finding_id, "field", "pdf", "", f["rule_code"],
                    f.get("pdf_value", ""), f.get("pdf_value", ""),
                    f.get("detail", "")[:200], page_number,
                    f.get("bbox_json", "[]"), 0.7, now,
                ),
            )


def process_attachment(
    db: Database, *, document_id: int, url: str, filename: str,
    expected_kind: str, payload: bytes | None, fetch_error: str = "",
) -> AttachmentClassification:
    """Classify one attachment and persist to attachment_records + emit finding."""
    classification = classify_attachment(
        url=url, filename=filename, expected_kind=expected_kind,
        payload=payload, fetch_error=fetch_error,
    )
    now = utc_now()
    content_hash = ""
    size_bytes = 0
    if payload:
        import hashlib
        content_hash = hashlib.sha256(payload).hexdigest()
        size_bytes = len(payload)
    with db.connect() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO attachment_records(
                document_id, url, filename, expected_kind, detected_kind,
                status, content_hash, size_bytes, checked_at, notes
            ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                document_id, url, filename, expected_kind,
                classification.detected_kind, classification.status,
                content_hash, size_bytes, now, classification.notes,
            ),
        )
    if classification.status != "ok":
        _emit_findings(db, document_id, [{
            "rule_code": {
                "missing": "ATT-001", "mismatch": "ATT-002",
                "broken": "ATT-003", "encrypted": "ATT-003",
            }.get(classification.status, "ATT-003"),
            "category": "attachment",
            "severity": "high" if classification.status in ("missing", "mismatch") else "medium",
            "detail": f"附件 {url} status={classification.status} notes={classification.notes}",
            "page_value": filename, "body_value": expected_kind,
            "pdf_value": classification.detected_kind,
            "page_number": 1,
            "machine_status": "suspected",
            "allow_customer_output": True,
        }])
    return classification
