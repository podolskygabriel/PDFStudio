"""
signature.py — Visual signature drawing/upload and cryptographic (PKI) signing.
Includes cross-platform font detection with fallbacks.
"""

import io
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QTabWidget,
    QWidget, QLabel, QFileDialog, QLineEdit, QFormLayout,
    QMessageBox, QSizePolicy, QScrollArea, QGridLayout, QApplication
)
from PyQt6.QtGui import (
    QPainter, QPen, QColor, QPixmap, QImage, QFont, QCursor,
    QFontDatabase
)
from PyQt6.QtCore import Qt, QPoint, QSize


def _is_font_available(family: str) -> bool:
    """Check if a font family is available on the current system."""
    # PyQt6: QFontDatabase methods are static — do NOT instantiate.
    available = QFontDatabase.families()
    family_lower = family.lower()
    return any(f.lower() == family_lower for f in available)


def _resolve_font(candidates: list[str]) -> str:
    """Given a list of font candidates in priority order, return the first available one.
    Falls back to the system default sans-serif if none are found.
    """
    for font in candidates:
        if _is_font_available(font):
            return font
    # Ultimate fallback
    return QFont().defaultFamily()


# Cross-platform font resolution table.
# Each style has a list of candidates: Windows fonts first, then macOS, then Linux.
_FONT_CANDIDATES = {
    "clean": ["Arial", "Helvetica Neue", "Helvetica", "Liberation Sans", "DejaVu Sans"],
    "formal": ["Georgia", "Times New Roman", "Noto Serif", "Liberation Serif", "DejaVu Serif"],
    "script": [
        "Segoe Script", "Apple Chancery", "Comic Sans MS",
        "URW Chancery L", "Liberation Sans",
    ],
    "cursive": [
        "Segoe Script", "Apple Chancery", "Comic Sans MS",
        "URW Chancery L", "Liberation Sans",
    ],
    "handwritten": [
        "Ink Free", "Bradley Hand", "Comic Sans MS",
        "Noto Sans", "Liberation Sans",
    ],
    "elegant": [
        "Palace Script MT", "Snell Roundhand", "Apple Chancery",
        "URW Chancery L", "Georgia",
    ],
    "classic": [
        "Times New Roman", "Times", "Noto Serif",
        "Liberation Serif", "DejaVu Serif",
    ],
    "modern": [
        "Calibri", "Helvetica Neue", "Arial",
        "Liberation Sans", "DejaVu Sans",
    ],
}


@dataclass
class SignatureResult:
    """Result from the signature dialog."""
    image_data: bytes  # PNG bytes of the visual signature
    apply_crypto: bool = False
    pfx_path: str = ""
    pfx_password: str = ""


class SignatureCanvas(QWidget):
    """A drawing canvas for freehand signatures."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(500, 200)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setStyleSheet(
            "background-color: white; border: 1px solid #ccc; border-radius: 4px;"
        )
        self._image = QImage(500, 200, QImage.Format.Format_ARGB32)
        self._image.fill(Qt.GlobalColor.transparent)
        self._drawing = False
        self._last_point = QPoint()
        self._pen = QPen(
            QColor(0, 0, 80), 2.5, Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin
        )
        self._has_content = False

    def resizeEvent(self, event):
        new_image = QImage(self.size(), QImage.Format.Format_ARGB32)
        new_image.fill(Qt.GlobalColor.transparent)
        p = QPainter(new_image)
        p.drawImage(0, 0, self._image)
        p.end()
        self._image = new_image
        super().resizeEvent(event)

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(255, 255, 255))
        p.drawImage(0, 0, self._image)
        # guide line
        y = int(self.height() * 0.72)
        p.setPen(QPen(QColor(200, 200, 200), 1, Qt.PenStyle.DashLine))
        p.drawLine(30, y, self.width() - 30, y)
        p.end()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drawing = True
            self._last_point = event.position().toPoint()
            self._has_content = True

    def mouseMoveEvent(self, event):
        if self._drawing:
            p = QPainter(self._image)
            p.setPen(self._pen)
            current = event.position().toPoint()
            p.drawLine(self._last_point, current)
            p.end()
            self._last_point = current
            self.update()

    def mouseReleaseEvent(self, event):
        self._drawing = False

    def clear(self):
        self._image.fill(Qt.GlobalColor.transparent)
        self._has_content = False
        self.update()

    def get_signature_bytes(self) -> bytes:
        """Return the signature as PNG bytes with transparency."""
        img = self._image
        buffer = img.bits()
        buffer.setsize(img.sizeInBytes())
        from PIL import Image
        pil_img = Image.frombytes(
            "RGBA", (img.width(), img.height()), bytes(buffer), "raw", "BGRA"
        )
        bbox = pil_img.getbbox()
        if bbox:
            pil_img = pil_img.crop(bbox)
        ba = io.BytesIO()
        pil_img.save(ba, "PNG")
        return ba.getvalue()

    @property
    def has_content(self) -> bool:
        return self._has_content


class SignatureStylePreview(QWidget):
    """A clickable preview card showing a typed signature in one font style."""

    def __init__(self, style_name: str, font_family: str, font_size: int = 32,
                 italic: bool = False, on_selected=None, parent=None):
        super().__init__(parent)
        self.style_name = style_name
        self.font_family = font_family
        self.font_size = font_size
        self.italic = italic
        self._text = ""
        self._selected = False
        self._on_selected_callback = on_selected

        self.setFixedHeight(72)
        self.setMinimumWidth(200)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._update_style()

    def _update_style(self):
        border_color = "#1a73e8" if self._selected else "#555"
        border_width = "2px" if self._selected else "1px"
        bg = "#2a3a5c" if self._selected else "white"
        self.setStyleSheet(
            f"SignatureStylePreview {{ background: {bg}; "
            f"border: {border_width} solid {border_color}; "
            f"border-radius: 6px; }}"
        )

    @property
    def selected(self):
        return self._selected

    @selected.setter
    def selected(self, value: bool):
        self._selected = value
        self._update_style()
        self.update()

    def set_text(self, text: str):
        self._text = text
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        if not self._selected:
            p.fillRect(self.rect().adjusted(1, 1, -1, -1), QColor(255, 255, 255))

        # Style label
        label_font = QFont("Segoe UI", 8)
        p.setFont(label_font)
        label_color = QColor(180, 200, 255) if self._selected else QColor(140, 140, 140)
        p.setPen(label_color)
        p.drawText(10, 16, self.style_name)

        # Signature text
        sig_font = QFont(self.font_family, self.font_size)
        sig_font.setItalic(self.italic)
        p.setFont(sig_font)
        text_color = QColor(220, 230, 255) if self._selected else QColor(0, 0, 80)
        p.setPen(text_color)

        display = self._text or "Your Name"
        fm = p.fontMetrics()
        avail_w = self.width() - 24
        actual_size = self.font_size
        while fm.horizontalAdvance(display) > avail_w and actual_size > 10:
            actual_size -= 1
            fit_font = QFont(self.font_family, actual_size)
            fit_font.setItalic(self.italic)
            p.setFont(fit_font)
            fm = p.fontMetrics()

        text_y = 28 + (self.height() - 28) // 2 + fm.ascent() // 2 - 2
        p.drawText(12, text_y, display)
        p.end()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            if self._on_selected_callback:
                self._on_selected_callback(self)

    def render_to_png(self, text: str) -> bytes:
        """Render the signature text in this style to PNG bytes with transparency."""
        font = QFont(self.font_family, self.font_size)
        font.setItalic(self.italic)

        tmp = QImage(1, 1, QImage.Format.Format_ARGB32)
        tmp_p = QPainter(tmp)
        tmp_p.setFont(font)
        bound = tmp_p.fontMetrics().boundingRect(text)
        tmp_p.end()

        padding = 12
        w = bound.width() + padding * 2
        h = bound.height() + padding * 2

        img = QImage(max(w, 40), max(h, 30), QImage.Format.Format_ARGB32)
        img.fill(Qt.GlobalColor.transparent)

        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setFont(font)
        p.setPen(QColor(0, 0, 80))
        p.drawText(padding, padding + p.fontMetrics().ascent(), text)
        p.end()

        buffer = img.bits()
        buffer.setsize(img.sizeInBytes())
        from PIL import Image
        pil_img = Image.frombytes(
            "RGBA", (img.width(), img.height()), bytes(buffer), "raw", "BGRA"
        )
        bbox = pil_img.getbbox()
        if bbox:
            pil_img = pil_img.crop(bbox)
        buf = io.BytesIO()
        pil_img.save(buf, "PNG")
        return buf.getvalue()


class TypedSignatureWidget(QWidget):
    """Tab content for typing a signature with multiple font style previews.

    Uses cross-platform font resolution — each style tries multiple font
    families and picks the first one available on the current OS.
    """

    # (display_name, font_key, size, italic)
    STYLES = [
        ("Clean",       "clean",       30, False),
        ("Formal",      "formal",      28, False),
        ("Script",      "script",      30, False),
        ("Cursive",     "cursive",     30, True),
        ("Handwritten", "handwritten", 30, False),
        ("Elegant",     "elegant",     36, False),
        ("Classic",     "classic",     28, True),
        ("Modern",      "modern",      30, True),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._selected_preview: Optional[SignatureStylePreview] = None
        self._previews: list[SignatureStylePreview] = []
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        layout.addWidget(QLabel("Type your name:"))
        self._name_input = QLineEdit()
        self._name_input.setPlaceholderText("Your Name")
        self._name_input.setStyleSheet(
            "QLineEdit { font-size: 16px; padding: 8px; background: white; "
            "color: #222; border: 1px solid #ccc; border-radius: 4px; }"
        )
        self._name_input.textChanged.connect(self._on_text_changed)
        layout.addWidget(self._name_input)

        layout.addWidget(QLabel("Choose a style:"))

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        scroll.setMaximumHeight(340)

        grid_widget = QWidget()
        grid = QGridLayout(grid_widget)
        grid.setSpacing(8)
        grid.setContentsMargins(0, 0, 0, 0)

        for i, (name, font_key, size, italic) in enumerate(self.STYLES):
            # Resolve the actual font family for this platform
            candidates = _FONT_CANDIDATES.get(font_key, ["Arial"])
            resolved_family = _resolve_font(candidates)

            preview = SignatureStylePreview(
                name, resolved_family, size, italic,
                on_selected=self._on_style_selected,
            )
            preview.set_text("")
            self._previews.append(preview)
            row = i // 2
            col = i % 2
            grid.addWidget(preview, row, col)

        scroll.setWidget(grid_widget)
        layout.addWidget(scroll)

        if self._previews:
            self._on_style_selected(self._previews[0])

    def _on_text_changed(self, text: str):
        for preview in self._previews:
            preview.set_text(text)

    def _on_style_selected(self, preview: SignatureStylePreview):
        if self._selected_preview:
            self._selected_preview.selected = False
        preview.selected = True
        self._selected_preview = preview

    @property
    def has_content(self) -> bool:
        return bool(self._name_input.text().strip())

    def get_signature_bytes(self) -> bytes:
        """Render the selected typed signature to PNG."""
        text = self._name_input.text().strip()
        if not text or not self._selected_preview:
            return b""
        return self._selected_preview.render_to_png(text)


class SignatureDialog(QDialog):
    """Dialog for creating, typing, or uploading a signature."""

    TAB_DRAW = 0
    TAB_TYPE = 1
    TAB_UPLOAD = 2

    def __init__(self, parent=None, enable_crypto=True):
        super().__init__(parent)
        self.setWindowTitle("Add Signature")
        self.setMinimumSize(620, 540)
        self.result_data: Optional[SignatureResult] = None
        self._uploaded_image_data: Optional[bytes] = None
        self._enable_crypto = enable_crypto
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        tabs = QTabWidget()

        # Tab 1: Draw
        draw_tab = QWidget()
        dl = QVBoxLayout(draw_tab)
        dl.setContentsMargins(12, 12, 12, 12)
        dl.addWidget(QLabel("Draw your signature below:"))
        self._canvas = SignatureCanvas()
        dl.addWidget(self._canvas)
        clear_btn = QPushButton("Clear")
        clear_btn.setFixedWidth(100)
        clear_btn.clicked.connect(self._canvas.clear)
        dl.addWidget(clear_btn, alignment=Qt.AlignmentFlag.AlignRight)
        tabs.addTab(draw_tab, "Draw")

        # Tab 2: Type
        self._typed_sig = TypedSignatureWidget()
        tabs.addTab(self._typed_sig, "Type")

        # Tab 3: Upload Image
        upload_tab = QWidget()
        ul = QVBoxLayout(upload_tab)
        ul.setContentsMargins(12, 12, 12, 12)
        ul.addWidget(QLabel("Upload a signature image (PNG with transparency recommended):"))
        self._upload_preview = QLabel("No image selected")
        self._upload_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._upload_preview.setMinimumHeight(160)
        self._upload_preview.setStyleSheet(
            "border: 1px solid #ccc; background: white; border-radius: 4px;"
        )
        ul.addWidget(self._upload_preview)
        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(self._browse_image)
        ul.addWidget(browse_btn, alignment=Qt.AlignmentFlag.AlignRight)
        tabs.addTab(upload_tab, "Upload Image")

        layout.addWidget(tabs)
        self._tabs = tabs

        # Crypto section
        if self._enable_crypto:
            crypto_group = QWidget()
            fl = QFormLayout(crypto_group)
            fl.setContentsMargins(0, 8, 0, 0)
            fl.addRow(QLabel("─── Digital Signature (Optional) ───"))
            self._pfx_path_edit = QLineEdit()
            self._pfx_path_edit.setPlaceholderText("Path to .pfx / .p12 certificate")
            pfx_browse = QPushButton("...")
            pfx_browse.setFixedWidth(36)
            pfx_browse.clicked.connect(self._browse_pfx)
            pfx_row = QHBoxLayout()
            pfx_row.addWidget(self._pfx_path_edit)
            pfx_row.addWidget(pfx_browse)
            fl.addRow("Certificate:", pfx_row)
            self._pfx_pass_edit = QLineEdit()
            self._pfx_pass_edit.setEchoMode(QLineEdit.EchoMode.Password)
            self._pfx_pass_edit.setPlaceholderText("Certificate password")
            fl.addRow("Password:", self._pfx_pass_edit)
            layout.addWidget(crypto_group)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        apply_btn = QPushButton("Apply Signature")
        apply_btn.setDefault(True)
        apply_btn.clicked.connect(self._on_apply)
        apply_btn.setStyleSheet(
            "QPushButton { background-color: #1a73e8; color: white; padding: 8px 24px; "
            "border-radius: 4px; font-weight: bold; } "
            "QPushButton:hover { background-color: #1557b0; }"
        )
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(apply_btn)
        layout.addLayout(btn_row)

    def _browse_image(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Signature Image", "",
            "Images (*.png *.jpg *.jpeg *.bmp *.svg)"
        )
        if path:
            try:
                size = os.path.getsize(path)
            except OSError:
                QMessageBox.warning(self, "Cannot Read", "Could not read the selected file.")
                return
            if size > 10 * 1024 * 1024:
                QMessageBox.warning(
                    self, "File Too Large",
                    "Signature image must be under 10 MB."
                )
                return
            with open(path, "rb") as f:
                self._uploaded_image_data = f.read()
            pix = QPixmap(path).scaled(
                400, 150, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation
            )
            self._upload_preview.setPixmap(pix)

    def _browse_pfx(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Certificate", "",
            "Certificates (*.pfx *.p12);;All Files (*)"
        )
        if path:
            self._pfx_path_edit.setText(path)

    def _on_apply(self):
        tab = self._tabs.currentIndex()

        if tab == self.TAB_DRAW:
            if not self._canvas.has_content:
                QMessageBox.warning(self, "Empty", "Please draw a signature first.")
                return
            img_data = self._canvas.get_signature_bytes()

        elif tab == self.TAB_TYPE:
            if not self._typed_sig.has_content:
                QMessageBox.warning(self, "Empty", "Please type your name first.")
                return
            img_data = self._typed_sig.get_signature_bytes()

        elif tab == self.TAB_UPLOAD:
            if not self._uploaded_image_data:
                QMessageBox.warning(self, "No Image", "Please upload a signature image.")
                return
            img_data = self._uploaded_image_data

        else:
            return

        apply_crypto = False
        pfx_path = ""
        pfx_password = ""
        if self._enable_crypto:
            pfx_path = self._pfx_path_edit.text().strip()
            pfx_password = self._pfx_pass_edit.text()
            if pfx_path:
                apply_crypto = True

        self.result_data = SignatureResult(
            image_data=img_data,
            apply_crypto=apply_crypto,
            pfx_path=pfx_path,
            pfx_password=pfx_password,
        )
        self.accept()


def apply_crypto_signature(pdf_path: str, output_path: str,
                           pfx_path: str, pfx_password: str,
                           reason: str = "Document signed",
                           location: str = "") -> bool:
    """Apply a PKCS#12 digital signature to a PDF file.
    Returns True on success.
    """
    try:
        from endesive.pdf import cms as pdf_cms
        from endesive import signer
        from cryptography.hazmat.primitives.serialization import pkcs12
        from cryptography.hazmat.backends import default_backend

        with open(pfx_path, "rb") as f:
            pfx_data = f.read()

        private_key, certificate, additional_certs = pkcs12.load_key_and_certificates(
            pfx_data, pfx_password.encode(), default_backend()
        )

        now = datetime.now()
        date_str = now.strftime("D:%Y%m%d%H%M%S+00'00'")

        dct = {
            "aligned": 0,
            "sigflags": 3,
            "sigflagsft": 132,
            "sigpage": 0,
            "sigbutton": True,
            "sigfield": "Signature1",
            "auto_sigfield": True,
            "sigandcertify": True,
            "signaturebox": (0, 0, 0, 0),
            "signature": f"Digitally signed on {now.strftime('%Y-%m-%d %H:%M')}",
            "contact": "",
            "location": location,
            "signingdate": date_str,
            "reason": reason,
        }

        with open(pdf_path, "rb") as f:
            pdf_data = f.read()

        signed_data = pdf_cms.sign(
            pdf_data, dct,
            private_key, certificate,
            additional_certs or [],
            "sha256",
        )

        with open(output_path, "wb") as f:
            f.write(pdf_data)
            f.write(signed_data)

        return True

    except ImportError:
        return False
    except Exception as e:
        import logging
        logging.getLogger("PDFStudio.signature").warning(
            "Crypto signing failed: %s", type(e).__name__
        )
        return False
