"""
Application-side browser integration service (Stage 6A).

Bridges authenticated IPC requests to the *existing* ``QueueManager`` API.
This module never touches SQLite directly and never creates its own queue:
every browser download is added through ``QueueManager.add_download()``,
exactly as the "Add Download" dialog does, so filename detection,
sanitisation, categories, priority, connection count, bandwidth limits,
retry policy and persistence all behave identically.
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from PySide6.QtCore import QObject, QThread, QCoreApplication, Signal

from mdm import __version__ as APP_VERSION
from mdm.browser_integration import ipc_protocol as proto
from mdm.browser_integration.ipc_server import BrowserIpcServer
from mdm.browser_integration.request_validator import (
    ValidationError,
    redact_url,
    validate_add_download_request,
)

logger = logging.getLogger("mdm.browser_integration")

APP_NAME = "My Download Manager"
BROWSER_LABELS = {"chrome": "Google Chrome", "edge": "Microsoft Edge"}


def default_download_folder() -> Path:
    """Same default save folder used by the existing Add Download dialog."""
    return Path.home() / "Downloads"


class BrowserIntegrationService(QObject):
    """Owns the IPC server and turns validated browser requests into queue items."""

    download_received = Signal(dict)  # the queue item dict returned by QueueManager.add_download

    def __init__(
        self,
        queue_mgr: Any,
        *,
        folder_provider: Callable[[], Path | str] = default_download_folder,
        show_window: Optional[Callable[[], None]] = None,
        endpoint_path: Optional[Path] = None,
        parent: Optional[QObject] = None,
        server_kwargs: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(parent)
        self.queue_mgr = queue_mgr
        self._folder_provider = folder_provider
        self._show_window = show_window
        # Used only when the QueueManager has no database (e.g. in-memory tests).
        self._memory_requests: Dict[str, Dict[str, Any]] = {}
        self.server = BrowserIpcServer(
            self.handle_request,
            endpoint_path=endpoint_path,
            parent=self,
            **(server_kwargs or {}),
        )

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> bool:
        removed = self.purge_old_requests()
        if removed:
            logger.info("Removed %d expired browser request record(s).", removed)
        ok = self.server.start()
        if not ok:
            logger.warning("Browser integration is unavailable for this application instance.")
        return ok

    def stop(self) -> None:
        self.server.stop()

    @property
    def is_running(self) -> bool:
        return self.server.is_listening

    # ------------------------------------------------------------------ handler
    def handle_request(self, action: str, payload: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        """Executes one authenticated IPC action. Always called on the Qt main thread."""
        app = QCoreApplication.instance()
        if app is not None and QThread.currentThread() is not app.thread():
            raise RuntimeError("Browser integration requests must be handled on the Qt main thread.")

        if action == "ping":
            return "ok", {"app": APP_NAME, "version": APP_VERSION}, "Connected"
        if action == "show_window":
            if self._show_window is None:
                raise ValidationError("unsupported_action", "Showing the window is not supported.")
            self._show_window()
            return "ok", {}, "My Download Manager window shown."
        if action == "add_download":
            return self._add_download(payload)
        raise ValidationError("unsupported_action", "Unsupported action.")

    # ------------------------------------------------------------ idempotency
    @property
    def _db(self):
        return getattr(self.queue_mgr, "db", None)

    def _lookup_request(self, request_id: str) -> Optional[Dict[str, Any]]:
        """Returns the durable idempotency record (or the in-memory one when the
        queue has no database). Raises ValidationError if storage is unreadable:
        without a reliable lookup we cannot guarantee at-most-once, so we refuse."""
        db = self._db
        if db is None:
            return self._memory_requests.get(request_id)
        try:
            return db.get_browser_request(request_id)
        except sqlite3.Error:
            logger.exception("Browser request lookup failed (request=%s).", request_id)
            raise ValidationError("storage_error",
                                  "My Download Manager could not verify the request. Try again.") from None

    def _duplicate_response(self, req, record: Dict[str, Any], url_hash: str) -> Tuple[str, Dict[str, Any], str]:
        if record.get("url_sha256") != url_hash:
            logger.warning("Request id reused with a different URL (request=%s).", req.request_id)
            raise ValidationError("request_id_conflict",
                                  "This request ID was already used for a different link.")
        download_id = record.get("download_id") or ""
        item = self.queue_mgr.get_item(download_id) if download_id else None
        logger.info("Duplicate browser request %s -> download %s", req.request_id, download_id)
        return (
            "duplicate",
            {"download_id": download_id, "filename": (item or {}).get("filename", ""),
             "folder": (item or {}).get("folder", "")},
            "This download was already added to My Download Manager.",
        )

    def purge_old_requests(self) -> int:
        db = self._db
        if db is None or not hasattr(db, "purge_browser_requests"):
            return 0
        try:
            return db.purge_browser_requests()
        except sqlite3.Error:
            logger.warning("Could not apply the browser request retention policy.")
            return 0

    def _add_download(self, payload: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        # Re-validate on the application side: the native host is not trusted either.
        req = validate_add_download_request(dict(payload, action="add_download"))
        url_hash = hashlib.sha256(req.url.encode("utf-8")).hexdigest()

        # 1. Durable idempotency: a request_id is accepted at most once, ever
        #    (within the retention window), including across restarts.
        existing = self._lookup_request(req.request_id)
        if existing is not None:
            return self._duplicate_response(req, existing, url_hash)

        folder = Path(self._folder_provider())
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise ValidationError("queue_rejected", "The default download folder is not available.") from None

        record = {
            "request_id": req.request_id,
            "browser": req.browser,
            "url_sha256": url_hash,  # fingerprint only - signed URLs are never stored here
            "status": "accepted",
            "created_at": datetime.now().isoformat(),
        }

        # 2. The download row and the idempotency record are committed in ONE
        #    SQLite transaction inside QueueManager -> DatabaseManager.add_download.
        try:
            item = self.queue_mgr.add_download(
                url=req.url,
                folder=str(folder),
                suggested_filename=req.filename,
                auto_start=True,
                **({"browser_request": record} if self._db is not None else {}),
            )
        except sqlite3.IntegrityError:
            # Lost a race with an identical request_id: nothing was committed for
            # this attempt; answer with the winner's record.
            winner = self._lookup_request(req.request_id)
            if winner is not None:
                return self._duplicate_response(req, winner, url_hash)
            logger.exception("Integrity error adding browser download %s", redact_url(req.url))
            raise ValidationError("storage_error", "My Download Manager could not store the request.") from None
        except sqlite3.Error:
            logger.exception("Database error adding browser download %s", redact_url(req.url))
            raise ValidationError("storage_error",
                                  "My Download Manager could not store the download. Try again.") from None
        except Exception:
            logger.exception("QueueManager rejected browser download %s", redact_url(req.url))
            raise ValidationError("queue_rejected", "My Download Manager could not add the download.") from None

        if not isinstance(item, dict) or not item.get("id"):
            raise ValidationError("queue_rejected", "My Download Manager could not add the download.")
        if self._db is None:
            self._memory_requests[req.request_id] = dict(record, download_id=item["id"])

        # Browser metadata is informational and kept on the in-memory item only
        # (no database schema change in Stage 6A).
        item["source"] = req.browser
        item["source_label"] = BROWSER_LABELS.get(req.browser, req.browser)
        item["referrer"] = req.referrer
        item["page_title"] = req.page_title
        item["browser_request_id"] = req.request_id

        logger.info(
            "Browser download accepted: request=%s browser=%s url=%s id=%s",
            req.request_id, req.browser, redact_url(req.url), item["id"],
        )
        self.download_received.emit(item)
        return (
            "accepted",
            {"download_id": item["id"], "filename": item.get("filename", ""), "folder": item.get("folder", "")},
            f"Added to My Download Manager: {item.get('filename', '')}",
        )


__all__ = ["BrowserIntegrationService", "default_download_folder", "APP_NAME", "proto"]
