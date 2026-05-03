"""
canvas.py — QGraphicsView-based PDF canvas with interactive annotation overlays.
Supports per-action undo and text search highlighting.
"""

import io
from PyQt6.QtWidgets import (
    QGraphicsView, QGraphicsScene, QGraphicsPixmapItem,
    QGraphicsRectItem, QGraphicsTextItem, QGraphicsPathItem,
    QGraphicsItem, QInputDialog, QMenu
)
from PyQt6.QtGui import (
    QPixmap, QPainter, QPen, QBrush, QColor, QFont,
    QPainterPath, QImage, QCursor, QAction
)
from PyQt6.QtCore import Qt, QRectF, QPointF, QSizeF, QTimer, pyqtSignal


class PageItem(QGraphicsItem):
    """A PDF page in the canvas. Renders lazily.

    Stores its target rendered pixel size. Until set_pixmap() is called
    it paints a flat gray placeholder with a "Loading..." label.
    clear_pixmap() reverts it back to the placeholder so the parent can
    free memory for off-screen pages.

    Provides .pixmap() / .setPixmap() shims so existing call sites that
    expect a QGraphicsPixmapItem-like API keep working.
    """

    PLACEHOLDER_BG = QColor("#3a3a3a")
    PLACEHOLDER_BORDER = QColor("#5a5a5a")
    PLACEHOLDER_TEXT = QColor("#888")

    def __init__(self, width: float, height: float, page_index: int):
        super().__init__()
        self._size = QSizeF(width, height)
        self._pixmap: QPixmap | None = None
        self._page_index = page_index
        self.setData(0, page_index)

    def boundingRect(self) -> QRectF:
        return QRectF(0, 0, self._size.width(), self._size.height())

    def paint(self, painter, option, widget=None):
        if self._pixmap is not None and not self._pixmap.isNull():
            painter.drawPixmap(0, 0, self._pixmap)
            return
        rect = self.boundingRect()
        painter.fillRect(rect, self.PLACEHOLDER_BG)
        painter.setPen(QPen(self.PLACEHOLDER_BORDER, 1))
        painter.drawRect(rect)
        painter.setPen(self.PLACEHOLDER_TEXT)
        painter.drawText(
            rect, Qt.AlignmentFlag.AlignCenter,
            f"Loading page {self._page_index + 1}…",
        )

    def set_pixmap(self, pixmap: QPixmap):
        """Attach a rendered pixmap. The placeholder stops showing."""
        self._pixmap = pixmap
        # If the rendered size differs from our advertised size (e.g.
        # after a rotation that flipped dimensions), update the bounding
        # rect so layout-dependent code stays consistent.
        if not pixmap.isNull():
            new_size = QSizeF(pixmap.width(), pixmap.height())
            if new_size != self._size:
                self.prepareGeometryChange()
                self._size = new_size
        self.update()

    def clear_pixmap(self):
        self._pixmap = None
        self.update()

    def has_pixmap(self) -> bool:
        return self._pixmap is not None and not self._pixmap.isNull()

    # Backward-compat shims so existing call sites that used a real
    # QGraphicsPixmapItem keep working without changes.
    def setPixmap(self, pixmap: QPixmap):
        self.set_pixmap(pixmap)

    def pixmap(self) -> QPixmap:
        return self._pixmap if self._pixmap is not None else QPixmap()


class DraggableImageItem(QGraphicsPixmapItem):
    """A signature/image overlay that can be moved and resized."""

    def __init__(self, pixmap, parent=None, is_signature=False):
        super().__init__(pixmap, parent)
        self.is_signature = is_signature
        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsMovable |
            QGraphicsItem.GraphicsItemFlag.ItemIsSelectable |
            QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges
        )
        self.setCursor(QCursor(Qt.CursorShape.OpenHandCursor))
        self._resize_handle_size = 10

    def contextMenuEvent(self, event):
        menu = QMenu()
        if self.is_signature:
            edit_action = QAction("Edit Signature...", None)
            edit_action.triggered.connect(self._request_edit)
            menu.addAction(edit_action)
            menu.addSeparator()
        delete_action = QAction("Delete", None)
        delete_action.triggered.connect(lambda: self.scene().removeItem(self))
        menu.addAction(delete_action)
        scale_up = QAction("Scale Up (120%)", None)
        scale_up.triggered.connect(lambda: self.setScale(self.scale() * 1.2))
        menu.addAction(scale_up)
        scale_down = QAction("Scale Down (80%)", None)
        scale_down.triggered.connect(lambda: self.setScale(self.scale() * 0.8))
        menu.addAction(scale_down)
        menu.exec(event.screenPos())

    def _request_edit(self):
        """Ask the parent canvas to open the signature edit dialog for this item."""
        scene = self.scene()
        if not scene:
            return
        views = scene.views()
        if views and hasattr(views[0], "signature_edit_requested"):
            views[0].signature_edit_requested.emit(self)


class DraggableTextItem(QGraphicsTextItem):
    """An editable, movable text annotation.

    Single-click + drag = move the item (Select mode).
    Double-click = enter text editing mode (inline editing).
    Click elsewhere / lose focus = back to draggable.
    """

    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsMovable |
            QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
        )
        # Don't enable text interaction by default — it blocks dragging.
        # It gets enabled on double-click or when first placed.
        self.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.setDefaultTextColor(QColor(0, 0, 0))
        self.setFont(QFont("Helvetica", 12))
        self.setCursor(QCursor(Qt.CursorShape.OpenHandCursor))
        self._editing = False

    def enable_editing(self):
        """Enter inline text editing mode."""
        self._editing = True
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextEditorInteraction)
        self.setCursor(QCursor(Qt.CursorShape.IBeamCursor))
        self.setFocus()

    def disable_editing(self):
        """Exit text editing, return to draggable mode."""
        self._editing = False
        self.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.setCursor(QCursor(Qt.CursorShape.OpenHandCursor))
        # Clear text selection
        cursor = self.textCursor()
        cursor.clearSelection()
        self.setTextCursor(cursor)

    def mouseDoubleClickEvent(self, event):
        """Double-click enters text editing mode."""
        self.enable_editing()
        super().mouseDoubleClickEvent(event)

    def focusOutEvent(self, event):
        """Leaving focus returns to draggable mode."""
        self.disable_editing()
        super().focusOutEvent(event)

    def contextMenuEvent(self, event):
        menu = QMenu()
        edit_action = QAction("Edit", None)
        edit_action.triggered.connect(self._request_edit)
        menu.addAction(edit_action)
        menu.addSeparator()
        delete_action = QAction("Delete", None)
        delete_action.triggered.connect(lambda: self.scene().removeItem(self))
        menu.addAction(delete_action)
        menu.exec(event.screenPos())

    def _request_edit(self):
        """Re-enter inline editing mode — same state the box is in right after creation."""
        self.enable_editing()


class FreehandPathItem(QGraphicsPathItem):
    """A freehand drawing stroke. Supports right-click → Erase."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)

    def contextMenuEvent(self, event):
        menu = QMenu()
        erase_action = QAction("Erase", None)
        erase_action.triggered.connect(self._request_erase)
        menu.addAction(erase_action)
        menu.exec(event.screenPos())

    def _request_erase(self):
        """Ask the parent canvas to fully remove this drawing from all state."""
        scene = self.scene()
        if not scene:
            return
        views = scene.views()
        if views and hasattr(views[0], "remove_overlay"):
            views[0].remove_overlay(self)
        else:
            # Fallback: at least take it out of the scene
            scene.removeItem(self)


class PDFCanvas(QGraphicsView):
    """Main PDF viewing and annotation canvas with undo support."""

    # Emitted when user clicks a point in PDF coordinates: (page_index, x, y)
    point_clicked = pyqtSignal(int, float, float)
    # Emitted when a rectangular selection is completed
    rect_selected = pyqtSignal(int, float, float, float, float)
    # Emitted when user clicks a DraggableTextItem in edit_text mode
    overlay_text_edit = pyqtSignal(object)
    # Emitted when user requests "Edit Signature" from a signature's context menu
    signature_edit_requested = pyqtSignal(object)
    zoom_changed = pyqtSignal(float)
    # Emitted (debounced) when the visible page range changes due to scroll/zoom
    visible_pages_changed = pyqtSignal(int, int)  # first, last (inclusive)

    # Tool modes
    MODE_SELECT = "select"
    MODE_HIGHLIGHT = "highlight"
    MODE_TEXT = "text"
    MODE_FREEHAND = "freehand"
    MODE_SIGNATURE = "signature"
    MODE_EDIT_TEXT = "edit_text"
    MODE_ERASER = "eraser"

    # Undo stack limit
    MAX_UNDO = 100

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHints(
            QPainter.RenderHint.Antialiasing |
            QPainter.RenderHint.SmoothPixmapTransform
        )
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)

        # State
        self._page_items: list[PageItem] = []
        self._page_y_offsets: list[float] = []
        self._current_zoom = 1.0
        self._mode = self.MODE_SELECT
        self._page_gap = 16

        # Debounce timer for visible-pages-changed: scroll/zoom can fire
        # hundreds of events per second; we only want to react after the
        # user pauses, otherwise the lazy-render loop spins on every tick.
        self._visible_timer = QTimer(self)
        self._visible_timer.setSingleShot(True)
        self._visible_timer.setInterval(80)
        self._visible_timer.timeout.connect(self._emit_visible_pages)

        # Drawing state
        self._drawing = False
        self._draw_start = QPointF()
        self._current_path: QPainterPath | None = None
        self._current_path_item: QGraphicsPathItem | None = None
        self._highlight_rect: QGraphicsRectItem | None = None

        # Eraser state — radius is in VIEWPORT pixels, hit-testing converts
        # to scene coords using current view transform so the circle on screen
        # matches the deletion area regardless of zoom level.
        self._eraser_radius = 22
        self._erasing = False

        # Panning state
        self._panning = False
        self._pan_start = QPointF()

        # Annotation overlays (kept separate from page pixmaps)
        self._overlay_items: list[QGraphicsItem] = []

        # Undo / redo stacks — stores overlay items
        self._undo_stack: list[QGraphicsItem] = []
        self._redo_stack: list[QGraphicsItem] = []

        # Search highlight overlays (separate from annotations)
        self._search_highlights: list[QGraphicsRectItem] = []
        self._active_search_index: int = -1

        # Colors
        self._highlight_color = QColor(255, 255, 0, 90)
        self._search_color = QColor(255, 180, 0, 100)
        self._search_active_color = QColor(255, 100, 0, 160)

        # Drawing tool config — three presets the toolbar can switch between.
        # Each preset has a width multiplier and an opacity. Color and base
        # size are user-controlled and apply across presets.
        self._draw_presets = {
            "pen":    {"width_mul": 1.0, "opacity": 1.0},
            "pencil": {"width_mul": 0.6, "opacity": 0.85},
            "marker": {"width_mul": 3.2, "opacity": 0.45},
        }
        self._draw_tool = "pen"
        self._draw_color = QColor(0, 0, 120)
        self._draw_base_width = 2.5  # "medium" reference width before preset multiplier

        self.setStyleSheet("QGraphicsView { border: none; background-color: #525659; }")

    # ── Properties ──────────────────────────────────────────────

    @property
    def mode(self):
        return self._mode

    @mode.setter
    def mode(self, value: str):
        self._mode = value
        if value == self.MODE_SELECT:
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.setCursor(Qt.CursorShape.ArrowCursor)
        elif value == self.MODE_HIGHLIGHT:
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.setCursor(Qt.CursorShape.CrossCursor)
        elif value == self.MODE_TEXT:
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.setCursor(Qt.CursorShape.IBeamCursor)
        elif value == self.MODE_FREEHAND:
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.setCursor(Qt.CursorShape.CrossCursor)
        elif value == self.MODE_SIGNATURE:
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.setCursor(Qt.CursorShape.CrossCursor)
        elif value == self.MODE_EDIT_TEXT:
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.setCursor(Qt.CursorShape.IBeamCursor)
        elif value == self.MODE_ERASER:
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self._update_eraser_cursor()

    # ── Undo / Redo ─────────────────────────────────────────────

    def _push_undo(self, item: QGraphicsItem):
        """Record an overlay action for undo."""
        self._undo_stack.append(item)
        if len(self._undo_stack) > self.MAX_UNDO:
            self._undo_stack.pop(0)
        # Adding a new action invalidates the redo stack
        self._redo_stack.clear()

    def undo(self):
        """Undo the last overlay action."""
        if not self._undo_stack:
            return
        item = self._undo_stack.pop()
        # Remove from scene and overlay list
        if item in self._overlay_items:
            self._overlay_items.remove(item)
        if item.scene():
            self._scene.removeItem(item)
        self._redo_stack.append(item)

    def redo(self):
        """Redo the last undone overlay action."""
        if not self._redo_stack:
            return
        item = self._redo_stack.pop()
        self._scene.addItem(item)
        self._overlay_items.append(item)
        self._undo_stack.append(item)

    @property
    def can_undo(self) -> bool:
        return len(self._undo_stack) > 0

    @property
    def can_redo(self) -> bool:
        return len(self._redo_stack) > 0

    def remove_overlay(self, item: QGraphicsItem):
        """Fully remove an overlay item from scene and all tracking lists.

        Use this for permanent deletes (eraser, right-click → Erase) so the
        item doesn't get re-burned into the PDF on save or reappear via undo.
        """
        if item in self._overlay_items:
            self._overlay_items.remove(item)
        if item in self._undo_stack:
            self._undo_stack.remove(item)
        if item in self._redo_stack:
            self._redo_stack.remove(item)
        if item.scene():
            self._scene.removeItem(item)

    # ── Eraser ──────────────────────────────────────────────────

    def set_eraser_radius(self, radius: int):
        """Set eraser radius in viewport pixels. Updates cursor if eraser is active."""
        self._eraser_radius = max(4, int(radius))
        if self._mode == self.MODE_ERASER:
            self._update_eraser_cursor()

    def _update_eraser_cursor(self):
        """Build a translucent-circle cursor sized to the current eraser radius."""
        diameter = self._eraser_radius * 2
        pm_size = diameter + 4
        pm = QPixmap(pm_size, pm_size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(50, 50, 50), 1.5))
        p.setBrush(QBrush(QColor(255, 255, 255, 90)))
        p.drawEllipse(2, 2, diameter, diameter)
        p.end()
        # Hotspot at center so clicks register at the circle's middle.
        self.setCursor(QCursor(pm, pm_size // 2, pm_size // 2))

    def _eraser_scene_radius(self) -> float:
        """Convert the viewport-pixel eraser radius to scene coordinates.

        Hit-testing happens in scene space, but the cursor circle the user
        sees is in viewport pixels. At zoom 2.0, a 22-px circle on screen
        corresponds to 11 units in scene space.
        """
        scale = self.transform().m11()
        if scale <= 0:
            return float(self._eraser_radius)
        return self._eraser_radius / scale

    def _partial_erase_at(self, scene_pos: QPointF):
        """Erase path segments within the eraser radius of scene_pos.

        For each freehand path the eraser circle touches, walks the path's
        points and drops any that fall inside the circle. Remaining points
        are split into runs of 2+ contiguous survivors; each run becomes a
        new FreehandPathItem. The original is deleted.
        """
        radius = self._eraser_scene_radius()
        radius_sq = radius * radius

        # Spatial pre-filter: only inspect items whose bounding rect overlaps
        # the eraser. Qt's scene index (BSP by default) makes this cheap.
        eraser_rect = QRectF(
            scene_pos.x() - radius, scene_pos.y() - radius,
            radius * 2, radius * 2
        )
        candidates = [
            item for item in self._scene.items(eraser_rect)
            if isinstance(item, FreehandPathItem)
        ]
        if not candidates:
            return

        ex, ey = scene_pos.x(), scene_pos.y()

        for item in candidates:
            path = item.path()
            offset_x = item.pos().x()
            offset_y = item.pos().y()

            # Build mask: True = keep this point, False = inside eraser circle
            keep_mask = []
            points = []
            for i in range(path.elementCount()):
                e = path.elementAt(i)
                px = e.x + offset_x
                py = e.y + offset_y
                points.append(QPointF(px, py))
                dx = px - ex
                dy = py - ey
                keep_mask.append(dx * dx + dy * dy > radius_sq)

            # Nothing erased → leave alone
            if all(keep_mask):
                continue

            # Everything erased → drop the whole stroke
            if not any(keep_mask):
                self.remove_overlay(item)
                continue

            # Split surviving points into runs of consecutive keepers.
            # A run needs at least 2 points to form a visible line segment.
            runs = []
            current = []
            for pt, keep in zip(points, keep_mask):
                if keep:
                    current.append(pt)
                else:
                    if len(current) >= 2:
                        runs.append(current)
                    current = []
            if len(current) >= 2:
                runs.append(current)

            original_pen = item.pen()
            self.remove_overlay(item)

            # Each run becomes a new freehand stroke with the same pen.
            for run in runs:
                new_path = QPainterPath(run[0])
                for pt in run[1:]:
                    new_path.lineTo(pt)
                new_item = FreehandPathItem()
                new_item.setPath(new_path)
                new_item.setPen(original_pen)
                self._scene.addItem(new_item)
                self._overlay_items.append(new_item)
                # Note: deliberately not pushed to undo stack — eraser is a
                # destructive op, consistent with click-to-erase semantics.

    # ── Drawing Pen Config ──────────────────────────────────────

    def set_draw_tool(self, tool: str):
        """Switch active drawing tool. Valid: 'pen', 'pencil', 'marker'."""
        if tool in self._draw_presets:
            self._draw_tool = tool

    def set_draw_color(self, color: QColor):
        """Set the freehand stroke color (alpha is overridden per preset)."""
        if color is not None and color.isValid():
            # Normalize to fully opaque; preset opacity is applied at pen-build time.
            self._draw_color = QColor(color.red(), color.green(), color.blue())

    def set_draw_size(self, base_width: float):
        """Set the base width (multiplied by preset's width_mul to get final width)."""
        self._draw_base_width = max(0.5, float(base_width))

    @property
    def draw_tool(self) -> str:
        return self._draw_tool

    @property
    def draw_color(self) -> QColor:
        return QColor(self._draw_color)

    @property
    def draw_base_width(self) -> float:
        return self._draw_base_width

    def _build_draw_pen(self) -> QPen:
        """Construct a QPen from the active tool/color/size config."""
        preset = self._draw_presets[self._draw_tool]
        color = QColor(self._draw_color)
        # Apply preset opacity to the color's alpha channel.
        color.setAlphaF(preset["opacity"])
        width = self._draw_base_width * preset["width_mul"]
        return QPen(
            color, width,
            Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap,
            Qt.PenJoinStyle.RoundJoin,
        )

    # ── Page Loading ────────────────────────────────────────────

    def prepare_pages(self, page_sizes: list[tuple[float, float]]):
        """Lay out the scene with placeholder PageItems sized to match each page.

        The caller is responsible for calling set_page_pixmap() to fill in
        rendered content for visible pages. This is the preferred entry
        point for opening a document — it lets us page in only what's
        on screen, instead of rendering the whole doc up front.
        """
        self._scene.clear()
        self._page_items.clear()
        self._page_y_offsets.clear()
        self._overlay_items.clear()
        self._undo_stack.clear()
        self._redo_stack.clear()
        self._search_highlights.clear()
        self._active_search_index = -1

        y_offset = 0.0
        for i, (w, h) in enumerate(page_sizes):
            item = PageItem(float(w), float(h), i)
            item.setPos(0, y_offset)
            self._scene.addItem(item)
            self._page_items.append(item)
            self._page_y_offsets.append(y_offset)
            y_offset += h + self._page_gap

        self._scene.setSceneRect(self._scene.itemsBoundingRect().adjusted(-20, -20, 20, 20))

    def load_pages(self, page_images: list[bytes]):
        """Eagerly load all rendered page images. Kept for callers that
        already have every pixmap in hand."""
        sizes = []
        pixmaps = []
        for png_data in page_images:
            pix = QPixmap()
            pix.loadFromData(png_data)
            pixmaps.append(pix)
            sizes.append((pix.width(), pix.height()))
        self.prepare_pages(sizes)
        for i, pix in enumerate(pixmaps):
            self._page_items[i].set_pixmap(pix)

    def reload_page(self, page_index: int, png_data: bytes):
        """Re-render a single page (after edits)."""
        if page_index < 0 or page_index >= len(self._page_items):
            return
        pix = QPixmap()
        pix.loadFromData(png_data)
        self._page_items[page_index].set_pixmap(pix)

    def set_page_pixmap(self, page_index: int, pixmap: QPixmap):
        """Attach a rendered pixmap to a previously-prepared page slot."""
        if page_index < 0 or page_index >= len(self._page_items):
            return
        self._page_items[page_index].set_pixmap(pixmap)

    def clear_page_pixmap(self, page_index: int):
        """Drop a page's rendered pixmap to free memory; placeholder shows."""
        if page_index < 0 or page_index >= len(self._page_items):
            return
        self._page_items[page_index].clear_pixmap()

    def is_page_loaded(self, page_index: int) -> bool:
        if page_index < 0 or page_index >= len(self._page_items):
            return False
        return self._page_items[page_index].has_pixmap()

    def invalidate_all_pages(self):
        """Drop all rendered pixmaps so they'll be re-rendered on demand.

        Used after operations that modify document contents (Save burn,
        OCR, etc.) so the next paint reflects the new state.
        """
        for item in self._page_items:
            item.clear_pixmap()

    def visible_page_range(self, margin_px: float = 200.0) -> tuple[int, int]:
        """Return inclusive (first, last) page indices currently visible
        (plus a small margin above/below to pre-load adjacent pages)."""
        if not self._page_items:
            return (0, -1)
        viewport_rect = self.mapToScene(self.viewport().rect()).boundingRect()
        top = viewport_rect.top() - margin_px
        bottom = viewport_rect.bottom() + margin_px

        first = 0
        last = len(self._page_items) - 1
        for i, y_off in enumerate(self._page_y_offsets):
            page_h = self._page_items[i].boundingRect().height()
            if y_off + page_h < top:
                first = i + 1
            elif y_off > bottom:
                last = i - 1
                break
        first = max(0, min(first, len(self._page_items) - 1))
        last = max(first, min(last, len(self._page_items) - 1))
        return (first, last)

    def _emit_visible_pages(self):
        first, last = self.visible_page_range()
        if last >= first:
            self.visible_pages_changed.emit(first, last)

    def schedule_visible_emit(self):
        """Public hook so callers (e.g. after fit_width) can request a
        debounced visible-pages-changed emission."""
        self._visible_timer.start()

    def scrollContentsBy(self, dx, dy):
        super().scrollContentsBy(dx, dy)
        self._visible_timer.start()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._visible_timer.start()

    def scroll_to_page(self, page_index: int):
        """Scroll the view so the given page is visible."""
        if page_index < 0 or page_index >= len(self._page_items):
            return
        item = self._page_items[page_index]
        self.centerOn(item)

    # ── Coordinate Mapping ──────────────────────────────────────

    def _scene_to_page(self, scene_pos: QPointF) -> tuple[int, float, float]:
        """Map a scene position to (page_index, page_x, page_y)."""
        for i in range(len(self._page_items) - 1, -1, -1):
            item = self._page_items[i]
            y_off = self._page_y_offsets[i]
            rect = item.boundingRect()
            if scene_pos.y() >= y_off and scene_pos.y() <= y_off + rect.height():
                px = scene_pos.x() - item.pos().x()
                py = scene_pos.y() - y_off
                return (i, px, py)
        return (0, 0, 0)

    def _page_to_scene(self, page_index: int, px: float, py: float) -> QPointF:
        """Map page coordinates to scene position."""
        if page_index < 0 or page_index >= len(self._page_items):
            return QPointF(px, py)
        item = self._page_items[page_index]
        return QPointF(item.pos().x() + px, self._page_y_offsets[page_index] + py)

    def get_visible_page(self) -> int:
        """Return the index of the page most visible in the viewport."""
        center = self.mapToScene(self.viewport().rect().center())
        for i in range(len(self._page_items) - 1, -1, -1):
            if center.y() >= self._page_y_offsets[i]:
                return i
        return 0

    # ── Search Highlights ───────────────────────────────────────

    def show_search_results(self, results: list[dict], zoom: float = 2.0):
        """Display search result highlights on the canvas.

        results: list of {"page_index": int, "rect": (x0, y0, x1, y1)} in PDF points.
        zoom: the rendering zoom factor to convert PDF points to scene pixels.
        """
        self.clear_search_highlights()

        for r in results:
            page_idx = r["page_index"]
            x0, y0, x1, y1 = r["rect"]

            # Convert from PDF points to scene pixels
            sx0 = x0 * zoom
            sy0 = y0 * zoom
            sx1 = x1 * zoom
            sy1 = y1 * zoom

            scene_tl = self._page_to_scene(page_idx, sx0, sy0)
            scene_br = self._page_to_scene(page_idx, sx1, sy1)

            rect_item = QGraphicsRectItem(QRectF(scene_tl, scene_br))
            rect_item.setBrush(QBrush(self._search_color))
            rect_item.setPen(QPen(Qt.PenStyle.NoPen))
            rect_item.setZValue(100)  # above page, below overlays
            self._scene.addItem(rect_item)
            self._search_highlights.append(rect_item)

    def highlight_search_result(self, index: int):
        """Highlight a specific search result as 'active' and scroll to it."""
        # Reset previous active
        if 0 <= self._active_search_index < len(self._search_highlights):
            self._search_highlights[self._active_search_index].setBrush(
                QBrush(self._search_color)
            )

        if 0 <= index < len(self._search_highlights):
            self._active_search_index = index
            item = self._search_highlights[index]
            item.setBrush(QBrush(self._search_active_color))
            self.centerOn(item)
        else:
            self._active_search_index = -1

    def clear_search_highlights(self):
        """Remove all search highlights."""
        for item in self._search_highlights:
            if item.scene():
                self._scene.removeItem(item)
        self._search_highlights.clear()
        self._active_search_index = -1

    # ── Zoom ────────────────────────────────────────────────────

    def set_zoom(self, factor: float):
        """Set absolute zoom level."""
        if factor < 0.1:
            factor = 0.1
        elif factor > 5.0:
            factor = 5.0
        scale_change = factor / self._current_zoom
        self.scale(scale_change, scale_change)
        self._current_zoom = factor
        self.zoom_changed.emit(factor)
        self._visible_timer.start()

    def zoom_in(self):
        self.set_zoom(self._current_zoom * 1.25)

    def zoom_out(self):
        self.set_zoom(self._current_zoom / 1.25)

    def fit_width(self):
        if not self._page_items:
            return
        page_width = self._page_items[0].boundingRect().width()
        if page_width <= 0:
            return
        view_width = self.viewport().width() - 40
        factor = view_width / page_width
        self.set_zoom(factor)

    def wheelEvent(self, event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y()
            if delta > 0:
                self.zoom_in()
            else:
                self.zoom_out()
            event.accept()
        else:
            super().wheelEvent(event)

    # ── Mouse Events (Tool Interactions) ────────────────────────

    def _has_interactive_item_at(self, pos) -> bool:
        """Check if there's a movable/selectable overlay item under the cursor."""
        scene_pos = self.mapToScene(pos)
        items = self._scene.items(scene_pos)
        for item in items:
            if isinstance(item, (DraggableImageItem, DraggableTextItem)):
                return True
        return False

    def _find_overlay_text_at(self, pos) -> DraggableTextItem | None:
        """Find a DraggableTextItem overlay at the given viewport position."""
        scene_pos = self.mapToScene(pos)
        items = self._scene.items(scene_pos)
        for item in items:
            if isinstance(item, DraggableTextItem):
                return item
        return None

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return

        if self._mode == self.MODE_SELECT:
            if self._has_interactive_item_at(event.position().toPoint()):
                super().mousePressEvent(event)
            else:
                self._panning = True
                self._pan_start = event.position()
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
                event.accept()
            return

        scene_pos = self.mapToScene(event.position().toPoint())
        page_idx, px, py = self._scene_to_page(scene_pos)

        if self._mode == self.MODE_HIGHLIGHT:
            self._drawing = True
            self._draw_start = scene_pos
            self._highlight_rect = QGraphicsRectItem()
            self._highlight_rect.setBrush(QBrush(self._highlight_color))
            self._highlight_rect.setPen(QPen(Qt.PenStyle.NoPen))
            self._scene.addItem(self._highlight_rect)
            event.accept()

        elif self._mode == self.MODE_TEXT:
            self._add_text_annotation(scene_pos)
            event.accept()

        elif self._mode == self.MODE_FREEHAND:
            self._drawing = True
            self._current_path = QPainterPath(scene_pos)
            self._current_path_item = FreehandPathItem()
            self._current_path_item.setPen(self._build_draw_pen())
            self._scene.addItem(self._current_path_item)
            event.accept()

        elif self._mode == self.MODE_SIGNATURE:
            self.point_clicked.emit(page_idx, px, py)
            event.accept()

        elif self._mode == self.MODE_EDIT_TEXT:
            # Check for overlay text items first (annotations not yet burned in)
            overlay_item = self._find_overlay_text_at(event.position().toPoint())
            if overlay_item:
                self.overlay_text_edit.emit(overlay_item)
            else:
                self.point_clicked.emit(page_idx, px, py)
            event.accept()

        elif self._mode == self.MODE_ERASER:
            # Start a drag-erase. Erase at the press point and continue while
            # the user holds the button and drags.
            self._erasing = True
            self._partial_erase_at(scene_pos)
            event.accept()

        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._panning:
            delta = event.position() - self._pan_start
            self._pan_start = event.position()
            self.horizontalScrollBar().setValue(
                self.horizontalScrollBar().value() - int(delta.x())
            )
            self.verticalScrollBar().setValue(
                self.verticalScrollBar().value() - int(delta.y())
            )
            event.accept()
            return

        # Eraser drag: continue erasing as the cursor moves with the button held.
        if self._erasing and self._mode == self.MODE_ERASER:
            scene_pos = self.mapToScene(event.position().toPoint())
            self._partial_erase_at(scene_pos)
            event.accept()
            return

        if not self._drawing:
            super().mouseMoveEvent(event)
            return

        scene_pos = self.mapToScene(event.position().toPoint())

        if self._mode == self.MODE_HIGHLIGHT and self._highlight_rect:
            rect = QRectF(self._draw_start, scene_pos).normalized()
            self._highlight_rect.setRect(rect)
            event.accept()

        elif self._mode == self.MODE_FREEHAND and self._current_path:
            self._current_path.lineTo(scene_pos)
            self._current_path_item.setPath(self._current_path)
            event.accept()

        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._panning:
            self._panning = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
            event.accept()
            return

        # End an eraser drag.
        if event.button() == Qt.MouseButton.LeftButton and self._erasing:
            self._erasing = False
            event.accept()
            return

        if event.button() != Qt.MouseButton.LeftButton or not self._drawing:
            super().mouseReleaseEvent(event)
            return

        self._drawing = False

        if self._mode == self.MODE_HIGHLIGHT and self._highlight_rect:
            rect = self._highlight_rect.rect()
            if rect.width() > 4 and rect.height() > 4:
                self._overlay_items.append(self._highlight_rect)
                self._push_undo(self._highlight_rect)
                page_idx, px0, py0 = self._scene_to_page(rect.topLeft())
                _, px1, py1 = self._scene_to_page(rect.bottomRight())
                self.rect_selected.emit(page_idx, px0, py0, px1, py1)
            else:
                self._scene.removeItem(self._highlight_rect)
            self._highlight_rect = None

        elif self._mode == self.MODE_FREEHAND and self._current_path_item:
            self._overlay_items.append(self._current_path_item)
            self._push_undo(self._current_path_item)
            self._current_path_item = None
            self._current_path = None

        event.accept()

    # ── Annotation Helpers ──────────────────────────────────────

    def _add_text_annotation(self, scene_pos: QPointF):
        """Place an editable text box at the click position."""
        item = DraggableTextItem("Type here...")
        item.setPos(scene_pos)
        self._scene.addItem(item)
        self._overlay_items.append(item)
        self._push_undo(item)
        # Enable inline editing immediately so user can start typing.
        # When they click elsewhere, focusOutEvent disables editing
        # and the item becomes draggable.
        item.enable_editing()
        cursor = item.textCursor()
        cursor.select(cursor.SelectionType.Document)
        item.setTextCursor(cursor)

    def place_signature(self, scene_pos: QPointF, image_data: bytes, width=180):
        """Place a signature image at the given position."""
        pix = QPixmap()
        pix.loadFromData(image_data)
        if pix.width() > width:
            pix = pix.scaledToWidth(width, Qt.TransformationMode.SmoothTransformation)
        item = DraggableImageItem(pix, is_signature=True)
        item.setPos(scene_pos)
        self._scene.addItem(item)
        self._overlay_items.append(item)
        self._push_undo(item)
        return item

    def place_image(self, scene_pos: QPointF, pixmap: QPixmap):
        """Place an arbitrary image at the given position."""
        item = DraggableImageItem(pixmap)
        item.setPos(scene_pos)
        self._scene.addItem(item)
        self._overlay_items.append(item)
        self._push_undo(item)
        return item

    def clear_overlays(self):
        """Remove all annotation overlays."""
        for item in self._overlay_items:
            if item.scene():
                self._scene.removeItem(item)
        self._overlay_items.clear()
        self._undo_stack.clear()
        self._redo_stack.clear()

    def get_overlay_data(self) -> list[dict]:
        """Export overlay items for burning into the PDF.
        Returns list of dicts with type, position, and content info."""
        results = []
        for item in self._overlay_items:
            if isinstance(item, QGraphicsRectItem) and item not in self._search_highlights:
                # highlight
                r = item.rect()
                page_idx, x0, y0 = self._scene_to_page(r.topLeft())
                _, x1, y1 = self._scene_to_page(r.bottomRight())
                results.append({
                    "type": "highlight",
                    "page": page_idx,
                    "rect": (x0, y0, x1, y1),
                })
            elif isinstance(item, DraggableTextItem):
                pos = item.pos()
                page_idx, px, py = self._scene_to_page(pos)
                results.append({
                    "type": "text",
                    "page": page_idx,
                    "pos": (px, py),
                    "text": item.toPlainText(),
                    "font_size": item.font().pointSizeF(),
                })
            elif isinstance(item, DraggableImageItem):
                pos = item.pos()
                page_idx, px, py = self._scene_to_page(pos)
                pix = item.pixmap()
                buf = io.BytesIO()
                img = pix.toImage()
                ba_raw = img.bits()
                ba_raw.setsize(img.sizeInBytes())
                from PIL import Image
                pil = Image.frombytes(
                    "RGBA", (img.width(), img.height()),
                    bytes(ba_raw), "raw", "BGRA"
                )
                pil.save(buf, "PNG")
                w = pix.width() * item.scale()
                h = pix.height() * item.scale()
                results.append({
                    "type": "image",
                    "page": page_idx,
                    "rect": (px, py, px + w, py + h),
                    "image_data": buf.getvalue(),
                })
            elif isinstance(item, QGraphicsPathItem):
                path = item.path()
                points = []
                for i in range(path.elementCount()):
                    e = path.elementAt(i)
                    sp = QPointF(e.x, e.y)
                    pg, ppx, ppy = self._scene_to_page(sp)
                    points.append((ppx, ppy))
                if points:
                    page_idx, _, _ = self._scene_to_page(
                        QPointF(path.elementAt(0).x, path.elementAt(0).y)
                    )
                    results.append({
                        "type": "freehand",
                        "page": page_idx,
                        "points": points,
                    })
        return results
