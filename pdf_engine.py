"""
pdf_engine.py — PDF rendering, text extraction, form handling, and saving.
Uses PyMuPDF (fitz) as the backend.
"""

import logging
import os
import tempfile

import fitz  # PyMuPDF
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("PDFStudio.engine")


class PDFPasswordRequired(Exception):
    """Raised when an encrypted PDF needs a password to open."""


@dataclass
class TextBlock:
    """Represents an editable text span extracted from a PDF page."""
    page_index: int
    bbox: tuple  # (x0, y0, x1, y1) in PDF coordinates
    text: str
    font_name: str = "helv"
    font_size: float = 11.0
    color: tuple = (0, 0, 0)  # RGB 0-1
    span_index: int = 0
    block_index: int = 0


@dataclass
class FormField:
    """Represents a fillable form widget."""
    page_index: int
    field_name: str
    field_type: str  # "Text", "CheckBox", "ComboBox", "ListBox", "RadioButton"
    bbox: tuple
    value: str = ""
    options: list = field(default_factory=list)


@dataclass
class SearchResult:
    """A single text search hit."""
    page_index: int
    rect: tuple  # (x0, y0, x1, y1) bounding rect in PDF points


@dataclass
class Annotation:
    """An annotation to be burned into the PDF on save."""
    page_index: int
    ann_type: str  # "highlight", "text", "freehand", "image", "signature"
    bbox: tuple = (0, 0, 0, 0)
    text: str = ""
    color: tuple = (1, 1, 0)  # RGB 0-1
    opacity: float = 0.5
    font_size: float = 12.0
    points: list = field(default_factory=list)  # for freehand
    image_path: str = ""
    image_data: bytes = b""


class PDFEngine:
    """Core PDF manipulation engine wrapping PyMuPDF."""

    def __init__(self):
        self.doc: Optional[fitz.Document] = None
        self.file_path: Optional[str] = None
        self.annotations: list[Annotation] = []
        self.text_edits: dict[tuple, str] = {}
        self._page_cache: dict[int, fitz.Pixmap] = {}

        # Suppress MuPDF C-level warnings (xref errors, repair messages)
        # that print to stderr and alarm users. Python-level exceptions
        # are still raised normally.
        try:
            fitz.TOOLS.mupdf_display_errors(False)
        except AttributeError:
            pass  # older PyMuPDF versions without this method

    # ── File I/O ────────────────────────────────────────────────

    def open(self, path: str) -> int:
        """Open a PDF file. Returns page count.

        Automatically attempts repair on damaged files by doing a
        save-to-bytes-and-reopen cycle if the initial open detects
        xref or structural issues.

        Raises PDFPasswordRequired if the PDF needs a user password.
        Use open_with_password() to supply one.
        """
        self.close()
        self.file_path = path
        log.info("Opening PDF: %s", path)
        self.doc = fitz.open(path)

        # Encrypted PDFs must be detected BEFORE the repair cycle. The
        # tobytes(garbage=4) call below fails on a still-encrypted doc
        # and silently falls back to reopening the original — leaving
        # us with an unauthenticated handle that returns no pages.
        if self.doc.needs_pass:
            log.info("PDF requires a password")
            raise PDFPasswordRequired()

        # Attempt repair on damaged PDFs
        try:
            repaired_bytes = self.doc.tobytes(garbage=4, deflate=True)
            self.doc.close()
            self.doc = fitz.open(stream=repaired_bytes, filetype="pdf")
            log.debug("PDF opened and repaired/cleaned successfully")
        except Exception as e:
            log.warning("PDF repair cycle failed, using original: %s", e)
            self.doc = fitz.open(path)

        log.info("PDF loaded: %d pages", self.doc.page_count)
        return self.doc.page_count

    def open_with_password(self, path: str, password: str) -> int:
        """Open an encrypted PDF using the supplied password.

        Raises PDFPasswordRequired if the password is rejected.
        Skips the repair cycle for encrypted PDFs since reopening the
        doc from a byte stream loses the authenticated state on some
        PyMuPDF builds.
        """
        self.close()
        self.file_path = path
        log.info("Opening encrypted PDF: %s", path)
        self.doc = fitz.open(path)

        if self.doc.needs_pass:
            ok = self.doc.authenticate(password)
            if not ok:
                log.info("Password rejected for %s", path)
                self.doc.close()
                self.doc = None
                self.file_path = None
                raise PDFPasswordRequired()
            log.info("Password accepted")

        log.info("PDF loaded: %d pages", self.doc.page_count)
        return self.doc.page_count

    def close(self):
        if self.doc:
            self.doc.close()
            self.doc = None
        self.file_path = None
        self.annotations.clear()
        self.text_edits.clear()
        self._page_cache.clear()

    @property
    def page_count(self) -> int:
        return self.doc.page_count if self.doc else 0

    # ── Rendering ───────────────────────────────────────────────

    def render_page(self, page_index: int, zoom: float = 2.0) -> bytes:
        """Render a page to PNG bytes at the given zoom factor."""
        if not self.doc or page_index < 0 or page_index >= self.page_count:
            return b""
        mat = fitz.Matrix(zoom, zoom)
        page = self.doc[page_index]
        pix = page.get_pixmap(matrix=mat, alpha=False)
        return pix.tobytes("png")

    def render_page_thumbnail(self, page_index: int, max_width: int = 180) -> bytes:
        """Render a small thumbnail for the sidebar."""
        if not self.doc or page_index < 0 or page_index >= self.page_count:
            return b""
        page = self.doc[page_index]
        rect = page.rect
        zoom = max_width / rect.width
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        return pix.tobytes("png")

    def get_page_size(self, page_index: int) -> tuple:
        """Return (width, height) in points for the given page."""
        if not self.doc:
            return (612, 792)
        return (self.doc[page_index].rect.width, self.doc[page_index].rect.height)

    # ── Text Search ─────────────────────────────────────────────

    def search_text(self, query: str, page_index: Optional[int] = None) -> list[SearchResult]:
        """Search for text across all pages or a specific page."""
        if not self.doc or not query:
            return []

        log.debug("Searching for '%s' (page=%s)", query, page_index)
        results = []

        start = page_index if page_index is not None else 0
        end = (page_index + 1) if page_index is not None else self.page_count

        for i in range(start, end):
            page = self.doc[i]
            hits = page.search_for(query)
            for rect in hits:
                results.append(SearchResult(
                    page_index=i,
                    rect=(rect.x0, rect.y0, rect.x1, rect.y1),
                ))

        log.debug("Search found %d result(s)", len(results))
        return results

    def get_page_text(self, page_index: int) -> str:
        """Get plain text content of a page."""
        if not self.doc or page_index < 0 or page_index >= self.page_count:
            return ""
        return self.doc[page_index].get_text()

    # ── Text Extraction ─────────────────────────────────────────

    def extract_text_blocks(self, page_index: int) -> list[TextBlock]:
        """Extract all text spans with position and font info."""
        if not self.doc:
            return []
        page = self.doc[page_index]
        blocks = []
        text_dict = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
        for bi, block in enumerate(text_dict.get("blocks", [])):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for si, span in enumerate(line.get("spans", [])):
                    if not span["text"].strip():
                        continue
                    r, g, b = self._int_to_rgb(span.get("color", 0))
                    blocks.append(TextBlock(
                        page_index=page_index,
                        bbox=tuple(span["bbox"]),
                        text=span["text"],
                        font_name=span.get("font", "helv"),
                        font_size=span.get("size", 11),
                        color=(r, g, b),
                        span_index=si,
                        block_index=bi,
                    ))
        return blocks

    def find_text_at_point(self, page_index: int, x: float, y: float) -> Optional[TextBlock]:
        """Find the text block at a given PDF-coordinate point."""
        blocks = self.extract_text_blocks(page_index)
        for tb in blocks:
            bx0, by0, bx1, by1 = tb.bbox
            if bx0 <= x <= bx1 and by0 <= y <= by1:
                return tb
        return None

    # ── Text Editing ────────────────────────────────────────────

    def edit_text(self, block: TextBlock, new_text: str,
                  font_size: float = None, font_name: str = None,
                  color: tuple = None):
        """Replace text content in the PDF page."""
        if not self.doc:
            return
        log.info("Editing text on page %d: '%s' -> '%s'",
                 block.page_index, block.text[:30], new_text[:30])
        page = self.doc[block.page_index]

        fs = font_size if font_size is not None else block.font_size
        fn = font_name if font_name is not None else "helv"
        clr = color if color is not None else block.color

        # Expand redaction rect slightly to ensure full cleanup
        rect = fitz.Rect(block.bbox)
        rect.x0 -= 1
        rect.y0 -= 1
        rect.x1 += 1
        rect.y1 += 1

        page.add_redact_annot(rect, fill=False)
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)

        # Insert at baseline
        baseline_x = block.bbox[0]
        baseline_y = block.bbox[3] - (fs * 0.15)

        page.insert_text(
            fitz.Point(baseline_x, baseline_y),
            new_text,
            fontsize=fs,
            fontname=fn,
            color=clr,
        )

    # ── Form Fields ─────────────────────────────────────────────

    def extract_form_fields(self, page_index: int) -> list[FormField]:
        """Extract interactive form widgets from a page."""
        if not self.doc:
            return []
        page = self.doc[page_index]
        fields = []
        for widget in page.widgets():
            ft = widget.field_type_string or "Text"
            fields.append(FormField(
                page_index=page_index,
                field_name=widget.field_name or "",
                field_type=ft,
                bbox=tuple(widget.rect),
                value=widget.field_value or "",
                options=widget.choice_values or [],
            ))
        return fields

    def fill_form_field(self, page_index: int, field_name: str, value: str):
        """Set a form field value."""
        if not self.doc:
            return
        page = self.doc[page_index]
        for widget in page.widgets():
            if widget.field_name == field_name:
                widget.field_value = value
                widget.update()
                break

    # ── Annotations ─────────────────────────────────────────────

    def add_highlight(self, page_index: int, rect: tuple, color=(1, 1, 0), opacity=0.35):
        """Add a highlight annotation to the page."""
        if not self.doc:
            return
        page = self.doc[page_index]
        annot = page.add_highlight_annot(fitz.Rect(rect))
        annot.set_colors(stroke=color)
        annot.set_opacity(opacity)
        annot.update()

    def add_text_annotation(self, page_index: int, rect: tuple, text: str,
                            font_size: float = 12, color=(0, 0, 0)):
        """Insert a text box at the given rect."""
        if not self.doc:
            return
        page = self.doc[page_index]
        page.insert_textbox(
            fitz.Rect(rect), text,
            fontsize=font_size, color=color,
            fontname="helv",
        )

    def add_freehand_annotation(self, page_index: int, points: list,
                                 color=(0, 0, 0), width=2.0):
        """Add an ink (freehand) annotation."""
        if not self.doc or not points:
            return
        page = self.doc[page_index]
        annot = page.add_ink_annot([points])
        annot.set_colors(stroke=color)
        annot.set_border(width=width)
        annot.update()

    def add_image(self, page_index: int, rect: tuple, image_data: bytes):
        """Insert an image (signature, stamp, etc.) at the given rect."""
        if not self.doc:
            return
        page = self.doc[page_index]
        page.insert_image(fitz.Rect(rect), stream=image_data)

    def add_stamp_image(self, page_index: int, rect: tuple, image_path: str):
        """Insert an image from file path."""
        if not self.doc:
            return
        page = self.doc[page_index]
        page.insert_image(fitz.Rect(rect), filename=image_path)

    # ── Page Structure ──────────────────────────────────────────

    def rotate_page(self, page_index: int, degrees: int):
        """Rotate a page by `degrees` (any multiple of 90, +/- direction)."""
        if not self.doc:
            return
        page = self.doc[page_index]
        new_rot = (page.rotation + degrees) % 360
        page.set_rotation(new_rot)
        self._page_cache.pop(page_index, None)

    def reorder_pages(self, permutation: list):
        """Reorder pages by a permutation list (must contain every page index).

        Uses fitz.Document.select(), which is more predictable than chained
        move_page calls — `select([2,0,1])` produces a doc whose pages are
        the original 2, 0, 1 in that order, regardless of move-direction
        edge cases.
        """
        if not self.doc:
            return
        if sorted(permutation) != list(range(self.doc.page_count)):
            raise ValueError(
                f"reorder_pages requires a full permutation of "
                f"{self.doc.page_count} indices; got {permutation}"
            )
        self.doc.select(permutation)
        self._page_cache.clear()

    def delete_page(self, page_index: int):
        """Delete a page from the document."""
        if not self.doc:
            return
        self.doc.delete_page(page_index)
        self._page_cache.clear()

    def insert_blank_page(self, at_index: int, width: float = 612.0,
                          height: float = 792.0):
        """Insert a blank page at the given index. Default size is US Letter."""
        if not self.doc:
            return
        self.doc.new_page(pno=at_index, width=width, height=height)
        self._page_cache.clear()

    # ── Saving (Safe — temp file strategy) ──────────────────────

    def save(self, output_path: Optional[str] = None):
        """Save the PDF safely.

        Always uses the safe temp-file-and-rename strategy. We avoid
        saveIncr() because the repair cycle in open() reopens the doc
        from a byte stream, which clears doc.name and breaks saveIncr.
        """
        if not self.doc:
            return
        path = output_path or self.file_path
        if not path:
            raise ValueError("No file path specified for save.")

        self._safe_save_to(path)

    def save_as(self, output_path: str):
        """Save to a new file using safe temp-file strategy."""
        if not self.doc:
            return
        self._safe_save_to(output_path)

    def _safe_save_to(self, target_path: str):
        """Write to a temp file in the same directory, then rename."""
        log.info("Safe save to: %s", target_path)
        target = Path(target_path)
        target_dir = target.parent
        target_dir.mkdir(parents=True, exist_ok=True)

        fd, tmp_path = tempfile.mkstemp(
            suffix=".pdf.tmp",
            prefix=f".{target.stem}_",
            dir=str(target_dir),
        )
        os.close(fd)
        log.debug("Writing to temp file: %s", tmp_path)

        try:
            self.doc.save(tmp_path, garbage=4, deflate=True)
            log.debug("Temp file written, renaming to target")
            if os.path.exists(target_path):
                backup_path = target_path + ".bak"
                try:
                    if os.path.exists(backup_path):
                        os.remove(backup_path)
                    os.rename(target_path, backup_path)
                    os.rename(tmp_path, target_path)
                    os.remove(backup_path)
                except Exception:
                    if os.path.exists(backup_path) and not os.path.exists(target_path):
                        os.rename(backup_path, target_path)
                    raise
            else:
                os.rename(tmp_path, target_path)
            log.info("Save complete: %s", target_path)
        except Exception:
            log.error("Save failed, cleaning up temp file", exc_info=True)
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise

    # ── Helpers ──────────────────────────────────────────────────

    @staticmethod
    def _int_to_rgb(color_int: int) -> tuple:
        """Convert integer color to (r, g, b) in 0-1 range."""
        r = ((color_int >> 16) & 0xFF) / 255.0
        g = ((color_int >> 8) & 0xFF) / 255.0
        b = (color_int & 0xFF) / 255.0
        return (r, g, b)
