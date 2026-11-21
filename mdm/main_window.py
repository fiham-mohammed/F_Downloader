import os
import subprocess
from datetime import datetime
from typing import Optional, Dict, Any, List
from pathlib import Path
from urllib.parse import urlparse

from PySide6.QtCore import Qt, QUrl, Slot, QCoreApplication, QDateTime, QTimer
from PySide6.QtGui import QColor, QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QFrame, QLabel, QPushButton, QLineEdit,
    QVBoxLayout, QHBoxLayout, QGridLayout, QStackedWidget, QTableWidget,
    QTableWidgetItem, QHeaderView, QDialog, QFormLayout, QFileDialog,
    QSpinBox, QMessageBox, QAbstractItemView, QProgressBar, QMenu,
    QProgressDialog, QComboBox, QCheckBox, QDateTimeEdit
)

from mdm.queue_manager import QueueManager
from mdm.database import DatabaseManager
from mdm.models import DownloadStatus, DownloadPriority
from mdm.utils import sanitize_filename, extract_filename_from_url, format_bytes, format_speed
from mdm.categories import ALL_CATEGORIES, get_category_for_filename
from mdm.settings import (
    get_default_connections, set_default_connections, VALID_CONNECTION_COUNTS,
    get_max_simultaneous_downloads, set_max_simultaneous_downloads,
    get_default_priority, set_default_priority,
    get_global_speed_limit, set_global_speed_limit,
    is_global_speed_limit_enabled, set_global_speed_limit_enabled,
    get_default_individual_speed_limit, set_default_individual_speed_limit,
    is_auto_retry_enabled, set_auto_retry_enabled,
    get_max_retry_attempts, set_max_retry_attempts,
    app_settings,
)


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


class SpeedLimitDialog(QDialog):
    """Dialog allowing user to configure per-download or global speed limits."""
    def __init__(self, current_bps: int = 0, title: str = "Set Speed Limit", parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(360, 150)
        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("Download Speed Limit (KB/s):"))
        self.spin = QSpinBox()
        self.spin.setRange(0, 1000000)
        self.spin.setSingleStep(50)
        self.spin.setValue(int(current_bps / 1024) if current_bps else 0)
        self.spin.setSuffix(" KB/s (0 = unlimited)")
        layout.addWidget(self.spin)

        btn_box = QHBoxLayout()
        btn_box.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        save_btn = QPushButton("Save")
        save_btn.setObjectName("PrimaryButton")
        save_btn.clicked.connect(self.accept)
        btn_box.addWidget(cancel_btn)
        btn_box.addWidget(save_btn)
        layout.addLayout(btn_box)

    def get_limit_bps(self) -> int:
        return self.spin.value() * 1024


class ScheduleDownloadDialog(QDialog):
    """Dialog allowing user to configure scheduled start and stop for a download."""
    def __init__(self, start_at: Optional[str] = None, stop_at: Optional[str] = None, enabled: bool = False, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Schedule Download")
        self.resize(440, 260)
        layout = QVBoxLayout(self)

        self.enable_cb = QCheckBox("Enable Schedule")
        self.enable_cb.setChecked(enabled)
        layout.addWidget(self.enable_cb)

        form = QFormLayout()

        self.start_edit = QDateTimeEdit()
        self.start_edit.setCalendarPopup(True)
        self.start_edit.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        if start_at:
            dt = QDateTime.fromString(start_at[:19], "yyyy-MM-ddTHH:mm:ss")
            if not dt.isValid():
                dt = QDateTime.fromString(start_at[:19], "yyyy-MM-dd HH:mm:ss")
            self.start_edit.setDateTime(dt if dt.isValid() else QDateTime.currentDateTime().addSecs(300))
        else:
            self.start_edit.setDateTime(QDateTime.currentDateTime().addSecs(300))

        self.stop_edit = QDateTimeEdit()
        self.stop_edit.setCalendarPopup(True)
        self.stop_edit.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        if stop_at:
            dt = QDateTime.fromString(stop_at[:19], "yyyy-MM-ddTHH:mm:ss")
            if not dt.isValid():
                dt = QDateTime.fromString(stop_at[:19], "yyyy-MM-dd HH:mm:ss")
            self.stop_edit.setDateTime(dt if dt.isValid() else QDateTime.currentDateTime().addSecs(3600))
        else:
            self.stop_edit.setDateTime(QDateTime.currentDateTime().addSecs(3600))

        self.has_stop_cb = QCheckBox("Stop download at:")
        self.has_stop_cb.setChecked(bool(stop_at))

        form.addRow("Start download at:", self.start_edit)
        form.addRow(self.has_stop_cb, self.stop_edit)
        layout.addLayout(form)

        btn_box = QHBoxLayout()
        btn_box.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        save_btn = QPushButton("Save")
        save_btn.setObjectName("PrimaryButton")
        save_btn.clicked.connect(self.validate_and_accept)
        btn_box.addWidget(cancel_btn)
        btn_box.addWidget(save_btn)
        layout.addLayout(btn_box)

    def validate_and_accept(self):
        if self.enable_cb.isChecked() and self.has_stop_cb.isChecked():
            start_dt = self.start_edit.dateTime()
            stop_dt = self.stop_edit.dateTime()
            if stop_dt <= start_dt:
                QMessageBox.warning(
                    self, "Invalid Schedule",
                    "Stop time must be strictly after the start time."
                )
                return
        self.accept()

    def get_schedule(self):
        enabled = self.enable_cb.isChecked()
        start_iso = self.start_edit.dateTime().toString("yyyy-MM-ddTHH:mm:ss")
        stop_iso = self.stop_edit.dateTime().toString("yyyy-MM-ddTHH:mm:ss") if self.has_stop_cb.isChecked() else None
        return start_iso, stop_iso, enabled


class DownloadDetailsDialog(QDialog):
    """Dialog displaying detailed metadata, scheduler, speed limit, and retry status."""
    def __init__(self, item: dict, queue_mgr: QueueManager, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Details — {item.get('filename', 'Download')}")
        self.resize(640, 480)
        layout = QVBoxLayout(self)

        title = QLabel(item.get("filename", ""))
        title.setObjectName("PageTitle")
        layout.addWidget(title)

        form = QFormLayout()
        form.addRow("URL:", QLabel(item.get("url", "")))
        form.addRow("Folder:", QLabel(item.get("folder", "")))
        form.addRow("Status:", QLabel(item.get("status", "")))
        form.addRow("Size:", QLabel(item.get("size", "Unknown")))
        form.addRow("Downloaded:", QLabel(format_bytes(item.get("downloaded_bytes", 0))))
        form.addRow("Category:", QLabel(item.get("category", "Other")))
        form.addRow("Priority:", QLabel(item.get("priority", "Normal")))
        if item.get("source_label"):
            form.addRow("Source:", QLabel(str(item["source_label"])))
        if item.get("page_title"):
            form.addRow("Page Title:", QLabel(str(item["page_title"])))
        if item.get("referrer"):
            form.addRow("Referrer:", QLabel(str(item["referrer"])))

        lim = item.get("individual_speed_limit", 0)
        lim_str = f"{lim // 1024} KB/s" if lim > 0 else "Unlimited"
        form.addRow("Speed Limit:", QLabel(lim_str))

        sched_str = "Disabled"
        if item.get("schedule_enabled"):
            st = item.get("scheduled_start_at") or "None"
            sp = item.get("scheduled_stop_at") or "None"
            sched_str = f"Start: {st} | Stop: {sp}"
        form.addRow("Schedule:", QLabel(sched_str))

        retries_str = f"{item.get('retry_count', 0)} / {item.get('max_retry_attempts', 3)}"
        if item.get("next_retry_at"):
            retries_str += f" (Next: {item['next_retry_at']})"
        form.addRow("Retries:", QLabel(retries_str))

        if item.get("etag"):
            form.addRow("ETag:", QLabel(str(item.get("etag"))))
        if item.get("last_modified"):
            form.addRow("Last-Modified:", QLabel(str(item.get("last_modified"))))
        if item.get("error"):
            err_lbl = QLabel(item.get("error", ""))
            err_lbl.setStyleSheet("color: #f87171;")
            form.addRow("Error Message:", err_lbl)

        layout.addLayout(form)

        btn_box = QHBoxLayout()
        btn_box.addStretch()
        seg_btn = QPushButton("View Segments…")
        seg_btn.clicked.connect(lambda: SegmentDetailsDialog(item["id"], queue_mgr, self).exec())
        btn_box.addWidget(seg_btn)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btn_box.addWidget(close_btn)
        layout.addLayout(btn_box)


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

        self.priority_combo = QComboBox()
        for p in [DownloadPriority.HIGH, DownloadPriority.NORMAL, DownloadPriority.LOW]:
            self.priority_combo.addItem(p, p)
        self.priority_combo.setCurrentText(get_default_priority())

        self.category_combo = QComboBox()
        self.category_combo.addItem("Auto-detect", "")
        for cat in ALL_CATEGORIES:
            self.category_combo.addItem(cat, cat)

        form.addRow("Download URL", self.url_input)
        form.addRow("Save folder", folder_row)
        form.addRow("Connections", self.connections_combo)
        form.addRow("Priority", self.priority_combo)
        form.addRow("Category", self.category_combo)
        layout.addLayout(form)

        note = QLabel(
            "Stage 5 Download Engine: Multi-connection segmented downloading, global/individual speed limiting, "
            "priority ordering, automatic retries, and scheduled downloads."
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
        cat = self.category_combo.currentData() or None
        prio = self.priority_combo.currentData() or DownloadPriority.NORMAL
        return {
            "url": url_text,
            "explicit_filename": None,
            "folder": self.folder_input.text().strip(),
            "connections": self.connections_combo.currentData(),
            "priority": prio,
            "category": cat,
        }


class MainWindow(QMainWindow):
    def __init__(self, db: Optional[DatabaseManager] = None):
        super().__init__()
        self.setWindowTitle("My Download Manager")
        self.resize(1260, 820)
        self.setMinimumSize(1020, 680)

        self.db = db
        self.queue_mgr = QueueManager(self, db=self.db)
        if self.db:
            app_settings.bind_database(self.db)
        elif self.queue_mgr.db:
            app_settings.bind_database(self.queue_mgr.db)
        self.current_filter = "All Downloads"
        self.browser_integration = None  # Stage 6A, attached by app.py
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
            ("LIBRARY", ["Videos", "Audio", "Documents", "Images", "Archives", "Software", "Other"]),
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
        version = QLabel("MY DOWNLOAD MANAGER\nStage 5 • Advanced Management")
        version.setObjectName("Muted")
        version.setStyleSheet("font-size: 8pt;")
        layout.addWidget(version)
        return sidebar

    @staticmethod
    def nav_icon(name):
        icons = {
            "Dashboard": "▦", "All Downloads": "⇩", "Downloading": "↓",
            "Completed": "✓", "Queue": "☷", "Videos": "▶", "Audio": "♫",
            "Documents": "▤", "Images": "🖼", "Archives": "▧", "Software": "⚙",
            "Other": "📁", "Scheduler": "◷", "Settings": "⚙",
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

        # Stage 6A: browser integration indicator (hidden until a service is attached)
        self.browser_status_label = QLabel("")
        self.browser_status_label.setObjectName("Muted")
        self.browser_status_label.setVisible(False)
        layout.addWidget(self.browser_status_label)
        layout.addSpacing(14)

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
        self.btn_start_all = QPushButton("▶  Start All")
        self.btn_start_all.setCursor(Qt.PointingHandCursor)
        self.btn_start_all.setToolTip("Start processing queued downloads")
        self.btn_start_all.clicked.connect(self.queue_mgr.start_all)
        table_header.addWidget(self.btn_start_all)

        self.btn_pause_all = QPushButton("⏸  Pause All")
        self.btn_pause_all.setCursor(Qt.PointingHandCursor)
        self.btn_pause_all.setToolTip("Pause all active and queued downloads")
        self.btn_pause_all.clicked.connect(self.queue_mgr.pause_all)
        table_header.addWidget(self.btn_pause_all)

        self.btn_resume_all = QPushButton("▶  Resume All")
        self.btn_resume_all.setCursor(Qt.PointingHandCursor)
        self.btn_resume_all.setToolTip("Resume all paused/failed downloads")
        self.btn_resume_all.clicked.connect(self.queue_mgr.resume_all)
        table_header.addWidget(self.btn_resume_all)

        self.btn_stop_queue = QPushButton("⏹  Stop Queue")
        self.btn_stop_queue.setCursor(Qt.PointingHandCursor)
        self.btn_stop_queue.setToolTip("Stop queue processing and pause active downloads")
        self.btn_stop_queue.clicked.connect(self.queue_mgr.stop_queue)
        table_header.addWidget(self.btn_stop_queue)

        self.btn_clear_completed = QPushButton("🧹  Clear Completed")
        self.btn_clear_completed.setCursor(Qt.PointingHandCursor)
        self.btn_clear_completed.setToolTip("Remove all completed downloads from queue and history")
        self.btn_clear_completed.clicked.connect(self._on_clear_completed)
        table_header.addWidget(self.btn_clear_completed)

        self.btn_move_up = QPushButton("▲  Up")
        self.btn_move_up.setCursor(Qt.PointingHandCursor)
        self.btn_move_up.setToolTip("Move selected download up in queue")
        self.btn_move_up.clicked.connect(self._on_move_up)
        table_header.addWidget(self.btn_move_up)

        self.btn_move_down = QPushButton("▼  Down")
        self.btn_move_down.setCursor(Qt.PointingHandCursor)
        self.btn_move_down.setToolTip("Move selected download down in queue")
        self.btn_move_down.clicked.connect(self._on_move_down)
        table_header.addWidget(self.btn_move_down)

        table_header.addStretch()
        self.list_hint = QLabel("New links will appear here")
        self.list_hint.setObjectName("Muted")
        table_header.addWidget(self.list_hint)
        panel_layout.addLayout(table_header)

        # 10 Columns: FILE NAME, STATUS, SIZE, PROGRESS, SPEED, ETA, PRIORITY, CATEGORY, SCHEDULE, ADDED
        self.table = QTableWidget(0, 10)
        self.table.setHorizontalHeaderLabels(
            ["FILE NAME", "STATUS", "SIZE", "PROGRESS", "SPEED", "ETA", "PRIORITY", "CATEGORY", "SCHEDULE", "ADDED"]
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
        for col in range(1, 10):
            header.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.Interactive)
        self.table.setColumnWidth(3, 160)

        panel_layout.addWidget(self.table, 1)
        layout.addWidget(panel, 1)

        footer = QLabel(
            "Stage 5 Engine: Advanced Download Management — Multi-connection, Speed Limiter, Scheduler, Priority Queue & Auto-Retry."
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

        # Queue Management Panel
        queue_box = QFrame()
        queue_box.setObjectName("Panel")
        queue_layout = QVBoxLayout(queue_box)
        queue_layout.setContentsMargins(20, 16, 20, 16)
        queue_layout.setSpacing(10)

        queue_title = QLabel("Queue & Priority Management")
        queue_title.setStyleSheet("font-size: 11pt; font-weight: 650;")
        queue_layout.addWidget(queue_title)

        q_form = QFormLayout()

        self.max_simul_spin = QSpinBox()
        self.max_simul_spin.setRange(1, 5)
        self.max_simul_spin.setValue(get_max_simultaneous_downloads())
        self.max_simul_spin.valueChanged.connect(self._on_max_simul_changed)
        q_form.addRow("Max simultaneous downloads (1–5):", self.max_simul_spin)

        self.default_prio_combo = QComboBox()
        for p in [DownloadPriority.HIGH, DownloadPriority.NORMAL, DownloadPriority.LOW]:
            self.default_prio_combo.addItem(p, p)
        self.default_prio_combo.setCurrentText(get_default_priority())
        self.default_prio_combo.currentTextChanged.connect(self._on_default_prio_changed)
        q_form.addRow("Default download priority:", self.default_prio_combo)

        self.default_conns_combo = QComboBox()
        for c in VALID_CONNECTION_COUNTS:
            self.default_conns_combo.addItem(f"{c} connection{'s' if c > 1 else ''}", c)
        idx = self.default_conns_combo.findData(get_default_connections())
        if idx >= 0:
            self.default_conns_combo.setCurrentIndex(idx)
        self.default_conns_combo.currentIndexChanged.connect(self._on_default_conns_changed)
        q_form.addRow("Default connections per download:", self.default_conns_combo)

        queue_layout.addLayout(q_form)
        layout.addWidget(queue_box)

        # Speed Limiter Panel
        speed_box = QFrame()
        speed_box.setObjectName("Panel")
        speed_layout = QVBoxLayout(speed_box)
        speed_layout.setContentsMargins(20, 16, 20, 16)
        speed_layout.setSpacing(10)

        speed_title = QLabel("Bandwidth & Speed Limiter")
        speed_title.setStyleSheet("font-size: 11pt; font-weight: 650;")
        speed_layout.addWidget(speed_title)

        s_form = QFormLayout()
        self.global_limit_cb = QCheckBox("Enable Global Download Speed Limit")
        self.global_limit_cb.setChecked(is_global_speed_limit_enabled())
        self.global_limit_cb.toggled.connect(self._on_global_limit_toggled)

        self.global_limit_spin = QSpinBox()
        self.global_limit_spin.setRange(10, 1000000)
        self.global_limit_spin.setSingleStep(50)
        self.global_limit_spin.setValue(int(get_global_speed_limit() / 1024))
        self.global_limit_spin.setSuffix(" KB/s")
        self.global_limit_spin.valueChanged.connect(self._on_global_limit_val_changed)

        self.default_ind_limit_spin = QSpinBox()
        self.default_ind_limit_spin.setRange(0, 1000000)
        self.default_ind_limit_spin.setSingleStep(50)
        self.default_ind_limit_spin.setValue(int(get_default_individual_speed_limit() / 1024))
        self.default_ind_limit_spin.setSuffix(" KB/s (0 = unlimited)")
        self.default_ind_limit_spin.valueChanged.connect(self._on_default_ind_limit_changed)

        s_form.addRow(self.global_limit_cb)
        s_form.addRow("Global Speed Limit:", self.global_limit_spin)
        s_form.addRow("Default Individual Limit:", self.default_ind_limit_spin)
        speed_layout.addLayout(s_form)
        layout.addWidget(speed_box)

        # Auto-Retry Panel
        retry_box = QFrame()
        retry_box.setObjectName("Panel")
        retry_layout = QVBoxLayout(retry_box)
        retry_layout.setContentsMargins(20, 16, 20, 16)
        retry_layout.setSpacing(10)

        retry_title = QLabel("Automatic Retry Scheduling")
        retry_title.setStyleSheet("font-size: 11pt; font-weight: 650;")
        retry_layout.addWidget(retry_title)

        r_form = QFormLayout()
        self.auto_retry_cb = QCheckBox("Enable Automatic Retries for Transient Errors")
        self.auto_retry_cb.setChecked(is_auto_retry_enabled())
        self.auto_retry_cb.toggled.connect(self._on_auto_retry_toggled)

        self.max_retries_spin = QSpinBox()
        self.max_retries_spin.setRange(1, 10)
        self.max_retries_spin.setValue(get_max_retry_attempts())
        self.max_retries_spin.valueChanged.connect(self._on_max_retries_changed)

        r_form.addRow(self.auto_retry_cb)
        r_form.addRow("Maximum retry attempts (1–10):", self.max_retries_spin)
        retry_layout.addLayout(r_form)
        layout.addWidget(retry_box)

        # Stage 5 Information Panel
        info = QFrame()
        info.setObjectName("Panel")
        info_layout = QVBoxLayout(info)
        info_layout.setContentsMargins(20, 20, 20, 20)
        info_layout.addWidget(QLabel("My Download Manager — Stage 5: Advanced Download Management"))

        description = QLabel(
            "Stage 5 introduces professional enterprise download manager capabilities:\n\n"
            "• Global & Per-Download Bandwidth Limiter using token-bucket rate limiting.\n"
            "• Download Scheduler with timed start/stop and timezone-aware checks.\n"
            "• Advanced Queue Management: 1–5 simultaneous downloads, High/Normal/Low priorities.\n"
            "• Automatic Retries: Exponential backoff (5s, 15s, 45s) and Retry-After header respect.\n"
            "• Category Management: Automatic extension classification and custom filters.\n"
            "• Full persistence across sessions via SQLite version 2 schema."
        )
        description.setObjectName("Muted")
        description.setWordWrap(True)
        info_layout.addWidget(description)
        layout.addWidget(info)
        layout.addStretch()
        return page

    def _on_max_simul_changed(self, val: int):
        set_max_simultaneous_downloads(val)
        self.queue_mgr.max_simultaneous_downloads = val
        self.queue_mgr.start_next()

    def _on_default_prio_changed(self, val: str):
        set_default_priority(val)
        self.queue_mgr.default_priority = val

    def _on_global_limit_toggled(self, checked: bool):
        set_global_speed_limit_enabled(checked)
        self.queue_mgr.bandwidth_controller.set_global_enabled(checked)

    def _on_global_limit_val_changed(self, val_kb: int):
        bps = val_kb * 1024
        set_global_speed_limit(bps)
        self.queue_mgr.bandwidth_controller.set_global_limit(bps)

    def _on_default_ind_limit_changed(self, val_kb: int):
        bps = val_kb * 1024
        set_default_individual_speed_limit(bps)

    def _on_auto_retry_toggled(self, checked: bool):
        set_auto_retry_enabled(checked)
        self.queue_mgr.automatic_retry_enabled = checked

    def _on_max_retries_changed(self, val: int):
        set_max_retry_attempts(val)
        self.queue_mgr.max_retry_attempts = val

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
                priority=data.get("priority"),
                category=data.get("category"),
                auto_start=True,
            )

    def _on_item_added(self, item):
        self.refresh_table()

    # ------------------------------------------------------------------
    # Stage 6A: browser integration
    # ------------------------------------------------------------------
    def attach_browser_integration(self, service) -> None:
        """Attaches a started/startable BrowserIntegrationService to this window.

        The service adds downloads through ``self.queue_mgr`` only; this window
        just reflects status and owns the service's shutdown.
        """
        self.browser_integration = service
        service.download_received.connect(self._on_browser_download_received)
        self.update_browser_status()

    def update_browser_status(self) -> None:
        service = self.browser_integration
        if service is None:
            self.browser_status_label.setVisible(False)
            return
        if service.is_running:
            self.browser_status_label.setText("●  Browser integration ready")
            self.browser_status_label.setStyleSheet("color: #4ade80;")
        else:
            self.browser_status_label.setText("●  Browser integration unavailable")
            self.browser_status_label.setStyleSheet("color: #f59e0b;")
        self.browser_status_label.setVisible(True)

    def _on_browser_download_received(self, item: dict) -> None:
        source = item.get("source_label") or "browser"
        self.browser_status_label.setText(f"⇩  Added from {source}: {item.get('filename', '')}"[:80])
        self.browser_status_label.setStyleSheet("color: #60a5fa;")
        self.browser_status_label.setVisible(True)
        QTimer.singleShot(6000, self, self.update_browser_status)

    def bring_to_front(self) -> None:
        """Shows and focuses the main window (requested from the browser popup)."""
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()

    def _stop_browser_integration(self) -> None:
        if self.browser_integration is not None:
            self.browser_integration.stop()

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

        # 6: Priority
        prio_item = self.table.item(row, 6)
        if prio_item:
            prio = item.get("priority", "Normal")
            prio_item.setText(prio)
            if prio == DownloadPriority.HIGH:
                prio_item.setForeground(QColor("#f87171"))
            elif prio == DownloadPriority.LOW:
                prio_item.setForeground(QColor("#91a0b7"))
            else:
                prio_item.setForeground(QColor("#4c8dff"))

        # 7: Category
        cat_item = self.table.item(row, 7)
        if cat_item:
            cat_item.setText(item.get("category", "Other"))

        # 8: Schedule
        sched_item = self.table.item(row, 8)
        if sched_item:
            if item.get("schedule_enabled") and item.get("scheduled_start_at"):
                sched_item.setText(f"Starts: {item['scheduled_start_at'][:16].replace('T', ' ')}")
            elif item.get("schedule_enabled") and item.get("scheduled_stop_at"):
                sched_item.setText(f"Stops: {item['scheduled_stop_at'][:16].replace('T', ' ')}")
            else:
                sched_item.setText("—")

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
            if selected == "Scheduler" and not (item.get("schedule_enabled") or item.get("scheduled_start_at") or item.get("scheduled_stop_at")):
                continue
            if selected in ALL_CATEGORIES:
                if item.get("category") != selected:
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

            # 6: Priority
            p_item = QTableWidgetItem(item.get("priority", "Normal"))
            prio = item.get("priority", "Normal")
            if prio == DownloadPriority.HIGH:
                p_item.setForeground(QColor("#f87171"))
            elif prio == DownloadPriority.LOW:
                p_item.setForeground(QColor("#91a0b7"))
            else:
                p_item.setForeground(QColor("#4c8dff"))
            self.table.setItem(row, 6, p_item)

            # 7: Category
            self.table.setItem(row, 7, QTableWidgetItem(item.get("category", "Other")))

            # 8: Schedule
            if item.get("schedule_enabled") and item.get("scheduled_start_at"):
                sc_str = f"Starts: {item['scheduled_start_at'][:16].replace('T', ' ')}"
            elif item.get("schedule_enabled") and item.get("scheduled_stop_at"):
                sc_str = f"Stops: {item['scheduled_stop_at'][:16].replace('T', ' ')}"
            else:
                sc_str = "—"
            self.table.setItem(row, 8, QTableWidgetItem(sc_str))

            # 9: Added
            self.table.setItem(row, 9, QTableWidgetItem(item.get("added", "")))

        self.list_hint.setText(f"{len(rows)} item(s) shown")

    def _on_clear_completed(self):
        self.queue_mgr.clear_completed()
        self.refresh_table()

    def _on_move_up(self):
        selected_rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if selected_rows:
            row = selected_rows[0].row()
            for item_id, r in self._item_row_map.items():
                if r == row:
                    self.queue_mgr.move_up(item_id)
                    self.refresh_table()
                    return

    def _on_move_down(self):
        selected_rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if selected_rows:
            row = selected_rows[0].row()
            for item_id, r in self._item_row_map.items():
                if r == row:
                    self.queue_mgr.move_down(item_id)
                    self.refresh_table()
                    return

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
            action_retry = menu.addAction("🔄  Retry Now")
            action_retry.triggered.connect(lambda: self.queue_mgr.manual_retry(item_id))

        if download_item.get("next_retry_at"):
            action_cancel_retry = menu.addAction("❌  Cancel Pending Retry")
            action_cancel_retry.triggered.connect(lambda: self.queue_mgr.cancel_pending_retry(item_id))

        menu.addSeparator()

        # Priority submenu
        prio_menu = menu.addMenu("⚡  Set Priority")
        for prio in [DownloadPriority.HIGH, DownloadPriority.NORMAL, DownloadPriority.LOW]:
            act = prio_menu.addAction(prio)
            act.triggered.connect(lambda checked=False, p=prio: self.queue_mgr.set_priority(item_id, p))

        # Category submenu
        cat_menu = menu.addMenu("🏷  Change Category")
        for cat in ALL_CATEGORIES:
            act = cat_menu.addAction(cat)
            act.triggered.connect(lambda checked=False, c=cat: self.queue_mgr.set_category(item_id, c))

        # Speed limit action
        action_speed = menu.addAction("⏱  Set Speed Limit…")
        action_speed.triggered.connect(lambda: self._open_speed_limit_dialog(item_id))

        # Schedule action
        action_sched = menu.addAction("📅  Schedule Download…")
        action_sched.triggered.connect(lambda: self._open_schedule_dialog(item_id))

        # Details action
        action_details = menu.addAction("ℹ️  View Details…")
        action_details.triggered.connect(lambda: DownloadDetailsDialog(download_item, self.queue_mgr, self).exec())

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

    def _open_speed_limit_dialog(self, item_id: str):
        item = self.queue_mgr.get_item(item_id)
        if item:
            dlg = SpeedLimitDialog(current_bps=item.get("individual_speed_limit", 0), parent=self)
            if dlg.exec() == QDialog.Accepted:
                self.queue_mgr.set_individual_speed_limit(item_id, dlg.get_limit_bps())

    def _open_schedule_dialog(self, item_id: str):
        item = self.queue_mgr.get_item(item_id)
        if item:
            dlg = ScheduleDownloadDialog(
                start_at=item.get("scheduled_start_at"),
                stop_at=item.get("scheduled_stop_at"),
                enabled=bool(item.get("schedule_enabled")),
                parent=self,
            )
            if dlg.exec() == QDialog.Accepted:
                st, sp, en = dlg.get_schedule()
                self.queue_mgr.set_schedule(item_id, st, sp, en)

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
        # Stage 6A: stop accepting browser requests before the queue shuts down.
        browser_was_running = bool(self.browser_integration and self.browser_integration.is_running)
        self._stop_browser_integration()

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
                if browser_was_running:
                    # The application stays open, so browser integration resumes.
                    self.browser_integration.start()
                    self.update_browser_status()
                event.ignore()
                return
        else:
            self.queue_mgr.shutdown()

        event.accept()
