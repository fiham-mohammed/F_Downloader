import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional, Any

from PySide6.QtCore import QObject, QThread, Signal, Slot, QCoreApplication

from mdm.download_worker import DownloadWorker
from mdm.settings import get_default_connections
from mdm.utils import (
    format_bytes,
    format_speed,
    format_eta,
    extract_filename_from_url,
    sanitize_filename,
)


def is_active_status(status: str) -> bool:
    """Returns True if the status represents an actively running download (downloading or retrying)."""
    return status == "Downloading" or status.startswith("Retrying")


class QueueManager(QObject):
    """
    Manages the sequential download queue, worker threads, and download lifecycle.
    Only one download executes at any time (sequential mode for Stage 2).
    """

    # Signals for UI and observers
    item_added = Signal(dict)
    item_updated = Signal(str)                        # item_id
    metrics_changed = Signal(int, int, int, int)       # total, downloading, completed, queued
    download_started = Signal(str)                     # item_id
    download_completed = Signal(str, str, int)         # item_id, path, size
    download_failed = Signal(str, str)                 # item_id, error
    download_cancelled = Signal(str)                   # item_id
    segments_updated = Signal(str, list)               # item_id, list of segment dicts
    connection_status = Signal(str, int, int, str)     # item_id, active_conns, max_conns, mode_desc
    queue_empty = Signal()

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.items: List[Dict[str, Any]] = []
        self.active_item_id: Optional[str] = None
        self.active_thread: Optional[QThread] = None
        self.active_worker: Optional[DownloadWorker] = None
        self.is_auto_queue_running: bool = True

    def add_download(
        self,
        url: str,
        folder: str | Path,
        explicit_filename: Optional[str] = None,
        suggested_filename: Optional[str] = None,
        connections: Optional[int] = None,
        min_segment_size: Optional[int] = None,
        auto_start: bool = True,
        transport: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Adds a new download item to the queue."""
        item_id = uuid.uuid4().hex[:8]

        # Use explicit_filename if user explicitly supplied one; otherwise None
        user_explicit = explicit_filename if explicit_filename is not None else suggested_filename

        # For initial table display preview before downloading begins:
        if user_explicit:
            display_name = sanitize_filename(user_explicit)
        else:
            display_name = extract_filename_from_url(url)

        num_conns = connections if connections is not None else get_default_connections()

        item = {
            "id": item_id,
            "url": url.strip(),
            "folder": str(folder),
            "filename": display_name,
            "explicit_filename": user_explicit,
            "connections": num_conns,
            "min_segment_size": min_segment_size,
            "active_connections": 0,
            "mode_description": f"{num_conns} connections" if num_conns > 1 else "Single connection",
            "segments": [],
            "status": "Queued",
            "size": "Unknown",
            "total_bytes": None,
            "downloaded_bytes": 0,
            "progress": "0%",
            "progress_value": 0,
            "speed": "—",
            "eta": "—",
            "added": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "error": "",
            "final_path": "",
            "transport": transport,
        }

        self.items.append(item)
        self.item_added.emit(item)
        self.emit_metrics()

        if auto_start and not self.active_item_id:
            self.start_next()

        return item

    def get_item(self, item_id: str) -> Optional[Dict[str, Any]]:
        for item in self.items:
            if item["id"] == item_id:
                return item
        return None

    def is_busy(self) -> bool:
        """Returns True if there is an active worker or running thread."""
        return bool(self.active_item_id or (self.active_thread and self.active_thread.isRunning()))

    def emit_metrics(self):
        total = len(self.items)
        active = sum(1 for x in self.items if is_active_status(x["status"]))
        completed = sum(1 for x in self.items if x["status"] == "Completed")
        queued = sum(1 for x in self.items if x["status"] == "Queued")
        self.metrics_changed.emit(total, active, completed, queued)

    @Slot()
    def start_all(self):
        """Starts sequential processing of all queued downloads."""
        self.is_auto_queue_running = True
        if not self.active_item_id:
            self.start_next()

    @Slot()
    def start_next(self):
        """Starts the next queued download job if no download is currently active."""
        if self.active_item_id:
            return

        # Find next queued item
        next_item = None
        for item in self.items:
            if item["status"] == "Queued":
                next_item = item
                break

        if not next_item:
            self.queue_empty.emit()
            return

        self._start_item(next_item["id"])

    def start_item(self, item_id: str):
        """Starts a specific item if idle, or queues it if active."""
        item = self.get_item(item_id)
        if not item:
            return

        if self.active_item_id:
            if item["status"] not in ("Queued", "Downloading"):
                item["status"] = "Queued"
                item["error"] = ""
                self.item_updated.emit(item_id)
                self.emit_metrics()
            return

        self._start_item(item_id)

    def _start_item(self, item_id: str):
        item = self.get_item(item_id)
        if not item:
            return

        self.active_item_id = item_id
        item["status"] = "Downloading"
        item["error"] = ""
        self.item_updated.emit(item_id)
        self.emit_metrics()

        # Create worker and thread
        thread = QThread()
        worker = DownloadWorker(
            item_id=item_id,
            url=item["url"],
            folder=item["folder"],
            explicit_filename=item.get("explicit_filename"),
            connections=item.get("connections", 8),
            min_segment_size=item.get("min_segment_size"),
            transport=item.get("transport"),
        )
        worker.moveToThread(thread)

        # Connect worker signals to manager slots
        thread.started.connect(worker.run)
        worker.started.connect(self._on_worker_started)
        worker.progress.connect(self._on_worker_progress)
        worker.filename_detected.connect(self._on_worker_filename_detected)
        worker.completed.connect(self._on_worker_completed)
        worker.failed.connect(self._on_worker_failed)
        worker.cancelled.connect(self._on_worker_cancelled)
        worker.retry.connect(self._on_worker_retry)
        worker.segments_updated.connect(self._on_worker_segments_updated)
        worker.connection_status.connect(self._on_worker_connection_status)

        # Thread cleanup
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._on_thread_finished)

        self.active_thread = thread
        self.active_worker = worker

        thread.start()

    @Slot(str)
    def cancel_download(self, item_id: str):
        """Cancels a download if active, or marks it cancelled if queued."""
        item = self.get_item(item_id)
        if not item:
            return

        if self.active_item_id == item_id and self.active_worker:
            self.active_worker.cancel()
        elif item["status"] == "Queued":
            item["status"] = "Cancelled"
            item["speed"] = "—"
            item["eta"] = "—"
            self.item_updated.emit(item_id)
            self.emit_metrics()

    @Slot()
    def cancel_active(self):
        """Cancels the currently active download."""
        if self.active_item_id and self.active_worker:
            self.active_worker.cancel()

    def remove_item(self, item_id: str):
        """Cancels if active, and removes from the list."""
        if self.active_item_id == item_id:
            self.cancel_download(item_id)

        self.items = [x for x in self.items if x["id"] != item_id]
        self.emit_metrics()

    def shutdown(self, timeout_ms: int = 5000) -> bool:
        """
        Stops queue processing, cancels active download worker, and waits for worker thread
        to terminate cleanly before application destruction.

        Returns True if the thread has stopped (or was not running).
        Returns False if the thread is still running after the timeout — in that case
        all references to the active thread and worker are PRESERVED so the caller
        can retry later.
        """
        self.is_auto_queue_running = False

        if self.active_worker:
            self.active_worker.cancel()

        if not self.active_thread or not self.active_thread.isRunning():
            # Thread already stopped (or never existed).  Safe to clear everything.
            self.active_item_id = None
            self.active_thread = None
            self.active_worker = None
            return True

        start = time.time()
        timeout_sec = timeout_ms / 1000.0

        while self.active_thread and self.active_thread.isRunning():
            QCoreApplication.processEvents()
            if time.time() - start >= timeout_sec:
                break
            time.sleep(0.02)

        if self.active_thread and self.active_thread.isRunning():
            # Thread is STILL running after the timeout elapsed.
            # Preserve all references so the caller can retry later.
            # Do NOT destroy or abandon a running QThread.
            return False

        # Thread has stopped — safe to clear state
        QCoreApplication.processEvents()
        self.active_item_id = None
        self.active_thread = None
        self.active_worker = None
        self.emit_metrics()

        return True

    # Worker signal handlers
    def _on_worker_started(self, item_id: str):
        item = self.get_item(item_id)
        if item:
            item["status"] = "Downloading"
            self.item_updated.emit(item_id)
            self.emit_metrics()
            self.download_started.emit(item_id)

    def _on_worker_progress(self, item_id: str, downloaded: int, total: Optional[int], speed: float, eta: Optional[float]):
        item = self.get_item(item_id)
        if not item:
            return

        item["downloaded_bytes"] = downloaded
        item["total_bytes"] = total
        item["speed"] = format_speed(speed)
        item["eta"] = format_eta(eta)

        if total and total > 0:
            pct = min(100.0, (downloaded / total) * 100.0)
            item["progress"] = f"{pct:.1f}%"
            item["progress_value"] = int(round(pct))
            item["size"] = f"{format_bytes(downloaded)} / {format_bytes(total)}"
        else:
            item["progress"] = "Downloading…"
            item["progress_value"] = -1
            item["size"] = format_bytes(downloaded)

        self.item_updated.emit(item_id)

    def _on_worker_filename_detected(self, item_id: str, filename: str):
        item = self.get_item(item_id)
        if item:
            item["filename"] = filename
            self.item_updated.emit(item_id)

    def _on_worker_retry(self, item_id: str, attempt: int, max_attempts: int, message: str):
        item = self.get_item(item_id)
        if item:
            item["status"] = f"Retrying ({attempt}/{max_attempts})"
            item["speed"] = "—"
            item["error"] = message
            self.item_updated.emit(item_id)
            self.emit_metrics()

    def _on_worker_segments_updated(self, item_id: str, segments_data: list):
        item = self.get_item(item_id)
        if item:
            item["segments"] = segments_data
            self.segments_updated.emit(item_id, segments_data)

    def _on_worker_connection_status(self, item_id: str, active: int, total: int, mode_desc: str):
        item = self.get_item(item_id)
        if item:
            item["active_connections"] = active
            item["mode_description"] = mode_desc
            self.connection_status.emit(item_id, active, total, mode_desc)
            self.item_updated.emit(item_id)

    def _on_worker_completed(self, item_id: str, final_path: str, total_downloaded: int):
        item = self.get_item(item_id)
        if item:
            item["status"] = "Completed"
            item["active_connections"] = 0
            item["progress"] = "100%"
            item["progress_value"] = 100
            item["speed"] = "—"
            item["eta"] = "—"
            item["size"] = format_bytes(total_downloaded)
            item["final_path"] = final_path
            self.item_updated.emit(item_id)
            self.emit_metrics()
            self.download_completed.emit(item_id, final_path, total_downloaded)

    def _on_worker_failed(self, item_id: str, error_message: str):
        item = self.get_item(item_id)
        if item:
            item["status"] = "Failed"
            item["active_connections"] = 0
            item["speed"] = "—"
            item["eta"] = "—"
            item["error"] = error_message
            self.item_updated.emit(item_id)
            self.emit_metrics()
            self.download_failed.emit(item_id, error_message)

    def _on_worker_cancelled(self, item_id: str):
        item = self.get_item(item_id)
        if item:
            item["status"] = "Cancelled"
            item["active_connections"] = 0
            item["speed"] = "—"
            item["eta"] = "—"
            self.item_updated.emit(item_id)
            self.emit_metrics()
            self.download_cancelled.emit(item_id)

    def _on_thread_finished(self):
        """Called when active worker QThread exits."""
        self.active_item_id = None
        self.active_thread = None
        self.active_worker = None
        self.emit_metrics()

        # Proceed to next queued download only if queue processing is still active
        # (not during shutdown or after shutdown was requested)
        if self.is_auto_queue_running:
            self.start_next()
