"""
main.py — Main application window with Acrobat-style UI.
"""

import json
import logging
import os
import sys
from pathlib import Path

# ── Logging Setup ───────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("PDFStudio")

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QToolBar, QStatusBar, QLabel, QFileDialog, QSplitter,
    QListWidget, QListWidgetItem, QInputDialog, QMessageBox,
    QComboBox, QSpinBox, QDoubleSpinBox, QToolButton, QMenu, QScrollArea,
    QDockWidget, QFormLayout, QLineEdit, QCheckBox, QPushButton,
    QGroupBox, QTextEdit, QSizePolicy, QWidgetAction, QDialog,
    QColorDialog
)
from PyQt6.QtGui import (
    QPixmap, QIcon, QAction, QActionGroup, QKeySequence, QFont,
    QColor, QPainter, QPen, QBrush, QPageLayout
)
from PyQt6.QtCore import Qt, QSize, QTimer, QPointF, QMarginsF, QRectF, pyqtSignal
from PyQt6.QtPrintSupport import QPrinter, QPrintDialog

from pdf_engine import PDFEngine, PDFPasswordRequired
from canvas import PDFCanvas
from signature import SignatureDialog, apply_crypto_signature
from ocr import check_tesseract, ocr_full_document, get_available_languages
from email_sign import SigningStore, SendForSigningDialog, SigningTrackerDialog, SMTPSetupDialog


class RecentFilesStore:
    """Persistent MRU list of recently opened/saved PDFs.

    Stored as a JSON list at ~/.pdf_studio/recent_files.json. Capped at MAX
    entries; de-duplicated case-insensitively (os.path.normcase) so the same
    file under different case spellings on Windows doesn't appear twice.
    """

    MAX = 10

    def __init__(self, store_dir: str | None = None):
        if store_dir is None:
            store_dir = os.path.join(Path.home(), ".pdf_studio")
        os.makedirs(store_dir, exist_ok=True)
        self._path = os.path.join(store_dir, "recent_files.json")

    def load(self) -> list[str]:
        if not os.path.exists(self._path):
            return []
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return [p for p in data if isinstance(p, str)]
        except (OSError, ValueError):
            log.warning("Failed to read recent files store; resetting")
        return []

    def _save(self, items: list[str]):
        try:
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(items, f, indent=2)
        except OSError as e:
            log.warning("Failed to write recent files store: %s", e)

    def add(self, path: str):
        path = os.path.abspath(path)
        key = os.path.normcase(path)
        items = [p for p in self.load() if os.path.normcase(p) != key]
        items.insert(0, path)
        self._save(items[: self.MAX])

    def remove(self, path: str):
        key = os.path.normcase(os.path.abspath(path))
        items = [p for p in self.load() if os.path.normcase(p) != key]
        self._save(items)

    def clear(self):
        self._save([])


class ThumbnailSidebar(QListWidget):
    """Page thumbnail sidebar (Acrobat-style left panel)."""

    page_delete_requested = pyqtSignal(int)
    page_insert_requested = pyqtSignal(int, str)  # at_index, where ("before"/"after")
    pages_reordered_full = pyqtSignal(list)  # permutation: new[i] = old index

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setIconSize(QSize(120, 170))
        self.setSpacing(6)
        self.setViewMode(QListWidget.ViewMode.IconMode)
        self.setFlow(QListWidget.Flow.TopToBottom)
        self.setWrapping(False)
        self.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.setFixedWidth(150)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu)
        # Drag-drop reorder
        self.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setMovement(QListWidget.Movement.Snap)
        self.setStyleSheet("""
            QListWidget {
                background-color: #2b2b2b;
                border: none;
                padding: 4px;
            }
            QListWidget::item {
                background-color: #3c3c3c;
                border: 1px solid #555;
                border-radius: 4px;
                padding: 4px;
                margin: 2px;
                color: #ccc;
                text-align: center;
            }
            QListWidget::item:selected {
                background-color: #1a73e8;
                border-color: #5a9ef4;
            }
        """)

    def load_thumbnails(self, engine: PDFEngine):
        self.clear()
        for i in range(engine.page_count):
            png = engine.render_page_thumbnail(i, max_width=120)
            pix = QPixmap()
            pix.loadFromData(png)
            item = QListWidgetItem(QIcon(pix), f"Page {i + 1}")
            # Stable identity for permutation tracking after drag-drop reorder.
            item.setData(Qt.ItemDataRole.UserRole, i)
            self.addItem(item)

    def _on_context_menu(self, pos):
        """Show right-click menu on a thumbnail with structural page actions."""
        item = self.itemAt(pos)
        if item is None:
            return
        idx = self.row(item)
        menu = QMenu(self)
        a_before = menu.addAction("Insert Blank Page Before")
        a_after = menu.addAction("Insert Blank Page After")
        menu.addSeparator()
        a_delete = menu.addAction("Delete Page")
        if self.count() <= 1:
            a_delete.setEnabled(False)
            a_delete.setToolTip("Cannot delete the only page in the document")

        chosen = menu.exec(self.viewport().mapToGlobal(pos))
        if chosen is a_before:
            self.page_insert_requested.emit(idx, "before")
        elif chosen is a_after:
            self.page_insert_requested.emit(idx, "after")
        elif chosen is a_delete:
            self.page_delete_requested.emit(idx)

    def dropEvent(self, event):
        """Capture the new permutation after an internal drag-drop reorder."""
        before = [
            self.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.count())
        ]
        super().dropEvent(event)
        after = [
            self.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.count())
        ]
        if before != after:
            # `after[i]` is the old index of the page now at position i.
            self.pages_reordered_full.emit(after)


class FormPanel(QWidget):
    """Right-side panel for filling form fields."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(280)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(8, 8, 8, 8)
        self._title = QLabel("Form Fields")
        self._title.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        self._title.setStyleSheet("color: #ddd;")
        self._layout.addWidget(self._title)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        self._inner = QWidget()
        self._form_layout = QFormLayout(self._inner)
        self._form_layout.setContentsMargins(0, 0, 0, 0)
        self._scroll.setWidget(self._inner)
        self._layout.addWidget(self._scroll)
        self._field_widgets: dict[str, QWidget] = {}
        self._engine: PDFEngine | None = None
        self._page_index = 0

        self.setStyleSheet("""
            QWidget { background-color: #2b2b2b; color: #ddd; }
            QLineEdit { background: #3c3c3c; border: 1px solid #555; padding: 4px;
                        border-radius: 3px; color: #eee; }
            QCheckBox { color: #ddd; }
            QComboBox { background: #3c3c3c; border: 1px solid #555; padding: 4px; color: #eee; }
            QPushButton { background: #1a73e8; color: white; padding: 6px 16px;
                         border-radius: 3px; border: none; }
            QPushButton:hover { background: #1557b0; }
        """)

        apply_btn = QPushButton("Apply Form Values")
        apply_btn.clicked.connect(self._apply_values)
        self._layout.addWidget(apply_btn)

    def load_fields(self, engine: PDFEngine, page_index: int):
        self._engine = engine
        self._page_index = page_index
        for i in reversed(range(self._form_layout.rowCount())):
            self._form_layout.removeRow(i)
        self._field_widgets.clear()

        fields = engine.extract_form_fields(page_index)
        if not fields:
            self._form_layout.addRow(QLabel("No form fields on this page."))
            return

        for f in fields:
            label = f.field_name or "(unnamed)"
            if f.field_type in ("Text",):
                w = QLineEdit(f.value)
                self._field_widgets[f.field_name] = w
                self._form_layout.addRow(label, w)
            elif f.field_type == "CheckBox":
                w = QCheckBox()
                w.setChecked(f.value == "Yes")
                self._field_widgets[f.field_name] = w
                self._form_layout.addRow(label, w)
            elif f.field_type in ("ComboBox", "ListBox"):
                w = QComboBox()
                w.addItems(f.options)
                if f.value in f.options:
                    w.setCurrentText(f.value)
                self._field_widgets[f.field_name] = w
                self._form_layout.addRow(label, w)

    def _apply_values(self):
        if not self._engine:
            return
        for name, widget in self._field_widgets.items():
            if isinstance(widget, QLineEdit):
                self._engine.fill_form_field(self._page_index, name, widget.text())
            elif isinstance(widget, QCheckBox):
                self._engine.fill_form_field(
                    self._page_index, name,
                    "Yes" if widget.isChecked() else "Off"
                )
            elif isinstance(widget, QComboBox):
                self._engine.fill_form_field(self._page_index, name, widget.currentText())
        QMessageBox.information(self, "Forms", "Form values applied. Re-rendering page...")


class SearchBar(QWidget):
    """Inline search bar for Ctrl+F text search."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(40)
        self.setVisible(False)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(6)

        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("Find in document...")
        self._search_input.setClearButtonEnabled(True)
        self._search_input.returnPressed.connect(self._on_next)
        layout.addWidget(self._search_input, 1)

        self._result_label = QLabel("")
        self._result_label.setFixedWidth(90)
        self._result_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._result_label)

        prev_btn = QPushButton("Prev")
        prev_btn.setFixedWidth(50)
        prev_btn.clicked.connect(self._on_prev)
        layout.addWidget(prev_btn)

        next_btn = QPushButton("Next")
        next_btn.setFixedWidth(50)
        next_btn.clicked.connect(self._on_next)
        layout.addWidget(next_btn)

        close_btn = QPushButton("X")
        close_btn.setFixedWidth(28)
        close_btn.clicked.connect(self.hide_search)
        layout.addWidget(close_btn)

        self.setStyleSheet("""
            SearchBar {
                background: #333;
                border-bottom: 1px solid #555;
            }
            QLineEdit {
                background: #3c3c3c; color: #eee; border: 1px solid #555;
                border-radius: 3px; padding: 4px 8px; font-size: 13px;
            }
            QPushButton {
                background: #444; color: #ddd; border: 1px solid #555;
                border-radius: 3px; padding: 4px;
            }
            QPushButton:hover { background: #555; }
            QLabel { color: #aaa; font-size: 11px; }
        """)

        # Search state
        self._results: list = []
        self._current_index: int = -1
        self._search_callback = None  # set by MainWindow
        self._navigate_callback = None

    def show_search(self):
        """Show the search bar and focus the input."""
        self.setVisible(True)
        self._search_input.setFocus()
        self._search_input.selectAll()

    def hide_search(self):
        """Hide the search bar and clear highlights."""
        self.setVisible(False)
        self._results.clear()
        self._current_index = -1
        self._result_label.setText("")
        if self._navigate_callback:
            self._navigate_callback(-1, clear=True)

    def set_callbacks(self, search_fn, navigate_fn):
        """Set callbacks: search_fn(query) -> results, navigate_fn(index, clear)."""
        self._search_callback = search_fn
        self._navigate_callback = navigate_fn
        self._search_input.textChanged.connect(self._on_search_changed)

    def _on_search_changed(self, text: str):
        if not self._search_callback:
            return
        text = text.strip()
        if len(text) < 2:
            self._results.clear()
            self._current_index = -1
            self._result_label.setText("")
            if self._navigate_callback:
                self._navigate_callback(-1, clear=True)
            return

        self._results = self._search_callback(text)
        if self._results:
            self._current_index = 0
            self._update_label()
            if self._navigate_callback:
                self._navigate_callback(0, clear=False)
        else:
            self._current_index = -1
            self._result_label.setText("No results")
            if self._navigate_callback:
                self._navigate_callback(-1, clear=True)

    def _on_next(self):
        if not self._results:
            return
        self._current_index = (self._current_index + 1) % len(self._results)
        self._update_label()
        if self._navigate_callback:
            self._navigate_callback(self._current_index, clear=False)

    def _on_prev(self):
        if not self._results:
            return
        self._current_index = (self._current_index - 1) % len(self._results)
        self._update_label()
        if self._navigate_callback:
            self._navigate_callback(self._current_index, clear=False)

    def _update_label(self):
        if self._results:
            self._result_label.setText(
                f"{self._current_index + 1} / {len(self._results)}"
            )
        else:
            self._result_label.setText("No results")


class TextEditDialog(QDialog):
    """Dialog for editing PDF text with font controls."""

    FONT_MAP = {
        "Helvetica": "helv",
        "Times Roman": "tiro",
        "Courier": "cour",
    }

    def __init__(self, block, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit Text")
        self.setMinimumWidth(500)
        self._block = block
        self._color = QColor(
            int(block.color[0] * 255),
            int(block.color[1] * 255),
            int(block.color[2] * 255),
        )
        self.result = None  # (new_text, font_size, font_name, color_tuple)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(16, 16, 16, 16)

        info = QLabel(f"Original: \"{self._block.text[:100]}\"")
        info.setWordWrap(True)
        info.setStyleSheet("color: #999; font-size: 11px;")
        layout.addWidget(info)

        layout.addWidget(QLabel("Text:"))
        self._text_edit = QLineEdit(self._block.text)
        self._text_edit.setStyleSheet(
            "QLineEdit { font-size: 14px; padding: 8px; background: #3c3c3c; "
            "color: #eee; border: 1px solid #555; border-radius: 4px; }"
        )
        self._text_edit.selectAll()
        layout.addWidget(self._text_edit)

        controls = QHBoxLayout()
        controls.setSpacing(12)

        font_group = QVBoxLayout()
        font_group.addWidget(QLabel("Font:"))
        self._font_combo = QComboBox()
        self._font_combo.addItems(self.FONT_MAP.keys())
        self._font_combo.setCurrentIndex(0)
        self._font_combo.setFixedWidth(140)
        font_group.addWidget(self._font_combo)
        controls.addLayout(font_group)

        size_group = QVBoxLayout()
        size_group.addWidget(QLabel("Size:"))
        self._size_spin = QDoubleSpinBox()
        self._size_spin.setRange(4.0, 72.0)
        self._size_spin.setSingleStep(0.5)
        self._size_spin.setValue(self._block.font_size)
        self._size_spin.setSuffix(" pt")
        self._size_spin.setFixedWidth(100)
        size_group.addWidget(self._size_spin)
        controls.addLayout(size_group)

        color_group = QVBoxLayout()
        color_group.addWidget(QLabel("Color:"))
        self._color_btn = QPushButton()
        self._color_btn.setFixedSize(60, 30)
        self._update_color_btn()
        self._color_btn.clicked.connect(self._pick_color)
        color_group.addWidget(self._color_btn)
        controls.addLayout(color_group)

        controls.addStretch()
        layout.addLayout(controls)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        apply_btn = QPushButton("Apply")
        apply_btn.setDefault(True)
        apply_btn.clicked.connect(self._on_apply)
        apply_btn.setStyleSheet(
            "QPushButton { background: #1a73e8; color: white; padding: 8px 20px; "
            "border-radius: 4px; font-weight: bold; }"
        )
        btn_row.addWidget(apply_btn)
        layout.addLayout(btn_row)

    def _update_color_btn(self):
        self._color_btn.setStyleSheet(
            f"QPushButton {{ background-color: {self._color.name()}; "
            f"border: 1px solid #888; border-radius: 4px; }}"
        )

    def _pick_color(self):
        color = QColorDialog.getColor(self._color, self, "Text Color")
        if color.isValid():
            self._color = color
            self._update_color_btn()

    def _on_apply(self):
        text = self._text_edit.text()
        if not text.strip():
            QMessageBox.warning(self, "Empty", "Text cannot be empty.")
            return

        font_display = self._font_combo.currentText()
        font_name = self.FONT_MAP.get(font_display, "helv")
        font_size = self._size_spin.value()
        color = (
            self._color.redF(),
            self._color.greenF(),
            self._color.blueF(),
        )
        self.result = (text, font_size, font_name, color)
        self.accept()


class OverlayTextEditDialog(QDialog):
    """Dialog for editing an overlay text annotation's formatting."""

    def __init__(self, text_item, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit Text Annotation")
        self.setMinimumWidth(460)
        self._item = text_item
        self._color = text_item.defaultTextColor()
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(16, 16, 16, 16)

        # Text content
        layout.addWidget(QLabel("Text:"))
        self._text_edit = QLineEdit(self._item.toPlainText())
        self._text_edit.setStyleSheet(
            "QLineEdit { font-size: 14px; padding: 8px; background: #3c3c3c; "
            "color: #eee; border: 1px solid #555; border-radius: 4px; }"
        )
        self._text_edit.selectAll()
        layout.addWidget(self._text_edit)

        # Controls row
        controls = QHBoxLayout()
        controls.setSpacing(12)

        # Font family
        font_group = QVBoxLayout()
        font_group.addWidget(QLabel("Font:"))
        self._font_combo = QComboBox()
        self._font_combo.addItems(["Helvetica", "Times New Roman", "Courier",
                                   "Arial", "Georgia", "Verdana"])
        # Match current font
        current_family = self._item.font().family()
        idx = self._font_combo.findText(current_family)
        self._font_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._font_combo.setFixedWidth(160)
        font_group.addWidget(self._font_combo)
        controls.addLayout(font_group)

        # Font size
        size_group = QVBoxLayout()
        size_group.addWidget(QLabel("Size:"))
        self._size_spin = QDoubleSpinBox()
        self._size_spin.setRange(4.0, 72.0)
        self._size_spin.setSingleStep(0.5)
        self._size_spin.setValue(self._item.font().pointSizeF())
        self._size_spin.setSuffix(" pt")
        self._size_spin.setFixedWidth(100)
        size_group.addWidget(self._size_spin)
        controls.addLayout(size_group)

        # Color
        color_group = QVBoxLayout()
        color_group.addWidget(QLabel("Color:"))
        self._color_btn = QPushButton()
        self._color_btn.setFixedSize(60, 30)
        self._update_color_btn()
        self._color_btn.clicked.connect(self._pick_color)
        color_group.addWidget(self._color_btn)
        controls.addLayout(color_group)

        controls.addStretch()
        layout.addLayout(controls)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        apply_btn = QPushButton("Apply")
        apply_btn.setDefault(True)
        apply_btn.clicked.connect(self._on_apply)
        apply_btn.setStyleSheet(
            "QPushButton { background: #1a73e8; color: white; padding: 8px 20px; "
            "border-radius: 4px; font-weight: bold; }"
        )
        btn_row.addWidget(apply_btn)
        layout.addLayout(btn_row)

    def _update_color_btn(self):
        self._color_btn.setStyleSheet(
            f"QPushButton {{ background-color: {self._color.name()}; "
            f"border: 1px solid #888; border-radius: 4px; }}"
        )

    def _pick_color(self):
        color = QColorDialog.getColor(self._color, self, "Text Color")
        if color.isValid():
            self._color = color
            self._update_color_btn()

    def _on_apply(self):
        text = self._text_edit.text()
        if not text.strip():
            QMessageBox.warning(self, "Empty", "Text cannot be empty.")
            return

        # Apply changes to the overlay item
        self._item.setPlainText(text)
        font = QFont(self._font_combo.currentText(), int(self._size_spin.value()))
        self._item.setFont(font)
        self._item.setDefaultTextColor(self._color)
        self.accept()


class DrawSubToolbar(QWidget):
    """Contextual sub-toolbar shown only when the Draw tool is active.

    Layout: [Pen | Pencil | Marker]  |  [S | M | L]  |  [color swatches] [Custom...]

    Exposes one signal — settings_changed(tool, color, base_width) — emitted
    whenever any control changes. MainWindow connects this to the canvas's
    set_draw_tool / set_draw_color / set_draw_size setters.
    """

    settings_changed = pyqtSignal(str, QColor, float)

    # (label, tool key)
    TOOLS = [("Pen", "pen"), ("Pencil", "pencil"), ("Marker", "marker")]

    # (label, base width). The active drawing tool's width multiplier is
    # applied on top of this in the canvas (e.g. Marker × 3.2 of base).
    SIZES = [("S", 1.5), ("M", 2.5), ("L", 4.0)]

    # Preset colors. Black first since it's the most common pick.
    PRESET_COLORS = [
        ("Black",   "#000000"),
        ("Red",     "#d93025"),
        ("Orange",  "#f29900"),
        ("Yellow",  "#fbbc04"),
        ("Green",   "#188038"),
        ("Blue",    "#1a73e8"),
        ("Purple",  "#9334e6"),
        ("White",   "#ffffff"),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(36)
        self._tool = "pen"
        self._color = QColor("#000000")
        self._base_width = 2.5  # corresponds to "M"
        self._tool_buttons: dict[str, QPushButton] = {}
        self._size_buttons: dict[str, QPushButton] = {}
        self._color_buttons: list[QPushButton] = []
        self._build_ui()

    def _build_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 4, 10, 4)
        layout.setSpacing(4)

        # ── Tool type ──
        for label, key in self.TOOLS:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setFixedHeight(26)
            btn.setMinimumWidth(56)
            btn.clicked.connect(lambda _, k=key: self._select_tool(k))
            self._tool_buttons[key] = btn
            layout.addWidget(btn)
        self._tool_buttons["pen"].setChecked(True)

        layout.addWidget(self._make_separator())

        # ── Size ──
        size_label = QLabel("Size:")
        size_label.setStyleSheet("color: #aaa; padding: 0 4px;")
        layout.addWidget(size_label)
        for label, width in self.SIZES:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setFixedSize(28, 26)
            btn.clicked.connect(lambda _, w=width: self._select_size(w))
            self._size_buttons[label] = btn
            layout.addWidget(btn)
        self._size_buttons["M"].setChecked(True)

        layout.addWidget(self._make_separator())

        # ── Color presets ──
        color_label = QLabel("Color:")
        color_label.setStyleSheet("color: #aaa; padding: 0 4px;")
        layout.addWidget(color_label)
        for name, hex_color in self.PRESET_COLORS:
            btn = QPushButton()
            btn.setFixedSize(22, 22)
            btn.setToolTip(name)
            btn.setProperty("color_hex", hex_color)
            btn.clicked.connect(
                lambda _, c=hex_color: self._select_color_preset(c)
            )
            self._color_buttons.append(btn)
            layout.addWidget(btn)

        custom_btn = QPushButton("Custom...")
        custom_btn.setFixedHeight(26)
        custom_btn.clicked.connect(self._pick_custom_color)
        layout.addWidget(custom_btn)

        layout.addStretch()

        self._apply_styles()
        self._refresh_visual_state()

    def _make_separator(self) -> QWidget:
        sep = QWidget()
        sep.setFixedWidth(1)
        sep.setFixedHeight(20)
        sep.setStyleSheet("background: #555; margin: 0 6px;")
        return sep

    def _apply_styles(self):
        self.setStyleSheet("""
            DrawSubToolbar { background: #2b2b2b; border-bottom: 1px solid #444; }
            QPushButton {
                background: #3c3c3c; color: #ddd;
                border: 1px solid #555; border-radius: 3px;
                padding: 2px 8px; font-size: 12px;
            }
            QPushButton:hover { background: #4a4a4a; }
            QPushButton:checked {
                background: #1a73e8; color: white; border-color: #5a9ef4;
            }
        """)

    def _refresh_visual_state(self):
        """Refresh checked states and color swatches to match current settings."""
        for key, btn in self._tool_buttons.items():
            btn.setChecked(key == self._tool)
        # Match size button by base width
        for label, width in self.SIZES:
            self._size_buttons[label].setChecked(abs(self._base_width - width) < 0.01)
        # Repaint color swatches — outline the one matching the active color
        active_hex = self._color.name().lower()
        for btn in self._color_buttons:
            hex_color = btn.property("color_hex")
            is_active = hex_color.lower() == active_hex
            border = "#ffffff" if is_active else "#888"
            border_w = "2px" if is_active else "1px"
            btn.setStyleSheet(
                f"QPushButton {{ background: {hex_color}; "
                f"border: {border_w} solid {border}; border-radius: 3px; }}"
            )

    def _select_tool(self, key: str):
        self._tool = key
        self._refresh_visual_state()
        self._emit()

    def _select_size(self, width: float):
        self._base_width = width
        self._refresh_visual_state()
        self._emit()

    def _select_color_preset(self, hex_color: str):
        self._color = QColor(hex_color)
        self._refresh_visual_state()
        self._emit()

    def _pick_custom_color(self):
        color = QColorDialog.getColor(self._color, self, "Pick a Drawing Color")
        if color.isValid():
            self._color = QColor(color.red(), color.green(), color.blue())
            self._refresh_visual_state()
            self._emit()

    def _emit(self):
        self.settings_changed.emit(self._tool, QColor(self._color), self._base_width)

    # Public read-only accessors
    @property
    def tool(self) -> str: return self._tool

    @property
    def color(self) -> QColor: return QColor(self._color)

    @property
    def base_width(self) -> float: return self._base_width


class MainWindow(QMainWindow):
    """Main application window — Acrobat-style PDF editor."""

    APP_NAME = "PDF Studio"

    def __init__(self):
        super().__init__()
        self.setWindowTitle(self.APP_NAME)
        self.resize(1280, 860)

        # Core components
        self._engine = PDFEngine()
        self._base_zoom = 2.0  # base render factor (before DPI scaling)
        self._zoom = self._base_zoom  # effective zoom, updated on first render
        self._current_page = 0
        self._pending_sig_pos: QPointF | None = None
        self._pending_sig_data: bytes | None = None
        self._pending_crypto: dict = {}
        self._signing_store = SigningStore()
        self._recent_store = RecentFilesStore()

        # Save state: _save_path is the working copy. The original file
        # (engine.file_path) is never overwritten. First Save prompts
        # for a new location; subsequent Saves write there directly.
        self._save_path: str | None = None

        # Search state
        self._search_results: list = []

        self._build_ui()
        self._build_menubar()
        self._build_toolbar()
        self._connect_signals()
        self._apply_dark_theme()

        self.setAcceptDrops(True)

    # ── UI Construction ─────────────────────────────────────────

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Search bar (hidden by default)
        self._search_bar = SearchBar()
        self._search_bar.set_callbacks(self._do_search, self._navigate_search)
        layout.addWidget(self._search_bar)

        # Draw sub-toolbar (shown only when the Draw tool is active)
        self._draw_subtoolbar = DrawSubToolbar()
        self._draw_subtoolbar.settings_changed.connect(self._on_draw_settings_changed)
        self._draw_subtoolbar.hide()
        layout.addWidget(self._draw_subtoolbar)

        # Splitter: sidebar | canvas | form panel
        self._splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left: page thumbnails
        self._sidebar = ThumbnailSidebar()
        self._splitter.addWidget(self._sidebar)

        # Center: PDF canvas
        self._canvas = PDFCanvas()
        self._splitter.addWidget(self._canvas)

        # Right: form panel (hidden by default)
        self._form_panel = FormPanel()
        self._form_panel.hide()
        self._splitter.addWidget(self._form_panel)

        self._splitter.setStretchFactor(0, 0)
        self._splitter.setStretchFactor(1, 1)
        self._splitter.setStretchFactor(2, 0)

        layout.addWidget(self._splitter)

        # Status bar
        self._status = QStatusBar()
        self.setStatusBar(self._status)
        self._page_label = QLabel("No document")
        self._zoom_label = QLabel("100%")
        self._tool_label = QLabel("Select")
        self._status.addWidget(self._page_label, 1)
        self._status.addPermanentWidget(self._tool_label)
        self._status.addPermanentWidget(self._zoom_label)

    def _make_action(self, text, slot, shortcut=None, parent=None):
        """Helper to create a QAction compatible with PyQt6."""
        act = QAction(text, parent or self)
        act.triggered.connect(slot)
        if shortcut:
            act.setShortcut(
                shortcut if isinstance(shortcut, QKeySequence)
                else QKeySequence(shortcut)
            )
        return act

    def _make_eraser_icon(self, size: int = 64) -> QIcon:
        """Generate a stylized eraser icon (blue cap, pink rubber, tilted)."""
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.translate(size / 2, size / 2)
        p.rotate(-25)

        body_w = size * 0.68
        body_h = size * 0.32
        cap_h = body_h * 0.42

        # Pink rubber body
        p.setBrush(QBrush(QColor("#f5a89e")))
        p.setPen(QPen(QColor("#7a3a32"), 1.5))
        p.drawRoundedRect(
            QRectF(-body_w / 2, -body_h / 2 + cap_h, body_w, body_h - cap_h), 3, 3
        )

        # Blue holder cap
        p.setBrush(QBrush(QColor("#5e9bd6")))
        p.setPen(QPen(QColor("#2c4d6e"), 1.5))
        p.drawRoundedRect(
            QRectF(-body_w / 2, -body_h / 2, body_w, cap_h), 3, 3
        )

        p.end()
        return QIcon(pm)

    def _build_menubar(self):
        mb = self.menuBar()

        # File menu
        file_menu = mb.addMenu("&File")
        self._act_open = self._make_action(
            "&Open...", self._open_file, QKeySequence.StandardKey.Open
        )
        file_menu.addAction(self._act_open)
        self._recent_menu = file_menu.addMenu("Open &Recent")
        self._rebuild_recent_menu()
        file_menu.addSeparator()
        self._act_save = self._make_action(
            "&Save", self._save_file, QKeySequence.StandardKey.Save
        )
        file_menu.addAction(self._act_save)
        self._act_save_as = self._make_action("Save &As...", self._save_as, "Ctrl+Shift+S")
        file_menu.addAction(self._act_save_as)
        file_menu.addSeparator()
        file_menu.addAction(
            self._make_action("&Print...", self._print_document, "Ctrl+P")
        )
        file_menu.addSeparator()
        file_menu.addAction(self._make_action("E&xit", self.close, "Ctrl+Q"))

        # Edit menu
        edit_menu = mb.addMenu("&Edit")
        edit_menu.addAction(self._make_action("&Undo", self._undo, "Ctrl+Z"))
        edit_menu.addAction(self._make_action("&Redo", self._redo, "Ctrl+Shift+Z"))
        edit_menu.addSeparator()
        edit_menu.addAction(self._make_action("&Find...", self._toggle_search, "Ctrl+F"))
        edit_menu.addSeparator()
        edit_menu.addAction(
            self._make_action("Clear All Overlays", self._canvas.clear_overlays)
        )

        # View menu
        view_menu = mb.addMenu("&View")
        view_menu.addAction(self._make_action("Zoom &In", self._canvas.zoom_in, "Ctrl+="))
        view_menu.addAction(self._make_action("Zoom &Out", self._canvas.zoom_out, "Ctrl+-"))
        view_menu.addAction(self._make_action("&Fit Width", self._canvas.fit_width, "Ctrl+0"))
        view_menu.addSeparator()
        rotate_left = self._make_action(
            "Rotate Page &Left", lambda: self._rotate_current_page(-90), "Ctrl+L"
        )
        rotate_left.setToolTip("Rotate the current page 90° counter-clockwise (cannot be undone)")
        view_menu.addAction(rotate_left)
        rotate_right = self._make_action(
            "Rotate Page &Right", lambda: self._rotate_current_page(90), "Ctrl+R"
        )
        rotate_right.setToolTip("Rotate the current page 90° clockwise (cannot be undone)")
        view_menu.addAction(rotate_right)
        view_menu.addSeparator()
        self._act_toggle_sidebar = QAction("Toggle &Sidebar", self)
        self._act_toggle_sidebar.setCheckable(True)
        self._act_toggle_sidebar.setChecked(True)
        self._act_toggle_sidebar.toggled.connect(lambda v: self._sidebar.setVisible(v))
        view_menu.addAction(self._act_toggle_sidebar)
        self._act_toggle_forms = QAction("Toggle &Form Panel", self)
        self._act_toggle_forms.setCheckable(True)
        self._act_toggle_forms.setChecked(False)
        self._act_toggle_forms.toggled.connect(self._toggle_form_panel)
        view_menu.addAction(self._act_toggle_forms)

        # Tools menu
        tools_menu = mb.addMenu("&Tools")
        tools_menu.addAction(
            self._make_action("&Select", lambda: self._set_tool("select"), "V")
        )
        tools_menu.addAction(
            self._make_action("&Highlight", lambda: self._set_tool("highlight"), "H")
        )
        tools_menu.addAction(
            self._make_action("Add &Text", lambda: self._set_tool("text"), "T")
        )
        tools_menu.addAction(
            self._make_action("&Freehand Draw", lambda: self._set_tool("freehand"), "D")
        )
        tools_menu.addAction(
            self._make_action("E&raser", lambda: self._set_tool("eraser"), "X")
        )
        tools_menu.addAction(
            self._make_action("&Edit Text", lambda: self._set_tool("edit_text"), "E")
        )
        tools_menu.addSeparator()
        tools_menu.addAction(self._make_action("&Sign...", self._open_signature_dialog, "S"))
        tools_menu.addSeparator()
        tools_menu.addAction(
            self._make_action("&OCR Scanned Pages...", self._run_ocr)
        )
        tools_menu.addAction(
            self._make_action("Send for Si&gnature...", self._send_for_signing)
        )
        tools_menu.addAction(
            self._make_action("Signing &Requests...", self._view_signing_requests)
        )

        # Settings menu
        settings_menu = mb.addMenu("S&ettings")
        settings_menu.addAction(self._make_action("&Email Setup...", self._open_smtp_setup))

    def _build_toolbar(self):
        tb = QToolBar("Main Toolbar")
        tb.setMovable(False)
        tb.setIconSize(QSize(20, 20))
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.addToolBar(tb)

        tb.addAction("Open", self._open_file)
        tb.addAction("Save", self._save_file)
        tb.addAction("Print", self._print_document)
        tb.addSeparator()

        # Tool buttons
        self._tool_buttons: dict[str, QAction] = {}
        for name, mode, shortcut in [
            ("Select", "select", "V"),
            ("Highlight", "highlight", "H"),
            ("Text", "text", "T"),
            ("Draw", "freehand", "D"),
            # Eraser handled below — needs an icon and a size dropdown.
            # Edit Text added after Eraser so visual order stays Draw → Eraser → Edit Text.
        ]:
            act = tb.addAction(name)
            act.setCheckable(True)
            act.setShortcut(QKeySequence(shortcut))
            act.triggered.connect(lambda checked, m=mode: self._set_tool(m))
            self._tool_buttons[mode] = act

        # Eraser: icon + text button with a size dropdown.
        # The defaultAction approach lets us reuse the standard checked-state
        # / shortcut pipeline alongside the popup menu.
        self._eraser_action = QAction(self._make_eraser_icon(), "Eraser", self)
        self._eraser_action.setCheckable(True)
        self._eraser_action.setShortcut(QKeySequence("X"))
        self._eraser_action.triggered.connect(lambda: self._set_tool("eraser"))
        # Add to the window so the keyboard shortcut works app-wide.
        self.addAction(self._eraser_action)

        self._eraser_btn = QToolButton()
        self._eraser_btn.setDefaultAction(self._eraser_action)
        self._eraser_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._eraser_btn.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)

        size_menu = QMenu(self._eraser_btn)
        self._eraser_size_group = QActionGroup(self)
        self._eraser_size_group.setExclusive(True)
        # (label, viewport-pixel radius, is_default)
        for label, radius, is_default in [
            ("Small",  12, False),
            ("Medium", 22, True),
            ("Large",  40, False),
        ]:
            sa = QAction(label, self)
            sa.setCheckable(True)
            sa.setChecked(is_default)
            sa.triggered.connect(
                lambda checked, r=radius: self._canvas.set_eraser_radius(r)
            )
            self._eraser_size_group.addAction(sa)
            size_menu.addAction(sa)
        self._eraser_btn.setMenu(size_menu)
        tb.addWidget(self._eraser_btn)

        # Edit Text — added after the Eraser widget so display order is correct.
        edit_text_act = tb.addAction("Edit Text")
        edit_text_act.setCheckable(True)
        edit_text_act.setShortcut(QKeySequence("E"))
        edit_text_act.triggered.connect(lambda checked: self._set_tool("edit_text"))
        self._tool_buttons["edit_text"] = edit_text_act

        self._tool_buttons["select"].setChecked(True)

        tb.addSeparator()
        tb.addAction("Sign", self._open_signature_dialog)
        tb.addAction("Send", self._send_for_signing)

        tb.addSeparator()
        tb.addAction("Find", self._toggle_search)
        tb.addAction("Zoom In", self._canvas.zoom_in)
        tb.addAction("Zoom Out", self._canvas.zoom_out)
        tb.addAction("Fit Width", self._canvas.fit_width)

        # Page nav
        tb.addSeparator()
        tb.addAction("Prev", self._prev_page)
        self._page_spin = QSpinBox()
        self._page_spin.setMinimum(1)
        self._page_spin.setMaximum(1)
        self._page_spin.setPrefix("Page ")
        self._page_spin.setFixedWidth(100)
        self._page_spin.valueChanged.connect(lambda v: self._go_to_page(v - 1))
        tb.addWidget(self._page_spin)
        tb.addAction("Next", self._next_page)

        tb.setStyleSheet("""
            QToolBar {
                background: #333;
                border-bottom: 1px solid #444;
                spacing: 4px;
                padding: 2px 8px;
            }
            QToolButton {
                color: #ddd;
                background: transparent;
                border: 1px solid transparent;
                border-radius: 3px;
                padding: 4px 8px;
                font-size: 12px;
            }
            QToolButton:hover { background: #444; }
            QToolButton:checked { background: #1a73e8; color: white; }
            QToolButton:pressed { background: #555; }
            QSpinBox {
                background: #3c3c3c;
                color: #ddd;
                border: 1px solid #555;
                border-radius: 3px;
                padding: 3px;
            }
        """)

    def _connect_signals(self):
        self._sidebar.currentRowChanged.connect(self._on_thumbnail_clicked)
        self._sidebar.page_delete_requested.connect(self._on_page_delete)
        self._sidebar.page_insert_requested.connect(self._on_page_insert)
        self._sidebar.pages_reordered_full.connect(self._on_pages_reordered)
        self._canvas.point_clicked.connect(self._on_canvas_click)
        self._canvas.overlay_text_edit.connect(self._on_overlay_text_edit)
        self._canvas.signature_edit_requested.connect(self._on_signature_edit)
        self._canvas.rect_selected.connect(self._on_rect_selected)
        self._canvas.zoom_changed.connect(
            lambda z: self._zoom_label.setText(f"{int(z * 100)}%")
        )
        self._canvas.visible_pages_changed.connect(self._update_rendered_pages)

    def _apply_dark_theme(self):
        self.setStyleSheet("""
            QMainWindow { background-color: #1e1e1e; }
            QMenuBar { background: #2b2b2b; color: #ddd; border-bottom: 1px solid #444; }
            QMenuBar::item:selected { background: #444; }
            QMenu { background: #333; color: #ddd; border: 1px solid #555; }
            QMenu::item:selected { background: #1a73e8; }
            QStatusBar { background: #2b2b2b; color: #aaa; border-top: 1px solid #444; }
            QSplitter::handle { background: #444; width: 2px; }
            QLabel { color: #ddd; }
        """)

    # ── File Operations ─────────────────────────────────────────

    def _rebuild_recent_menu(self):
        """Refresh the Open Recent submenu from the persistent store."""
        self._recent_menu.clear()
        items = self._recent_store.load()
        if not items:
            empty = QAction("(No recent files)", self)
            empty.setEnabled(False)
            self._recent_menu.addAction(empty)
            return
        for p in items:
            display = Path(p).name
            act = QAction(display, self)
            act.setToolTip(p)
            act.triggered.connect(lambda _checked, p=p: self._open_recent(p))
            self._recent_menu.addAction(act)
        self._recent_menu.addSeparator()
        clear = QAction("Clear Recent Files", self)
        clear.triggered.connect(self._clear_recent_files)
        self._recent_menu.addAction(clear)

    def _open_recent(self, path: str):
        if not os.path.exists(path):
            log.info("Recent file no longer exists: %s", path)
            self._recent_store.remove(path)
            self._rebuild_recent_menu()
            QMessageBox.warning(
                self, "File Not Found",
                f"The file no longer exists:\n{path}\n\nIt has been removed from "
                f"your recent files.",
            )
            return
        self._open_file(path)

    def _clear_recent_files(self):
        self._recent_store.clear()
        self._rebuild_recent_menu()

    def _prompt_and_open_encrypted(self, path: str) -> int | None:
        """Prompt for a password and try to open an encrypted PDF.

        Returns the page count on success, None if the user cancelled
        or all attempts failed (after showing the user a warning).
        """
        for attempt in range(3):
            pwd, ok = QInputDialog.getText(
                self,
                "Password Required",
                f"Enter password for {Path(path).name}:",
                QLineEdit.EchoMode.Password,
            )
            if not ok:
                log.info("User cancelled password prompt")
                return None
            try:
                return self._engine.open_with_password(path, pwd)
            except PDFPasswordRequired:
                if attempt < 2:
                    QMessageBox.warning(
                        self, "Incorrect Password",
                        "The password was incorrect. Please try again.",
                    )
                    continue
                QMessageBox.critical(
                    self, "Incorrect Password",
                    "The password was incorrect. Aborting.",
                )
                return None
            except Exception as e:
                log.error("Failed to open encrypted PDF: %s", e)
                QMessageBox.critical(self, "Error", f"Failed to open PDF:\n{e}")
                return None
        return None

    def _open_file(self, path: str = ""):
        """Open a PDF file. Can be called with a path or interactively."""
        if not path:
            path, _ = QFileDialog.getOpenFileName(
                self, "Open PDF", "", "PDF Files (*.pdf);;All Files (*)"
            )
        if not path:
            return
        log.info("Opening file: %s", path)
        try:
            count = self._engine.open(path)
        except PDFPasswordRequired:
            count = self._prompt_and_open_encrypted(path)
            if count is None:
                return
        except Exception as e:
            log.error("Failed to open PDF: %s", e)
            QMessageBox.critical(self, "Error", f"Failed to open PDF:\n{e}")
            return
        log.info("Opened PDF: %d pages", count)
        self._recent_store.add(path)
        self._rebuild_recent_menu()
        self.setWindowTitle(f"{self.APP_NAME} — {Path(path).name}")
        self._save_path = None  # reset working copy for new document
        self._page_spin.setMaximum(count)
        self._page_spin.setValue(1)

        self._status.showMessage("Preparing pages...")
        QApplication.processEvents()

        # Scale render resolution to match display DPI for sharp text.
        dpr = self.screen().devicePixelRatio() if self.screen() else 1.0
        self._zoom = self._base_zoom * max(dpr, 1.0)
        log.debug("Render zoom=%.2f (base=%.1f, dpr=%.1f)", self._zoom, self._base_zoom, dpr)

        # Lazy load: lay out placeholder PageItems sized to each page,
        # then render only the visible window. This keeps open-time
        # constant in document length — a 500-page PDF opens as fast
        # as a 5-page one.
        sizes = []
        for i in range(count):
            w, h = self._engine.get_page_size(i)
            sizes.append((w * self._zoom, h * self._zoom))
        self._canvas.prepare_pages(sizes)
        self._sidebar.load_thumbnails(self._engine)
        self._canvas.fit_width()
        # fit_width changes zoom which fires the visible-pages-changed
        # signal; that callback paints the visible window. We also call
        # it directly here so the first paint isn't blank.
        self._update_rendered_pages()
        log.info("Document loaded: %d pages (lazy)", count)
        self._status.showMessage(f"Loaded {count} pages", 3000)

        self._load_forms_for_page(0)

    def _update_rendered_pages(self, *_):
        """Render any visible pages that aren't loaded yet; evict far ones.

        Called whenever the visible page range changes (scroll, zoom,
        resize, document load). Connected to canvas.visible_pages_changed.
        """
        if not self._engine.doc:
            return
        first, last = self._canvas.visible_page_range()
        if last < first:
            return

        margin = 2  # render this many pages above/below the viewport
        keep_first = max(0, first - margin)
        keep_last = min(self._engine.page_count - 1, last + margin)

        # Render anything in the keep window that isn't loaded yet.
        for i in range(keep_first, keep_last + 1):
            if not self._canvas.is_page_loaded(i):
                png = self._engine.render_page(i, zoom=self._zoom)
                pix = QPixmap()
                pix.loadFromData(png)
                self._canvas.set_page_pixmap(i, pix)

        # Evict pages well outside the keep window so memory stays bounded.
        # The eviction radius is intentionally larger than the keep window
        # so a small scroll back and forth doesn't thrash the renderer.
        eviction_radius = 15
        for i in range(self._engine.page_count):
            if i < keep_first - eviction_radius or i > keep_last + eviction_radius:
                if self._canvas.is_page_loaded(i):
                    self._canvas.clear_page_pixmap(i)

    def _burn_overlays(self) -> tuple[bool, list[dict]]:
        """Collect overlay data and burn into the PDF."""
        overlays = self._canvas.get_overlay_data()
        if not overlays:
            log.debug("No overlays to burn")
            return False, []

        log.info("Burning %d overlay(s) into PDF", len(overlays))
        sf = 1.0 / self._zoom

        for ov in overlays:
            page_idx = ov["page"]
            log.debug("  Burning %s on page %d", ov["type"], page_idx)
            if ov["type"] == "highlight":
                x0, y0, x1, y1 = ov["rect"]
                self._engine.add_highlight(
                    page_idx, (x0 * sf, y0 * sf, x1 * sf, y1 * sf)
                )

            elif ov["type"] == "text":
                px, py = ov["pos"]
                text = ov["text"]
                fs = ov.get("font_size", 12)
                w = max(len(text) * fs * 0.6, 60)
                h = fs * 1.5
                rect = (px * sf, py * sf, px * sf + w, py * sf + h)
                self._engine.add_text_annotation(page_idx, rect, text, font_size=fs * sf)

            elif ov["type"] == "freehand":
                pts = [(x * sf, y * sf) for x, y in ov["points"]]
                self._engine.add_freehand_annotation(page_idx, pts)

            elif ov["type"] == "image":
                x0, y0, x1, y1 = ov["rect"]
                self._engine.add_image(
                    page_idx,
                    (x0 * sf, y0 * sf, x1 * sf, y1 * sf),
                    ov["image_data"]
                )

        self._canvas.clear_overlays()
        log.info("Overlays burned and cleared")
        return True, overlays

    def _apply_pending_crypto(self, path: str):
        """Apply pending cryptographic signature if one was set."""
        if self._pending_crypto.get("apply"):
            log.info("Applying crypto signature from %s", self._pending_crypto["pfx_path"])
            ok = apply_crypto_signature(
                path, path,
                self._pending_crypto["pfx_path"],
                self._pending_crypto["pfx_password"],
            )
            if ok:
                log.info("Crypto signature applied successfully")
                self._status.showMessage("Digital signature applied!", 5000)
            else:
                log.warning("Crypto signature failed")
                QMessageBox.warning(
                    self, "Crypto Sign",
                    "Digital signature failed. Visual signature was saved."
                )
            self._pending_crypto = {}

    def _save_file(self):
        """Save the document to the working copy.

        First save prompts for a new filename (original is never overwritten).
        Subsequent saves write directly to that working copy.
        """
        if not self._engine.doc:
            return

        if not self._save_path:
            # First save — prompt for a new location
            original = Path(self._engine.file_path)
            suggested = str(original.parent / f"{original.stem}_edited.pdf")
            path, _ = QFileDialog.getSaveFileName(
                self, "Save As New File", suggested, "PDF Files (*.pdf)"
            )
            if not path:
                return  # user cancelled
            self._save_path = path
            log.info("Working copy set to: %s", path)

        log.info("Saving to: %s", self._save_path)
        try:
            had_overlays, _ = self._burn_overlays()
            self._engine.save_as(self._save_path)
            label = "Saved (with annotations)." if had_overlays else "Saved."
            log.info(label)
            self._status.showMessage(label, 3000)
            self.setWindowTitle(
                f"{self.APP_NAME} — {Path(self._save_path).name}"
            )
            self._recent_store.add(self._save_path)
            self._rebuild_recent_menu()

            if had_overlays:
                self._refresh_all_pages()

            self._apply_pending_crypto(self._save_path)
        except Exception as e:
            log.error("Save failed: %s", e, exc_info=True)
            QMessageBox.critical(self, "Error", f"Save failed:\n{e}")

    def _save_as(self):
        """Save to a new file (always prompts). Updates the working copy path."""
        if not self._engine.doc:
            return
        # Start from the current working copy or original
        start_path = self._save_path or self._engine.file_path or ""
        path, _ = QFileDialog.getSaveFileName(
            self, "Save As", start_path, "PDF Files (*.pdf)"
        )
        if not path:
            return
        log.info("Save As: %s", path)
        try:
            self._burn_overlays()
            self._engine.save_as(path)
            self._save_path = path  # update working copy
            log.info("Saved successfully to %s", path)
            self._status.showMessage(f"Saved to {path}", 3000)
            self.setWindowTitle(f"{self.APP_NAME} — {Path(path).name}")
            self._recent_store.add(path)
            self._rebuild_recent_menu()
            self._refresh_all_pages()
            self._apply_pending_crypto(path)
        except Exception as e:
            log.error("Save As failed: %s", e, exc_info=True)
            QMessageBox.critical(self, "Error", f"Save failed:\n{e}")

    def _refresh_all_pages(self):
        """Invalidate every page's cached render so the next paint reflects
        document mutations (Save burn, OCR text layer add, etc.).

        Only the visible window is re-rendered immediately; off-screen
        pages will lazily render when scrolled into view.
        """
        if not self._engine.doc:
            return
        self._canvas.invalidate_all_pages()
        self._update_rendered_pages()
        self._sidebar.load_thumbnails(self._engine)

    def _reload_all_pages_full(self):
        """Full canvas reload — required when page count or dimensions change.

        Rotation, insert, delete, and reorder all change page Y-offsets
        and/or the page list, so we re-prepare the layout from scratch
        and only render the visible window.
        """
        if not self._engine.doc:
            return
        sizes = []
        for i in range(self._engine.page_count):
            w, h = self._engine.get_page_size(i)
            sizes.append((w * self._zoom, h * self._zoom))
        self._canvas.prepare_pages(sizes)
        self._update_rendered_pages()
        self._sidebar.load_thumbnails(self._engine)
        self._page_spin.setMaximum(max(1, self._engine.page_count))

    # ── Page Structure Operations ───────────────────────────────

    def _confirm_discard_overlays(self, action_name: str) -> bool:
        """Prompt to discard unsaved overlays before a structural page change.

        Returns True if the caller may proceed (no overlays, or user
        confirmed). On confirm, also clears the overlays so the caller
        doesn't end up burning re-targeted ones into the wrong page.
        """
        n = len(self._canvas._overlay_items)
        if n == 0:
            return True
        reply = QMessageBox.question(
            self, action_name,
            f"This will discard {n} unsaved annotation(s) on the current "
            f"document because they cannot be safely re-mapped after a "
            f"page structure change.\n\nContinue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return False
        self._canvas.clear_overlays()
        return True

    def _rotate_current_page(self, delta_degrees: int):
        """Rotate the current page by the given delta (typically ±90)."""
        if not self._engine.doc:
            return
        if not self._confirm_discard_overlays("Rotate Page"):
            return
        log.info("Rotating page %d by %d°", self._current_page, delta_degrees)
        self._engine.rotate_page(self._current_page, delta_degrees)
        self._reload_all_pages_full()
        self._page_spin.setValue(self._current_page + 1)
        self._canvas.scroll_to_page(self._current_page)
        self._status.showMessage(
            f"Rotated page {self._current_page + 1} by {delta_degrees}°", 3000
        )

    def _on_page_delete(self, idx: int):
        """Delete the page at idx after confirmation."""
        if not self._engine.doc or self._engine.page_count <= 1:
            return
        reply = QMessageBox.question(
            self, "Delete Page",
            f"Delete page {idx + 1}? This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        if not self._confirm_discard_overlays("Delete Page"):
            return
        log.info("Deleting page %d", idx)
        self._engine.delete_page(idx)
        # Clamp current page so we don't point past the end of the doc.
        self._current_page = min(self._current_page, self._engine.page_count - 1)
        self._current_page = max(0, self._current_page)
        self._canvas.clear_search_highlights()
        self._search_results.clear()
        self._reload_all_pages_full()
        self._page_spin.setValue(self._current_page + 1)
        self._canvas.scroll_to_page(self._current_page)
        self._status.showMessage(f"Deleted page {idx + 1}", 3000)

    def _on_page_insert(self, at_idx: int, where: str):
        """Insert a blank page before/after the page at at_idx."""
        if not self._engine.doc:
            return
        if not self._confirm_discard_overlays("Insert Blank Page"):
            return
        # Match the adjacent page's size so insertions feel natural.
        ref_idx = max(0, min(at_idx, self._engine.page_count - 1))
        width, height = self._engine.get_page_size(ref_idx)
        insert_at = at_idx if where == "before" else at_idx + 1
        log.info("Inserting blank page at index %d (size %.0fx%.0f)",
                 insert_at, width, height)
        self._engine.insert_blank_page(insert_at, width=width, height=height)
        self._canvas.clear_search_highlights()
        self._search_results.clear()
        self._reload_all_pages_full()
        self._current_page = insert_at
        self._page_spin.setValue(self._current_page + 1)
        self._canvas.scroll_to_page(self._current_page)
        self._status.showMessage(
            f"Inserted blank page at position {insert_at + 1}", 3000
        )

    def _on_pages_reordered(self, perm: list):
        """Apply a thumbnail-driven reorder. perm[i] = old index of page now at i."""
        if not self._engine.doc:
            return
        if perm == list(range(self._engine.page_count)):
            return  # no-op
        if not self._confirm_discard_overlays("Reorder Pages"):
            # User cancelled — restore the visual order to match the doc.
            self._sidebar.load_thumbnails(self._engine)
            return
        log.info("Reordering pages: %s", perm)
        self._engine.reorder_pages(perm)

        # Track where the previously-current page ended up.
        try:
            self._current_page = perm.index(self._current_page)
        except ValueError:
            self._current_page = 0

        self._canvas.clear_search_highlights()
        self._search_results.clear()
        self._reload_all_pages_full()
        self._page_spin.setValue(self._current_page + 1)
        self._canvas.scroll_to_page(self._current_page)
        self._status.showMessage("Pages reordered.", 3000)

    # ── Print ───────────────────────────────────────────────────

    def _print_document(self):
        """Print the current PDF via the system print dialog."""
        if not self._engine.doc:
            QMessageBox.warning(self, "No Document", "Open a PDF first.")
            return

        log.info("Print requested for: %s", self._engine.file_path)
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        printer.setDocName(
            Path(self._engine.file_path).stem if self._engine.file_path else "PDF Studio"
        )

        dialog = QPrintDialog(printer, self)
        dialog.setWindowTitle("Print Document")

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        self._status.showMessage("Printing...")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        QApplication.processEvents()

        try:
            painter = QPainter()
            if not painter.begin(printer):
                QApplication.restoreOverrideCursor()
                QMessageBox.critical(self, "Print Error", "Could not start printing.")
                return

            page_count = self._engine.page_count

            # Get printable area in device pixels (the actual coordinate
            # system the QPainter uses in HighResolution mode).
            target_rect = printer.pageRect(QPrinter.Unit.DevicePixel)
            log.debug("Printer target rect: %.0fx%.0f device pixels",
                      target_rect.width(), target_rect.height())

            for i in range(page_count):
                if i > 0:
                    printer.newPage()

                # Render at good quality (288 DPI). We don't render at
                # full printer DPI (e.g. 1200) because that would create
                # enormous pixmaps. Qt's drawPixmap scales smoothly.
                render_zoom = 4.0
                png_data = self._engine.render_page(i, zoom=render_zoom)
                pix = QPixmap()
                pix.loadFromData(png_data)

                # Scale the rendered image to fill the printable area
                # while maintaining aspect ratio.
                pix_w = float(pix.width())
                pix_h = float(pix.height())
                pix_aspect = pix_w / pix_h
                target_aspect = target_rect.width() / target_rect.height()

                if pix_aspect > target_aspect:
                    # Page is wider than target — fit to width
                    draw_w = target_rect.width()
                    draw_h = draw_w / pix_aspect
                else:
                    # Page is taller than target — fit to height
                    draw_h = target_rect.height()
                    draw_w = draw_h * pix_aspect

                # Center on the printed page
                x_off = target_rect.x() + (target_rect.width() - draw_w) / 2
                y_off = target_rect.y() + (target_rect.height() - draw_h) / 2

                painter.drawPixmap(
                    QRectF(x_off, y_off, draw_w, draw_h),
                    pix,
                    QRectF(0, 0, pix_w, pix_h),
                )

                self._status.showMessage(f"Printing page {i + 1} / {page_count}...")
                QApplication.processEvents()

            painter.end()

            QApplication.restoreOverrideCursor()
            log.info("Printed %d page(s) successfully", page_count)
            self._status.showMessage(f"Printed {page_count} page(s).", 5000)

        except Exception as e:
            QApplication.restoreOverrideCursor()
            log.error("Print failed: %s", e, exc_info=True)
            QMessageBox.critical(self, "Print Error", f"Printing failed:\n{e}")

    # ── Undo / Redo ─────────────────────────────────────────────

    def _undo(self):
        self._canvas.undo()

    def _redo(self):
        self._canvas.redo()

    # ── Search ──────────────────────────────────────────────────

    def _toggle_search(self):
        """Toggle the search bar visibility."""
        if self._search_bar.isVisible():
            self._search_bar.hide_search()
        else:
            self._search_bar.show_search()

    def _do_search(self, query: str) -> list:
        """Run a text search and show highlights. Returns result list."""
        results = self._engine.search_text(query)
        self._search_results = results

        # Convert to dicts for canvas highlighting
        result_dicts = [
            {"page_index": r.page_index, "rect": r.rect}
            for r in results
        ]
        self._canvas.show_search_results(result_dicts, zoom=self._zoom)
        return results

    def _navigate_search(self, index: int, clear: bool = False):
        """Navigate to a specific search result."""
        if clear:
            self._canvas.clear_search_highlights()
            self._search_results.clear()
            return

        if 0 <= index < len(self._search_results):
            self._canvas.highlight_search_result(index)
            # Also scroll to the page containing this result
            page_idx = self._search_results[index].page_index
            self._page_spin.setValue(page_idx + 1)

    # ── Navigation ──────────────────────────────────────────────

    def _prev_page(self):
        cur = self._page_spin.value()
        if cur > 1:
            self._page_spin.setValue(cur - 1)

    def _next_page(self):
        cur = self._page_spin.value()
        if cur < self._page_spin.maximum():
            self._page_spin.setValue(cur + 1)

    def _go_to_page(self, page_index: int):
        self._current_page = page_index
        self._canvas.scroll_to_page(page_index)
        self._page_label.setText(
            f"Page {page_index + 1} / {self._engine.page_count}"
        )
        self._load_forms_for_page(page_index)

    def _on_thumbnail_clicked(self, row):
        if row >= 0:
            self._page_spin.setValue(row + 1)

    # ── Tool Switching ──────────────────────────────────────────

    def _set_tool(self, mode: str):
        log.debug("Tool switched to: %s", mode)
        self._canvas.mode = mode
        for m, act in self._tool_buttons.items():
            act.setChecked(m == mode)
        # Eraser is on a separate QToolButton with its own QAction
        if hasattr(self, "_eraser_action"):
            self._eraser_action.setChecked(mode == "eraser")
        # Draw sub-toolbar is only visible while drawing
        if hasattr(self, "_draw_subtoolbar"):
            self._draw_subtoolbar.setVisible(mode == "freehand")
        names = {
            "select": "Select", "highlight": "Highlight",
            "text": "Add Text", "freehand": "Freehand Draw",
            "edit_text": "Edit Text", "signature": "Place Signature",
            "eraser": "Eraser",
        }
        self._tool_label.setText(names.get(mode, mode))

    def _on_draw_settings_changed(self, tool: str, color: QColor, base_width: float):
        """Propagate sub-toolbar choices to the canvas's pen config."""
        log.debug("Draw settings: tool=%s, color=%s, width=%.1f",
                  tool, color.name(), base_width)
        self._canvas.set_draw_tool(tool)
        self._canvas.set_draw_color(color)
        self._canvas.set_draw_size(base_width)

    # ── Canvas Event Handlers ───────────────────────────────────

    def _on_canvas_click(self, page_idx: int, px: float, py: float):
        sf = 1.0 / self._zoom
        log.debug("Canvas click: page=%d, px=%.1f, py=%.1f, mode=%s",
                  page_idx, px, py, self._canvas.mode)

        if self._canvas.mode == PDFCanvas.MODE_EDIT_TEXT:
            block = self._engine.find_text_at_point(page_idx, px * sf, py * sf)
            if block:
                log.debug("Found PDF text at click: '%s'", block.text[:50])
                dlg = TextEditDialog(block, self)
                if dlg.exec() and dlg.result:
                    new_text, font_size, font_name, color = dlg.result
                    log.info("Editing PDF text: '%s' -> '%s'", block.text[:30], new_text[:30])
                    self._engine.edit_text(
                        block, new_text,
                        font_size=font_size,
                        font_name=font_name,
                        color=color,
                    )
                    png = self._engine.render_page(page_idx, zoom=self._zoom)
                    self._canvas.reload_page(page_idx, png)
                    self._status.showMessage("Text edited.", 3000)
            else:
                log.debug("No PDF text found at click point")
                self._status.showMessage("No text found at click point.", 2000)

        elif self._canvas.mode == PDFCanvas.MODE_SIGNATURE:
            if self._pending_sig_data:
                log.info("Placing signature on page %d", page_idx)
                scene_pos = self._canvas._page_to_scene(page_idx, px, py)
                self._canvas.place_signature(scene_pos, self._pending_sig_data)
                self._status.showMessage(
                    "Signature placed. Save (Ctrl+S) to finalize.", 5000
                )
                self._pending_sig_data = None
                self._set_tool("select")

    def _on_rect_selected(self, page_idx, x0, y0, x1, y1):
        if self._canvas.mode == PDFCanvas.MODE_HIGHLIGHT:
            log.debug("Highlight added on page %d", page_idx)
            self._status.showMessage(
                "Highlight added. Save (Ctrl+S) to finalize.", 3000
            )

    def _on_overlay_text_edit(self, text_item):
        """Handle edit request on an overlay text annotation."""
        log.debug("Editing overlay text: '%s'", text_item.toPlainText()[:50])
        dlg = OverlayTextEditDialog(text_item, self)
        if dlg.exec():
            log.info("Overlay text updated: '%s'", text_item.toPlainText()[:50])
            self._status.showMessage("Text annotation updated.", 3000)

    # ── Signature ───────────────────────────────────────────────

    def _open_signature_dialog(self):
        dlg = SignatureDialog(self, enable_crypto=True)
        if dlg.exec() and dlg.result_data:
            self._pending_sig_data = dlg.result_data.image_data
            self._pending_crypto = {
                "apply": dlg.result_data.apply_crypto,
                "pfx_path": dlg.result_data.pfx_path,
                "pfx_password": dlg.result_data.pfx_password,
            }
            self._set_tool("signature")
            self._canvas.mode = PDFCanvas.MODE_SIGNATURE
            self._status.showMessage(
                "Click on the document to place your signature.", 0
            )

    def _on_signature_edit(self, item):
        """Reopen the signature dialog to replace an existing signature in place.

        Preserves the item's scene position and scale. Pending crypto choice
        from the dialog is applied on the next save, just like a fresh signature.
        """
        log.debug("Editing signature on existing overlay")
        dlg = SignatureDialog(self, enable_crypto=True)
        if not dlg.exec() or not dlg.result_data:
            return

        # Swap the pixmap on the existing item — keeps position, scale,
        # and undo history intact.
        new_pix = QPixmap()
        new_pix.loadFromData(dlg.result_data.image_data)
        if new_pix.width() > 180:
            new_pix = new_pix.scaledToWidth(
                180, Qt.TransformationMode.SmoothTransformation
            )
        item.setPixmap(new_pix)

        # Update pending crypto so the new choice applies on next save.
        self._pending_crypto = {
            "apply": dlg.result_data.apply_crypto,
            "pfx_path": dlg.result_data.pfx_path,
            "pfx_password": dlg.result_data.pfx_password,
        }
        log.info("Signature replaced on existing overlay")
        self._status.showMessage("Signature updated.", 3000)

    # ── Forms ───────────────────────────────────────────────────

    def _toggle_form_panel(self, visible: bool):
        self._form_panel.setVisible(visible)
        if visible:
            self._load_forms_for_page(self._current_page)

    def _toggle_form_panel_btn(self):
        vis = not self._form_panel.isVisible()
        self._form_panel.setVisible(vis)
        self._act_toggle_forms.setChecked(vis)
        if vis:
            self._load_forms_for_page(self._current_page)

    def _load_forms_for_page(self, page_index: int):
        if self._form_panel.isVisible():
            self._form_panel.load_fields(self._engine, page_index)

    # ── OCR ─────────────────────────────────────────────────────

    def _run_ocr(self):
        """Run OCR on scanned pages in the current document."""
        if not self._engine.doc:
            QMessageBox.warning(self, "No Document", "Open a PDF first.")
            return

        log.info("OCR requested for %d page document", self._engine.page_count)
        available, info = check_tesseract()
        if not available:
            log.warning("Tesseract not found: %s", info)
            QMessageBox.critical(self, "Tesseract Not Found", info)
            return
        log.debug("Tesseract found at: %s", info)

        reply = QMessageBox.question(
            self, "Run OCR",
            f"This will scan {self._engine.page_count} page(s) for scanned/image content "
            f"and add an invisible searchable text layer.\n\n"
            f"Tesseract found at: {info}\n\n"
            f"Proceed?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self._status.showMessage("Running OCR...")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        QApplication.processEvents()

        try:
            results = ocr_full_document(
                self._engine.doc,
                lang="eng",
                dpi=300,
                tesseract_path=info,
                progress_callback=lambda i, t, msg: (
                    self._status.showMessage(msg),
                    QApplication.processEvents(),
                ),
            )

            QApplication.restoreOverrideCursor()

            if not results:
                log.info("OCR: no scanned pages detected")
                QMessageBox.information(
                    self, "OCR Complete",
                    "No scanned pages detected — all pages already contain text."
                )
            else:
                for r in results:
                    png = self._engine.render_page(r.page_index, zoom=self._zoom)
                    self._canvas.reload_page(r.page_index, png)

                avg_conf = sum(r.confidence for r in results) / len(results)
                log.info("OCR complete: %d pages, avg confidence %.1f%%",
                         len(results), avg_conf)
                QMessageBox.information(
                    self, "OCR Complete",
                    f"Processed {len(results)} scanned page(s).\n"
                    f"Average confidence: {avg_conf:.1f}%\n\n"
                    f"Text layer added — use Save to keep changes."
                )
            self._status.showMessage("OCR complete.", 5000)

        except ImportError as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(
                self, "Missing Dependency",
                f"{e}\n\nInstall it with:\n  pip install pytesseract"
            )
        except Exception as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "OCR Error", f"OCR failed:\n{e}")

    # ── Email Signing ───────────────────────────────────────────

    def _send_for_signing(self):
        send_path = self._save_path or self._engine.file_path
        if not self._engine.doc or not send_path:
            QMessageBox.warning(self, "No Document", "Open and save a PDF first.")
            return
        dlg = SendForSigningDialog(send_path, self._signing_store, self)
        dlg.exec()

    def _view_signing_requests(self):
        dlg = SigningTrackerDialog(self._signing_store, self)
        dlg.exec()

    def _open_smtp_setup(self):
        dlg = SMTPSetupDialog(self._signing_store, self)
        dlg.exec()

    # ── Window Events ───────────────────────────────────────────

    def closeEvent(self, event):
        if self._engine.doc:
            reply = QMessageBox.question(
                self, "Exit",
                "Close without saving?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.No:
                event.ignore()
                return
        self._engine.close()
        event.accept()

    # ── Drag and Drop ───────────────────────────────────────────

    def _pdf_paths_from_event(self, event) -> list[str]:
        """Extract local .pdf file paths from a drag/drop event."""
        md = event.mimeData()
        if not md.hasUrls():
            return []
        paths = []
        for url in md.urls():
            p = url.toLocalFile()
            if p and p.lower().endswith(".pdf") and os.path.isfile(p):
                paths.append(p)
        return paths

    def dragEnterEvent(self, event):
        if self._pdf_paths_from_event(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if self._pdf_paths_from_event(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        paths = self._pdf_paths_from_event(event)
        if not paths:
            event.ignore()
            return
        event.acceptProposedAction()
        first = paths[0]
        log.info("Drop: opening %s (%d additional files ignored)",
                 first, len(paths) - 1)
        self._open_file(first)
        if len(paths) > 1:
            self._status.showMessage(
                f"Opened {Path(first).name}. Ignored {len(paths) - 1} "
                f"additional file(s) — multi-document is not yet supported.",
                6000,
            )


def main():
    log.info("═" * 50)
    log.info("PDF Studio starting")
    log.info("Python %s", sys.version.split()[0])
    log.info("═" * 50)

    app = QApplication(sys.argv)
    app.setApplicationName(MainWindow.APP_NAME)
    app.setStyle("Fusion")

    from PyQt6.QtGui import QPalette
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(30, 30, 30))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(220, 220, 220))
    palette.setColor(QPalette.ColorRole.Base, QColor(45, 45, 45))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(55, 55, 55))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(60, 60, 60))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(220, 220, 220))
    palette.setColor(QPalette.ColorRole.Text, QColor(220, 220, 220))
    palette.setColor(QPalette.ColorRole.Button, QColor(55, 55, 55))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(220, 220, 220))
    palette.setColor(QPalette.ColorRole.BrightText, QColor(255, 255, 255))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(26, 115, 232))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
    app.setPalette(palette)

    screen = app.primaryScreen()
    if screen:
        log.info("Screen: %s (%.0fx%.0f, DPR=%.1f)",
                 screen.name(), screen.size().width(), screen.size().height(),
                 screen.devicePixelRatio())

    window = MainWindow()
    window.show()
    log.info("Window shown, ready")

    # Open file from command-line argument
    if len(sys.argv) > 1:
        path = sys.argv[1]
        if os.path.isfile(path) and path.lower().endswith(".pdf"):
            log.info("Opening file from command line: %s", path)
            QTimer.singleShot(100, lambda p=path: window._open_file(p))

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
