"""PDF -> text using pypdf with pdfplumber fallback.

Each page is prefixed with a <!-- page N --> marker so callers can
slice the document by page number.
"""
from __future__ import annotations

from pathlib import Path


def convert(path: Path) -> str:
    """Return full text of *path* with page markers."""
    text = _try_pypdf(path)
    if not text or len(text.strip()) < 100:
        text = _try_pdfplumber(path)
    return text


def _try_pypdf(path: Path) -> str:
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        pages = []
        for i, page in enumerate(reader.pages, start=1):
            page_text = page.extract_text() or ""
            if page_text.strip():
                pages.append(f"<!-- page {i} -->\n\n{page_text.strip()}")
        return "\n\n".join(pages)
    except Exception:
        return ""


def _try_pdfplumber(path: Path) -> str:
    import pdfplumber

    pages = []
    with pdfplumber.open(str(path)) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            page_text = page.extract_text() or ""
            parts = []
            if page_text.strip():
                parts.append(page_text.strip())
            if parts:
                pages.append(f"<!-- page {i} -->\n\n" + "\n\n".join(parts))
    return "\n\n".join(pages)
