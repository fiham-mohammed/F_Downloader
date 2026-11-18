import os
import sys
import time
import uuid
import shutil
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional, Any

logger = logging.getLogger("mdm.queue_manager")

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
from mdm.models import DownloadModel, SegmentModel, DownloadStatus
from mdm.database import DatabaseManager


def is_active_status(status: str) -> bool:
    """Returns True if the status represents an actively running download (downloading or retrying)."""
    return status == "Downloading" or status.startswith("Retrying")


class QueueManager(QObject):
    """
    Manages the sequential download queue, worker threads, download lifecycle,
    and persistent SQLite database storage (Stage 4).
    Only one download executes at any time (sequential mode).
    """

    # Signals for UI and observers
    item_added = Signal(dict)
    item_updated = Signal(str)                        # item_id
    metrics_changed = Signal(int, int, int, int)       # total, downloading, completed, queued
    download_started = Signal(str)                     # item_id
    download_completed = Signal(str, str, int)         # item_id, path, size
    download_failed = Signal(str, str)                 # item_id, error
    download_cancelled = Signal(str)                   # item_id
    download_paused = Signal(str)                      # item_id (Stage 4)
    segments_updated = Signal(str, list)               # item_id, list of segment dicts
    connection_status = Signal(str, int, int, str)     # item_id, active_conns, max_conns, mode_desc
    queue_empty = Signal()

    def __init__(self, parent_or_db: Optional[Any] = None, db: Optional[DatabaseManager] = None):
        parent = None
        if isinstance(parent_or_db, DatabaseManager):
            self.db: Optional[DatabaseManager] = parent_or_db
        elif isinstance(parent_or_db, QObject):
            parent = parent_or_db
            self.db = db
        else:
            self.db = db

        # In production runtime (not unittests), default to DatabaseManager if none provided
        if self.db is None and "unittest" not in sys.modules:
            try:
                self.db = DatabaseManager()
            except Exception:
                self.db = None

        super().__init__(parent)
        self.items: List[Dict[str, Any]] = []
        self.active_item_id: Optional[str] = None
        self.active_thread: Optional[QThread] = None
        self.active_worker: Optional[DownloadWorker] = None
        self.is_auto_queue_running: bool = True

        if self.db is not None:
            self.db.recover_interrupted_downloads()
            self._load_from_db()

    def _load_from_db(self):
        """Loads persistent downloads and segment state from SQLite database."""
        if not self.db:
            return
        downloads = self.db.get_all_downloads()
        for d in downloads:
            segs = self.db.get_segments(d.id)
            seg_dicts = [s.to_dict() for s in segs]

            # Filesystem verification vs database state
            actual_downloaded = d.downloaded_size
            temp_p = d.temp_path
            if temp_p and Path(temp_p).exists():
                p = Path(temp_p)
                if p.is_dir():
                    parts = list(p.glob("*.part"))
                    if parts:
                        actual_downloaded = sum(f.stat().st_size for f in parts)
                elif p.is_file():
                    actual_downloaded = p.stat().st_size
            elif not temp_p and d.destination_folder and d.filename:
                candidate_p = Path(d.destination_folder) / f"{sanitize_filename(d.filename)}.part"
                if candidate_p.exists():
                    temp_p = str(candidate_p)
                    actual_downloaded = candidate_p.stat().st_size

            # If paused or interrupted but temp files are missing, reset to 0
            if d.status in (DownloadStatus.PAUSED, DownloadStatus.INTERRUPTED) and (not temp_p or not Path(temp_p).exists()):
                actual_downloaded = 0

            # Sync segment downloaded counts with filesystem
            for s_dict in seg_dicts:
                s_part = s_dict.get("temp_path")
                if s_part and Path(s_part).exists():
                    actual_s_sz = Path(s_part).stat().st_size
                    s_dict["downloaded"] = actual_s_sz
                    s_exp = s_dict.get("expected_size", 0)
                    if s_exp > 0 and actual_s_sz >= s_exp:
                        s_dict["status"] = "Completed"
                        pct = 100.0
                    else:
                        pct = (actual_s_sz / s_exp * 100.0) if s_exp > 0 else 0.0
                    s_dict["progress"] = f"{min(100.0, pct):.1f}%"
                    s_dict["progress_value"] = int(round(min(100.0, pct)))

            if d.status == DownloadStatus.COMPLETED:
                sz_str = format_bytes(d.total_size or actual_downloaded)
                prog_str = "100%"
                prog_val = 100
            elif d.total_size and d.total_size > 0:
                sz_str = f"{format_bytes(actual_downloaded)} / {format_bytes(d.total_size)}"
                pct = min(100.0, (actual_downloaded / d.total_size) * 100.0)
                prog_str = f"{pct:.1f}%"
                prog_val = int(round(pct))
            elif actual_downloaded > 0:
                sz_str = format_bytes(actual_downloaded)
                prog_str = "Paused" if d.status == DownloadStatus.PAUSED else "Interrupted"
                prog_val = -1
            else:
                sz_str = "Unknown"
                prog_str = "0%"
                prog_val = 0

            num_conns = d.connections if d.connections is not None else getattr(d, "connection_count", 1)
            item = {
                "id": d.id,
                "url": d.url,
                "folder": d.destination_folder,
                "filename": d.filename or extract_filename_from_url(d.url),
                "explicit_filename": d.explicit_filename,
                "connections": num_conns,
                "min_segment_size": None,
                "active_connections": 0,
                "mode_description": f"{num_conns} connections" if num_conns > 1 else "Single connection",
                "segments": seg_dicts,
                "status": d.status,
                "size": sz_str,
                "total_bytes": d.total_size,
                "downloaded_bytes": actual_downloaded,
                "progress": prog_str,
                "progress_value": prog_val,
                "speed": "—",
                "eta": "—",
                "added": d.created_at[:16].replace("T", " ") if d.created_at else datetime.now().strftime("%Y-%m-%d %H:%M"),
                "error": d.error_message or "",
                "final_path": d.final_path or "",
                "temp_path": temp_p or "",
                "etag": d.etag,
                "last_modified": d.last_modified,
                "transport": None,
            }
            self.items.append(item)

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
        """Adds a new download item to the queue and persists it to database."""
        item_id = uuid.uuid4().hex[:8]

        user_explicit = explicit_filename if explicit_filename is not None else suggested_filename

        if user_explicit:
            display_name = sanitize_filename(user_explicit)
        else:
            display_name = extract_filename_from_url(url)

        num_conns = connections if connections is not None else get_default_connections()
        created_at_str = datetime.now().isoformat()

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
            "status": DownloadStatus.QUEUED,
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
            "temp_path": "",
            "etag": None,
            "last_modified": None,
            "transport": transport,
        }

        # Persist to database if available
        if self.db:
            dl_model = DownloadModel(
                id=item_id,
                url=url.strip(),
                destination_folder=str(folder),
                filename=display_name,
                explicit_filename=user_explicit,
                total_size=None,
                downloaded_size=0,
                status=DownloadStatus.QUEUED,
                connections=num_conns,
                queue_order=0,
                created_at=created_at_str,
            )
            self.db.add_download(dl_model)

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
                if self.db:
                    self.db.update_status(item_id, DownloadStatus.QUEUED)
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

        # Retrieve resume metadata from DB or in-memory item
        etag = item.get("etag")
        last_modified = item.get("last_modified")
        temp_path = item.get("temp_path")
        resumed_segments = item.get("segments", [])

        if self.db:
            self.db.update_status(item_id, DownloadStatus.DOWNLOADING)
            db_dl = self.db.get_download(item_id)
            if db_dl:
                etag = db_dl.etag or etag
                last_modified = db_dl.last_modified or last_modified
                temp_path = db_dl.temp_path or temp_path
            db_segs = self.db.get_segments(item_id)
            if db_segs:
                resumed_segments = db_segs

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
            etag=etag,
            last_modified=last_modified,
            temp_path=temp_path,
            resumed_segments=resumed_segments,
            on_db_persist=self._make_db_persist_callback(item_id),
            transport=item.get("transport"),
        )
        worker.moveToThread(thread)

        # Connect worker signals to manager slots
        thread.started.connect(worker.run)
        worker.started.connect(self._on_worker_started)
        worker.progress.connect(self._on_worker_progress)
        worker.filename_detected.connect(self._on_worker_filename_detected)
        worker.metadata_detected.connect(self._on_worker_metadata_detected)
        worker.completed.connect(self._on_worker_completed)
        worker.failed.connect(self._on_worker_failed)
        worker.cancelled.connect(self._on_worker_cancelled)
        worker.paused.connect(self._on_worker_paused)
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

    def _make_db_persist_callback(self, item_id: str):
        """Creates a callback invoked by download engines to persist resume metadata into DB."""
        last_persist_time = 0.0
        lock = threading.Lock()

        def callback(
            downloaded_bytes: Optional[int] = None,
            speed: Optional[float] = None,
            eta: Optional[float] = None,
            force: bool = False,
            *args,
            **kwargs,
        ):
            nonlocal last_persist_time
            if not self.db:
                return

            now = time.time()
            with lock:
                # Throttle database progress updates to at most once per 1.0 second unless forced
                if not force and (now - last_persist_time < 1.0):
                    return
                last_persist_time = now

            item = self.get_item(item_id)
            if not item:
                return

            try:
                dl_bytes = downloaded_bytes if downloaded_bytes is not None else item.get("downloaded_bytes", 0)
                spd = speed if speed is not None else item.get("speed_bps", 0.0)
                e = eta if eta is not None else item.get("eta_sec")
                tot = item.get("total_bytes")

                if downloaded_bytes is not None:
                    item["downloaded_bytes"] = downloaded_bytes
                if speed is not None:
                    item["speed_bps"] = spd
                if eta is not None:
                    item["eta_sec"] = e

                self.db.update_progress(
                    download_id=item_id,
                    downloaded_size=dl_bytes,
                    total_size=tot,
                    speed=spd,
                    eta=e,
                )
                self.db.update_validators(
                    download_id=item_id,
                    etag=item.get("etag"),
                    last_modified=item.get("last_modified"),
                    temp_path=item.get("temp_path"),
                )
            except Exception as exc:
                logger.warning("Failed to persist download progress for item %s: %s", item_id, exc)

        return callback

    @Slot(str)
    def pause_download(self, item_id: str):
        """Pauses a download: requests pause on active worker, or pauses a queued download."""
        item = self.get_item(item_id)
        if not item:
            return

        if self.active_item_id == item_id and self.active_worker:
            self.active_worker.pause()
        elif item["status"] == "Queued":
            item["status"] = "Paused"
            item["speed"] = "—"
            item["eta"] = "—"
            if self.db:
                self.db.update_status(item_id, DownloadStatus.PAUSED)
            self.item_updated.emit(item_id)
            self.emit_metrics()
            self.download_paused.emit(item_id)

    @Slot()
    def pause_active(self):
        """Pauses the currently active download."""
        if self.active_item_id and self.active_worker:
            self.active_worker.pause()

    @Slot(str)
    def resume_download(self, item_id: str):
        """Resumes a paused, interrupted, or failed download."""
        item = self.get_item(item_id)
        if not item:
            return

        if item["status"] in ("Paused", "Interrupted", "Failed", "Cancelled"):
            item["status"] = "Queued"
            item["error"] = ""
            item["speed"] = "—"
            item["eta"] = "—"
            if self.db:
                self.db.update_status(item_id, DownloadStatus.QUEUED)
            self.item_updated.emit(item_id)
            self.emit_metrics()

            if not self.active_item_id and self.is_auto_queue_running:
                self.start_next()

    @Slot(str)
    def cancel_download(self, item_id: str):
        """Cancels a download if active, or marks it cancelled if queued/paused/interrupted."""
        item = self.get_item(item_id)
        if not item:
            return

        if self.active_item_id == item_id and self.active_worker:
            self.active_worker.cancel()
        elif item["status"] in ("Queued", "Paused", "Interrupted"):
            item["status"] = "Cancelled"
            item["speed"] = "—"
            item["eta"] = "—"
            # Clean temporary files (.part or .parts directory)
            temp_p = item.get("temp_path")
            if temp_p and Path(temp_p).exists():
                try:
                    if Path(temp_p).is_dir():
                        shutil.rmtree(temp_p, ignore_errors=True)
                    else:
                        Path(temp_p).unlink(missing_ok=True)
                except Exception:
                    pass
            folder = item.get("folder")
            fn = item.get("filename")
            if folder and fn:
                candidate = Path(folder) / f"{sanitize_filename(fn)}.part"
                if candidate.exists():
                    try:
                        candidate.unlink(missing_ok=True)
                    except Exception:
                        pass
            if self.db:
                self.db.update_status(item_id, DownloadStatus.CANCELLED)
                self.db.delete_segments(item_id)
            self.item_updated.emit(item_id)
            self.emit_metrics()
            self.download_cancelled.emit(item_id)

    @Slot()
    def cancel_active(self):
        """Cancels the currently active download."""
        if self.active_item_id and self.active_worker:
            self.active_worker.cancel()

    def remove_item(self, item_id: str, delete_file: bool = False):
        """Cancels if active, removes from database and list, and optionally deletes file on disk."""
        item = self.get_item(item_id)
        if not item:
            return

        if self.active_item_id == item_id:
            self.cancel_download(item_id)

        if self.db:
            self.db.delete_download(item_id)

        if delete_file:
            final_p = item.get("final_path")
            if final_p and Path(final_p).exists():
                try:
                    Path(final_p).unlink(missing_ok=True)
                except Exception:
                    pass
            temp_p = item.get("temp_path")
            if temp_p and Path(temp_p).exists():
                try:
                    if Path(temp_p).is_dir():
                        shutil.rmtree(temp_p, ignore_errors=True)
                    else:
                        Path(temp_p).unlink(missing_ok=True)
                except Exception:
                    pass

        self.items = [x for x in self.items if x["id"] != item_id]
        self.emit_metrics()

    def shutdown(self, timeout_ms: int = 5000) -> bool:
        """
        Stops queue processing, pauses active download worker (preserving partial data on disk),
        and waits for worker thread to terminate cleanly before application destruction.

        Returns True if the thread has stopped (or was not running).
        Returns False if the thread is still running after the timeout — in that case
        all references to the active thread and worker are PRESERVED so the caller
        can retry later.
        """
        self.is_auto_queue_running = False
        active_id = self.active_item_id

        if self.active_worker:
            if getattr(self.active_worker.cancel, "__name__", "") != "cancel":
                self.active_worker.cancel()
            else:
                self.active_worker.pause()

        if not self.active_thread or not self.active_thread.isRunning():
            if active_id:
                item = self.get_item(active_id)
                if item and item.get("status") not in (DownloadStatus.COMPLETED, DownloadStatus.CANCELLED):
                    item["status"] = DownloadStatus.INTERRUPTED
                    item["speed"] = "—"
                    item["eta"] = "—"
                    if self.db:
                        self.db.update_status(active_id, DownloadStatus.INTERRUPTED)
            self.active_item_id = None
            self.active_thread = None
            self.active_worker = None
            if self.db:
                self.db.close()
            return True

        start = time.time()
        timeout_sec = timeout_ms / 1000.0

        while self.active_thread and self.active_thread.isRunning():
            QCoreApplication.processEvents()
            if time.time() - start >= timeout_sec:
                break
            time.sleep(0.02)

        if self.active_thread and self.active_thread.isRunning():
            return False

        # Thread has stopped — safe to clear state
        QCoreApplication.processEvents()
        if active_id:
            item = self.get_item(active_id)
            if item and item.get("status") not in (DownloadStatus.COMPLETED, DownloadStatus.CANCELLED):
                item["status"] = DownloadStatus.INTERRUPTED
                item["speed"] = "—"
                item["eta"] = "—"
                if self.db:
                    self.db.update_status(active_id, DownloadStatus.INTERRUPTED)
        self.active_item_id = None
        self.active_thread = None
        self.active_worker = None
        self.emit_metrics()

        if self.db:
            self.db.close()

        return True

    # Worker signal handlers
    def _on_worker_started(self, item_id: str):
        item = self.get_item(item_id)
        if item:
            item["status"] = "Downloading"
            if self.db:
                self.db.update_status(item_id, DownloadStatus.DOWNLOADING)
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
        item["speed_bps"] = speed
        item["eta_sec"] = eta

        if total and total > 0:
            pct = min(100.0, (downloaded / total) * 100.0)
            item["progress"] = f"{pct:.1f}%"
            item["progress_value"] = int(round(pct))
            item["size"] = f"{format_bytes(downloaded)} / {format_bytes(total)}"
        else:
            item["progress"] = "Downloading…"
            item["progress_value"] = -1
            item["size"] = format_bytes(downloaded)

        now = time.time()
        if self.db and (now - item.get("_last_db_save", 0.0) >= 1.0):
            item["_last_db_save"] = now
            self.db.update_progress(item_id, downloaded, total, speed, eta)

        self.item_updated.emit(item_id)

    def _on_worker_filename_detected(self, item_id: str, filename: str):
        item = self.get_item(item_id)
        if item:
            item["filename"] = filename
            self.item_updated.emit(item_id)

    def _on_worker_metadata_detected(self, item_id: str, meta: dict):
        item = self.get_item(item_id)
        if not item:
            return
        if meta.get("filename"):
            item["filename"] = meta["filename"]
        if meta.get("temp_path"):
            item["temp_path"] = meta["temp_path"]
        if meta.get("etag"):
            item["etag"] = meta["etag"]
        if meta.get("last_modified"):
            item["last_modified"] = meta["last_modified"]
        if meta.get("total_size") is not None:
            item["total_bytes"] = meta["total_size"]

        if self.db:
            self.db.update_metadata(
                download_id=item_id,
                filename=meta.get("filename"),
                temp_path=meta.get("temp_path"),
                etag=meta.get("etag"),
                last_modified=meta.get("last_modified"),
                supports_range=1 if meta.get("supports_range") else 0,
                total_size=meta.get("total_size"),
            )
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
            if self.db and segments_data:
                seg_models = [
                    SegmentModel(
                        download_id=item_id,
                        segment_index=s.get("index", s.get("segment_index", 0)),
                        start_byte=s.get("start", s.get("start_byte", 0)),
                        end_byte=s.get("end", s.get("end_byte", 0)),
                        downloaded_bytes=s.get("downloaded", s.get("downloaded_bytes", 0)),
                        status=s.get("status", "Queued"),
                        temp_path=str(s.get("temp_path", "")),
                    )
                    for s in segments_data
                ]
                self.db.save_segments(item_id, seg_models)
            self.segments_updated.emit(item_id, segments_data)

    def _on_worker_connection_status(self, item_id: str, active: int, total: int, mode_desc: str):
        item = self.get_item(item_id)
        if item:
            item["active_connections"] = active
            item["mode_description"] = mode_desc
            self.connection_status.emit(item_id, active, total, mode_desc)
            self.item_updated.emit(item_id)

    def _on_worker_paused(self, item_id: str):
        item = self.get_item(item_id)
        if item:
            target_status = DownloadStatus.INTERRUPTED if not self.is_auto_queue_running else DownloadStatus.PAUSED
            item["status"] = target_status
            item["active_connections"] = 0
            item["speed"] = "—"
            item["eta"] = "—"
            if self.db:
                self.db.update_status(item_id, target_status)
                self.db.update_progress(
                    item_id,
                    item.get("downloaded_bytes", 0),
                    item.get("total_bytes"),
                    0.0,
                    None,
                )
            self.item_updated.emit(item_id)
            self.emit_metrics()
            if target_status == DownloadStatus.PAUSED:
                self.download_paused.emit(item_id)

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
            item["temp_path"] = ""
            item["segments"] = []
            if self.db:
                self.db.update_status(
                    item_id,
                    DownloadStatus.COMPLETED,
                    final_path=final_path,
                    completed_at=datetime.now().isoformat(),
                )
                self.db.update_metadata(item_id, temp_path="")
                self.db.delete_segments(item_id)
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
            if self.db:
                self.db.update_status(
                    item_id,
                    DownloadStatus.FAILED,
                    error_message=error_message,
                    failed_at=datetime.now().isoformat(),
                )
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
            if self.db:
                self.db.update_status(item_id, DownloadStatus.CANCELLED)
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
        if self.is_auto_queue_running:
            self.start_next()
