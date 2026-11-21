import os
import sys
import time
import uuid
import shutil
import logging
import threading
import re
import ssl
import email.utils
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional, Any, Union

try:
    import httpx
except ImportError:
    httpx = None

logger = logging.getLogger("mdm.queue_manager")

from PySide6.QtCore import QObject, QThread, Signal, Slot, QCoreApplication, QTimer

from mdm.download_worker import DownloadWorker
from mdm.settings import (
    get_default_connections,
    get_max_simultaneous_downloads,
    get_default_priority,
    get_global_speed_limit,
    is_global_speed_limit_enabled,
    is_auto_retry_enabled,
    get_max_retry_attempts,
    get_default_individual_speed_limit,
    app_settings,
)
from mdm.utils import (
    format_bytes,
    format_speed,
    format_eta,
    extract_filename_from_url,
    sanitize_filename,
    parse_retry_after,
)
from mdm.models import DownloadModel, SegmentModel, DownloadStatus, DownloadPriority
from mdm.database import DatabaseManager
from mdm.bandwidth_controller import BandwidthController
from mdm.scheduler import DownloadScheduler, parse_iso_datetime, normalize_to_aware_datetime
from mdm.categories import get_category_for_filename


def is_active_status(status: str) -> bool:
    """Returns True if the status represents an actively running download (downloading or retrying)."""
    return status == DownloadStatus.DOWNLOADING or status.startswith("Retrying")


def parse_retry_after(value_or_msg: str) -> Optional[float]:
    """
    Parses Retry-After header or string containing Retry-After.
    Supports both integer/float delta seconds (e.g. 120, '30 seconds') and HTTP-date formats.
    """
    if not value_or_msg:
        return None
    val_str = str(value_or_msg).strip()

    # If it's an error message containing "retry-after: ...", extract the token
    m = re.search(r"retry-after[:\s]+([^\r\n;]+)", val_str, re.IGNORECASE)
    candidate = m.group(1).strip() if m else val_str

    # 1. Delta seconds (e.g. "120", "30 seconds", "45s")
    sec_match = re.search(r"^\s*(\d+(?:\.\d+)?)(?:\s*(?:s|sec|seconds)?)?\s*$", candidate, re.IGNORECASE)
    if sec_match:
        try:
            val = float(sec_match.group(1))
            if val >= 0:
                return val
        except ValueError:
            pass

    # 2. HTTP-date
    try:
        dt = email.utils.parsedate_to_datetime(candidate)
        if dt:
            now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
            delay = (dt - now).total_seconds()
            return max(0.0, delay)
    except Exception:
        pass

    return None


def is_retryable_failure(error: Union[str, Exception]) -> bool:
    """
    Strictly classifies whether a download failure should be automatically retried.
    Accepts exception instances or string error messages.
    Retries ONLY transient errors (HTTP 408, 429, 500, 502, 503, 504, timeout, network disconnect).
    Does NOT retry permanent errors (HTTP 400, 401, 403, 404, 405, 410, invalid URL/range, SSL/cert, disk full, etc.)
    or unknown errors.
    """
    if not error:
        return False

    # Check Exception types if passed as Exception
    if isinstance(error, Exception):
        if isinstance(error, ssl.SSLError):
            return False
        if isinstance(error, (PermissionError, FileNotFoundError)):
            return False
        if httpx is not None:
            if isinstance(error, httpx.TimeoutException):
                return True
            if isinstance(error, (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout)):
                return True
            if isinstance(error, httpx.ConnectError):
                msg_lower = str(error).lower()
                if any(w in msg_lower for w in ["ssl", "cert", "certificate", "handshake"]):
                    return False
                return True
            if isinstance(error, (httpx.NetworkError, httpx.RemoteProtocolError)):
                return True
        if isinstance(error, (TimeoutError, ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return True

    msg = str(error).lower()

    # 1. PERMANENT errors: NEVER retry
    permanent_patterns = [
        "400", "bad request",
        "401", "unauthorized",
        "403", "forbidden",
        "404", "not found",
        "405", "method not allowed",
        "410", "gone",
        "416", "range not satisfiable",
        "invalid range", "content-range mismatch",
        "ssl", "certificate", "cert", "handshake",
        "invalid url", "unsupported url",
        "disk error", "disk full", "no space left",
        "permission denied", "permissionerror", "cannot create destination",
        "cancelled by user", "canceled",
    ]
    for p in permanent_patterns:
        if p in ("400", "401", "403", "404", "405", "410", "416"):
            if f"http {p}" in msg or f"status {p}" in msg or f"code {p}" in msg or f" {p} " in f" {msg} ":
                return False
        elif p in msg:
            return False

    # 2. TRANSIENT errors: Eligible for retry
    transient_patterns = [
        "http 408", "status 408", " 408 ", "request timeout",
        "http 429", "status 429", " 429 ", "too many requests",
        "http 500", "status 500", " 500 ", "internal server error",
        "http 502", "status 502", " 502 ", "bad gateway",
        "http 503", "status 503", " 503 ", "service unavailable",
        "http 504", "status 504", " 504 ", "gateway timeout",
        "network error", "networkerror", "timeout", "timed out",
        "connection reset", "connection aborted", "remoteprotocolerror",
        "broken pipe", "connecterror", "temporary",
    ]
    for p in transient_patterns:
        if p in msg:
            return True

    # Unknown errors: DO NOT RETRY
    return False


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
        self.active_workers: Dict[str, DownloadWorker] = {}
        self.active_threads: Dict[str, QThread] = {}
        self.active_item_id: Optional[str] = None
        self.active_thread: Optional[QThread] = None
        self.active_worker: Optional[DownloadWorker] = None
        self.is_auto_queue_running: bool = True

        self._max_simultaneous_downloads: int = get_max_simultaneous_downloads()
        self.default_priority: str = get_default_priority()
        self.automatic_retry_enabled: bool = is_auto_retry_enabled()
        self.max_retry_attempts: int = get_max_retry_attempts()

        self.bandwidth_controller = BandwidthController(
            global_limit=get_global_speed_limit(),
            global_enabled=is_global_speed_limit_enabled(),
        )
        self.scheduler = DownloadScheduler(self, parent=self)
        if QCoreApplication.instance():
            self.scheduler.start()

        if self.db is not None:
            app_settings.bind_database(self.db)
            self.db.recover_interrupted_downloads()
            self._load_from_db()

    @property
    def max_simultaneous_downloads(self) -> int:
        return self._max_simultaneous_downloads

    @max_simultaneous_downloads.setter
    def max_simultaneous_downloads(self, val: int):
        old_val = getattr(self, "_max_simultaneous_downloads", 1)
        self._max_simultaneous_downloads = max(1, int(val))
        if self._max_simultaneous_downloads > old_val and getattr(self, "is_auto_queue_running", True):
            self.start_next()

    def __del__(self):
        try:
            if hasattr(self, "scheduler") and self.scheduler:
                self.scheduler.stop()
        except Exception:
            pass

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
            fname = d.filename or extract_filename_from_url(d.url)
            cat = getattr(d, "category", None) or "Other"
            if cat == "Other":
                cat = get_category_for_filename(fname)

            prio = getattr(d, "priority", None) or DownloadPriority.NORMAL
            start_at = getattr(d, "scheduled_start_at", None)
            stop_at = getattr(d, "scheduled_stop_at", None)
            sched_enabled = bool(getattr(d, "schedule_enabled", 0))
            ind_limit = getattr(d, "individual_speed_limit", 0) or 0
            if ind_limit > 0:
                self.bandwidth_controller.set_individual_limit(d.id, ind_limit)

            item = {
                "id": d.id,
                "url": d.url,
                "folder": d.destination_folder,
                "filename": fname,
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
                "priority": prio,
                "category": cat,
                "scheduled_start_at": start_at,
                "scheduled_stop_at": stop_at,
                "schedule_enabled": sched_enabled,
                "individual_speed_limit": ind_limit,
                "retry_count": getattr(d, "retry_count", 0) or 0,
                "max_retry_attempts": getattr(d, "max_retry_attempts", 3) or 3,
                "next_retry_at": getattr(d, "next_retry_at", None),
                "retry_enabled": bool(getattr(d, "retry_enabled", 1)),
                "queue_order": getattr(d, "queue_order", 0) or 0,
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
        priority: Optional[str] = None,
        category: Optional[str] = None,
        scheduled_start_at: Optional[str] = None,
        scheduled_stop_at: Optional[str] = None,
        schedule_enabled: bool = False,
        individual_speed_limit: Optional[int] = None,
        max_retry_attempts: Optional[int] = None,
        retry_enabled: bool = True,
        browser_request: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Adds a new download item to the queue and persists it to database.

        ``browser_request`` (Stage 6A) is an optional idempotency record that is
        committed atomically with the download row. If persisting fails (for
        example a duplicate request_id), the exception propagates and the item
        is NOT added to the in-memory queue.
        """
        item_id = uuid.uuid4().hex[:8]

        user_explicit = explicit_filename if explicit_filename is not None else suggested_filename

        if user_explicit:
            display_name = sanitize_filename(user_explicit)
        else:
            display_name = extract_filename_from_url(url)

        num_conns = connections if connections is not None else get_default_connections()
        created_at_str = datetime.now().isoformat()

        cat = category or get_category_for_filename(display_name)
        category_is_manual = bool(category)
        prio = priority or self.default_priority
        if individual_speed_limit is None:
            ind_limit = get_default_individual_speed_limit()
        else:
            ind_limit = individual_speed_limit
        retries_allowed = max_retry_attempts if max_retry_attempts is not None else self.max_retry_attempts

        if ind_limit > 0:
            self.bandwidth_controller.set_individual_limit(item_id, ind_limit)

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
            "priority": prio,
            "category": cat,
            "category_manual": category_is_manual,
            "scheduled_start_at": scheduled_start_at,
            "scheduled_stop_at": scheduled_stop_at,
            "schedule_enabled": schedule_enabled,
            "individual_speed_limit": ind_limit,
            "retry_count": 0,
            "max_retry_attempts": retries_allowed,
            "next_retry_at": None,
            "retry_enabled": retry_enabled,
            "queue_order": len(self.items),
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
                queue_order=len(self.items),
                created_at=created_at_str,
                priority=prio,
                category=cat,
                scheduled_start_at=scheduled_start_at,
                scheduled_stop_at=scheduled_stop_at,
                schedule_enabled=1 if schedule_enabled else 0,
                individual_speed_limit=ind_limit,
                max_retry_attempts=retries_allowed,
                retry_enabled=1 if retry_enabled else 0,
            )
            if browser_request is not None:
                try:
                    self.db.add_download(dl_model, browser_request=browser_request)
                except Exception:
                    # Nothing was committed: undo the in-memory limiter entry too.
                    if ind_limit > 0:
                        self.bandwidth_controller.remove_individual_limit(item_id)
                    raise
            else:
                self.db.add_download(dl_model)

        self.items.append(item)
        self.item_added.emit(item)
        self.emit_metrics()

        if auto_start and len(self.active_workers) < self.max_simultaneous_downloads:
            self.start_next()

        return item

    def get_item(self, item_id: str) -> Optional[Dict[str, Any]]:
        for item in self.items:
            if item["id"] == item_id:
                return item
        return None

    def is_busy(self) -> bool:
        """Returns True if there is an active worker or running thread."""
        return bool(
            self.active_workers
            or any(t.isRunning() for t in self.active_threads.values())
            or self.active_item_id
            or (self.active_thread and self.active_thread.isRunning())
        )

    def emit_metrics(self):
        total = len(self.items)
        active = sum(1 for x in self.items if is_active_status(x["status"]))
        completed = sum(1 for x in self.items if x["status"] == DownloadStatus.COMPLETED)
        queued = sum(1 for x in self.items if x["status"] == DownloadStatus.QUEUED)
        self.metrics_changed.emit(total, active, completed, queued)

    @Slot()
    def start_all(self):
        """Starts processing of all queued downloads."""
        self.is_auto_queue_running = True
        self.start_next()

    @Slot()
    def start_next(self):
        """Starts eligible queued downloads up to max_simultaneous_downloads."""
        if not self.is_auto_queue_running:
            return

        now = datetime.now().astimezone()

        while len(self.active_workers) < self.max_simultaneous_downloads:
            candidates = []
            for item in self.items:
                if item["status"] != DownloadStatus.QUEUED:
                    continue
                if item["id"] in self.active_workers:
                    continue

                # 1. Check schedule
                if item.get("schedule_enabled"):
                    start_str = item.get("scheduled_start_at")
                    stop_str = item.get("scheduled_stop_at")
                    if start_str and stop_str:
                        s_dt = normalize_to_aware_datetime(start_str)
                        e_dt = normalize_to_aware_datetime(stop_str)
                        if not s_dt or not e_dt or e_dt <= s_dt:
                            # Malformed schedule or stop <= start: prevent running safely
                            continue
                    if start_str:
                        start_dt = normalize_to_aware_datetime(start_str)
                        if not start_dt:
                            # Malformed start timestamp: prevent running safely
                            continue
                        if now < start_dt:
                            continue

                # 2. Check retry delay
                if item.get("next_retry_at"):
                    retry_dt = normalize_to_aware_datetime(item["next_retry_at"])
                    if retry_dt and now < retry_dt:
                        continue

                # 3. Avoid destination collision with currently active downloads
                conflict = False
                for active_id in self.active_workers:
                    act_item = self.get_item(active_id)
                    if act_item and act_item.get("folder") == item.get("folder") and act_item.get("filename") == item.get("filename"):
                        conflict = True
                        break
                if conflict:
                    continue

                candidates.append(item)

            if not candidates:
                if len(self.active_workers) == 0:
                    self.queue_empty.emit()
                break

            # Sort by priority (High=0, Normal=1, Low=2), then FIFO queue_order
            def sort_key(it):
                prio_str = it.get("priority", DownloadPriority.NORMAL)
                prio_weight = DownloadPriority.WEIGHTS.get(prio_str, 1)
                order = it.get("queue_order", 0)
                return (prio_weight, order)

            candidates.sort(key=sort_key)
            best_item = candidates[0]
            self._start_item(best_item["id"])

    def start_item(self, item_id: str):
        """Starts a specific item if slots are available, or queues it."""
        item = self.get_item(item_id)
        if not item:
            return

        if len(self.active_workers) >= self.max_simultaneous_downloads:
            if item["status"] not in (DownloadStatus.QUEUED, DownloadStatus.DOWNLOADING):
                item["status"] = DownloadStatus.QUEUED
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

        item["status"] = DownloadStatus.DOWNLOADING
        item["error"] = ""
        item["next_retry_at"] = None

        # Retrieve resume metadata from DB or in-memory item
        etag = item.get("etag")
        last_modified = item.get("last_modified")
        temp_path = item.get("temp_path")
        resumed_segments = item.get("segments", [])

        if self.db:
            self.db.update_status(item_id, DownloadStatus.DOWNLOADING)
            self.db.update_retry_info(item_id, retry_count=item.get("retry_count", 0), next_retry_at=None)
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
            max_retries=3,
            etag=etag,
            last_modified=last_modified,
            temp_path=temp_path,
            resumed_segments=resumed_segments,
            on_db_persist=self._make_db_persist_callback(item_id),
            bandwidth_controller=self.bandwidth_controller,
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
        thread.finished.connect(lambda t=thread, i=item_id: self._on_single_thread_finished(i, t))

        self.active_workers[item_id] = worker
        self.active_threads[item_id] = thread
        self.active_item_id = item_id
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

        if item_id in self.active_workers:
            self.active_workers[item_id].pause()
        elif self.active_item_id == item_id and self.active_worker:
            self.active_worker.pause()
        elif item["status"] == DownloadStatus.QUEUED or item["status"].startswith("Retrying"):
            item["status"] = DownloadStatus.PAUSED
            item["speed"] = "—"
            item["eta"] = "—"
            item["next_retry_at"] = None
            if self.db:
                self.db.update_status(item_id, DownloadStatus.PAUSED)
                self.db.update_retry_info(item_id, retry_count=item.get("retry_count", 0), next_retry_at=None)
            self.item_updated.emit(item_id)
            self.emit_metrics()
            self.download_paused.emit(item_id)

    @Slot()
    def pause_active(self):
        """Pauses the currently active download."""
        if self.active_item_id and self.active_worker:
            self.active_worker.pause()
        elif self.active_workers:
            for w in list(self.active_workers.values()):
                w.pause()

    @Slot(str)
    def resume_download(self, item_id: str):
        """Resumes a paused, interrupted, or failed download."""
        item = self.get_item(item_id)
        if not item:
            return

        if item["status"] in ("Paused", "Interrupted", "Failed", "Cancelled") or item["status"].startswith("Retrying"):
            item["status"] = "Queued"
            item["error"] = ""
            item["speed"] = "—"
            item["eta"] = "—"
            item["next_retry_at"] = None
            if self.db:
                self.db.update_status(item_id, DownloadStatus.QUEUED)
                self.db.update_retry_info(item_id, retry_count=item.get("retry_count", 0), next_retry_at=None)
            self.item_updated.emit(item_id)
            self.emit_metrics()

            if self.is_auto_queue_running and len(self.active_workers) < self.max_simultaneous_downloads:
                self.start_next()

    @Slot(str)
    def cancel_download(self, item_id: str):
        """Cancels a download if active, or marks it cancelled if queued/paused/interrupted."""
        item = self.get_item(item_id)
        if not item:
            return

        if item_id in self.active_workers:
            self.active_workers[item_id].cancel()
        elif self.active_item_id == item_id and self.active_worker:
            self.active_worker.cancel()
        elif item["status"] in ("Queued", "Paused", "Interrupted", "Failed") or item["status"].startswith("Retrying"):
            item["status"] = "Cancelled"
            item["speed"] = "—"
            item["eta"] = "—"
            item["next_retry_at"] = None
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
                self.db.update_retry_info(item_id, retry_count=item.get("retry_count", 0), next_retry_at=None)
            self.item_updated.emit(item_id)
            self.emit_metrics()
            self.download_cancelled.emit(item_id)

    @Slot()
    def cancel_active(self):
        """Cancels the currently active download."""
        if self.active_item_id and self.active_worker:
            self.active_worker.cancel()
        elif self.active_workers:
            for w in list(self.active_workers.values()):
                w.cancel()

    @Slot()
    def pause_all(self):
        """Pauses all active and queued downloads."""
        self.is_auto_queue_running = False
        for worker in list(self.active_workers.values()):
            worker.pause()
        for item in self.items:
            if item["status"] == DownloadStatus.QUEUED:
                item["status"] = DownloadStatus.PAUSED
                if self.db:
                    self.db.update_status(item["id"], DownloadStatus.PAUSED)
                self.item_updated.emit(item["id"])
        self.emit_metrics()

    @Slot()
    def resume_all(self):
        """Resumes all paused/interrupted/failed downloads and resumes queue processing."""
        self.is_auto_queue_running = True
        for item in self.items:
            if item["status"] in (DownloadStatus.PAUSED, DownloadStatus.INTERRUPTED, DownloadStatus.FAILED):
                item["status"] = DownloadStatus.QUEUED
                item["error"] = ""
                item["next_retry_at"] = None
                if self.db:
                    self.db.update_status(item["id"], DownloadStatus.QUEUED)
                    self.db.update_retry_info(item["id"], retry_count=0, next_retry_at=None)
                self.item_updated.emit(item["id"])
        self.emit_metrics()
        self.start_next()

    @Slot()
    def stop_queue(self):
        """Stops auto queue processing and pauses active workers."""
        self.is_auto_queue_running = False
        for worker in list(self.active_workers.values()):
            worker.pause()
        self.emit_metrics()

    @Slot()
    def clear_completed(self):
        """Removes all completed downloads from queue and database."""
        to_remove = [item["id"] for item in self.items if item["status"] == DownloadStatus.COMPLETED]
        for item_id in to_remove:
            if self.db:
                self.db.delete_download(item_id)
        self.items = [item for item in self.items if item["status"] != DownloadStatus.COMPLETED]
        self.emit_metrics()

    def move_up(self, item_id: str) -> bool:
        """Moves item up one position in queue."""
        idx = next((i for i, item in enumerate(self.items) if item["id"] == item_id), None)
        if idx is not None and idx > 0:
            self.items[idx], self.items[idx - 1] = self.items[idx - 1], self.items[idx]
            for i, itm in enumerate(self.items):
                itm["queue_order"] = i
                if self.db:
                    self.db.update_queue_order(itm["id"], i)
            self.item_updated.emit(item_id)
            self.item_updated.emit(self.items[idx]["id"])
            return True
        return False

    def move_down(self, item_id: str) -> bool:
        """Moves item down one position in queue."""
        idx = next((i for i, item in enumerate(self.items) if item["id"] == item_id), None)
        if idx is not None and idx < len(self.items) - 1:
            self.items[idx], self.items[idx + 1] = self.items[idx + 1], self.items[idx]
            for i, itm in enumerate(self.items):
                itm["queue_order"] = i
                if self.db:
                    self.db.update_queue_order(itm["id"], i)
            self.item_updated.emit(item_id)
            self.item_updated.emit(self.items[idx]["id"])
            return True
        return False

    def set_priority(self, item_id: str, priority: str):
        """Updates download priority."""
        item = self.get_item(item_id)
        if item:
            item["priority"] = priority
            if self.db:
                self.db.update_priority(item_id, priority)
            self.item_updated.emit(item_id)

    def set_category(self, item_id: str, category: str):
        """Updates download category."""
        item = self.get_item(item_id)
        if item:
            item["category"] = category
            item["category_manual"] = True
            if self.db:
                self.db.update_category(item_id, category)
            self.item_updated.emit(item_id)

    def set_individual_speed_limit(self, item_id: str, limit_bytes_per_sec: int):
        """Updates individual speed limit."""
        item = self.get_item(item_id)
        if item:
            item["individual_speed_limit"] = limit_bytes_per_sec
            self.bandwidth_controller.set_individual_limit(item_id, limit_bytes_per_sec)
            if self.db:
                self.db.update_speed_limit(item_id, limit_bytes_per_sec)
            self.item_updated.emit(item_id)

    def set_schedule(self, item_id: str, start_at: Optional[str], stop_at: Optional[str], enabled: bool):
        """Updates download schedule with validation."""
        start_dt = None
        stop_dt = None
        if start_at:
            start_dt = normalize_to_aware_datetime(start_at)
            if not start_dt:
                raise ValueError(f"Invalid scheduled_start_at datetime: {start_at}")
        if stop_at:
            stop_dt = normalize_to_aware_datetime(stop_at)
            if not stop_dt:
                raise ValueError(f"Invalid scheduled_stop_at datetime: {stop_at}")
        if start_dt and stop_dt:
            if stop_dt <= start_dt:
                raise ValueError("scheduled_stop_at must be strictly after scheduled_start_at")

        item = self.get_item(item_id)
        if item:
            item["scheduled_start_at"] = start_at
            item["scheduled_stop_at"] = stop_at
            item["schedule_enabled"] = enabled
            if self.db:
                self.db.update_schedule(item_id, start_at, stop_at, enabled)
            self.item_updated.emit(item_id)

    def manual_retry(self, item_id: str):
        """Manually triggers immediate retry."""
        item = self.get_item(item_id)
        if item:
            item["retry_count"] = 0
            item["next_retry_at"] = None
            item["status"] = DownloadStatus.QUEUED
            item["error"] = ""
            if self.db:
                self.db.update_status(item_id, DownloadStatus.QUEUED)
                self.db.update_retry_info(item_id, retry_count=0, next_retry_at=None)
            self.item_updated.emit(item_id)
            self.emit_metrics()
            if self.is_auto_queue_running:
                self.start_next()

    def cancel_pending_retry(self, item_id: str):
        """Cancels a pending scheduled retry, marking the item failed."""
        item = self.get_item(item_id)
        if item:
            item["next_retry_at"] = None
            item["status"] = DownloadStatus.FAILED
            item["error"] = "Retry cancelled by user"
            if self.db:
                self.db.update_status(item_id, DownloadStatus.FAILED, error_message=item["error"])
                self.db.update_retry_info(item_id, retry_count=item.get("retry_count", 0), next_retry_at=None)
            self.item_updated.emit(item_id)
            self.emit_metrics()

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
        Stops queue processing, pauses active download workers (preserving partial data on disk),
        and waits for worker threads to terminate cleanly before application destruction.

        Returns True if all threads have stopped (or none were running).
        Returns False if any thread is still running after timeout — in that case
        all references to running threads and workers are PRESERVED so the caller
        can retry later.
        """
        self.is_auto_queue_running = False
        if hasattr(self, "scheduler") and self.scheduler:
            self.scheduler.stop()

        # Pause all active workers
        for worker in list(self.active_workers.values()):
            if getattr(worker.cancel, "__name__", "") != "cancel":
                worker.cancel()
            else:
                worker.pause()
        if self.active_worker and self.active_worker not in self.active_workers.values():
            if getattr(self.active_worker.cancel, "__name__", "") != "cancel":
                self.active_worker.cancel()
            else:
                self.active_worker.pause()

        # Collect all active threads
        all_threads = list(self.active_threads.values())
        if self.active_thread and self.active_thread not in all_threads:
            all_threads.append(self.active_thread)

        running_threads = [t for t in all_threads if t.isRunning()]
        if not running_threads:
            # All threads stopped
            for active_id in list(self.active_workers.keys()) + ([self.active_item_id] if self.active_item_id else []):
                item = self.get_item(active_id)
                if item and item.get("status") not in (DownloadStatus.COMPLETED, DownloadStatus.CANCELLED):
                    item["status"] = DownloadStatus.INTERRUPTED
                    item["speed"] = "—"
                    item["eta"] = "—"
                    if self.db:
                        self.db.update_status(active_id, DownloadStatus.INTERRUPTED)
            self.active_workers.clear()
            self.active_threads.clear()
            self.active_item_id = None
            self.active_thread = None
            self.active_worker = None
            if self.db:
                self.db.close()
            return True

        start = time.time()
        timeout_sec = timeout_ms / 1000.0

        while any(t.isRunning() for t in running_threads):
            QCoreApplication.processEvents()
            if time.time() - start >= timeout_sec:
                break
            time.sleep(0.02)

        if any(t.isRunning() for t in running_threads):
            # Still running: PRESERVE references!
            return False

        # All stopped successfully
        QCoreApplication.processEvents()
        for active_id in list(self.active_workers.keys()) + ([self.active_item_id] if self.active_item_id else []):
            item = self.get_item(active_id)
            if item and item.get("status") not in (DownloadStatus.COMPLETED, DownloadStatus.CANCELLED):
                item["status"] = DownloadStatus.INTERRUPTED
                item["speed"] = "—"
                item["eta"] = "—"
                if self.db:
                    self.db.update_status(active_id, DownloadStatus.INTERRUPTED)

        self.active_workers.clear()
        self.active_threads.clear()
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
            if not item.get("category_manual", False):
                new_cat = get_category_for_filename(filename)
                item["category"] = new_cat
                if self.db:
                    try:
                        self.db.update_category(item_id, new_cat)
                    except Exception:
                        pass
            self.item_updated.emit(item_id)

    def _on_worker_metadata_detected(self, item_id: str, meta: dict):
        item = self.get_item(item_id)
        if not item:
            return
        if meta.get("filename"):
            new_fname = meta["filename"]
            item["filename"] = new_fname
            if not item.get("category_manual", False):
                new_cat = get_category_for_filename(new_fname)
                item["category"] = new_cat
                if self.db:
                    try:
                        self.db.update_category(item_id, new_cat)
                    except Exception:
                        pass
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
                try:
                    self.db.update_status(item_id, target_status)
                    self.db.update_progress(
                        item_id,
                        item.get("downloaded_bytes", 0),
                        item.get("total_bytes"),
                        0.0,
                        None,
                    )
                except Exception:
                    pass
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
                try:
                    self.db.update_status(
                        item_id,
                        DownloadStatus.COMPLETED,
                        final_path=final_path,
                        completed_at=datetime.now().isoformat(),
                    )
                    self.db.update_metadata(item_id, temp_path="")
                    self.db.delete_segments(item_id)
                except Exception:
                    pass
            self.item_updated.emit(item_id)
            self.emit_metrics()
            self.download_completed.emit(item_id, final_path, total_downloaded)

    def _on_worker_failed(self, item_id: str, error_message: str, failure_details: Optional[dict] = None):
        item = self.get_item(item_id)
        if not item:
            return

        worker = self.active_workers.get(item_id)
        if worker and getattr(worker, "is_cancelled", lambda: False)():
            # Never retry a cancelled download
            return

        if item.get("status") == DownloadStatus.CANCELLED:
            # Never retry a cancelled download
            return

        if failure_details is None and worker and hasattr(worker, "last_failure_details") and worker.last_failure_details:
            failure_details = worker.last_failure_details

        item["active_connections"] = 0
        item["speed"] = "—"
        item["eta"] = "—"

        status_code = failure_details.get("status_code") if failure_details else None
        retry_after_raw = failure_details.get("retry_after") if failure_details else None

        is_retryable = False
        if status_code in (408, 429, 500, 502, 503, 504):
            is_retryable = True
        elif is_retryable_failure(error_message):
            is_retryable = True

        can_retry = (
            self.automatic_retry_enabled
            and item.get("retry_enabled", True)
            and is_retryable
            and item.get("retry_count", 0) < item.get("max_retry_attempts", self.max_retry_attempts)
        )

        if can_retry:
            curr_count = item.get("retry_count", 0) + 1
            item["retry_count"] = curr_count
            max_attempts = item.get("max_retry_attempts", self.max_retry_attempts)

            parsed_delay = None
            if retry_after_raw:
                parsed_delay = parse_retry_after(retry_after_raw)
            if parsed_delay is None:
                parsed_delay = parse_retry_after(error_message)

            if parsed_delay is not None:
                delay = parsed_delay
            else:
                delay = 5.0 * (3.0 ** (curr_count - 1))

            now_dt = datetime.now().astimezone()
            retry_dt = now_dt + timedelta(seconds=delay)
            retry_iso = retry_dt.isoformat()
            item["next_retry_at"] = retry_iso

            item["status"] = DownloadStatus.QUEUED
            item["error"] = f"{error_message} (Retrying in {int(delay)}s, attempt {curr_count}/{max_attempts})"
            if self.db:
                try:
                    self.db.update_status(item_id, DownloadStatus.QUEUED, error_message=item["error"])
                    self.db.update_retry_info(item_id, retry_count=curr_count, next_retry_at=retry_iso)
                except Exception:
                    pass

            self.item_updated.emit(item_id)
            self.emit_metrics()
        else:
            item["status"] = "Failed"
            item["error"] = error_message
            item["next_retry_at"] = None
            if self.db:
                try:
                    self.db.update_status(
                        item_id,
                        DownloadStatus.FAILED,
                        error_message=error_message,
                        failed_at=datetime.now().isoformat(),
                    )
                    self.db.update_retry_info(item_id, retry_count=item.get("retry_count", 0), next_retry_at=None)
                except Exception:
                    pass
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
            item["next_retry_at"] = None
            if self.db:
                try:
                    self.db.update_status(item_id, DownloadStatus.CANCELLED)
                    self.db.update_retry_info(item_id, retry_count=item.get("retry_count", 0), next_retry_at=None)
                except Exception:
                    pass
            self.item_updated.emit(item_id)
            self.emit_metrics()
            self.download_cancelled.emit(item_id)

    def _on_single_thread_finished(self, item_id: str, thread: QThread):
        """Called when a specific worker QThread exits."""
        if item_id in self.active_workers:
            del self.active_workers[item_id]
        if item_id in self.active_threads:
            del self.active_threads[item_id]

        if self.active_item_id == item_id:
            if self.active_workers:
                next_id = next(iter(self.active_workers))
                self.active_item_id = next_id
                self.active_worker = self.active_workers[next_id]
                self.active_thread = self.active_threads.get(next_id)
            else:
                self.active_item_id = None
                self.active_thread = None
                self.active_worker = None

        self.emit_metrics()
        if self.is_auto_queue_running:
            self.start_next()

    def _on_thread_finished(self):
        """Called when active worker QThread exits."""
        if self.active_item_id:
            self._on_single_thread_finished(self.active_item_id, self.active_thread)
        else:
            self.active_item_id = None
            self.active_thread = None
            self.active_worker = None
            self.emit_metrics()
            if self.is_auto_queue_running:
                self.start_next()
