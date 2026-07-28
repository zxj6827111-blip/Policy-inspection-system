"""M3 OCR module for scanned PDFs.

Uses pytesseract + Pillow. Tesseract is required on the system; if it is
not installed, OCR gracefully fails and marks the page as 'ocr_failed',
so the pipeline can fall back to human review (per V3.0 §9.5:
'OCR 失败不自动判错').
"""

from __future__ import annotations

import io
import logging
import shutil
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_TESSERACT_AVAILABLE: bool | None = None


def tesseract_available() -> bool:
    """True if the `tesseract` binary is on PATH (cached after first call)."""
    global _TESSERACT_AVAILABLE
    if _TESSERACT_AVAILABLE is None:
        _TESSERACT_AVAILABLE = shutil.which("tesseract") is not None
    return _TESSERACT_AVAILABLE


@dataclass
class OcrPageResult:
    page_number: int
    text: str
    confidence: float
    source_type: str = "ocr"  # ocr | ocr_failed | ocr_empty


@dataclass
class OcrResult:
    pages: list[OcrPageResult]
    page_count: int
    full_text: str
    status: str  # ok | failed | not_needed | skipped
    avg_confidence: float = 0.0
    error: str = ""


def render_page_to_image(payload: bytes, page_number: int, *, dpi: int = 200) -> bytes:
    """Render one page of the PDF to PNG bytes.

    Uses pdfplumber + pdfminer's raster backend (no poppler required).
    Raises if rendering is not possible.
    """
    import pdfplumber
    from PIL import Image  # noqa: F401  (ensured installed)

    with pdfplumber.open(io.BytesIO(payload)) as pdf:
        if page_number < 1 or page_number > len(pdf.pages):
            raise ValueError(f"page {page_number} out of range")
        page = pdf.pages[page_number - 1]
        im = page.to_image(resolution=dpi)
        buf = io.BytesIO()
        im.original.save(buf, format="PNG")
        return buf.getvalue()


def ocr_image_bytes(png_bytes: bytes, *, lang: str = "chi_sim+eng") -> tuple[str, float]:
    """Run Tesseract on PNG bytes and return (text, confidence)."""
    from PIL import Image
    import pytesseract

    image = Image.open(io.BytesIO(png_bytes))
    # pytesseract returns a dict with 'conf' mean if config asks for it.
    data = pytesseract.image_to_data(image, lang=lang, output_type=pytesseract.Output.DICT)
    words = [
        (w, float(c))
        for w, c in zip(data.get("text", []), data.get("conf", []))
        if w and w.strip() and c not in ("-1", -1)
    ]
    text = " ".join(w for w, _ in words)
    confidence = sum(c for _, c in words) / len(words) if words else 0.0
    return text, confidence


def ocr_pdf(payload: bytes, *, max_pages: int | None = None, dpi: int = 200) -> OcrResult:
    """Run OCR on every page of the PDF.

    If Tesseract is not available, returns status='failed' with no pages
    (the caller should mark the document as ocr_status='failed' and not
    auto-flag any finding based on missing OCR text).
    """
    if not tesseract_available():
        return OcrResult(pages=[], page_count=0, full_text="", status="failed",
                         error="tesseract_not_installed")

    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(payload)) as pdf:
            page_count = len(pdf.pages)
    except Exception as exc:
        return OcrResult(pages=[], page_count=0, full_text="", status="failed",
                         error=f"open_failed: {exc}")

    pages: list[OcrPageResult] = []
    full_text_parts: list[str] = []
    total_conf = 0.0
    n_pages = min(page_count, max_pages) if max_pages is not None else page_count

    for page_num in range(1, n_pages + 1):
        try:
            png = render_page_to_image(payload, page_num, dpi=dpi)
            text, conf = ocr_image_bytes(png)
            if not text.strip():
                pages.append(OcrPageResult(
                    page_number=page_num, text="", confidence=0,
                    source_type="ocr_empty",
                ))
            else:
                pages.append(OcrPageResult(
                    page_number=page_num, text=text, confidence=conf,
                    source_type="ocr",
                ))
            total_conf += conf
            full_text_parts.append(text)
        except Exception as exc:
            logger.debug("OCR page %d failed: %s", page_num, exc)
            pages.append(OcrPageResult(
                page_number=page_num, text="", confidence=0,
                source_type="ocr_failed",
            ))

    avg_conf = (total_conf / n_pages) if n_pages else 0.0
    non_empty = sum(1 for p in pages if p.text.strip())
    status = "ok" if non_empty else "failed"
    return OcrResult(
        pages=pages,
        page_count=page_count,
        full_text="\n".join(full_text_parts),
        status=status,
        avg_confidence=avg_conf,
    )
