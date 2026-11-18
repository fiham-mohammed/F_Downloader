import time
from typing import Optional, Any, List, Dict, Callable
from pathlib import Path
from PySide6.QtCore import QObject, Signal, Slot

from mdm.downloader import HttpDownloader, DownloadError, DownloadCancelledError, DownloadPausedError
from mdm.segment_manager import SegmentedDownloader


class DownloadWorker(QObject):
    """
    Worker object executed inside a dedicated QThread.
    Performs streaming multi-connection or single-connection HTTP download
    and safely reports status, segments, and progress via Qt signals.
    """

    # Signals
    started = Signal(str)                                # item_id
    progress = Signal(str, int, object, float, object)  # item_id, downloaded, total, speed, eta
    filename_detected = Signal(str, str)                # item_id, filename
    metadata_detected = Signal(str, dict)               # item_id, metadata_dict
    completed = Signal(str, str, int)                   # item_id, final_path, file_size
    failed = Signal(str, str)                           # item_id, error_message
    cancelled = Signal(str)                             # item_id
    paused = Signal(str)                                # item_id (Stage 4)
    retry = Signal(str, int, int, str)                  # item_id, attempt, max_attempts, message
    segments_updated = Signal(str, list)                # item_id, list of segment dicts
    connection_status = Signal(str, int, int, str)      # item_id, active_conns, max_conns, mode_desc
    finished = Signal()                                 # Emitted when run() terminates

    def __init__(
        self,
        item_id: str,
        url: str,
        folder: str | Path,
        explicit_filename: Optional[str] = None,
        suggested_filename: Optional[str] = None,
        connections: int = 8,
        min_segment_size: Optional[int] = None,
        max_retries: int = 3,
        etag: Optional[str] = None,
        last_modified: Optional[str] = None,
        temp_path: Optional[str | Path] = None,
        resumed_segments: Optional[List[Any]] = None,
        on_db_persist: Optional[Callable[..., None]] = None,
        transport: Optional[Any] = None,
        parent: Optional[QObject] = None,
    ):
        super().__init__(parent)
        self.item_id = item_id
        self.url = url
        self.folder = folder
        self.explicit_filename = explicit_filename if explicit_filename is not None else suggested_filename
        self.connections = max(1, min(16, connections))
        self.min_segment_size = min_segment_size
        self.max_retries = max_retries
        self.etag = etag
        self.last_modified = last_modified
        self.temp_path = temp_path
        self.resumed_segments = resumed_segments
        self.on_db_persist_cb = on_db_persist
        self.transport = transport

        self._is_cancelled = False
        self._is_paused = False
        self._last_progress_emit = 0.0
        self._last_segments_emit = 0.0
        self._downloader: Optional[Any] = None

    def is_cancelled(self) -> bool:
        return self._is_cancelled

    def is_paused(self) -> bool:
        return self._is_paused

    @Slot()
    def pause(self):
        """Thread-safe request to pause active download and preserve partial data on disk."""
        self._is_paused = True
        if self._downloader:
            self._downloader.pause()

    @Slot()
    def cancel(self):
        """Thread-safe request to cancel active download and abort active connections."""
        self._is_cancelled = True
        if self._downloader:
            self._downloader.cancel()

    def _on_progress(self, downloaded: int, total: Optional[int], speed: float, eta: Optional[float]):
        """Throttles progress signal to ~10 updates per second to avoid flooding Qt event loop."""
        now = time.time()
        if total is not None and downloaded >= total:
            self.progress.emit(self.item_id, downloaded, total, speed, 0.0)
            self._last_progress_emit = now
        elif now - self._last_progress_emit >= 0.08:
            self.progress.emit(self.item_id, downloaded, total, speed, eta)
            self._last_progress_emit = now

    def _on_filename_detected(self, filename: str):
        self.filename_detected.emit(self.item_id, filename)

    def _on_metadata_detected(
        self,
        etag: Optional[str],
        last_modified: Optional[str],
        filename: str,
        temp_path: str,
        total_size: Optional[int],
        supports_range: bool,
    ):
        self.metadata_detected.emit(
            self.item_id,
            {
                "etag": etag,
                "last_modified": last_modified,
                "filename": filename,
                "temp_path": temp_path,
                "total_size": total_size,
                "supports_range": supports_range,
            },
        )

    def _on_retry(self, attempt: int, max_attempts: int, error_message: str):
        self.retry.emit(self.item_id, attempt, max_attempts, error_message)

    def _on_segments_updated(self, segments_data: List[Dict[str, Any]]):
        now = time.time()
        if now - self._last_segments_emit >= 0.1 or not segments_data:
            self.segments_updated.emit(self.item_id, segments_data)
            self._last_segments_emit = now

    def _on_connection_status(self, active_conns: int, total_conns: int, mode_desc: str):
        self.connection_status.emit(self.item_id, active_conns, total_conns, mode_desc)

    @Slot()
    def run(self):
        """Main execution slot running on worker QThread."""
        try:
            if self._is_cancelled:
                self.cancelled.emit(self.item_id)
                return
            if self._is_paused:
                self.paused.emit(self.item_id)
                return

            self.started.emit(self.item_id)

            if self.connections > 1:
                # Multi-connection download engine with capability probe and automatic fallback
                self._downloader = SegmentedDownloader(
                    url=self.url,
                    destination_folder=self.folder,
                    connections=self.connections,
                    explicit_filename=self.explicit_filename,
                    max_retries=self.max_retries,
                    min_segment_size=self.min_segment_size,
                    etag=self.etag,
                    last_modified=self.last_modified,
                    is_cancelled=self.is_cancelled,
                    is_paused=self.is_paused,
                    resumed_parts_dir=self.temp_path,
                    resumed_segments=self.resumed_segments,
                    on_db_persist=self.on_db_persist_cb,
                    on_progress=self._on_progress,
                    on_filename_detected=self._on_filename_detected,
                    on_retry=self._on_retry,
                    on_segments_updated=self._on_segments_updated,
                    on_connection_status=self._on_connection_status,
                    on_metadata_detected=self._on_metadata_detected,
                    transport=self.transport,
                )
            else:
                # Single-connection download engine
                self._downloader = HttpDownloader(
                    url=self.url,
                    destination_folder=self.folder,
                    explicit_filename=self.explicit_filename,
                    max_retries=self.max_retries,
                    etag=self.etag,
                    last_modified=self.last_modified,
                    is_cancelled=self.is_cancelled,
                    is_paused=self.is_paused,
                    resumed_part_path=self.temp_path,
                    on_db_persist=self.on_db_persist_cb,
                    on_progress=self._on_progress,
                    on_filename_detected=self._on_filename_detected,
                    on_retry=self._on_retry,
                    on_metadata_detected=self._on_metadata_detected,
                    transport=self.transport,
                )
                self.connection_status.emit(self.item_id, 1, 1, "Single connection")

            final_path, total_downloaded = self._downloader.download()

            # Emit final 100% progress
            self.progress.emit(self.item_id, total_downloaded, total_downloaded, 0.0, 0.0)
            self.completed.emit(self.item_id, str(final_path), total_downloaded)

        except DownloadPausedError:
            self.paused.emit(self.item_id)
        except DownloadCancelledError:
            self.cancelled.emit(self.item_id)
        except Exception as exc:
            self.failed.emit(self.item_id, str(exc))
        finally:
            self.finished.emit()
