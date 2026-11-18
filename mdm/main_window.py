import os
import subprocess
from datetime import datetime
from typing import Optional, Dict, Any, List
from pathlib import Path
from urllib.parse import urlparse

from PySide6.QtCore import Qt, QUrl, Slot, QCoreApplication
from PySide6.QtGui import QColor, QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QFrame, QLabel, QPushButton, QLineEdit,
    QVBoxLayout, QHBoxLayout, QGridLayout, QStackedWidget, QTableWidget,
    QTableWidgetItem, QHeaderView, QDialog, QFormLayout, QFileDialog,
    QSpinBox, QMessageBox, QAbstractItemView, QProgressBar, QMenu,
    QProgressDialog, QComboBox
)

from mdm.queue_manager import QueueManager
from mdm.database import DatabaseManager
from mdm.models import DownloadStatus
from mdm.utils import sanitize_filename, extract_filename_from_url, format_bytes
from mdm.settings import get_default_connections, set_default_connections, VALID_CONNECTION_COUNTS


APP_STYLE = """
QWidget {
    background: #0d1422;
    color: #e7edf7;
    font-family: "Segoe UI", sans-serif;
    font-size: 10pt;
}
QFrame#Sidebar { background: #101a2b; border-right: 1px solid #202c40; }
QFrame#TopBar { background: #111c2e; border-bottom: 1px solid #202c40; }
QLabel#Brand { color: #ffffff; font-size: 17pt; font-weight: 700; }
QLabel#Muted { color: #91a0b7; }
QLabel#PageTitle { color: #ffffff; font-size: 20pt; font-weight: 700; }
QLabel#CardValue { color: #ffffff; font-size: 22pt; font-weight: 700; }
QLabel#CardLabel { color: #91a0b7; font-size: 9pt; }
QFrame#MetricCard, QFrame#Panel {
    background: #142137;
    border: 1px solid #253650;
    border-radius: 12px;
}
QPushButton {
    background: #1b2a42;
    color: #dce7f7;
    border: 1px solid #30435f;
    border-radius: 8px;
    padding: 8px 14px;
    font-weight: 500;
}
QPushButton:hover { background: #263a58; }
QPushButton:pressed { background: #172439; }
QPushButton#PrimaryButton {
    background: #3478f6;
    color: white;
    border: 1px solid #3478f6;
    font-weight: 600;
}
QPushButton#PrimaryButton:hover { background: #2468e5; }
QPushButton#DangerButton {
    background: #2a1b24;
    color: #f87171;
    border: 1px solid #5a2635;
}
QPushButton#DangerButton:hover { background: #3b2230; color: #fca5a5; }
QPushButton#NavButton {
    text-align: left;
    padding: 10px 12px;
    background: transparent;
    border: none;
    color: #aebbd0;
}
QPushButton#NavButton:hover { background: #1b2a42; color: white; }
QPushButton#NavButton[active="true"] {
    background: #20395f;
    color: #ffffff;
    border-left: 3px solid #4c8dff;
}
QLineEdit, QSpinBox {
    background: #0d1728;
    border: 1px solid #30435f;
    border-radius: 7px;
    padding: 8px 10px;
    color: #ffffff;
    selection-background-color: #3478f6;
}
QTableWidget {
    background: #142137;
    alternate-background-color: #17263d;
    border: none;
    gridline-color: #263650;
    selection-background-color: #203c68;
    selection-color: white;
}
QHeaderView::section {
    background: #1a2a43;
    color: #aebbd0;
    padding: 10px;
    border: none;
    border-bottom: 1px solid #30435f;
    font-weight: 600;
}
QTableWidget::item { padding: 6px; }
QProgressBar {
    border: 1px solid #283a54;
    border-radius: 5px;
    text-align: center;
    background-color: #0b1322;
    color: #ffffff;
    font-size: 8.5pt;
    font-weight: 600;
}
QProgressBar::chunk {
    background-color: #3478f6;
    border-radius: 4px;
}
QMenu {
    background-color: #142137;
    border: 1px solid #2e4160;
    padding: 4px;
}
QMenu::item {
    padding: 6px 20px;
    color: #e7edf7;
    border-radius: 4px;
}
QMenu::item:selected {
    background-color: #254778;
    color: white;
}
QDialog { background: #111c2e; }
"""


class SegmentDetailsDialog(QDialog):
    """
    Dialog displaying real-time segmented download status, individual segment progress,
    active connections, byte ranges, and fallback state.
    """
    def __init__(self, item_id: str, queue_mgr: QueueManager, parent=None):
        super().__init__(parent)
        self.item_id = item_id
        self.queue_mgr = queue_mgr
        self.setWindowTitle("Segment Details")
        self.resize(720, 440)
        self.setMinimumSize(580, 320)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        self.title_label = QLabel("Segment Progress")
        self.title_label.setObjectName("PageTitle")
        self.subtitle_label = QLabel("Loading segment details…")
        self.subtitle_label.setObjectName("Muted")
        layout.addWidget(self.title_label)
        layout.addWidget(self.subtitle_label)

        overview = QFrame()
        overview.setObjectName("Panel")
        overview_layout = QVBoxLayout(overview)
        overview_layout.setContentsMargins(14, 12, 14, 12)
        overview_layout.setSpacing(6)

        self.overall_label = QLabel("Overall: 0%")
        self.overall_pbar = QProgressBar()
        self.overall_pbar.setFixedHeight(20)
        self.status_detail_label = QLabel("Mode: Initializing…")
        self.status_detail_label.setObjectName("Muted")
        self.status_detail_label.setStyleSheet("font-size: 8.5pt;")

        overview_layout.addWidget(self.overall_label)
        overview_layout.addWidget(self.overall_pbar)
        overview_layout.addWidget(self.status_detail_label)
        layout.addWidget(overview)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["SEGMENT", "BYTE RANGE", "PROGRESS", "DOWNLOADED", "STATUS"])
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(36)

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)

        layout.addWidget(self.table, 1)

        btn_box = QHBoxLayout()
        btn_box.addStretch()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btn_box.addWidget(close_btn)
        layout.addLayout(btn_box)

        self.queue_mgr.segments_updated.connect(self._on_segments_updated)
        self.queue_mgr.item_updated.connect(self._on_item_updated)

        self.refresh()

    def refresh(self):
        item = self.queue_mgr.get_item(self.item_id)
        if not item:
            return
        self.title_label.setText(f"Segments: {item.get('filename', 'Download')}")
        self.subtitle_label.setText(f"URL: {item.get('url', '')}")

        pval = item.get("progress_value", 0)
        if pval == -1:
            self.overall_pbar.setRange(0, 0)
            self.overall_pbar.setFormat("Downloading…")
        else:
            self.overall_pbar.setRange(0, 100)
            self.overall_pbar.setValue(pval)
            self.overall_pbar.setFormat(item.get("progress", f"{pval}%"))

        self.overall_label.setText(f"Overall Progress: {item.get('progress', '0%')} ({item.get('size', 'Unknown')})")
        mode = item.get("mode_description", "Segmented")
        active = item.get("active_connections", 0)
        total_conns = item.get("connections", 8)
        self.status_detail_label.setText(
            f"Mode: {mode} | Active connections: {active} / {total_conns} max | Speed: {item.get('speed', '—')}"
        )

        segments = item.get("segments", [])
        if not segments:
            self.table.setRowCount(1)
            self.table.setItem(0, 0, QTableWidgetItem("Segment 1 (Full stream)"))
            self.table.setItem(0, 1, QTableWidgetItem(f"0 – {item.get('size', 'Unknown')}"))
            pbar = QProgressBar()
            pbar.setFixedHeight(18)
            pbar.setRange(0, 100 if pval >= 0 else 0)
            pbar.setValue(max(0, pval))
            pbar.setFormat(item.get("progress", f"{pval}%"))
            self.table.setCellWidget(0, 2, pbar)
            self.table.setItem(
                0, 3,
                QTableWidgetItem(f"{format_bytes(item.get('downloaded_bytes', 0))} / {item.get('size', 'Unknown')}")
            )
            s_item = QTableWidgetItem(item.get("status", "Queued"))
            self._color_status(s_item, item.get("status", "Queued"))
            self.table.setItem(0, 4, s_item)
            return

        self.table.setRowCount(len(segments))
        for row, seg in enumerate(segments):
            self.table.setItem(row, 0, QTableWidgetItem(f"Segment {seg['index'] + 1}"))
            range_str = f"{format_bytes(seg['start'])} – {format_bytes(seg['end'])}"
            self.table.setItem(row, 1, QTableWidgetItem(range_str))

            pbar = QProgressBar()
            pbar.setFixedHeight(18)
            pbar.setRange(0, 100)
            pbar.setValue(seg.get("progress_value", 0))
            pbar.setFormat(seg.get("progress", "0%"))
            self.table.setCellWidget(row, 2, pbar)

            dl_str = f"{format_bytes(seg.get('downloaded', 0))} / {format_bytes(seg.get('expected_size', 0))}"
            self.table.setItem(row, 3, QTableWidgetItem(dl_str))

            s_item = QTableWidgetItem(seg.get("status", "Queued"))
            self._color_status(s_item, seg.get("status", "Queued"))
            self.table.setItem(row, 4, s_item)

    def _color_status(self, item: QTableWidgetItem, status: str):
        if status == "Completed":
            item.setForeground(QColor("#38c172"))
        elif status == "Downloading":
            item.setForeground(QColor("#4c8dff"))
        elif status.startswith("Retrying"):
            item.setForeground(QColor("#f6993f"))
        elif status == "Paused":
            item.setForeground(QColor("#f59e0b"))
        elif status == "Interrupted":
            item.setForeground(QColor("#a855f7"))
        elif status == "Failed":
            item.setForeground(QColor("#e3342f"))
        else:
            item.setForeground(QColor("#e5b839"))

    def _on_segments_updated(self, item_id: str, segments_data: list):
        if item_id == self.item_id:
            self.refresh()

    def _on_item_updated(self, item_id: str):
        if item_id == self.item_id:
            self.refresh()


class AddDownloadDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add download")
        self.setMinimumWidth(560)
        self.setModal(True)

        layout = QVBoxLayout(self)
        title = QLabel("Add a download")
        title.setObjectName("PageTitle")
        subtitle = QLabel("Add an HTTP or HTTPS link to your download queue.")
        subtitle.setObjectName("Muted")
        layout.addWidget(title)
        layout.addWidget(subtitle)

        form = QFormLayout()
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("https://example.com/file.zip")
        self.folder_input = QLineEdit(str(Path.home() / "Downloads"))
        browse_button = QPushButton("Browse…")
        browse_button.clicked.connect(self.choose_folder)

        folder_row = QHBoxLayout()
        folder_row.addWidget(self.folder_input, 1)
        folder_row.addWidget(browse_button)

        self.connections_combo = QComboBox()
        for c in VALID_CONNECTION_COUNTS:
            self.connections_combo.addItem(f"{c} connection{'s' if c > 1 else ''}", c)
        default_idx = self.connections_combo.findData(get_default_connections())
        if default_idx >= 0:
            self.connections_combo.setCurrentIndex(default_idx)
        self.connections_combo.setToolTip("Select number of simultaneous HTTP connections (segments) for this download.")

        form.addRow("Download URL", self.url_input)
        form.addRow("Save folder", folder_row)
        form.addRow("Connections", self.connections_combo)
        layout.addLayout(form)

        note = QLabel(
            "Stage 3 Engine: Advanced multi-connection downloading via HTTP Range requests. "
            "Supported files are accelerated using parallel segments with automatic single-connection fallback."
        )
        note.setObjectName("Muted")
        note.setWordWrap(True)
        layout.addWidget(note)

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel_button = QPushButton("Cancel")
        add_button = QPushButton("Add to queue")
        add_button.setObjectName("PrimaryButton")
        cancel_button.clicked.connect(self.reject)
        add_button.clicked.connect(self.validate_and_accept)
        buttons.addWidget(cancel_button)
        buttons.addWidget(add_button)
        layout.addLayout(buttons)

    def choose_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Choose download folder", self.folder_input.text()
        )
        if folder:
            self.folder_input.setText(folder)

    def validate_and_accept(self):
        url_text = self.url_input.text().strip()
        parsed = urlparse(url_text)

        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            QMessageBox.warning(
                self, "Invalid URL",
                "Enter a valid HTTP or HTTPS download URL."
            )
            return

        folder = Path(self.folder_input.text()).expanduser()
        if not folder.exists() or not folder.is_dir():
            try:
                folder.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                QMessageBox.warning(
                    self, "Invalid folder",
                    f"Unable to use chosen folder: {e}"
                )
                return

        self.accept()

    def get_data(self):
        url_text = self.url_input.text().strip()
        return {
            "url": url_text,
            "explicit_filename": None,
            "folder": self.folder_input.text().strip(),
            "connections": self.connections_combo.currentData(),
        }


class MainWindow(QMainWindow):
    def __init__(self, db: Optional[DatabaseManager] = None):
        super().__init__()
        self.setWindowTitle("My Download Manager")
        self.resize(1260, 820)
        self.setMinimumSize(1020, 680)

        self.db = db
        self.queue_mgr = QueueManager(self, db=self.db)
        self.current_filter = "All Downloads"
        self.nav_buttons = {}
        self._item_row_map = {}  # item_id -> row

        central = QWidget()
        self.setCentralWidget(central)
        outer = QHBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.sidebar = self.build_sidebar()
        outer.addWidget(self.sidebar)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)
        content_layout.addWidget(self.build_topbar())

        self.pages = QStackedWidget()
        self.download_page = self.build_download_page()
        self.settings_page = self.build_settings_page()
        self.pages.addWidget(self.download_page)
        self.pages.addWidget(self.settings_page)
        content_layout.addWidget(self.pages, 1)

        outer.addWidget(content, 1)
        self.setStyleSheet(APP_STYLE)
        self.set_nav_active("Dashboard")

        # Connect QueueManager signals
        self.queue_mgr.item_added.connect(self._on_item_added)
        self.queue_mgr.item_updated.connect(self._on_item_updated)
        self.queue_mgr.download_paused.connect(self._on_item_updated)
        self.queue_mgr.metrics_changed.connect(self._on_metrics_changed)

        # Initial render of persistent items and metrics
        self.refresh_table()
        self.queue_mgr.emit_metrics()

    def build_sidebar(self):
        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(225)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(16, 22, 16, 18)
        layout.setSpacing(8)

        brand = QLabel("⬇  MY DOWNLOADS")
        brand.setObjectName("Brand")
        layout.addWidget(brand)

        tagline = QLabel("PERSONAL DOWNLOAD MANAGER")
        tagline.setObjectName("Muted")
        tagline.setStyleSheet("font-size: 8pt; letter-spacing: 1px;")
        layout.addWidget(tagline)
        layout.addSpacing(28)

        sections = [
            ("MAIN", ["Dashboard", "All Downloads", "Downloading", "Completed", "Queue"]),
            ("LIBRARY", ["Videos", "Documents", "Compressed", "Programs"]),
            ("TOOLS", ["Scheduler", "Settings"]),
        ]

        for section_name, items in sections:
            label = QLabel(section_name)
            label.setObjectName("Muted")
            label.setStyleSheet("font-size: 8pt; font-weight: 700; padding: 8px 4px;")
            layout.addWidget(label)
            for item in items:
                button = QPushButton(f"  {self.nav_icon(item)}   {item}")
                button.setObjectName("NavButton")
                button.setProperty("active", False)
                button.setCursor(Qt.PointingHandCursor)
                button.clicked.connect(lambda checked=False, name=item: self.navigate(name))
                self.nav_buttons[item] = button
                layout.addWidget(button)

        layout.addStretch()
        version = QLabel("MY DOWNLOAD MANAGER\nStage 4 • Persistence & Recovery")
        version.setObjectName("Muted")
        version.setStyleSheet("font-size: 8pt;")
        layout.addWidget(version)
        return sidebar

    @staticmethod
    def nav_icon(name):
        icons = {
            "Dashboard": "▦", "All Downloads": "⇩", "Downloading": "↓",
            "Completed": "✓", "Queue": "☷", "Videos": "▶",
            "Documents": "▤", "Compressed": "▧", "Programs": "⚙",
            "Scheduler": "◷", "Settings": "⚙",
        }
        return icons.get(name, "•")

    def build_topbar(self):
        bar = QFrame()
        bar.setObjectName("TopBar")
        bar.setFixedHeight(76)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(26, 12, 26, 12)

        self.top_title = QLabel("Dashboard")
        self.top_title.setStyleSheet("font-size: 15pt; font-weight: 650;")
        layout.addWidget(self.top_title)
        layout.addStretch()

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search downloads…")
        self.search_input.setFixedWidth(260)
        self.search_input.textChanged.connect(self.refresh_table)
        layout.addWidget(self.search_input)

        add_button = QPushButton("+  Add URL")
        add_button.setObjectName("PrimaryButton")
        add_button.setCursor(Qt.PointingHandCursor)
        add_button.clicked.connect(self.open_add_dialog)
        layout.addWidget(add_button)
        return bar

    def build_download_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 24, 28, 26)
        layout.setSpacing(18)

        intro = QHBoxLayout()
        heading_box = QVBoxLayout()
        heading = QLabel("Your downloads, organised.")
        heading.setObjectName("PageTitle")
        subheading = QLabel("Manage files and monitor your download activity in real time.")
        subheading.setObjectName("Muted")
        heading_box.addWidget(heading)
        heading_box.addWidget(subheading)
        intro.addLayout(heading_box)
        intro.addStretch()
        layout.addLayout(intro)

        metrics = QGridLayout()
        metrics.setSpacing(14)
        self.metric_total = self.make_metric_card("TOTAL DOWNLOADS", "0", "All items in this session")
        self.metric_active = self.make_metric_card("DOWNLOADING", "0", "Currently active")
        self.metric_completed = self.make_metric_card("COMPLETED", "0", "Successfully finished")
        self.metric_queued = self.make_metric_card("QUEUED", "0", "Waiting to start")
        metrics.addWidget(self.metric_total, 0, 0)
        metrics.addWidget(self.metric_active, 0, 1)
        metrics.addWidget(self.metric_completed, 0, 2)
        metrics.addWidget(self.metric_queued, 0, 3)
        layout.addLayout(metrics)

        panel = QFrame()
        panel.setObjectName("Panel")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(16, 14, 16, 14)
        panel_layout.setSpacing(12)

        table_header = QHBoxLayout()
        table_title = QLabel("Download list")
        table_title.setStyleSheet("font-size: 13pt; font-weight: 650;")
        table_header.addWidget(table_title)

        table_header.addSpacing(16)
        self.btn_start_queue = QPushButton("▶  Start Queued")
        self.btn_start_queue.setCursor(Qt.PointingHandCursor)
        self.btn_start_queue.setToolTip("Start processing all queued downloads sequentially")
        self.btn_start_queue.clicked.connect(self.queue_mgr.start_all)
        table_header.addWidget(self.btn_start_queue)

        self.btn_pause = QPushButton("⏸  Pause Active")
        self.btn_pause.setCursor(Qt.PointingHandCursor)
        self.btn_pause.setToolTip("Pause the active download safely (preserves partial data)")
        self.btn_pause.clicked.connect(self.queue_mgr.pause_active)
        table_header.addWidget(self.btn_pause)

        self.btn_resume = QPushButton("▶  Resume")
        self.btn_resume.setCursor(Qt.PointingHandCursor)
        self.btn_resume.setToolTip("Resume the selected download or first paused/interrupted download")
        self.btn_resume.clicked.connect(self._on_resume_clicked)
        table_header.addWidget(self.btn_resume)

        self.btn_cancel = QPushButton("⏹  Cancel Active")
        self.btn_cancel.setObjectName("DangerButton")
        self.btn_cancel.setCursor(Qt.PointingHandCursor)
        self.btn_cancel.setToolTip("Cancel the active download")
        self.btn_cancel.clicked.connect(self.queue_mgr.cancel_active)
        table_header.addWidget(self.btn_cancel)

        table_header.addStretch()
        self.list_hint = QLabel("New links will appear here")
        self.list_hint.setObjectName("Muted")
        table_header.addWidget(self.list_hint)
        panel_layout.addLayout(table_header)

        # 7 Columns: FILE NAME, STATUS, SIZE, PROGRESS, SPEED, ETA, ADDED
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["FILE NAME", "STATUS", "SIZE", "PROGRESS", "SPEED", "ETA", "ADDED"]
        )
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(48)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.show_context_menu)
        self.table.doubleClicked.connect(self._on_table_double_clicked)

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        for col in range(1, 7):
            header.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.Interactive)
        self.table.setColumnWidth(3, 170)

        panel_layout.addWidget(self.table, 1)
        layout.addWidget(panel, 1)

        footer = QLabel(
            "Stage 4 Engine: Advanced multi-connection downloading with Pause, Resume, SQLite persistence, and crash recovery."
        )
        footer.setObjectName("Muted")
        footer.setWordWrap(True)
        layout.addWidget(footer)
        return page

    def make_metric_card(self, label, value, description):
        card = QFrame()
        card.setObjectName("MetricCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 14, 16, 14)
        name = QLabel(label)
        name.setObjectName("CardLabel")
        value_label = QLabel(value)
        value_label.setObjectName("CardValue")
        desc = QLabel(description)
        desc.setObjectName("Muted")
        desc.setStyleSheet("font-size: 8pt;")
        layout.addWidget(name)
        layout.addWidget(value_label)
        layout.addWidget(desc)
        card.value_label = value_label
        return card

    def build_settings_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 24, 28, 26)
        layout.setSpacing(18)
        title = QLabel("Settings")
        title.setObjectName("PageTitle")
        layout.addWidget(title)

        # Connection Settings Panel
        conn_box = QFrame()
        conn_box.setObjectName("Panel")
        conn_layout = QVBoxLayout(conn_box)
        conn_layout.setContentsMargins(20, 16, 20, 16)
        conn_layout.setSpacing(10)

        conn_title = QLabel("Multi-Connection Download Settings")
        conn_title.setStyleSheet("font-size: 11pt; font-weight: 650;")
        conn_layout.addWidget(conn_title)

        form = QFormLayout()
        self.default_conns_combo = QComboBox()
        for c in VALID_CONNECTION_COUNTS:
            self.default_conns_combo.addItem(f"{c} connection{'s' if c > 1 else ''}", c)
        idx = self.default_conns_combo.findData(get_default_connections())
        if idx >= 0:
            self.default_conns_combo.setCurrentIndex(idx)
        self.default_conns_combo.currentIndexChanged.connect(self._on_default_conns_changed)
        form.addRow("Default connections per download:", self.default_conns_combo)
        conn_layout.addLayout(form)

        conn_hint = QLabel(
            "Select default simultaneous HTTP connections for downloads (1, 2, 4, 8, 16). "
            "When a server supports HTTP Range requests, files >= 2 MB are split into parallel segments. "
            "If range requests are unsupported, downloads automatically fall back to single-connection mode."
        )
        conn_hint.setObjectName("Muted")
        conn_hint.setWordWrap(True)
        conn_layout.addWidget(conn_hint)
        layout.addWidget(conn_box)

        # Stage 4 Information Panel
        info = QFrame()
        info.setObjectName("Panel")
        info_layout = QVBoxLayout(info)
        info_layout.setContentsMargins(20, 20, 20, 20)
        info_layout.addWidget(QLabel("My Download Manager — Stage 4: Persistence & Recovery"))

        description = QLabel(
            "Stage 4 implements robust SQLite persistence, download pause and resume, crash recovery, "
            "and multi-connection restart capabilities.\n\n"
            "Key capabilities:\n"
            "• SQLite database storage for download history, settings, and segmented progress.\n"
            "• Pause and resume downloads (both single-connection and multi-connection modes).\n"
            "• Application restart recovery: resume interrupted downloads from where they stopped.\n"
            "• Reusable .parts directory and segment state preservation across sessions.\n"
            "• ETag, Last-Modified, and Content-Range validator consistency.\n"
            "• Graceful shutdown with automatic download interruption and partial file preservation.\n"
            "• Safe file finalisation and clean cancellation cleanup."
        )
        description.setObjectName("Muted")
        description.setWordWrap(True)
        info_layout.addWidget(description)
        layout.addWidget(info)
        layout.addStretch()
        return page

    def _on_default_conns_changed(self):
        val = self.default_conns_combo.currentData()
        if val in VALID_CONNECTION_COUNTS:
            set_default_connections(val)

    def navigate(self, name):
        self.current_filter = name
        self.set_nav_active(name)
        self.top_title.setText(name)
        if name == "Settings":
            self.pages.setCurrentWidget(self.settings_page)
        else:
            self.pages.setCurrentWidget(self.download_page)
            self.refresh_table()

    def set_nav_active(self, active_name):
        for name, button in self.nav_buttons.items():
            button.setProperty("active", name == active_name)
            button.style().unpolish(button)
            button.style().polish(button)

    def open_add_dialog(self):
        dialog = AddDownloadDialog(self)
        if dialog.exec() == QDialog.Accepted:
            data = dialog.get_data()
            self.queue_mgr.add_download(
                url=data["url"],
                folder=data["folder"],
                explicit_filename=data.get("explicit_filename"),
                connections=data.get("connections"),
                auto_start=True,
            )

    def _on_item_added(self, item):
        self.refresh_table()

    def _on_item_updated(self, item_id: str):
        # Update specific row directly if present in current view
        if item_id in self._item_row_map:
            row = self._item_row_map[item_id]
            item = self.queue_mgr.get_item(item_id)
            if item:
                self._update_row_display(row, item)
        else:
            # Item might now match active filter (or status changed)
            self.refresh_table()

    def _on_metrics_changed(self, total: int, active: int, completed: int, queued: int):
        self.metric_total.value_label.setText(str(total))
        self.metric_active.value_label.setText(str(active))
        self.metric_completed.value_label.setText(str(completed))
        self.metric_queued.value_label.setText(str(queued))

    def _update_row_display(self, row: int, item: dict):
        """Updates UI cells and progress widget for an existing row."""
        # 0: Filename
        name_item = self.table.item(row, 0)
        if name_item:
            name_item.setText(item["filename"])
            if item.get("error"):
                name_item.setToolTip(f"Error: {item['error']}")
            elif item.get("final_path"):
                name_item.setToolTip(item["final_path"])
            else:
                name_item.setToolTip(item["url"])

        # 1: Status
        status_item = self.table.item(row, 1)
        if status_item:
            status_item.setText(item["status"])
            self._color_status_item(status_item, item["status"])
            tooltip_parts = []
            if item.get("mode_description"):
                tooltip_parts.append(f"Mode: {item['mode_description']}")
            if item.get("active_connections", 0) > 0:
                tooltip_parts.append(f"Active connections: {item['active_connections']}")
            if item.get("error"):
                tooltip_parts.append(f"Error: {item['error']}")
            status_item.setToolTip("\n".join(tooltip_parts) if tooltip_parts else item["status"])

        # 2: Size
        size_item = self.table.item(row, 2)
        if size_item:
            size_item.setText(item["size"])

        # 3: Progress Widget
        pbar = self.table.cellWidget(row, 3)
        if isinstance(pbar, QProgressBar):
            pval = item.get("progress_value", 0)
            if pval == -1:
                # Indeterminate
                pbar.setRange(0, 0)
                pbar.setFormat("Downloading…")
            else:
                pbar.setRange(0, 100)
                pbar.setValue(pval)
                pbar.setFormat(item.get("progress", f"{pval}%"))

        # 4: Speed
        speed_item = self.table.item(row, 4)
        if speed_item:
            speed_item.setText(item.get("speed", "—"))

        # 5: ETA
        eta_item = self.table.item(row, 5)
        if eta_item:
            eta_item.setText(item.get("eta", "—"))

    def _color_status_item(self, item: QTableWidgetItem, status: str):
        color_map = {
            "Queued": QColor("#e5b839"),
            "Downloading": QColor("#4c8dff"),
            "Completed": QColor("#38c172"),
            "Failed": QColor("#e3342f"),
            "Cancelled": QColor("#91a0b7"),
            "Paused": QColor("#f59e0b"),
            "Interrupted": QColor("#a855f7"),
        }
        if status.startswith("Retrying"):
            item.setForeground(QColor("#f6993f"))
        elif status in color_map:
            item.setForeground(color_map[status])

    def refresh_table(self):
        if not hasattr(self, "table"):
            return

        query = self.search_input.text().strip().lower()
        rows = []
        category_extensions = {
            "Videos": {".mp4", ".mkv", ".avi", ".mov", ".webm"},
            "Documents": {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".txt", ".ppt", ".pptx"},
            "Compressed": {".zip", ".rar", ".7z", ".tar", ".gz"},
            "Programs": {".exe", ".msi", ".msix", ".dmg", ".deb"},
        }

        for item in self.queue_mgr.items:
            name = item["filename"]
            if query and query not in name.lower() and query not in item["url"].lower():
                continue

            selected = self.current_filter
            if selected == "Downloading" and not (item["status"] == "Downloading" or item["status"].startswith("Retrying")):
                continue
            if selected == "Completed" and item["status"] != "Completed":
                continue
            if selected == "Queue" and item["status"] not in ("Queued", "Paused", "Interrupted"):
                continue
            if selected in category_extensions:
                if Path(name).suffix.lower() not in category_extensions[selected]:
                    continue
            rows.append(item)

        self._item_row_map.clear()
        self.table.setRowCount(len(rows))

        for row, item in enumerate(rows):
            self._item_row_map[item["id"]] = row

            # 0: Filename
            f_item = QTableWidgetItem(item["filename"])
            if item.get("error"):
                f_item.setToolTip(f"Error: {item['error']}")
            elif item.get("final_path"):
                f_item.setToolTip(item["final_path"])
            else:
                f_item.setToolTip(item["url"])
            self.table.setItem(row, 0, f_item)

            # 1: Status
            s_item = QTableWidgetItem(item["status"])
            self._color_status_item(s_item, item["status"])
            s_tooltips = []
            if item.get("mode_description"):
                s_tooltips.append(f"Mode: {item['mode_description']}")
            if item.get("active_connections", 0) > 0:
                s_tooltips.append(f"Active connections: {item['active_connections']}")
            if item.get("error"):
                s_tooltips.append(f"Error: {item['error']}")
            s_item.setToolTip("\n".join(s_tooltips) if s_tooltips else item["status"])
            self.table.setItem(row, 1, s_item)

            # 2: Size
            self.table.setItem(row, 2, QTableWidgetItem(item["size"]))

            # 3: Progress Bar Widget
            pbar = QProgressBar()
            pbar.setFixedHeight(22)
            pval = item.get("progress_value", 0)
            if pval == -1:
                pbar.setRange(0, 0)
                pbar.setFormat(item.get("progress", "Downloading…"))
            else:
                pbar.setRange(0, 100)
                pbar.setValue(pval)
                pbar.setFormat(item.get("progress", f"{pval}%"))
            self.table.setCellWidget(row, 3, pbar)

            # 4: Speed
            self.table.setItem(row, 4, QTableWidgetItem(item.get("speed", "—")))

            # 5: ETA
            self.table.setItem(row, 5, QTableWidgetItem(item.get("eta", "—")))

            # 6: Added
            self.table.setItem(row, 6, QTableWidgetItem(item.get("added", "")))

        self.list_hint.setText(f"{len(rows)} item(s) shown")

    def _on_resume_clicked(self):
        """Resumes selected download item or first paused/interrupted download in queue."""
        selected_rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if selected_rows:
            row = selected_rows[0].row()
            for item_id, r in self._item_row_map.items():
                if r == row:
                    self.queue_mgr.resume_download(item_id)
                    return
        for item in self.queue_mgr.items:
            if item["status"] in ("Paused", "Interrupted", "Failed"):
                self.queue_mgr.resume_download(item["id"])
                return

    def show_context_menu(self, pos):
        item = self.table.itemAt(pos)
        if not item:
            return

        row = item.row()
        item_id = None
        for i_id, r in self._item_row_map.items():
            if r == row:
                item_id = i_id
                break

        if not item_id:
            return

        download_item = self.queue_mgr.get_item(item_id)
        if not download_item:
            return

        menu = QMenu(self)
        status = download_item["status"]

        if status == "Downloading" or status.startswith("Retrying"):
            action_pause = menu.addAction("⏸  Pause Download")
            action_pause.triggered.connect(lambda: self.queue_mgr.pause_download(item_id))
            action_cancel = menu.addAction("⏹  Cancel Download")
            action_cancel.triggered.connect(lambda: self.queue_mgr.cancel_download(item_id))
        elif status in ("Paused", "Interrupted"):
            action_resume = menu.addAction("▶  Resume Download")
            action_resume.triggered.connect(lambda: self.queue_mgr.resume_download(item_id))
            action_cancel = menu.addAction("⏹  Cancel Download")
            action_cancel.triggered.connect(lambda: self.queue_mgr.cancel_download(item_id))
        elif status == "Queued":
            action_start = menu.addAction("▶  Start Download")
            action_start.triggered.connect(lambda: self.queue_mgr.start_item(item_id))
            action_pause = menu.addAction("⏸  Pause")
            action_pause.triggered.connect(lambda: self.queue_mgr.pause_download(item_id))
            action_cancel = menu.addAction("⏹  Cancel Download")
            action_cancel.triggered.connect(lambda: self.queue_mgr.cancel_download(item_id))
        elif status in ("Failed", "Cancelled"):
            action_retry = menu.addAction("🔄  Retry / Resume")
            action_retry.triggered.connect(lambda: self.queue_mgr.resume_download(item_id))

        menu.addSeparator()

        action_segments = menu.addAction("📊  View Segments…")
        action_segments.triggered.connect(lambda: self.open_segment_dialog(item_id))

        action_folder = menu.addAction("📁  Open Containing Folder")
        action_folder.triggered.connect(lambda: self._open_folder(download_item))

        if status == "Completed" and download_item.get("final_path"):
            action_file = menu.addAction("📄  Open File")
            action_file.triggered.connect(lambda: self._open_file(download_item["final_path"]))

        action_copy = menu.addAction("📋  Copy Download URL")
        action_copy.triggered.connect(lambda: QGuiApplication.clipboard().setText(download_item["url"]))

        menu.addSeparator()
        action_remove = menu.addAction("🗑  Remove from History")
        action_remove.triggered.connect(lambda: self._remove_item(item_id, delete_file=False))

        action_delete = menu.addAction("🗑  Delete from Disk & History")
        action_delete.triggered.connect(lambda: self._remove_item(item_id, delete_file=True))

        menu.exec(self.table.viewport().mapToGlobal(pos))

    def open_segment_dialog(self, item_id: str):
        item = self.queue_mgr.get_item(item_id)
        if item:
            dialog = SegmentDetailsDialog(item_id, self.queue_mgr, self)
            dialog.exec()

    def _open_folder(self, download_item: dict):
        folder_path = Path(download_item.get("folder", "")).resolve()
        if folder_path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder_path)))
        else:
            QMessageBox.information(self, "Folder Not Found", f"Folder does not exist:\n{folder_path}")

    def _open_file(self, file_path_str: str):
        path = Path(file_path_str).resolve()
        if path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        else:
            QMessageBox.information(self, "File Not Found", f"File does not exist:\n{path}")

    def _remove_item(self, item_id: str, delete_file: bool = False):
        self.queue_mgr.remove_item(item_id, delete_file=delete_file)
        self.refresh_table()

    def _on_table_double_clicked(self, index):
        row = index.row()
        item_id = None
        for i_id, r in self._item_row_map.items():
            if r == row:
                item_id = i_id
                break
        if item_id:
            item = self.queue_mgr.get_item(item_id)
            if item and item.get("status") == "Completed" and item.get("final_path"):
                self._open_file(item["final_path"])
            elif item:
                self.open_segment_dialog(item_id)

    def closeEvent(self, event):
        """Clean up active threads gracefully on window close."""
        if self.queue_mgr.is_busy():
            self.setEnabled(False)
            self.top_title.setText("Closing… Cancelling active downloads")

            progress = QProgressDialog(
                "Cancelling active download and cleaning up safely…",
                None, 0, 0, self
            )
            progress.setWindowTitle("Shutting Down")
            progress.setWindowModality(Qt.ApplicationModal)
            progress.setCancelButton(None)
            progress.setMinimumDuration(150)
            progress.show()
            QCoreApplication.processEvents()

            shutdown_ok = self.queue_mgr.shutdown(timeout_ms=5000)
            progress.close()

            if not shutdown_ok:
                # Thread is still running — do NOT close the application.
                # Restore the UI so the user can retry later.
                self.setEnabled(True)
                self.top_title.setText("Dashboard")
                QMessageBox.warning(
                    self,
                    "Shutdown Timeout",
                    "The active download worker has not stopped yet.\n\n"
                    "Please wait a moment and try closing the application again.",
                )
                event.ignore()
                return

        event.accept()
