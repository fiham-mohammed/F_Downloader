"""
Download scheduler module for My Download Manager (Stage 5).
Manages date/time-based scheduled starts and stops with event-driven periodic checks,
as well as autonomous retry dispatch.
"""
import weakref
import logging
from datetime import datetime
from typing import Optional, Any
from PySide6.QtCore import QObject, QTimer, Slot, Signal

logger = logging.getLogger("mdm.scheduler")


def normalize_to_aware_datetime(dt_val: Any) -> Optional[datetime]:
    """
    Normalizes a datetime string or datetime object into a timezone-aware datetime in the local timezone.
    - Naive datetimes (e.g. from ScheduleDownloadDialog 'yyyy-MM-ddTHH:mm:ss') are interpreted as local system time.
    - Aware datetimes (e.g. with 'Z' or offset '+00:00') are converted to the local timezone.
    - Daylight-saving transitions are handled automatically by Python's datetime.astimezone().
    - Returns None if the input is None, empty, or cannot be parsed as a valid datetime.
    """
    if not dt_val:
        return None
    try:
        if isinstance(dt_val, str):
            clean_str = dt_val.strip().replace("Z", "+00:00")
            dt = datetime.fromisoformat(clean_str)
        elif isinstance(dt_val, datetime):
            dt = dt_val
        else:
            return None

        # If naive, astimezone() assumes system local timezone.
        # If aware, astimezone() converts to system local timezone.
        return dt.astimezone()
    except Exception as exc:
        logger.debug("Failed to normalize datetime '%s': %s", dt_val, exc)
        return None


def parse_iso_datetime(dt_str: Optional[str]) -> Optional[datetime]:
    """Parses ISO formatted datetime string into a timezone-aware datetime object."""
    return normalize_to_aware_datetime(dt_str)


class DownloadScheduler(QObject):
    """
    Schedules download starts, stops, and retry dispatch using a periodic QTimer check.
    Thread-safe and avoids one-thread-per-download overhead.
    """

    schedule_triggered = Signal(str, str)  # item_id, event_type ('start', 'stop', or 'retry')

    def __init__(self, queue_manager: Any, check_interval_ms: int = 5000, parent: Optional[QObject] = None):
        actual_parent = parent or (queue_manager if isinstance(queue_manager, QObject) else None)
        super().__init__(actual_parent)
        self._qm_ref = weakref.ref(queue_manager) if queue_manager is not None else None
        self.check_interval_ms = check_interval_ms
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.check_schedules)

    @property
    def queue_manager(self):
        if self._qm_ref is not None:
            return self._qm_ref()
        return None

    def start(self):
        """Starts the periodic scheduler timer."""
        if not self._timer.isActive():
            self._timer.start(self.check_interval_ms)

    def stop(self):
        """Stops the scheduler timer."""
        if self._timer.isActive():
            self._timer.stop()

    def is_running(self) -> bool:
        return self._timer.isActive()

    @Slot()
    def check_schedules(self, current_dt: Optional[datetime] = None):
        """
        Evaluates scheduled starts, stops, and due retries against current or simulated datetime.
        Callable manually for deterministic testing.
        """
        try:
            qm = self.queue_manager
            if not qm or not hasattr(qm, "items"):
                self.stop()
                return
            if not getattr(qm, "is_auto_queue_running", True):
                return

            now = normalize_to_aware_datetime(current_dt) if current_dt is not None else datetime.now().astimezone()
            if now is None:
                now = datetime.now().astimezone()
            retry_due = False
            start_due = False

            for item in list(qm.items):
                item_id = item["id"]
                status = item.get("status")

                # 1. Scheduled stop check for active downloads (Downloading or Retrying)
                is_active = (
                    status == "Downloading"
                    or (isinstance(status, str) and status.startswith("Retrying"))
                    or item_id in getattr(qm, "active_workers", {})
                )
                if is_active and item.get("schedule_enabled") and item.get("scheduled_stop_at"):
                    stop_dt = normalize_to_aware_datetime(item["scheduled_stop_at"])
                    if stop_dt and now >= stop_dt:
                        logger.info("Scheduled stop triggered for item %s (status: %s)", item_id, status)
                        self.schedule_triggered.emit(item_id, "stop")
                        qm.pause_download(item_id)
                        continue

                # 2. Scheduled start check for queued downloads
                if status == "Queued" and item.get("schedule_enabled") and item.get("scheduled_start_at"):
                    if item_id not in getattr(qm, "active_workers", {}):
                        start_dt = normalize_to_aware_datetime(item["scheduled_start_at"])
                        if start_dt and now >= start_dt:
                            logger.info("Scheduled start triggered for item %s", item_id)
                            self.schedule_triggered.emit(item_id, "start")
                            start_due = True

                # 3. Autonomous retry dispatch check for queued items with due next_retry_at
                if status == "Queued" and item.get("next_retry_at"):
                    if item_id not in getattr(qm, "active_workers", {}):
                        retry_dt = normalize_to_aware_datetime(item["next_retry_at"])
                        if retry_dt and now >= retry_dt:
                            logger.info("Autonomous retry triggered for item %s", item_id)
                            self.schedule_triggered.emit(item_id, "retry")
                            retry_due = True

            # If any scheduled starts or retries became eligible, trigger queue processing
            if (start_due or retry_due) and len(getattr(qm, "active_workers", {})) < getattr(qm, "max_simultaneous_downloads", 1):
                qm.start_next()

        except Exception as exc:
            logger.error("Error during scheduler check: %s", exc, exc_info=True)
