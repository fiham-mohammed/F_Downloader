"""
Browser Integration settings panel (Stage 6B) for the existing Settings page.

Every change is saved immediately through ``CaptureManager.update_config``
(SQLite ``settings`` table). Nothing here blocks the GUI thread: status is a
cheap registry/file read, and "Test connection" runs the installed native host
asynchronously with ``QProcess``.
"""
from __future__ import annotations

import json
import struct
import time
from typing import Any, Callable, Dict, Optional

from PySide6.QtCore import QProcess, QTimer, Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QPushButton, QVBoxLayout, QWidget,
)

from mdm.browser_integration import capture_rules as rules
from mdm.browser_integration.host_registration import query_registration

BROWSER_NAMES = {"chrome": "Google Chrome", "edge": "Microsoft Edge"}
CONNECTED_WINDOW_S = 120


def _frame(obj: Dict[str, Any]) -> bytes:
    body = json.dumps(obj).encode("utf-8")
    return struct.pack("<I", len(body)) + body


def describe_browser(browser: str, status: Optional[Dict[str, Any]], registration: Dict[str, Any],
                     now: Optional[float] = None) -> str:
    reg = registration.get(browser) or {}
    if not reg.get("registered"):
        return f"Native host not registered ({reg.get('problem') or 'not registered'})"
    if not status or "last_seen" not in status:
        return "Registered · no contact since the app started (open the extension popup to connect)"
    age = (now or time.time()) - status["last_seen"]
    parts = ["Connected" if age <= CONNECTED_WINDOW_S else f"Last contact {int(age // 60)} min ago"]
    if status.get("extension_version"):
        parts.append(f"extension {status['extension_version']}")
    perms = status.get("capture_permissions")
    if perms is True:
        parts.append("capture permissions granted")
    elif perms is False:
        parts.append("capture permissions NOT granted (enable capture from the extension popup)")
    return " · ".join(parts)


DECISION_TEXT = {
    "committed": "Sent to My Download Manager",
    "browser_fallback": "Restarted in the browser",
    "commit_pending": "Waiting to hand over",
}


def describe_decision(d: Dict[str, Any]) -> str:
    """One readable line for a browser capture decision (no URLs exist in it)."""
    outcome = DECISION_TEXT.get(d.get("decision") or "", "Left in the browser")
    reason = d.get("reason")
    if reason and d.get("decision") != "committed":
        outcome += f" ({reason.replace('_', ' ')})"
    facts = [BROWSER_NAMES.get(d.get("browser") or "", d.get("browser") or ""),
             f"download #{d['download_id']}" if d.get("download_id") is not None else "",
             d.get("method") or "method unknown",
             d.get("resource_type") or "",
             d.get("mime") or "",
             f"state {d['state']}" if d.get("state") else "",
             "paused" if d.get("paused") else "",
             f"started by {d['initiator'].replace('_', ' ')}" if d.get("by_extension") and d.get("initiator") else ""]
    return f"{d.get('filename') or 'download'} — {outcome} · " + " · ".join(f for f in facts if f)


class BrowserCaptureSettingsPanel(QFrame):
    def __init__(self, capture: Any, parent: Optional[QWidget] = None,
                 registration_reader: Callable[[], Dict[str, Any]] = query_registration):
        super().__init__(parent)
        self.capture = capture
        self._registration_reader = registration_reader
        self._registration: Dict[str, Any] = {}
        self._loading = False
        self._process: Optional[QProcess] = None
        self._process_timer: Optional[QTimer] = None
        self._process_started = 0.0
        self.setObjectName("Panel")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)

        title = QLabel("Browser Integration — Automatic Download Capture")
        title.setStyleSheet("font-size: 11pt; font-weight: 650;")
        layout.addWidget(title)

        # --- mode ---------------------------------------------------------
        form = QFormLayout()
        self.enable_cb = QCheckBox("Capture downloads started in Chrome and Edge automatically")
        self.enable_cb.toggled.connect(self._on_mode_controls_changed)
        form.addRow(self.enable_cb)
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Ask before downloading", "ask")
        self.mode_combo.addItem("Automatic (no confirmation)", "automatic")
        self.mode_combo.currentIndexChanged.connect(self._on_mode_controls_changed)
        form.addRow("When a download is detected:", self.mode_combo)
        layout.addLayout(form)

        # --- status -------------------------------------------------------
        status_form = QFormLayout()
        self.chrome_status = QLabel()
        self.edge_status = QLabel()
        self.host_status = QLabel()
        self.pending_status = QLabel()
        for lbl in (self.chrome_status, self.edge_status, self.host_status):
            lbl.setWordWrap(True)
        status_form.addRow("Google Chrome:", self.chrome_status)
        status_form.addRow("Microsoft Edge:", self.edge_status)
        status_form.addRow("Native messaging host:", self.host_status)
        status_form.addRow("Pending captures:", self.pending_status)
        self.last_decision = QLabel("No capture decision reported yet.")
        self.last_decision.setWordWrap(True)
        self.last_decision.setTextInteractionFlags(Qt.TextSelectableByMouse)
        status_form.addRow("Last capture decision:", self.last_decision)
        layout.addLayout(status_form)
        self.decision_list = QListWidget()
        self.decision_list.setMaximumHeight(110)
        self.decision_list.setToolTip("Recent browser capture decisions (file names only; links are never recorded).")
        layout.addWidget(self.decision_list)

        actions = QHBoxLayout()
        self.test_button = QPushButton("Test connection")
        self.test_button.clicked.connect(self.test_connection)
        self.reset_button = QPushButton("Reset to default settings")
        self.reset_button.clicked.connect(self.reset_defaults)
        actions.addWidget(self.test_button)
        actions.addWidget(self.reset_button)
        actions.addStretch()
        layout.addLayout(actions)
        self.test_result = QLabel("")
        self.test_result.setWordWrap(True)
        self.test_result.setObjectName("Muted")
        layout.addWidget(self.test_result)

        # --- file types ---------------------------------------------------
        types_title = QLabel("File types to capture")
        types_title.setStyleSheet("font-weight: 650;")
        layout.addWidget(types_title)
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        self.ext_checkboxes: Dict[str, QCheckBox] = {}
        for row, (group, exts) in enumerate(rules.DEFAULT_EXTENSION_GROUPS.items()):
            grid.addWidget(QLabel(group + ":"), row, 0)
            for col, ext in enumerate(exts, start=1):
                cb = QCheckBox(ext)
                cb.toggled.connect(self._on_extension_toggled)
                self.ext_checkboxes[ext] = cb
                grid.addWidget(cb, row, col)
        layout.addLayout(grid)

        custom_row = QHBoxLayout()
        self.custom_list = QListWidget()
        self.custom_list.setMaximumHeight(90)
        custom_controls = QVBoxLayout()
        self.custom_input = QLineEdit()
        self.custom_input.setPlaceholderText("Custom extension, e.g. epub or tar.xz")
        self.custom_input.returnPressed.connect(self.add_custom_extension)
        add_ext = QPushButton("Add extension")
        add_ext.clicked.connect(self.add_custom_extension)
        rm_ext = QPushButton("Remove selected")
        rm_ext.clicked.connect(self.remove_custom_extension)
        custom_controls.addWidget(self.custom_input)
        custom_controls.addWidget(add_ext)
        custom_controls.addWidget(rm_ext)
        custom_row.addWidget(self.custom_list, 1)
        custom_row.addLayout(custom_controls)
        layout.addWidget(QLabel("Custom extensions:"))
        layout.addLayout(custom_row)

        # --- exclusions ---------------------------------------------------
        ex_title = QLabel("Excluded websites (their downloads always stay in the browser)")
        ex_title.setStyleSheet("font-weight: 650;")
        layout.addWidget(ex_title)
        ex_row = QHBoxLayout()
        self.sites_list = QListWidget()
        self.sites_list.setMaximumHeight(110)
        ex_controls = QVBoxLayout()
        self.site_input = QLineEdit()
        self.site_input.setPlaceholderText("example.com (also covers its subdomains)")
        self.site_input.returnPressed.connect(self.add_site)
        add_site = QPushButton("Add website")
        add_site.clicked.connect(self.add_site)
        rm_site = QPushButton("Remove selected")
        rm_site.clicked.connect(self.remove_site)
        ex_controls.addWidget(self.site_input)
        ex_controls.addWidget(add_site)
        ex_controls.addWidget(rm_site)
        ex_row.addWidget(self.sites_list, 1)
        ex_row.addLayout(ex_controls)
        layout.addLayout(ex_row)

        self.error_label = QLabel("")
        self.error_label.setStyleSheet("color: #f87171;")
        self.error_label.setWordWrap(True)
        layout.addWidget(self.error_label)

        note = QLabel(
            "My Download Manager downloads without your browser's cookies or saved logins. Downloads that "
            "need you to be signed in are detected before the browser download is cancelled and stay in the "
            "browser. Private-window downloads, downloads the browser flags as dangerous, and anything other "
            "than a plain GET request are never captured."
        )
        note.setObjectName("Muted")
        note.setWordWrap(True)
        layout.addWidget(note)

        capture.config_changed.connect(lambda _cfg: self.load_config())
        capture.browsers_changed.connect(self.refresh_status)
        capture.capture_changed.connect(lambda *_: self.refresh_status())
        if hasattr(capture, "diagnostics_changed"):
            capture.diagnostics_changed.connect(self.refresh_decisions)
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(10_000)
        self._status_timer.timeout.connect(lambda: self.refresh_status(reread_registry=True))
        self._status_timer.start()

        self.load_config()
        self.refresh_status(reread_registry=True)
        self.refresh_decisions()

    # ------------------------------------------------------------- diagnostics
    def refresh_decisions(self) -> None:
        recent = self.capture.recent_decisions(10) if hasattr(self.capture, "recent_decisions") else []
        self.last_decision.setText(describe_decision(recent[0]) if recent else "No capture decision reported yet.")
        self.decision_list.clear()
        for d in recent:
            when = time.strftime("%H:%M:%S", time.localtime(d.get("received_at") or time.time()))
            self.decision_list.addItem(f"{when}  {describe_decision(d)}")
        self.decision_list.setVisible(bool(recent))

    # ------------------------------------------------------------------ config
    def load_config(self) -> None:
        cfg = self.capture.get_config()
        self._loading = True
        try:
            self.enable_cb.setChecked(cfg["enabled"])
            self.mode_combo.setCurrentIndex(max(0, self.mode_combo.findData(cfg["mode"])))
            self.mode_combo.setEnabled(cfg["enabled"])
            for ext, cb in self.ext_checkboxes.items():
                cb.setChecked(bool(cfg["extensions"].get(ext, True)))
            self.custom_list.clear()
            self.custom_list.addItems(cfg["custom_extensions"])
            self.sites_list.clear()
            self.sites_list.addItems(cfg["excluded_sites"])
        finally:
            self._loading = False

    def _save(self, cfg: Dict[str, Any]) -> None:
        self.error_label.setText("")
        self.capture.update_config(cfg)

    def _on_mode_controls_changed(self, *_args) -> None:
        if self._loading:
            return
        cfg = self.capture.get_config()
        cfg["enabled"] = self.enable_cb.isChecked()
        cfg["mode"] = self.mode_combo.currentData()
        self.mode_combo.setEnabled(cfg["enabled"])
        self._save(cfg)

    def _on_extension_toggled(self, *_args) -> None:
        if self._loading:
            return
        cfg = self.capture.get_config()
        for ext, cb in self.ext_checkboxes.items():
            cfg["extensions"][ext] = cb.isChecked()
        self._save(cfg)

    def add_custom_extension(self) -> None:
        try:
            ext = rules.normalize_extension(self.custom_input.text())
        except rules.RuleError as exc:
            self.error_label.setText(str(exc))
            return
        cfg = self.capture.get_config()
        if ext in rules.DEFAULT_EXTENSIONS:
            cfg["extensions"][ext] = True
        elif ext not in cfg["custom_extensions"]:
            cfg["custom_extensions"].append(ext)
        self.custom_input.clear()
        self._save(cfg)

    def remove_custom_extension(self) -> None:
        item = self.custom_list.currentItem()
        if not item:
            return
        cfg = self.capture.get_config()
        cfg["custom_extensions"] = [e for e in cfg["custom_extensions"] if e != item.text()]
        self._save(cfg)

    def add_site(self) -> None:
        try:
            site = rules.normalize_site(self.site_input.text())
        except rules.RuleError as exc:
            self.error_label.setText(str(exc))
            return
        cfg = self.capture.get_config()
        if site not in cfg["excluded_sites"]:
            cfg["excluded_sites"].append(site)
        self.site_input.clear()
        self._save(cfg)

    def remove_site(self) -> None:
        item = self.sites_list.currentItem()
        if not item:
            return
        cfg = self.capture.get_config()
        cfg["excluded_sites"] = [s for s in cfg["excluded_sites"] if s != item.text()]
        self._save(cfg)

    def reset_defaults(self) -> None:
        self.error_label.setText("")
        self.capture.reset_config()

    # ------------------------------------------------------------------ status
    def refresh_status(self, reread_registry: bool = False) -> None:
        if reread_registry or not self._registration:
            try:
                self._registration = self._registration_reader() or {}
            except Exception:  # noqa: BLE001
                self._registration = {}
        for browser, label in (("chrome", self.chrome_status), ("edge", self.edge_status)):
            label.setText(describe_browser(browser, self.capture.browser_status.get(browser), self._registration))
        registered = [BROWSER_NAMES[b] for b, r in self._registration.items() if r.get("registered")]
        self.host_status.setText(
            ("Registered for " + " and ".join(registered)) if registered
            else "Not registered — run native_host\\install_native_host.ps1"
        )
        self.pending_status.setText(str(self.capture.pending_count()))

    def host_executable(self) -> Optional[str]:
        for browser in ("chrome", "edge"):
            reg = self._registration.get(browser) or {}
            if reg.get("registered") and reg.get("host_path"):
                return reg["host_path"]
        return None

    def test_connection(self) -> None:
        """Runs the registered host like a browser would and pings this app through it."""
        if self._process is not None:
            return
        self.refresh_status(reread_registry=True)
        exe = self.host_executable()
        if not exe:
            self.test_result.setText("The native messaging host is not registered. Run "
                                     "native_host\\install_native_host.ps1 with your extension ID(s).")
            return
        self.test_button.setEnabled(False)
        self.test_result.setText("Testing…")
        proc = QProcess(self)
        proc.setProgram(exe)
        proc.finished.connect(self._on_test_finished)
        proc.errorOccurred.connect(self._on_test_error)
        self._process = proc
        self._process_started = time.monotonic()
        proc.start()
        proc.write(_frame({"action": "ping", "request_id": "settings-test-connection"}))
        proc.closeWriteChannel()
        self._process_timer = QTimer(self)
        self._process_timer.setSingleShot(True)
        self._process_timer.timeout.connect(self._on_test_timeout)
        self._process_timer.start(15_000)

    def _finish_test(self, text: str) -> None:
        if self._process_timer:
            self._process_timer.stop()
        proc, self._process = self._process, None
        if proc is not None:
            proc.deleteLater()
        self.test_button.setEnabled(True)
        self.test_result.setText(text)

    def _on_test_finished(self, *_args) -> None:
        if self._process is None:
            return
        data = bytes(self._process.readAllStandardOutput().data())
        elapsed = int((time.monotonic() - self._process_started) * 1000)
        try:
            (n,) = struct.unpack_from("<I", data, 0)
            resp = json.loads(data[4:4 + n].decode("utf-8"))
        except (struct.error, ValueError):
            self._finish_test("The native host did not return a valid response.")
            return
        if resp.get("ok"):
            self._finish_test(f"✔ Native host and application are communicating "
                              f"(host {resp.get('host_version', '?')}, round trip {elapsed} ms).")
        else:
            self._finish_test(f"✖ {resp.get('message') or resp.get('error_code') or 'Test failed.'}")

    def _on_test_error(self, error) -> None:
        if self._process is not None and error == QProcess.FailedToStart:
            self._finish_test("✖ The native host executable could not be started.")

    def _on_test_timeout(self) -> None:
        if self._process is not None:
            self._process.kill()
            self._finish_test("✖ The native host did not respond within 15 seconds.")
