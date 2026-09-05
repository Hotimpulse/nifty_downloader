"""Modern Qt presentation for the video downloader.

The downloader implementation still lives in ``main.py``.  This module keeps
that worker logic and replaces the old Tk widgets with a native Qt workspace.
"""

from __future__ import annotations

import base64
import sys
from types import SimpleNamespace

from PySide6.QtCore import QObject, QPointF, QRectF, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QSpinBox,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)


# When ``main.py`` is the executable entry point, use its already-loaded
# backend.  Importing this module directly still works for local UI testing.
backend = sys.modules.get("__main__")
if backend is None or not hasattr(backend, "VideoDownloaderGUI"):
    import main as backend  # type: ignore[no-redef]


class ValueVar(QObject):
    """Small StringVar/IntVar-compatible value object for the shared backend."""

    changed = Signal(object)

    def __init__(self, value):
        super().__init__()
        self._value = value

    def get(self):
        return self._value

    def set(self, value):
        if self._value == value:
            return
        self._value = value
        self.changed.emit(value)


class RootBridge(QObject):
    """Expose the handful of Tk root methods used by the backend workers."""

    schedule_requested = Signal(object)
    cancel_requested = Signal(object)

    def __init__(self, window: QMainWindow):
        super().__init__()
        self.window = window
        self._next_token = 0
        self._timers: dict[int, QTimer] = {}
        self._protocol = None
        self.schedule_requested.connect(self._schedule)
        self.cancel_requested.connect(self._cancel)

    def title(self, value: str):
        self.window.setWindowTitle(value)

    def geometry(self, value: str):
        dimensions = value.split("+")[0].split("x")
        if len(dimensions) == 2:
            self.window.resize(int(dimensions[0]), int(dimensions[1]))

    def minsize(self, width: int, height: int):
        self.window.setMinimumSize(width, height)

    def configure(self, **_kwargs):
        return None

    def protocol(self, _name: str, callback):
        self._protocol = callback

    def after(self, milliseconds: int, callback):
        self._next_token += 1
        token = self._next_token
        self.schedule_requested.emit((token, max(0, int(milliseconds)), callback))
        return token

    def after_idle(self, callback):
        return self.after(0, callback)

    def after_cancel(self, token):
        self.cancel_requested.emit(token)

    @Slot(object)
    def _schedule(self, payload):
        token, milliseconds, callback = payload
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: self._fire(token, callback))
        self._timers[token] = timer
        timer.start(milliseconds)

    @Slot(object)
    def _cancel(self, token):
        timer = self._timers.pop(token, None)
        if timer:
            timer.stop()
            timer.deleteLater()

    def _fire(self, token, callback):
        timer = self._timers.pop(token, None)
        if timer:
            timer.deleteLater()
        callback()

    def clipboard_clear(self):
        QApplication.clipboard().clear()

    def clipboard_append(self, value: str):
        QApplication.clipboard().setText(value)

    def clipboard_get(self):
        return QApplication.clipboard().text()

    def destroy(self):
        self.window._allow_close = True
        self.window.close()


class QtMessageBoxBridge:
    """Keep worker-thread notifications safe while preserving backend calls."""

    def __init__(self, root: RootBridge, window: QMainWindow):
        self.root = root
        self.window = window

    def _show(self, icon, title: str, text: str):
        def display():
            box = QMessageBox(self.window)
            box.setIcon(icon)
            box.setWindowTitle(title)
            box.setText(text)
            box.exec()

        self.root.after(0, display)

    def showwarning(self, title: str, text: str):
        self._show(QMessageBox.Icon.Warning, title, text)

    def showerror(self, title: str, text: str):
        self._show(QMessageBox.Icon.Critical, title, text)

    def showinfo(self, title: str, text: str):
        self._show(QMessageBox.Icon.Information, title, text)


class ButtonBridge:
    """Adapt Tk's configure/state calls used by the shared backend."""

    def __init__(self, button: QPushButton):
        self.widget = button

    def configure(self, **kwargs):
        if "text" in kwargs:
            self.widget.setText(kwargs["text"])
        style = kwargs.get("style")
        if style:
            variant = "download" if "Download" in style else "secondary"
            self.widget.setProperty("variant", variant)
            self.widget.style().unpolish(self.widget)
            self.widget.style().polish(self.widget)

    def state(self, states):
        for state in states:
            if state == "disabled":
                self.widget.setEnabled(False)
            elif state == "!disabled":
                self.widget.setEnabled(True)


class LabelBridge:
    """Adapt the small configure surface used by preview status updates."""

    def __init__(self, label: QLabel):
        self.widget = label

    def configure(self, **kwargs):
        if "text" in kwargs:
            self.widget.setText(kwargs["text"])
        if "image" in kwargs and not kwargs["image"]:
            self.widget.clear()


class QtLineEdit(QLineEdit):
    pasted = Signal()

    def focus_set(self):
        self.setFocus()

    def paste(self):
        super().paste()
        self.pasted.emit()


class TreeBridge:
    """Tk Treeview-shaped adapter backed by a styled QTreeWidget."""

    def __init__(self, tree: QTreeWidget):
        self.widget = tree
        self._items: dict[str, QTreeWidgetItem] = {}
        self._counter = 0

    def insert(self, _parent, _index, values=()):
        self._counter += 1
        iid = f"row-{self._counter}"
        item = QTreeWidgetItem([str(value) for value in values])
        item.setData(0, Qt.ItemDataRole.UserRole, iid)
        self.widget.addTopLevelItem(item)
        self._items[iid] = item
        return iid

    def delete(self, *items):
        targets = items or tuple(self._items)
        for iid in targets:
            item = self._items.pop(iid, None)
            if item is None:
                continue
            index = self.widget.indexOfTopLevelItem(item)
            if index >= 0:
                self.widget.takeTopLevelItem(index)
            del item

    def get_children(self):
        return list(self._items)

    def selection(self):
        return tuple(
            str(item.data(0, Qt.ItemDataRole.UserRole))
            for item in self.widget.selectedItems()
        )

    def selection_set(self, iid: str):
        item = self._items.get(iid)
        if item:
            item.setSelected(True)

    def selection_remove(self, *items):
        if not items:
            self.widget.clearSelection()
            return
        for iid in items:
            item = self._items.get(iid)
            if item:
                item.setSelected(False)

    def focus(self, iid: str):
        item = self._items.get(iid)
        if item:
            self.widget.setCurrentItem(item)

    def item(self, iid: str, _option=None):
        item = self._items.get(iid)
        return {"values": [item.text(0)] if item else []}

    def yview_moveto(self, _fraction: float):
        scrollbar = self.widget.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())


class RangePickerWidget(QWidget):
    pressed = Signal(float)
    dragged = Signal(float)
    released = Signal()

    def __init__(self, formatter, parent=None):
        super().__init__(parent)
        self.formatter = formatter
        self.duration = 0.0
        self.start = 0.0
        self.end = 0.0
        self.playhead = 0.0
        self.setMinimumHeight(76)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)

    def set_data(self, duration, start, end, playhead):
        self.duration = duration
        self.start = start
        self.end = end
        self.playhead = playhead
        self.update()

    def winfo_width(self):
        """Expose the Tk-style width API used by the shared range logic."""
        return self.width()

    def _bounds(self):
        return 20.0, max(21.0, float(self.width() - 20))

    def _x_for_seconds(self, seconds):
        left, right = self._bounds()
        if not self.duration:
            return left
        ratio = max(0.0, min(1.0, seconds / self.duration))
        return left + ratio * (right - left)

    def _seconds_for_x(self, x):
        left, right = self._bounds()
        if not self.duration or right <= left:
            return 0.0
        return round(max(0.0, min(1.0, (x - left) / (right - left))) * self.duration, 1)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#19243a"))
        left, right = self._bounds()
        center = 31.0
        track_pen = QPen(QColor("#64748b"), 8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        painter.setPen(track_pen)
        painter.drawLine(QPointF(left, center), QPointF(right, center))

        if not self.duration:
            painter.setPen(QColor("#cbd5e1"))
            painter.drawText(QRectF(left, 51, right - left, 18), "Time range becomes draggable after the video loads")
            return

        painter.setPen(QPen(QColor("#38bdf8"), 8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawLine(QPointF(self._x_for_seconds(self.start), center), QPointF(self._x_for_seconds(self.end), center))

        playhead_x = self._x_for_seconds(self.playhead)
        painter.setPen(QPen(QColor("#fbbf24"), 2))
        painter.drawLine(QPointF(playhead_x, 10), QPointF(playhead_x, 50))

        painter.setPen(QColor("#94a3b8"))
        for seconds in (0.0, self.duration / 2, self.duration):
            x = self._x_for_seconds(seconds)
            painter.drawLine(QPointF(x, 43), QPointF(x, 49))
            painter.drawText(QRectF(x - 45, 51, 90, 18), Qt.AlignmentFlag.AlignCenter, self.formatter(seconds))

        for seconds, color in ((self.start, "#22c55e"), (self.end, "#ef4444")):
            painter.setBrush(QColor(color))
            painter.setPen(QPen(QColor("#f8fafc"), 2))
            painter.drawEllipse(QPointF(self._x_for_seconds(seconds), center), 10, 10)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.pressed.emit(event.position().x())

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.MouseButton.LeftButton:
            self.dragged.emit(event.position().x())

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.released.emit()


class QtMainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.controller = None
        self._allow_close = False

    def closeEvent(self, event):
        if self._allow_close or self.controller is None:
            event.accept()
            return
        event.ignore()
        self.controller._on_close()


class QtVideoDownloaderGUI(backend.VideoDownloaderGUI):
    """Qt UI with the same downloader/preview workers as the original app."""

    def __init__(self, window: QtMainWindow):
        self.window = window
        self.root = RootBridge(window)
        self.root.title("Video Downloader")
        self.root.geometry("1360x900")
        self.root.minsize(1120, 760)
        window.controller = self

        self.url_var = ValueVar("")
        self.start_time_var = ValueVar("")
        self.end_time_var = ValueVar("")
        self.range_status_var = ValueVar("Paste a link to load the FFmpeg preview")
        self.video_title_var = ValueVar("No video loaded yet")
        self.video_meta_var = ValueVar("Paste a link above to ingest its available formats")
        self.quality_status_var = ValueVar("Video and audio qualities will appear here automatically")
        self.batch_url_var = ValueVar("")
        self.batch_count_var = ValueVar(10)
        self.progress_var = ValueVar(0.0)
        self.progress_text_var = ValueVar("Ready")
        self._progress_value = 0.0
        self._last_progress_update = 0.0
        self._is_checking_qualities = False
        self._update_remote = ""
        self._update_branch = "main"
        self._update_available = False
        self.preferences = backend.load_preferences()
        self.preferred_video_height = self.preferences["preferred_video_height"]
        self.preferred_audio_bitrate = self.preferences["preferred_audio_bitrate"]
        self.history_entries = []

        self.preview_duration = 0.0
        self.preview_path = None
        self.preview_dir = None
        self.preview_dirs = set()
        self.preview_photo = None
        self.preview_pixmap = None
        self.preview_generation = 0
        self.preview_request_id = 0
        self.preview_frame_after = None
        self.preview_player_process = None
        self.preview_player_stop = backend.threading.Event()
        self.preview_player_token = 0
        self.preview_playing = False
        self._closing = False
        self.preview_playhead = 0.0
        self.range_start_seconds = 0.0
        self.range_end_seconds = 0.0
        self.range_drag_handle = None

        self.video_formats = []
        self.audio_formats = []
        self.video_id_to_format = {}
        self.audio_id_to_format = {}

        backend.messagebox = QtMessageBoxBridge(self.root, window)
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        if backend.sys.platform in ("win32", "darwin"):
            self.update_button.configure(text="↓  Checking GitHub...")
            self._run_in_thread(self._check_for_update_worker)

    @staticmethod
    def _label(text: str, role: str = "Body"):
        label = QLabel(text)
        label.setObjectName(role)
        label.setWordWrap(True)
        label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        return label

    @staticmethod
    def _button(text: str, variant: str = "secondary"):
        button = QPushButton(text)
        button.setProperty("variant", variant)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        return button

    @staticmethod
    def _card(parent=None, margins=(18, 16, 18, 16)):
        card = QFrame(parent)
        card.setObjectName("Card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(*margins)
        layout.setSpacing(12)
        return card, layout

    @staticmethod
    def _step(number: str, title: str, subtitle: str = ""):
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        badge = QLabel(number)
        badge.setObjectName("StepBadge")
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setFixedSize(34, 28)
        layout.addWidget(badge, 0, Qt.AlignmentFlag.AlignTop)
        copy = QVBoxLayout()
        copy.setContentsMargins(0, 1, 0, 0)
        copy.setSpacing(2)
        heading = QLabel(title)
        heading.setObjectName("CardTitle")
        copy.addWidget(heading)
        if subtitle:
            sub = QLabel(subtitle)
            sub.setObjectName("Muted")
            sub.setWordWrap(True)
            copy.addWidget(sub)
        layout.addLayout(copy, 1)
        return row

    def _bind_text(self, widget: QLineEdit, value: ValueVar):
        widget.textChanged.connect(value.set)
        value.changed.connect(lambda text: widget.setText(str(text)) if widget.text() != str(text) else None)

    def _build_ui(self):
        self._apply_styles()
        central = QWidget()
        central.setObjectName("App")
        shell = QHBoxLayout(central)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)

        sidebar = QWidget()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(232)
        side_layout = QVBoxLayout(sidebar)
        side_layout.setContentsMargins(18, 24, 18, 20)
        side_layout.setSpacing(8)

        brand_row = QHBoxLayout()
        brand_row.setSpacing(10)
        logo = QLabel("◈")
        logo.setObjectName("Logo")
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo.setFixedSize(42, 42)
        brand_row.addWidget(logo)
        brand_copy = QVBoxLayout()
        brand_copy.setSpacing(2)
        brand_copy.addWidget(self._label("Video Downloader", "Brand"))
        brand_copy.addWidget(self._label("Fast. Reliable. High quality.", "MutedSmall"))
        brand_row.addLayout(brand_copy, 1)
        side_layout.addLayout(brand_row)
        side_layout.addSpacing(28)

        self.page_stack = QStackedWidget()
        self.nav_buttons = {}
        for label, icon in (("Download", "↓"), ("History", "↺"), ("Settings", "⚙")):
            nav = self._button(
                f"  {icon}    {label}",
                "nav-active" if label == "Download" else "nav",
            )
            nav.setObjectName("NavButton")
            nav.clicked.connect(lambda _checked=False, page=label: self._show_page(page))
            self.nav_buttons[label] = nav
            side_layout.addWidget(nav)

        side_layout.addStretch(1)
        shell.addWidget(sidebar)

        scroll = QScrollArea()
        scroll.setObjectName("ContentScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        content.setObjectName("Content")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(28, 24, 28, 24)
        content_layout.setSpacing(16)

        header = QHBoxLayout()
        header.setSpacing(12)
        heading = QVBoxLayout()
        heading.setSpacing(3)
        heading.addWidget(self._label("Download anything, your way", "Hero"))
        heading.addWidget(self._label("Paste a link, choose the moment and quality, then save it locally.", "Subtitle"))
        header.addLayout(heading, 1)
        self.update_button = ButtonBridge(self._button("↓  Checking GitHub..."))
        self.update_button.widget.setMinimumHeight(38)
        self.update_button.widget.clicked.connect(self.download_update_and_rebuild)
        self.update_button.state(["disabled"])
        header.addWidget(self.update_button.widget)
        open_button = self._button("Open downloads", "ghost")
        open_button.clicked.connect(self.open_download_folder)
        header.addWidget(open_button)
        content_layout.addLayout(header)

        url_card, url_layout = self._card()
        url_layout.addWidget(self._step("01", "Paste a video link", "We’ll ingest the available qualities and prepare a lightweight preview."))
        url_row = QHBoxLayout()
        url_row.setSpacing(10)
        self.url_entry = QtLineEdit()
        self.url_entry.setObjectName("UrlField")
        self.url_entry.setPlaceholderText("https://youtube.com/watch?v=…")
        self.url_entry.setMinimumHeight(46)
        self._bind_text(self.url_entry, self.url_var)
        self.url_entry.returnPressed.connect(lambda: self._check_url_from_entry())
        self.url_entry.pasted.connect(lambda: self._check_url_after_paste())
        url_row.addWidget(self.url_entry, 1)
        paste_button = self._button("Paste & Analyze", "primary")
        paste_button.setMinimumHeight(46)
        paste_button.clicked.connect(self.paste_and_check_url)
        url_row.addWidget(paste_button)
        url_layout.addLayout(url_row)
        url_layout.addWidget(self._label("Supports YouTube, X, Instagram, Facebook, TikTok, and other yt-dlp sites.", "Muted"))
        content_layout.addWidget(url_card)

        workspace = QHBoxLayout()
        workspace.setSpacing(16)
        workspace.setStretch(0, 3)
        workspace.setStretch(1, 2)

        preview_card, preview_layout = self._card(margins=(18, 16, 18, 16))
        preview_layout.addWidget(self._step("02", "Scrub and choose a range", "Drag either handle or edit the timestamps below."))
        metadata = QHBoxLayout()
        metadata.setSpacing(12)
        self.video_title_label = self._label("No video loaded yet", "CardTitle")
        self.video_meta_label = self._label("Paste a link above to ingest its available formats", "Muted")
        self.video_meta_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.video_title_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self.video_meta_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self.video_title_var.changed.connect(self.video_title_label.setText)
        self.video_meta_var.changed.connect(self.video_meta_label.setText)
        metadata.addWidget(self.video_title_label, 1)
        metadata.addWidget(self.video_meta_label, 1)
        preview_layout.addLayout(metadata)

        preview_surface = QFrame()
        preview_surface.setObjectName("PreviewSurface")
        preview_surface.setMinimumHeight(205)
        preview_surface.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        preview_surface_layout = QVBoxLayout(preview_surface)
        preview_surface_layout.setContentsMargins(0, 0, 0, 0)
        preview_label = QLabel("Paste a link to load the preview")
        preview_label.setObjectName("PreviewLabel")
        preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        preview_label.setWordWrap(True)
        preview_surface_layout.addWidget(preview_label)
        self.preview_label = LabelBridge(preview_label)
        preview_layout.addWidget(preview_surface, 1)

        self.range_canvas = RangePickerWidget(self._format_timestamp)
        self.range_canvas.pressed.connect(lambda x: self._range_picker_press(SimpleNamespace(x=x)))
        self.range_canvas.dragged.connect(lambda x: self._range_picker_drag(SimpleNamespace(x=x)))
        self.range_canvas.released.connect(lambda: self._range_picker_release(None))
        preview_layout.addWidget(self.range_canvas)

        range_controls = QHBoxLayout()
        range_controls.setSpacing(8)
        range_controls.addWidget(self._label("Start", "Muted"))
        self.start_entry = QtLineEdit()
        self.start_entry.setObjectName("TimeField")
        self.start_entry.setFixedWidth(106)
        self._bind_text(self.start_entry, self.start_time_var)
        self.start_entry.editingFinished.connect(self._sync_range_from_entries)
        range_controls.addWidget(self.start_entry)
        range_controls.addWidget(self._label("End", "Muted"))
        self.end_entry = QtLineEdit()
        self.end_entry.setObjectName("TimeField")
        self.end_entry.setFixedWidth(106)
        self._bind_text(self.end_entry, self.end_time_var)
        self.end_entry.editingFinished.connect(self._sync_range_from_entries)
        range_controls.addWidget(self.end_entry)
        range_controls.addWidget(self._label("seconds or HH:MM:SS", "Muted"))
        range_controls.addStretch(1)
        reset_button = self._button("Reset", "ghost")
        reset_button.clicked.connect(self._reset_range_to_full_video)
        range_controls.addWidget(reset_button)
        preview_layout.addLayout(range_controls)

        preview_footer = QHBoxLayout()
        status_label = self._label("Paste a link to load the FFmpeg preview", "Muted")
        self.range_status_var.changed.connect(status_label.setText)
        preview_footer.addWidget(status_label, 1)
        self.preview_play_button = ButtonBridge(self._button("▶  Play preview", "secondary"))
        self.preview_play_button.widget.clicked.connect(self._toggle_preview_playback)
        self.preview_play_button.state(["disabled"])
        preview_footer.addWidget(self.preview_play_button.widget)
        preview_layout.addLayout(preview_footer)
        workspace.addWidget(preview_card)

        quality_card, quality_layout = self._card(margins=(18, 16, 18, 16))
        quality_layout.addWidget(self._step("03", "Choose quality", "Select video, audio, or both."))
        quality_status = self._label("Video and audio qualities will appear here automatically", "Muted")
        self.quality_status_var.changed.connect(quality_status.setText)
        quality_layout.addWidget(quality_status)
        quality_layout.addWidget(self._quality_box("Video", True))
        quality_layout.addWidget(self._quality_box("Audio", False))
        save_row = QHBoxLayout()
        save_row.addWidget(self._label("Save to", "Muted"))
        save_path_label = self._label(str(backend.DOWNLOAD_DIR), "Muted")
        save_path_label.setToolTip(str(backend.DOWNLOAD_DIR))
        save_path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        save_row.addWidget(save_path_label, 1)
        quality_layout.addLayout(save_row)
        download_button = self._button("DOWNLOAD", "download")
        download_button.setMinimumHeight(44)
        download_button.clicked.connect(self.download_selected)
        quality_layout.addWidget(download_button)
        workspace.addWidget(quality_card)
        content_layout.addLayout(workspace, 1)

        progress_card, progress_layout = self._card(margins=(18, 12, 18, 12))
        progress_header = QHBoxLayout()
        progress_header.addWidget(self._step("04", "Download progress"))
        progress_header.addStretch(1)
        progress_status = self._label("Ready", "Muted")
        self.progress_text_var.changed.connect(progress_status.setText)
        progress_header.addWidget(progress_status)
        progress_layout.addLayout(progress_header)
        self.progress_bar = QProgressBar()
        self.progress_bar.setObjectName("Progress")
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_var.changed.connect(lambda value: self.progress_bar.setValue(int(float(value))))
        progress_layout.addWidget(self.progress_bar)
        content_layout.addWidget(progress_card)

        activity = QHBoxLayout()
        activity.setSpacing(16)
        batch_card, batch_layout = self._card(margins=(18, 14, 18, 14))
        batch_header = QHBoxLayout()
        batch_header.addWidget(self._label("Batch download", "CardTitle"))
        batch_header.addWidget(self._label("Channels / playlists", "Muted"), 1)
        batch_layout.addLayout(batch_header)
        batch_controls = QHBoxLayout()
        self.batch_entry = QtLineEdit()
        self.batch_entry.setPlaceholderText("Channel or playlist URL")
        self.batch_entry.setMinimumHeight(38)
        self._bind_text(self.batch_entry, self.batch_url_var)
        batch_controls.addWidget(self.batch_entry, 1)
        batch_controls.addWidget(self._label("Recent", "Muted"))
        self.batch_spin = QSpinBox()
        self.batch_spin.setRange(1, 1000)
        self.batch_spin.setValue(10)
        self.batch_spin.setFixedWidth(70)
        self.batch_spin.valueChanged.connect(self.batch_count_var.set)
        self.batch_count_var.changed.connect(lambda value: self.batch_spin.setValue(int(value)))
        batch_controls.addWidget(self.batch_spin)
        recent_button = self._button("Download recent", "secondary")
        recent_button.clicked.connect(self.download_recent)
        batch_controls.addWidget(recent_button)
        batch_layout.addLayout(batch_controls)
        activity.addWidget(batch_card, 3)

        logs_card, logs_layout = self._card(margins=(18, 14, 18, 14))
        logs_header = QHBoxLayout()
        logs_header.addWidget(self._label("Activity log", "CardTitle"))
        logs_header.addWidget(self._label("Click an entry to copy", "Muted"), 1, Qt.AlignmentFlag.AlignRight)
        logs_layout.addLayout(logs_header)
        log_tree = QTreeWidget()
        log_tree.setObjectName("LogTree")
        log_tree.setHeaderLabels(["Status"])
        log_tree.setRootIsDecorated(False)
        log_tree.setWordWrap(True)
        log_tree.setTextElideMode(Qt.TextElideMode.ElideNone)
        log_tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        log_tree.setUniformRowHeights(False)
        log_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        log_tree.setMinimumHeight(64)
        log_tree.setMaximumHeight(84)
        log_tree.itemClicked.connect(lambda _item, _column: self.copy_selected_log())
        self.log_box = TreeBridge(log_tree)
        logs_layout.addWidget(log_tree)
        activity.addWidget(logs_card, 2)
        content_layout.addLayout(activity)

        scroll.setWidget(content)
        self.page_stack.addWidget(scroll)
        self._build_history_page()
        self._build_settings_page()
        self._refresh_history_view()
        shell.addWidget(self.page_stack, 1)
        self.window.setCentralWidget(central)

    def _new_page(self):
        """Create a scrollable page with the same breathing room as Download."""
        scroll = QScrollArea()
        scroll.setObjectName("ContentScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        body.setObjectName("Content")
        layout = QVBoxLayout(body)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(16)
        scroll.setWidget(body)
        return scroll, layout

    def _page_header(self, title: str, subtitle: str):
        header = QHBoxLayout()
        header.setSpacing(12)
        copy = QVBoxLayout()
        copy.setSpacing(3)
        copy.addWidget(self._label(title, "Hero"))
        copy.addWidget(self._label(subtitle, "Subtitle"))
        header.addLayout(copy, 1)
        return header

    def _build_history_page(self):
        page, layout = self._new_page()
        header = self._page_header(
            "Download history",
            "Your completed videos and audio files, with a shortcut to their location.",
        )
        open_button = self._button("Open downloads", "ghost")
        open_button.clicked.connect(self.open_download_folder)
        header.addWidget(open_button, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(header)

        card, card_layout = self._card()
        self.history_empty_label = self._label(
            "No completed downloads yet. Your files will appear here after a download finishes.",
            "Muted",
        )
        self.history_empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card_layout.addWidget(self.history_empty_label)
        self.history_rows_layout = QVBoxLayout()
        self.history_rows_layout.setSpacing(10)
        card_layout.addLayout(self.history_rows_layout)
        layout.addWidget(card)
        layout.addStretch(1)
        self.history_page = page
        self.page_stack.addWidget(page)

    def _build_settings_page(self):
        page, layout = self._new_page()
        layout.addLayout(
            self._page_header(
                "Settings",
                "Choose the formats that are selected automatically when a link is analyzed.",
            )
        )

        card, card_layout = self._card()
        card_layout.addWidget(self._label("Preferred download quality", "CardTitle"))
        card_layout.addWidget(
            self._label(
                "The closest available format is selected for each list. You can still change it before downloading.",
                "Muted",
            )
        )

        video_row = QHBoxLayout()
        video_row.setSpacing(12)
        video_copy = QVBoxLayout()
        video_copy.addWidget(self._label("Video quality", "InnerTitle"))
        video_copy.addWidget(self._label("Target resolution for the video stream.", "Muted"))
        video_row.addLayout(video_copy, 1)
        self.video_quality_combo = QComboBox()
        for label, value in (
            ("Best available", 0),
            ("2160p (4K)", 2160),
            ("1440p", 1440),
            ("1080p", 1080),
            ("720p", 720),
            ("480p", 480),
        ):
            self.video_quality_combo.addItem(label, value)
        self.video_quality_combo.setCurrentIndex(
            max(0, self.video_quality_combo.findData(self.preferred_video_height))
        )
        self.video_quality_combo.setMinimumWidth(170)
        self.video_quality_combo.currentIndexChanged.connect(self._save_preferences_from_ui)
        video_row.addWidget(self.video_quality_combo, 0, Qt.AlignmentFlag.AlignTop)
        card_layout.addLayout(video_row)

        audio_row = QHBoxLayout()
        audio_row.setSpacing(12)
        audio_copy = QVBoxLayout()
        audio_copy.addWidget(self._label("Audio quality", "InnerTitle"))
        audio_copy.addWidget(self._label("Target bitrate for the audio stream.", "Muted"))
        audio_row.addLayout(audio_copy, 1)
        self.audio_quality_combo = QComboBox()
        for label, value in (
            ("Best available", 0),
            ("320 kbps", 320),
            ("256 kbps", 256),
            ("192 kbps", 192),
            ("128 kbps", 128),
            ("96 kbps", 96),
        ):
            self.audio_quality_combo.addItem(label, value)
        self.audio_quality_combo.setCurrentIndex(
            max(0, self.audio_quality_combo.findData(self.preferred_audio_bitrate))
        )
        self.audio_quality_combo.setMinimumWidth(170)
        self.audio_quality_combo.currentIndexChanged.connect(self._save_preferences_from_ui)
        audio_row.addWidget(self.audio_quality_combo, 0, Qt.AlignmentFlag.AlignTop)
        card_layout.addLayout(audio_row)
        layout.addWidget(card)
        layout.addStretch(1)
        self.settings_page = page
        self.page_stack.addWidget(page)

    def _save_preferences_from_ui(self):
        self.preferred_video_height = int(self.video_quality_combo.currentData() or 0)
        self.preferred_audio_bitrate = int(self.audio_quality_combo.currentData() or 0)
        self.preferences = {
            "preferred_video_height": self.preferred_video_height,
            "preferred_audio_bitrate": self.preferred_audio_bitrate,
        }
        backend.save_preferences(self.preferences)
        self._apply_preferred_selections()

    def _preferred_format_index(self, formats: list[dict], kind: str) -> int | None:
        """Choose the closest available format to the user's saved preference."""
        if not formats:
            return None
        target = (
            self.preferred_video_height
            if kind == "video"
            else self.preferred_audio_bitrate
        )
        if target <= 0:
            return 0

        metric_name = "height" if kind == "video" else "abr"
        candidates = [
            (index, self._numeric_value(item.get(metric_name)))
            for index, item in enumerate(formats)
        ]
        candidates = [(index, value) for index, value in candidates if value > 0]
        if not candidates:
            return 0
        return min(
            candidates,
            key=lambda pair: (
                0 if pair[1] <= target else 1,
                abs(pair[1] - target),
                pair[0],
            ),
        )[0]

    def _apply_preferred_selections(self):
        for formats, adapter, kind in (
            (self.video_formats, self.video_list, "video"),
            (self.audio_formats, self.audio_list, "audio"),
        ):
            items = adapter.get_children()
            index = self._preferred_format_index(formats, kind)
            if not items or index is None:
                continue
            adapter.selection_remove(*adapter.selection())
            selected = items[max(0, min(index, len(items) - 1))]
            adapter.selection_set(selected)
            adapter.focus(selected)

    def _show_page(self, page: str):
        page_indexes = {"Download": 0, "History": 1, "Settings": 2}
        index = page_indexes[page]
        self.page_stack.setCurrentIndex(index)
        for label, button in self.nav_buttons.items():
            button.setProperty("variant", "nav-active" if label == page else "nav")
            button.style().unpolish(button)
            button.style().polish(button)
        if page == "History":
            self._refresh_history_view()

    def _history_updated(self):
        self._refresh_history_view()

    def _refresh_history_view(self):
        if not hasattr(self, "history_rows_layout"):
            return
        self.history_entries = backend.load_download_history()
        try:
            existing = [
                path for path in backend.DOWNLOAD_DIR.iterdir()
                if path.is_file() and path.suffix not in {".part", ".ytdl"}
            ]
            existing_entries = [
                {
                    "path": str(path.resolve()),
                    "title": path.stem,
                    "kind": "Existing download",
                    "downloaded_at": backend.time.strftime(
                        "%Y-%m-%d %H:%M:%S",
                        backend.time.localtime(path.stat().st_mtime),
                    ),
                }
                for path in sorted(
                    existing,
                    key=lambda item: item.stat().st_mtime,
                    reverse=True,
                )
            ]
            known_paths = {
                str(entry.get("path"))
                for entry in self.history_entries
                if isinstance(entry, dict) and entry.get("path")
            }
            self.history_entries.extend(
                entry for entry in existing_entries
                if entry["path"] not in known_paths
            )
        except OSError:
            pass
        self.history_entries = [
            entry for entry in self.history_entries
            if isinstance(entry, dict) and entry.get("path")
        ][:100]

        while self.history_rows_layout.count():
            item = self.history_rows_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        self.history_empty_label.setVisible(not self.history_entries)
        reveal_text = "Show in Explorer"
        if backend.sys.platform == "darwin":
            reveal_text = "Reveal in Finder"
        elif backend.sys.platform not in ("win32", "darwin"):
            reveal_text = "Open folder"

        for entry in self.history_entries:
            path = backend.Path(str(entry["path"]))
            row = QFrame()
            row.setObjectName("HistoryRow")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(14, 12, 14, 12)
            row_layout.setSpacing(12)
            copy = QVBoxLayout()
            copy.setSpacing(3)
            copy.addWidget(self._label(str(entry.get("title") or path.stem), "CardTitle"))
            details = str(entry.get("kind") or "Download")
            if entry.get("downloaded_at"):
                details += f" · {entry['downloaded_at']}"
            copy.addWidget(self._label(details, "Muted"))
            path_label = self._label(str(path), "MutedSmall")
            path_label.setToolTip(str(path))
            copy.addWidget(path_label)
            row_layout.addLayout(copy, 1)
            reveal = self._button(reveal_text, "secondary")
            reveal.setEnabled(path.is_file())
            reveal.setToolTip(str(path) if path.is_file() else "This file is no longer available")
            reveal.clicked.connect(lambda _checked=False, value=str(path): self.reveal_download_path(value))
            row_layout.addWidget(reveal, 0, Qt.AlignmentFlag.AlignTop)
            self.history_rows_layout.addWidget(row)

    def _quality_box(self, title: str, video: bool):
        box = QFrame()
        box.setObjectName("InnerCard")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(7)
        layout.addWidget(self._label(title, "InnerTitle"))
        tree = QTreeWidget()
        tree.setHeaderLabels(["Available formats"])
        tree.setRootIsDecorated(False)
        tree.setSelectionMode(QTreeWidget.SelectionMode.SingleSelection)
        tree.setWordWrap(True)
        tree.setTextElideMode(Qt.TextElideMode.ElideNone)
        tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        tree.setUniformRowHeights(False)
        tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        tree.setMinimumHeight(88)
        tree.setMaximumHeight(116)
        tree.setAlternatingRowColors(False)
        adapter = TreeBridge(tree)
        if video:
            self.video_list = adapter
        else:
            self.audio_list = adapter
        layout.addWidget(tree)
        clear = self._button("Clear selection", "ghost")
        clear.clicked.connect(lambda: adapter.selection_remove(*adapter.selection()))
        layout.addWidget(clear, 0, Qt.AlignmentFlag.AlignLeft)
        return box

    def _apply_styles(self):
        self.window.setStyleSheet(
            """
            QWidget { color: #f8fafc; font-family: 'Segoe UI'; font-size: 10pt; }
            QMainWindow, QWidget#App, QWidget#ContentScroll { background: #0b1020; }
            QWidget#Sidebar { background: #080d1a; }
            QScrollArea#ContentScroll { border: 0; }
            QFrame#Card { background: #131b2f; border: 1px solid #2a3a5b; border-radius: 18px; }
            QFrame#InnerCard { background: #0e172a; border: 1px solid #243452; border-radius: 13px; }
            QFrame#HistoryRow { background: #0e172a; border: 1px solid #243452; border-radius: 12px; }
            QFrame#PreviewSurface { background: #0d1424; border: 1px solid #263554; border-radius: 14px; }
            QLabel#Hero { color: #f8fafc; font-size: 24pt; font-weight: 700; }
            QLabel#Subtitle { color: #94a3b8; font-size: 10.5pt; }
            QLabel#Brand { color: #f8fafc; font-size: 11pt; font-weight: 700; }
            QLabel#Muted, QLabel#Body { color: #94a3b8; }
            QLabel#MutedSmall { color: #64748b; font-size: 8pt; }
            QLabel#CardTitle { color: #f8fafc; font-size: 12pt; font-weight: 700; }
            QLabel#InnerTitle { color: #cbd5e1; font-weight: 700; }
            QLabel#PreviewLabel { color: #64748b; font-size: 11pt; }
            QLabel#Logo { color: #38bdf8; background: #10263d; border: 1px solid #235476; border-radius: 13px; font-size: 23pt; }
            QLabel#StepBadge { color: #ffffff; background: #2563eb; border-radius: 9px; font-weight: 700; }
            QPushButton { color: #e2e8f0; background: #19243a; border: 1px solid #2f4165; border-radius: 11px; padding: 9px 14px; font-weight: 600; }
            QPushButton:hover { background: #243452; border-color: #416187; }
            QPushButton:pressed { background: #2a4167; }
            QPushButton:disabled { color: #64748b; background: #11192a; border-color: #1d2a44; }
            QPushButton[variant="primary"] { color: #ffffff; background: #9333ea; border-color: #a855f7; }
            QPushButton[variant="primary"]:hover { background: #a855f7; }
            QPushButton[variant="download"] { color: #ffffff; background: #2563eb; border-color: #38bdf8; font-size: 11pt; }
            QPushButton[variant="download"]:hover { background: #1d7ed0; }
            QPushButton[variant="ghost"] { color: #94a3b8; background: transparent; border-color: transparent; }
            QPushButton[variant="ghost"]:hover { color: #f8fafc; background: #19243a; }
            QPushButton#NavButton { text-align: left; border: 0; border-radius: 11px; padding: 10px 12px; }
            QPushButton#NavButton[variant="nav-active"] { color: #f8fafc; background: #131b2f; border-left: 3px solid #9333ea; }
            QPushButton#NavButton[variant="nav"] { color: #64748b; background: transparent; }
            QLineEdit, QSpinBox { color: #f8fafc; background: #0d1424; border: 1px solid #2f4165; border-radius: 11px; padding: 9px 11px; selection-background-color: #24506c; }
            QLineEdit:focus, QSpinBox:focus { border-color: #38bdf8; }
            QComboBox { color: #f8fafc; background: #0d1424; border: 1px solid #2f4165; border-radius: 11px; padding: 9px 11px; min-height: 18px; }
            QComboBox:hover { border-color: #416187; }
            QComboBox QAbstractItemView { color: #f8fafc; background: #0d1424; border: 1px solid #2f4165; selection-background-color: #24506c; }
            QLineEdit#UrlField { font-size: 11pt; }
            QLineEdit#TimeField { padding: 7px 9px; }
            QTreeWidget { color: #cbd5e1; background: #0d1424; border: 1px solid #243452; border-radius: 9px; outline: 0; }
            QTreeWidget::item { padding: 7px 8px; border-bottom: 1px solid #18243b; }
            QTreeWidget::item:selected { color: #ffffff; background: #24506c; border-radius: 5px; }
            QHeaderView::section { color: #94a3b8; background: #19243a; border: 0; padding: 7px 8px; font-weight: 700; }
            QProgressBar#Progress { background: #0d1424; border: 1px solid #263554; border-radius: 7px; height: 12px; }
            QProgressBar#Progress::chunk { background: #38bdf8; border-radius: 6px; }
            """
        )

    def _draw_range_picker(self):
        if hasattr(self, "range_canvas"):
            self.range_canvas.set_data(
                self.preview_duration,
                self.range_start_seconds,
                self.range_end_seconds,
                self.preview_playhead,
            )

    def _preview_after(self, callback):
        if not self._closing:
            self.root.after(0, callback)

    def _display_pixmap(self, encoded: str):
        try:
            image = QImage.fromData(base64.b64decode(encoded))
            if image.isNull():
                return None
            return QPixmap.fromImage(image)
        except (ValueError, TypeError):
            return None

    def _set_preview_pixmap(self, pixmap: QPixmap):
        self.preview_pixmap = pixmap
        label = self.preview_label.widget
        scaled = pixmap.scaled(label.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        label.setPixmap(scaled)
        label.setText("")

    def _display_preview_frame(self, encoded, timestamp, generation, request_id):
        if self._closing or generation != self.preview_generation or request_id != self.preview_request_id:
            return
        pixmap = self._display_pixmap(encoded)
        if pixmap is None:
            return
        self.preview_photo = pixmap
        self._set_preview_pixmap(pixmap)
        self.preview_playhead = timestamp
        self._draw_range_picker()

    def _display_playback_frame(self, encoded, timestamp, generation, token):
        if self._closing or generation != self.preview_generation or token != self.preview_player_token or not self.preview_playing:
            return
        pixmap = self._display_pixmap(encoded)
        if pixmap is None:
            return
        self.preview_photo = pixmap
        self._set_preview_pixmap(pixmap)
        self.preview_playhead = timestamp
        self._draw_range_picker()


def run_qt_app(update_error: str = "") -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Video Downloader")
    app.setStyle("Fusion")
    window = QtMainWindow()
    controller = QtVideoDownloaderGUI(window)
    window.show()
    if update_error:
        controller.root.after(
            200,
            lambda: backend.messagebox.showerror(
                "Update Failed",
                f"The previous version was reopened. Details are in:\n{update_error}",
            ),
        )
    return app.exec()
