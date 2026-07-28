"""M3 three-way comparison: page field vs body text vs PDF content.

For each policy document we compare four fields (title, document number,
issuing agency, date) across three sources:
1. Page field — values captured from the listing/detail HTML page.
2. Body text — values extracted from the HTML page body text.
3. PDF — values extracted from the PDF text layer (or OCR fallback).

Differences surface as PDF-004~007 findings (see app/migrations/m3.py).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.rules import DATE_RE, extract_authored_date, extract_document_numbers, normalize_document_number


@dataclass
class FieldComparison:
    field_name: str  # title | document_number | issuing_agency | date
    page_value: str
    body_value: str
    pdf_value: str
    pdf_page_number: int | None
    pdf_bbox: list[dict[str, Any]] = field(default_factory=list)
    is_match: bool = True
    rule_code: str = ""  # PDF-004~007 when mismatch
    severity: str = "high"
    evidence: str = ""


@dataclass
class ThreeWayCompareResult:
    document_id: int
    fields: list[FieldComparison]
    has_mismatch: bool
    pdf_text_excerpt: str = ""
    body_text_excerpt: str = ""


def _normalize_date(value: str | None) -> str | None:
    if not value:
        return None
    import re

    match = re.search(r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})", value)
    if not match:
        return None
    try:
        d = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        return d.isoformat()
    except ValueError:
        return None


def _normalize_agency(value: str | None) -> str:
    if not value:
        return ""
    import re

    return re.sub(r"\s+", "", value)


def extract_pdf_title(pdf_metadata: dict[str, Any], pdf_full_text: str) -> tuple[str, int | None]:
    """Return (title, page_number) from PDF metadata or first-page text."""
    # Prefer metadata Title (commonly the first line of a PDF generator).
    title = ""
    for key in ("/Title", "Title", "/title"):
        v = pdf_metadata.get(key)
        if isinstance(v, str) and v.strip():
            title = v.strip()
            break
    if title:
        return title, 1
    # Fall back: first non-empty line of the PDF text.
    for line in (pdf_full_text or "").splitlines():
        line = line.strip()
        if line:
            return line[:200], 1
    return "", None


def extract_pdf_document_numbers(pdf_full_text: str) -> list[str]:
    """Extract all document numbers found in PDF text."""
    return extract_document_numbers(pdf_full_text or "")


def extract_pdf_issuing_agency(pdf_metadata: dict[str, Any], pdf_full_text: str) -> tuple[str, int | None]:
    """Return (issuing_agency, page_number).

    Uses PDF /Author or /Creator metadata; falls back to the first
    organization-like keyword in the first 2 pages.
    """
    for key in ("/Author", "Author", "/Creator", "Creator"):
        v = pdf_metadata.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip(), 1
    agency_keywords = ("人民政府", "委员会", "办公室", "管理局", "监管局", "局", "中心")
    for line in (pdf_full_text or "").splitlines()[:80]:
        for kw in agency_keywords:
            if kw in line:
                # Take the trimmed line up to 60 chars as a hint
                return line.strip()[:60], 1
    return "", None


def extract_pdf_dates(pdf_full_text: str) -> dict[str, str | None]:
    """Extract candidate dates from PDF text.

    Returns dict with possible keys: authored, published, effective, expired.
    Only `authored` is reliably derived from落款 context via extract_authored_date.
    """
    authored = extract_authored_date(pdf_full_text or "")
    return {
        "authored": authored.isoformat() if authored else None,
    }


def compare_three_way(
    *,
    document_id: int,
    page_title: str,
    page_document_number: str,
    page_issuing_agency: str,
    page_authored_date: str | None,
    body_text: str,
    pdf_full_text: str,
    pdf_metadata: dict[str, Any],
    body_text_excerpt: str = "",
) -> ThreeWayCompareResult:
    """Compare four fields across page/body/PDF and produce findings.

    - page_value comes from the HTML listing/detail page
    - body_value is extracted from body_text
    - pdf_value is extracted from pdf_full_text / pdf_metadata
    """
    comparisons: list[FieldComparison] = []

    # --- Title (PDF-005)
    pdf_title, pdf_title_page = extract_pdf_title(pdf_metadata, pdf_full_text)
    body_title = ""
    for line in (body_text or "").splitlines():
        line = line.strip()
        if line:
            body_title = line[:200]
            break
    title_match = (not pdf_title) or (pdf_title in (body_title or page_title))
    comparisons.append(FieldComparison(
        field_name="title",
        page_value=page_title,
        body_value=body_title,
        pdf_value=pdf_title,
        pdf_page_number=pdf_title_page,
        is_match=title_match,
        rule_code="" if title_match else "PDF-005",
        severity="high",
        evidence=f"page={page_title!r} body={body_title!r} pdf={pdf_title!r}",
    ))

    # --- Document number (PDF-004)
    pdf_doc_numbers = extract_pdf_document_numbers(pdf_full_text)
    body_doc_numbers = extract_document_numbers(body_text)
    pdf_doc_number = pdf_doc_numbers[0] if pdf_doc_numbers else ""
    body_doc_number = body_doc_numbers[0] if body_doc_numbers else ""
    page_dn_norm = normalize_document_number(page_document_number)
    pdf_dn_norm = normalize_document_number(pdf_doc_number)
    body_dn_norm = normalize_document_number(body_doc_number)
    # Match if any of body/pdf matches page, or if page is empty (no anchor).
    dn_match = (not pdf_dn_norm) or (pdf_dn_norm == page_dn_norm) or (pdf_dn_norm == body_dn_norm)
    comparisons.append(FieldComparison(
        field_name="document_number",
        page_value=page_document_number,
        body_value=body_doc_number,
        pdf_value=pdf_doc_number,
        pdf_page_number=1,
        is_match=dn_match,
        rule_code="" if dn_match else "PDF-004",
        severity="high",
        evidence=f"page={page_dn_norm!r} body={body_dn_norm!r} pdf={pdf_dn_norm!r}",
    ))

    # --- Issuing agency (PDF-007)
    pdf_agency, pdf_agency_page = extract_pdf_issuing_agency(pdf_metadata, pdf_full_text)
    body_agency_match = ""
    agency_keywords = ("人民政府", "委员会", "办公室", "管理局", "监管局", "局")
    for line in (body_text or "").splitlines()[:80]:
        for kw in agency_keywords:
            if kw in line:
                body_agency_match = line.strip()[:60]
                break
        if body_agency_match:
            break
    page_agency_norm = _normalize_agency(page_issuing_agency)
    pdf_agency_norm = _normalize_agency(pdf_agency)
    body_agency_norm = _normalize_agency(body_agency_match)
    agency_match = (not pdf_agency_norm) or (
        pdf_agency_norm in page_agency_norm or pdf_agency_norm in body_agency_norm
        or page_agency_norm in pdf_agency_norm or body_agency_norm in pdf_agency_norm
    )
    comparisons.append(FieldComparison(
        field_name="issuing_agency",
        page_value=page_issuing_agency,
        body_value=body_agency_match,
        pdf_value=pdf_agency,
        pdf_page_number=pdf_agency_page,
        is_match=agency_match,
        rule_code="" if agency_match else "PDF-007",
        severity="medium",
        evidence=f"page={page_agency_norm!r} body={body_agency_norm!r} pdf={pdf_agency_norm!r}",
    ))

    # --- Date (PDF-006)
    pdf_dates = extract_pdf_dates(pdf_full_text)
    pdf_authored = pdf_dates.get("authored") or ""
    body_authored_date = extract_authored_date(body_text or "")
    body_authored = body_authored_date.isoformat() if body_authored_date else None
    page_date_norm = _normalize_date(page_authored_date)
    pdf_date_norm = _normalize_date(pdf_authored)
    body_date_norm = _normalize_date(body_authored)
    # Match if page matches either body or pdf (when both present)
    date_match = True
    if pdf_date_norm and page_date_norm:
        date_match = pdf_date_norm == page_date_norm
    elif pdf_date_norm and body_date_norm:
        date_match = pdf_date_norm == body_date_norm
    comparisons.append(FieldComparison(
        field_name="date",
        page_value=page_authored_date or "",
        body_value=body_authored or "",
        pdf_value=pdf_authored or "",
        pdf_page_number=1,
        is_match=date_match,
        rule_code="" if date_match else "PDF-006",
        severity="high",
        evidence=f"page={page_date_norm!r} body={body_date_norm!r} pdf={pdf_date_norm!r}",
    ))

    has_mismatch = any(not c.is_match for c in comparisons)
    return ThreeWayCompareResult(
        document_id=document_id,
        fields=comparisons,
        has_mismatch=has_mismatch,
        pdf_text_excerpt=(pdf_full_text or "")[:500],
        body_text_excerpt=(body_text_excerpt or body_text or "")[:500],
    )


def comparison_to_findings(result: ThreeWayCompareResult) -> list[dict[str, Any]]:
    """Convert comparison result to finding dicts (one per mismatched field)."""
    findings: list[dict[str, Any]] = []
    for c in result.fields:
        if c.is_match:
            continue
        findings.append({
            "rule_code": c.rule_code,
            "category": "pdf_compare",
            "severity": c.severity,
            "detail": c.evidence,
            "page_value": c.page_value,
            "body_value": c.body_value,
            "pdf_value": c.pdf_value,
            "page_number": c.pdf_page_number,
            "bbox_json": json.dumps(c.pdf_bbox, ensure_ascii=False),
            "machine_status": "suspected",
            "allow_customer_output": True,
        })
    return findings
