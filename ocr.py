"""
ocr.py — OCR engine for scanned PDFs using Tesseract.
Converts scanned pages to searchable/editable text overlays.
"""

import io
import os
import shutil
import tempfile
from dataclasses import dataclass
from typing import Optional

import fitz  # PyMuPDF
from PIL import Image


@dataclass
class OCRResult:
    """Result of OCR on a single page."""
    page_index: int
    text: str
    words: list  # list of (x0, y0, x1, y1, text, confidence)
    confidence: float  # average confidence 0-100


def check_tesseract() -> tuple[bool, str]:
    """Check if Tesseract is available. Returns (available, path_or_error)."""
    # Check common install locations on Windows
    common_paths = [
        shutil.which("tesseract"),
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        "/usr/bin/tesseract",
        "/usr/local/bin/tesseract",
        "/opt/homebrew/bin/tesseract",
    ]
    for p in common_paths:
        if p and os.path.isfile(p):
            return True, p

    return False, (
        "Tesseract not found. Install it:\n"
        "  Windows: https://github.com/UB-Mannheim/tesseract/wiki\n"
        "  macOS:   brew install tesseract\n"
        "  Linux:   sudo apt install tesseract-ocr"
    )


def is_scanned_page(doc: fitz.Document, page_index: int) -> bool:
    """Heuristic: a page is 'scanned' if it has images but very little text."""
    page = doc[page_index]
    text = page.get_text().strip()
    images = page.get_images(full=True)
    # If page has images and less than ~20 chars of text, likely scanned
    return len(images) > 0 and len(text) < 20


def ocr_page(doc: fitz.Document, page_index: int, lang: str = "eng",
             dpi: int = 300, tesseract_path: Optional[str] = None) -> OCRResult:
    """Run OCR on a single page and return structured results."""
    try:
        import pytesseract
    except ImportError:
        raise ImportError(
            "pytesseract is required for OCR. Install it:\n"
            "  pip install pytesseract"
        )

    if tesseract_path:
        pytesseract.pytesseract.tesseract_cmd = tesseract_path

    page = doc[page_index]

    # Render page to high-res image
    zoom = dpi / 72.0
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, alpha=False)

    # Convert to PIL Image
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)

    # Run OCR with word-level data
    data = pytesseract.image_to_data(img, lang=lang, output_type=pytesseract.Output.DICT)

    words = []
    confidences = []
    scale = 72.0 / dpi  # convert pixel coords back to PDF points

    for i in range(len(data["text"])):
        text = data["text"][i].strip()
        conf = float(data["conf"][i])
        if not text or conf < 0:
            continue

        x = data["left"][i] * scale
        y = data["top"][i] * scale
        w = data["width"][i] * scale
        h = data["height"][i] * scale

        words.append((x, y, x + w, y + h, text, conf))
        confidences.append(conf)

    full_text = pytesseract.image_to_string(img, lang=lang)
    avg_conf = sum(confidences) / len(confidences) if confidences else 0.0

    return OCRResult(
        page_index=page_index,
        text=full_text,
        words=words,
        confidence=avg_conf,
    )


def make_searchable_page(doc: fitz.Document, page_index: int,
                         ocr_result: OCRResult):
    """Overlay invisible text on a scanned page to make it searchable/selectable."""
    page = doc[page_index]

    for x0, y0, x1, y1, text, conf in ocr_result.words:
        if conf < 30:  # skip very low confidence words
            continue
        rect = fitz.Rect(x0, y0, x1, y1)
        # Calculate font size to fit the bounding box
        font_size = (y1 - y0) * 0.85
        if font_size < 3:
            font_size = 3

        # Insert invisible text (render mode 3 = invisible)
        tw = fitz.TextWriter(page.rect)
        try:
            tw.append(
                fitz.Point(x0, y1 - (y1 - y0) * 0.15),
                text,
                fontsize=font_size,
                font=fitz.Font("helv"),
            )
            tw.write_text(page, render_mode=3, opacity=0)
        except Exception:
            # If text placement fails, skip this word
            pass


def ocr_full_document(doc: fitz.Document, lang: str = "eng", dpi: int = 300,
                      tesseract_path: Optional[str] = None,
                      progress_callback=None) -> list[OCRResult]:
    """OCR all scanned pages in a document.

    Args:
        progress_callback: Optional callable(page_index, total_pages, status_text)
    """
    results = []
    total = doc.page_count

    for i in range(total):
        if progress_callback:
            progress_callback(i, total, f"Analyzing page {i + 1}/{total}...")

        if not is_scanned_page(doc, i):
            if progress_callback:
                progress_callback(i, total, f"Page {i + 1} — already has text, skipping.")
            continue

        if progress_callback:
            progress_callback(i, total, f"Running OCR on page {i + 1}/{total}...")

        result = ocr_page(doc, i, lang=lang, dpi=dpi, tesseract_path=tesseract_path)
        make_searchable_page(doc, i, result)
        results.append(result)

    return results


def get_available_languages(tesseract_path: Optional[str] = None) -> list[str]:
    """Get list of installed Tesseract language packs."""
    try:
        import pytesseract
        if tesseract_path:
            pytesseract.pytesseract.tesseract_cmd = tesseract_path
        langs = pytesseract.get_languages()
        return [l for l in langs if l != "osd"]
    except Exception:
        return ["eng"]
