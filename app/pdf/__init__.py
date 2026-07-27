"""M3 PDF text-layer extraction, integrity check, and metadata extraction.

Uses pypdf for text extraction and metadata, with a robust fallback to
pdfplumber for pages where pypdf returns empty text. Both libraries are
pure-Python and already declared in requirements.txt.

This module does NOT depend on OCR — OCR is handled separately in app/ocr.py.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

logger = logging.getLogger(__name__)


@dataclass
class PdfPageResult:
    page_number: int
    text: str
    char_count: int
    source_type: str = "text_layer"  # text_layer | ocr | mixed | empty
    confidence: float = 1.0
    bbox_items: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class PdfExtractionResult:
    """Result of extracting one PDF file."""

    pages: list[PdfPageResult]
    page_count: int
    full_text: str
    metadata: dict[str, Any]
    status: str = "ok"  # ok | corrupted | encrypted | blank | failed
    error: str = ""


def _reader_from_payload(payload: bytes | BinaryIO) -> Any:
    """Return a PdfReader (pypdf) for the given bytes payload."""
    from pypdf import PdfReader

    if isinstance(payload, (bytes, bytearray)):
        return PdfReader(io.BytesIO(bytes(payload)))
    return PdfReader(payload)


def detect_pdf_integrity(payload: bytes | BinaryIO) -> str:
    """Return one of: ok | corrupted | encrypted | blank | not_pdf.

    Heuristics:
    - not_pdf: missing %PDF magic header
    - corrupted: PdfReader raises PdfReadError
    - encrypted: PdfReader.is_encrypted == True AND decrypt("") fails (no empty password)
    - blank: 0 pages OR no extractable text on any page (after decryption if applicable)
    - ok: at least one page with non-empty text layer
    """
    if isinstance(payload, (bytes, bytearray)) and not payload[:5].startswith(b"%PDF"):
        return "not_pdf"
    try:
        reader = _reader_from_payload(payload)
    except Exception as exc:  # pypdf raises various errors on corrupted files
        logger.debug("PDF integrity check failed: %s", exc)
        return "corrupted"
    try:
        if reader.is_encrypted:
            # Try empty password — many policy PDFs use owner-only encryption.
            try:
                decrypt_result = reader.decrypt("")
                # pypdf's decrypt returns 0 (PasswordType.NOT_DECRYPTED) on failure,
                # 1 (USER_PASSWORD) or 2 (OWNER_PASSWORD) on success.
                if not decrypt_result:
                    return "encrypted"
            except Exception:
                return "encrypted"
        if len(reader.pages) == 0:
            return "blank"
        # Sample first 3 pages to detect "blank" status quickly.
        has_text = False
        for page in reader.pages[:3]:
            try:
                text = page.extract_text() or ""
            except Exception:
                text = ""
            if text.strip():
                has_text = True
                break
        if not has_text and len(reader.pages) > 0:
            # Sample all pages before declaring blank — some PDFs have cover pages
            for page in reader.pages:
                try:
                    text = page.extract_text() or ""
                except Exception:
                    text = ""
                if text.strip():
                    has_text = True
                    break
        return "ok" if has_text else "blank"
    except Exception as exc:
        logger.debug("PDF integrity deeper check failed: %s", exc)
        return "corrupted"


def extract_pdf_metadata(payload: bytes | BinaryIO) -> dict[str, Any]:
    """Return PDF document-level metadata (title, author, creation date, etc.)."""
    try:
        reader = _reader_from_payload(payload)
        meta = reader.metadata or {}
        out: dict[str, Any] = {}
        # pypdf returns PsvPdfObject (dict-like). Normalize to plain Python types.
        for key, value in meta.items():
            if value is None:
                continue
            out[str(key)] = str(value)
        out["page_count"] = len(reader.pages)
        return out
    except Exception as exc:
        logger.debug("metadata extraction failed: %s", exc)
        return {"error": str(exc)}


def extract_pdf_text(payload: bytes | BinaryIO) -> PdfExtractionResult:
    """Extract text from each page using pypdf with pdfplumber fallback.

    For each page the returned bbox_items contain the text bounding boxes
    (from pdfplumber) that can be persisted to finding_evidence.

    Returns a PdfExtractionResult with status ok/corrupted/encrypted/blank/failed.
    """
    integrity = detect_pdf_integrity(payload)
    if integrity == "not_pdf":
        return PdfExtractionResult(
            pages=[], page_count=0, full_text="", metadata={},
            status="failed", error="not_pdf",
        )
    if integrity in ("corrupted", "encrypted"):
        return PdfExtractionResult(
            pages=[], page_count=0, full_text="", metadata={},
            status=integrity, error=integrity,
        )

    try:
        reader = _reader_from_payload(payload)
        if reader.is_encrypted:
            reader.decrypt("")
        metadata = extract_pdf_metadata(payload)
    except Exception as exc:
        return PdfExtractionResult(
            pages=[], page_count=0, full_text="", metadata={},
            status="corrupted", error=str(exc),
        )

    pages: list[PdfPageResult] = []
    full_text_parts: list[str] = []
    total_pages = len(reader.pages)
    try:
        import pdfplumber
        pdfplumber_reader = pdfplumber.open(io.BytesIO(bytes(payload)) if isinstance(payload, (bytes, bytearray)) else None)
    except Exception:
        pdfplumber_reader = None

    try:
        for idx, page in enumerate(reader.pages):
            text = ""
            try:
                text = page.extract_text() or ""
            except Exception:
                text = ""
            bbox_items: list[dict[str, Any]] = []
            # If pypdf returned empty, try pdfplumber (better at CJK extraction)
            if not text.strip() and pdfplumber_reader is not None:
                try:
                    plumber_page = pdfplumber_reader.pages[idx]
                    text = plumber_page.extract_text() or ""
                    # Collect bbox evidence for the page
                    for word in (plumber_page.extract_words() or [])[:50]:
                        bbox_items.append({
                            "text": word.get("text", ""),
                            "x0": round(word.get("x0", 0), 2),
                            "top": round(word.get("top", 0), 2),
                            "x1": round(word.get("x1", 0), 2),
                            "bottom": round(word.get("bottom", 0), 2),
                        })
                except Exception:
                    pass
            source_type = "text_layer" if text.strip() else "empty"
            pages.append(PdfPageResult(
                page_number=idx + 1,
                text=text,
                char_count=len(text),
                source_type=source_type,
                bbox_items=bbox_items,
            ))
            full_text_parts.append(text)
    finally:
        if pdfplumber_reader is not None:
            try:
                pdfplumber_reader.close()
            except Exception:
                pass

    full_text = "\n".join(full_text_parts)
    non_empty_pages = sum(1 for p in pages if p.char_count > 0)
    if total_pages > 0 and non_empty_pages == 0:
        status = "blank"
    else:
        status = "ok"

    return PdfExtractionResult(
        pages=pages,
        page_count=total_pages,
        full_text=full_text,
        metadata=metadata,
        status=status,
    )


def save_pdf_to_cache(payload: bytes, cache_dir: Path, pdf_hash: str) -> Path:
    """Save PDF bytes to cache_dir/<hash>.pdf and return the path."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{pdf_hash}.pdf"
    if not path.exists():
        path.write_bytes(payload)
    return path


def compute_pdf_hash(payload: bytes) -> str:
    import hashlib

    return hashlib.sha256(payload).hexdigest()
