"""
Ask-before-downloading confirmation dialog (Stage 6B).

Shown non-modally with ``show()`` (never ``exec()``), so the IPC handler that
triggered it returns immediately and the GUI thread is never blocked. The
dialog closes itself after ``timeout_s`` seconds; a timeout or closing the
window counts as "Continue with browser download", which is always safe.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QDialog, QFormLayout, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from mdm.utils import format_bytes


class CaptureApprovalDialog(QDialog):
    decided = Signal(bool)

    def __init__(self, info: Dict[str, Any], parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._done = False
        self._remaining = max(5, int(info.get("timeout_s") or 120))
        self.setWindowTitle("Download with My Download Manager?")
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.setMinimumWidth(460)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.setSpacing(12)

        title = QLabel(f"{info.get('browser') or 'Your browser'} started a download")
        title.setStyleSheet("font-size: 12pt; font-weight: 650;")
        layout.addWidget(title)

        form = QFormLayout()
        self.filename_label = QLabel(str(info.get("filename") or "download"))
        self.filename_label.setWordWrap(True)
        self.filename_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        form.addRow("File:", self.filename_label)
        form.addRow("Website:", QLabel(str(info.get("website") or "Unknown")))
        total = info.get("total_bytes")
        form.addRow("Size:", QLabel(format_bytes(total) if isinstance(total, int) and total >= 0 else "Unknown"))
        layout.addLayout(form)

        note = QLabel("The browser download is paused. My Download Manager downloads without your browser's "
                      "cookies or logins; choose the browser if this site needs you to be signed in.")
        note.setWordWrap(True)
        note.setObjectName("Muted")
        layout.addWidget(note)

        self.countdown_label = QLabel("")
        self.countdown_label.setObjectName("Muted")
        layout.addWidget(self.countdown_label)

        buttons = QHBoxLayout()
        buttons.addStretch()
        self.browser_button = QPushButton("Continue with browser download")
        self.browser_button.clicked.connect(lambda: self._decide(False))
        self.mdm_button = QPushButton("Download with My Download Manager")
        self.mdm_button.setObjectName("PrimaryButton")
        self.mdm_button.setDefault(True)
        self.mdm_button.clicked.connect(lambda: self._decide(True))
        buttons.addWidget(self.browser_button)
        buttons.addWidget(self.mdm_button)
        layout.addLayout(buttons)

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self._update_countdown()

    def _update_countdown(self) -> None:
        self.countdown_label.setText(f"Continuing in the browser automatically in {self._remaining} s.")

    def _tick(self) -> None:
        self._remaining -= 1
        if self._remaining <= 0:
            self._decide(False)
        else:
            self._update_countdown()

    def _decide(self, approve: bool) -> None:
        if self._done:
            return
        self._done = True
        self._timer.stop()
        self.decided.emit(approve)
        self.close()

    def close_silently(self) -> None:
        """Closes without reporting a decision (the capture was resolved elsewhere)."""
        self._done = True
        self._timer.stop()
        self.close()

    def closeEvent(self, event):  # noqa: N802 - Qt naming
        if not self._done:
            self._done = True
            self._timer.stop()
            self.decided.emit(False)
        super().closeEvent(event)


def make_dialog_approval_handler(parent_provider: Callable[[], Optional[QWidget]] = lambda: None):
    """Returns an approval handler for CaptureManager that shows a dialog."""

    def handler(info: Dict[str, Any], decide: Callable[[bool], None]) -> Callable[[], None]:
        dialog = CaptureApprovalDialog(info, parent_provider())
        dialog.decided.connect(decide)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        return dialog.close_silently

    return handler
