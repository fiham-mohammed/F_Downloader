"""
Data models and state definitions for My Download Manager (Stage 4).
"""
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any


class DownloadStatus:
    QUEUED = "Queued"
    DOWNLOADING = "Downloading"
    PAUSED = "Paused"
    COMPLETED = "Completed"
    FAILED = "Failed"
    CANCELLED = "Cancelled"
    INTERRUPTED = "Interrupted"

    ALL_STATUSES = {
        QUEUED,
        DOWNLOADING,
        PAUSED,
        COMPLETED,
        FAILED,
        CANCELLED,
        INTERRUPTED,
    }

    ACTIVE_STATUSES = {
        DOWNLOADING,
    }

    RESUMABLE_STATUSES = {
        PAUSED,
        INTERRUPTED,
    }


VALID_TRANSITIONS = {
    DownloadStatus.QUEUED: {DownloadStatus.DOWNLOADING, DownloadStatus.CANCELLED},
    DownloadStatus.DOWNLOADING: {
        DownloadStatus.PAUSED,
        DownloadStatus.COMPLETED,
        DownloadStatus.FAILED,
        DownloadStatus.CANCELLED,
        DownloadStatus.INTERRUPTED,
    },
    DownloadStatus.PAUSED: {DownloadStatus.QUEUED, DownloadStatus.DOWNLOADING, DownloadStatus.CANCELLED},
    DownloadStatus.INTERRUPTED: {DownloadStatus.QUEUED, DownloadStatus.DOWNLOADING, DownloadStatus.CANCELLED},
    DownloadStatus.FAILED: {DownloadStatus.QUEUED, DownloadStatus.DOWNLOADING, DownloadStatus.CANCELLED},
    DownloadStatus.CANCELLED: {DownloadStatus.QUEUED, DownloadStatus.DOWNLOADING},
    DownloadStatus.COMPLETED: set(),  # Terminal state: completed downloads never automatically re-queue
}


def can_transition(current_status: str, new_status: str) -> bool:
    """Validates if transitioning from current_status to new_status is allowed."""
    # Allow retrying states (e.g., Retrying (1/3))
    if current_status.startswith("Retrying") or current_status == DownloadStatus.DOWNLOADING:
        if new_status in VALID_TRANSITIONS[DownloadStatus.DOWNLOADING]:
            return True
        if new_status.startswith("Retrying"):
            return True
    allowed = VALID_TRANSITIONS.get(current_status, set())
    return new_status in allowed or current_status == new_status


@dataclass
class SegmentModel:
    download_id: str = ""
    segment_index: int = 0
    start_byte: int = 0
    end_byte: int = 0
    downloaded_bytes: int = 0
    status: str = DownloadStatus.QUEUED
    temp_path: Optional[str] = None
    retry_count: int = 0
    id: Optional[int] = None

    @property
    def expected_size(self) -> int:
        return max(0, self.end_byte - self.start_byte + 1)

    @property
    def is_complete(self) -> bool:
        return self.downloaded_bytes >= self.expected_size

    def to_dict(self) -> Dict[str, Any]:
        pct = (self.downloaded_bytes / self.expected_size * 100.0) if self.expected_size > 0 else 0.0
        return {
            "index": self.segment_index,
            "start": self.start_byte,
            "end": self.end_byte,
            "expected_size": self.expected_size,
            "downloaded": self.downloaded_bytes,
            "progress": f"{min(100.0, pct):.1f}%",
            "progress_value": int(round(min(100.0, pct))),
            "status": self.status,
            "temp_path": self.temp_path,
            "retry_count": self.retry_count,
        }


@dataclass
class DownloadModel:
    id: str
    url: str
    destination_path: str = ""
    filename: str = ""
    destination_folder: Optional[str] = None
    explicit_filename: Optional[str] = None
    connections: Optional[int] = None
    original_url: Optional[str] = None
    temp_path: Optional[str] = None
    status: str = DownloadStatus.QUEUED
    download_mode: str = "single"  # "single" or "multi"
    connection_count: int = 1
    total_size: Optional[int] = None
    downloaded_size: int = 0
    speed: float = 0.0
    eta: Optional[float] = None
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    started_at: Optional[str] = None
    paused_at: Optional[str] = None
    completed_at: Optional[str] = None
    failed_at: Optional[str] = None
    error_message: Optional[str] = None
    final_path: Optional[str] = None
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    supports_range: int = 1
    queue_order: int = 0
    segments: List[SegmentModel] = field(default_factory=list)

    def __post_init__(self):
        if not self.destination_path and self.destination_folder:
            self.destination_path = self.destination_folder
        elif not self.destination_folder and self.destination_path:
            self.destination_folder = self.destination_path
        if self.connections is not None and self.connection_count == 1:
            self.connection_count = self.connections
        elif self.connection_count != 1 and self.connections is None:
            self.connections = self.connection_count
        if self.final_path and not self.destination_path:
            self.destination_path = str(Path(self.final_path).parent)
        if self.final_path and not self.filename:
            self.filename = Path(self.final_path).name
