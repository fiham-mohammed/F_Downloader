"""
Automatic browser download capture - desktop side (Stage 6B).

Two-phase handshake (all actions are idempotent):

    browser                                   desktop (this module)
    -------                                   ---------------------
    pause browser download
    prepare_capture(request_id, url, ...) --> validate + eligibility
                                              durable record: state=probing
                                              cookie-less preflight probe (worker thread)
                                                ok + automatic -> approved
                                                ok + ask       -> awaiting_user -> dialog
                                                not ok         -> declined (e.g. auth_required)
    capture_status (poll)                 <-- probing | awaiting_user | approved | declined | expired
    approved: cancel browser download,
    confirm it is cancelled
    commit_capture(request_id, url)       --> ONE SQLite transaction:
                                                download row + browser_requests row
                                                + capture approved -> committed
                                              existing QueueManager starts it
    otherwise: abort_capture              --> aborted; browser resumes its own download

The URL is never written to ``browser_captures`` (only a SHA-256
fingerprint); it is kept in memory for the probe and re-sent by the browser
on commit. Everything here runs on the Qt main thread except the probe,
whose result is delivered back through a queued signal.
"""
from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx
from PySide6.QtCore import QObject, QTimer, Qt, Signal, Slot

from mdm import __version__ as APP_VERSION
from mdm.browser_integration import capture_rules as rules
from mdm.browser_integration.request_validator import (
    ValidationError,
    redact_url,
    validate_capture_config_request,
    validate_capture_decision,
    validate_capture_mode,
    validate_capture_reference,
    validate_prepare_capture,
)
from mdm.database import CAPTURE_ACTIVE_STATES, CaptureStateConflict
from mdm.utils import parse_content_disposition

logger = logging.getLogger("mdm.browser_integration.capture")

BROWSER_LABELS = {"chrome": "Google Chrome", "edge": "Microsoft Edge"}
AUTH_FAILURE_RE = re.compile(r"\bHTTP (401|403|407)\b")
_CONTENT_RANGE_RE = re.compile(r"bytes\s+\d+-\d+/(\d+|\*)", re.I)

Result = Tuple[str, Dict[str, Any], str]
ApprovalHandler = Callable[[Dict[str, Any], Callable[[bool], None]], Optional[Callable[[], None]]]

DECLINE_MESSAGES = {
    "capture_disabled": "Automatic capture is turned off.",
    "extension_not_selected": "This file type is not selected for capture.",
    "site_excluded": "This website is excluded from capture.",
    "browser_fallback": "This link was handed back to the browser.",
    "auth_required": "This download needs your browser session (login/cookies); it stays in the browser.",
    "html_response": "The server returned a web page instead of the file without your browser session; "
                     "it stays in the browser.",
    "not_found": "The file is not available to My Download Manager.",
    "http_error": "The server refused the request from My Download Manager.",
    "tls_error": "The secure connection could not be verified; the download stays in the browser.",
    "probe_timeout": "The server did not respond in time.",
    "probe_failed": "My Download Manager could not reach the file.",
    "user_declined": "You chose to continue in the browser.",
}


# ---------------------------------------------------------------------------
# Preflight probe (runs in a worker thread)
# ---------------------------------------------------------------------------

def probe_url(url: str, *, browser_mime: Optional[str] = None, timeout: float = 10.0) -> Dict[str, Any]:
    """Checks, WITHOUT any browser cookies or credentials, that the native
    client can fetch ``url``. Requests a single byte (Range: bytes=0-0), follows
    redirects and always verifies TLS certificates (httpx default).

    Returns {"ok", "reason", "status", "total_bytes", "filename", "content_type"}.
    """
    out: Dict[str, Any] = {"ok": False, "reason": "probe_failed", "status": None,
                           "total_bytes": None, "filename": None, "content_type": None}
    headers = {"Range": "bytes=0-0", "Accept-Encoding": "identity",
               "User-Agent": f"MyDownloadManager/{APP_VERSION}"}
    try:
        with httpx.Client(follow_redirects=True, timeout=timeout) as client:
            with client.stream("GET", url, headers=headers) as resp:
                status = resp.status_code
                out["status"] = status
                ctype = (resp.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
                out["content_type"] = ctype or None
                cd_name = parse_content_disposition(resp.headers.get("content-disposition"))
                out["filename"] = rules.safe_basename(cd_name) or None
                if status == 206:
                    m = _CONTENT_RANGE_RE.search(resp.headers.get("content-range") or "")
                    if m and m.group(1) != "*":
                        out["total_bytes"] = int(m.group(1))
                elif status == 200:
                    cl = resp.headers.get("content-length")
                    if cl and cl.isdigit():
                        out["total_bytes"] = int(cl)
    except httpx.TimeoutException:
        out["reason"] = "probe_timeout"
        return out
    except httpx.ConnectError as exc:
        out["reason"] = "tls_error" if "CERTIFICATE" in str(exc).upper() or "SSL" in str(exc).upper() else "probe_failed"
        return out
    except (httpx.HTTPError, ValueError, OSError):
        out["reason"] = "probe_failed"
        return out

    status = out["status"]
    if status in (401, 403, 407):
        out["reason"] = "auth_required"
    elif status in (404, 410):
        out["reason"] = "not_found"
    elif status is None or status >= 400 or status not in (200, 206):
        out["reason"] = "http_error"
    elif (out["content_type"] in ("text/html", "application/xhtml+xml")
          and not (browser_mime or "").startswith(("text/html", "application/xhtml"))):
        # The browser received a file, we received a web page: almost always a
        # login/consent page served because our request has no cookies.
        out["reason"] = "html_response"
    else:
        out["ok"] = True
        out["reason"] = None
    return out


# ---------------------------------------------------------------------------
# Capture manager
# ---------------------------------------------------------------------------

class CaptureManager(QObject):
    MAX_DECISIONS = 50

    capture_changed = Signal(str, str)     # request_id, new state
    download_captured = Signal(dict)       # queue item created by a committed capture
    config_changed = Signal(dict)          # full (internal) config
    browsers_changed = Signal()
    diagnostics_changed = Signal()         # a browser reported a capture decision
    _probe_finished = Signal(str, dict)    # worker thread -> main thread (queued)

    def __init__(
        self,
        queue_mgr: Any,
        *,
        folder_provider: Callable[[], Any],
        approval_handler: Optional[ApprovalHandler] = None,
        prober: Callable[..., Dict[str, Any]] = probe_url,
        parent: Optional[QObject] = None,
        ask_timeout_s: int = 120,
        pending_ttl_s: int = 10 * 60,
        approved_ttl_s: int = 24 * 3600,
        fallback_ttl_s: int = 15 * 60,
        maintenance_interval_ms: int = 30_000,
        probe_workers: int = 2,
    ):
        super().__init__(parent)
        self.queue_mgr = queue_mgr
        self.db = getattr(queue_mgr, "db", None)
        self.settings = rules.CaptureSettingsStore(self.db)
        self._folder_provider = folder_provider
        self._approval_handler = approval_handler
        self._prober = prober
        self.ask_timeout_s = ask_timeout_s
        self.pending_ttl_s = pending_ttl_s
        self.approved_ttl_s = approved_ttl_s
        self.fallback_ttl_s = fallback_ttl_s
        self._probe_workers = probe_workers
        self._executor = ThreadPoolExecutor(max_workers=probe_workers, thread_name_prefix="mdm-capture-probe")
        self._urls: Dict[str, str] = {}                 # request_id -> URL (memory only, for the probe)
        self._prepare_info: Dict[str, Dict[str, Any]] = {}
        self._fallback_hashes: Dict[str, float] = {}    # url_sha256 -> monotonic expiry
        self._approval_close: Dict[str, Callable[[], None]] = {}
        self.browser_status: Dict[str, Dict[str, Any]] = {}
        # Bounded, memory-only history of browser-side capture decisions (no URLs).
        self.decisions: "deque[Dict[str, Any]]" = deque(maxlen=self.MAX_DECISIONS)
        self._stopped = False
        self._probe_finished.connect(self._on_probe_finished, Qt.QueuedConnection)
        if hasattr(queue_mgr, "download_failed"):
            queue_mgr.download_failed.connect(self._on_download_failed)
        self._timer = QTimer(self)
        self._timer.setInterval(maintenance_interval_ms)
        self._timer.timeout.connect(self.maintenance)

    # ------------------------------------------------------------- lifecycle
    @property
    def available(self) -> bool:
        return self.db is not None

    def start(self) -> None:
        if self._stopped:
            # Restart after stop() (e.g. a vetoed application close): fresh worker pool.
            self._executor = ThreadPoolExecutor(max_workers=self._probe_workers,
                                                thread_name_prefix="mdm-capture-probe")
        self._stopped = False
        if not self.available:
            return
        try:
            # Probes and dialogs do not survive a restart: those captures expire and
            # the browser resumes its own download. Approved captures stay valid.
            for rec in self.db.list_captures(("probing", "awaiting_user"), limit=1000):
                self.db.transition_capture(rec["request_id"], ("probing", "awaiting_user"), "expired",
                                           reason="app_restarted")
        except sqlite3.Error:
            logger.exception("Capture recovery failed.")
        self.maintenance()
        self._timer.start()

    def stop(self) -> None:
        self._stopped = True
        self._timer.stop()
        for close in list(self._approval_close.values()):
            try:
                close()
            except Exception:  # noqa: BLE001
                pass
        self._approval_close.clear()
        self._executor.shutdown(wait=False, cancel_futures=True)

    @Slot()
    def maintenance(self) -> None:
        if not self.available:
            return
        try:
            self.db.expire_captures()
            self.db.purge_captures()
        except sqlite3.Error:
            logger.warning("Capture maintenance failed.")
        now = time.monotonic()
        for h, exp in list(self._fallback_hashes.items()):
            if exp < now:
                del self._fallback_hashes[h]
        for rid in list(self._approval_close):
            rec = self._get(rid)
            if not rec or rec["state"] != "awaiting_user":
                self._close_approval(rid)
        for rid in list(self._urls):
            rec = self._get(rid)
            if not rec or rec["state"] not in CAPTURE_ACTIVE_STATES:
                self._forget(rid)

    # ---------------------------------------------------------------- config
    def get_config(self) -> Dict[str, Any]:
        return self.settings.get()

    def update_config(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
        saved = self.settings.save(cfg)
        self.config_changed.emit(saved)
        return saved

    def reset_config(self) -> Dict[str, Any]:
        saved = self.settings.reset()
        self.config_changed.emit(saved)
        return saved

    def pending_count(self) -> int:
        if not self.available:
            return 0
        try:
            return len(self.db.list_captures(CAPTURE_ACTIVE_STATES, limit=1000))
        except sqlite3.Error:
            return 0

    # --------------------------------------------------------------- helpers
    def _require_db(self) -> None:
        if not self.available:
            raise ValidationError("capture_unavailable", "Automatic capture needs the download database.")

    def _get(self, request_id: str) -> Optional[Dict[str, Any]]:
        try:
            return self.db.get_capture(request_id)
        except sqlite3.Error:
            logger.exception("Capture lookup failed (request=%s).", request_id)
            raise ValidationError("storage_error", "My Download Manager could not read the capture state.") from None

    @staticmethod
    def _hash(url: str) -> str:
        return hashlib.sha256(url.encode("utf-8")).hexdigest()

    @staticmethod
    def view(rec: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "request_id": rec["request_id"],
            "state": rec["state"],
            "reason": rec.get("reason"),
            "download_id": rec.get("download_id"),
            "filename": rec.get("filename") or "",
            "total_bytes": rec.get("total_bytes"),
            "mode": rec.get("mode"),
        }

    def _seen(self, browser: Optional[str], **extra: Any) -> None:
        if not browser:
            return
        entry = self.browser_status.setdefault(browser, {})
        entry["last_seen"] = time.time()
        for k, v in extra.items():
            if v is not None:
                entry[k] = v
        self.browsers_changed.emit()

    def _forget(self, rid: str) -> None:
        self._urls.pop(rid, None)
        self._prepare_info.pop(rid, None)

    def _close_approval(self, rid: str) -> None:
        close = self._approval_close.pop(rid, None)
        if close:
            try:
                close()
            except Exception:  # noqa: BLE001
                pass

    def _transition(self, rid: str, from_states: Tuple[str, ...], to_state: str, **fields: Any) -> bool:
        try:
            changed = self.db.transition_capture(rid, from_states, to_state, **fields)
        except sqlite3.Error:
            logger.exception("Capture transition failed (request=%s).", rid)
            raise ValidationError("storage_error", "My Download Manager could not store the capture state.") from None
        if changed:
            logger.info("Capture %s -> %s", rid, to_state)
            self.capture_changed.emit(rid, to_state)
        return changed

    def _check_expiry(self, rec: Dict[str, Any]) -> Dict[str, Any]:
        if rec["state"] in CAPTURE_ACTIVE_STATES and rec["expires_at"] < datetime.now().isoformat():
            self._transition(rec["request_id"], CAPTURE_ACTIVE_STATES, "expired", reason=rec.get("reason") or "timeout")
            self._close_approval(rec["request_id"])
            self._forget(rec["request_id"])
            rec = self._get(rec["request_id"]) or rec
        return rec

    def _eligibility(self, req, cfg: Dict[str, Any], url_hash: str) -> Optional[str]:
        if rules.effective_mode(cfg) == "disabled":
            return "capture_disabled"
        if rules.host_is_excluded(rules.host_of(req.url), cfg["excluded_sites"]) or \
                rules.host_is_excluded(rules.host_of(req.referrer), cfg["excluded_sites"]):
            return "site_excluded"
        name = req.filename or rules.filename_from_url(req.url)
        if rules.extension_matches(name, rules.enabled_extensions(cfg)) is None and req.filename:
            # A filename the browser resolved (incl. Content-Disposition) is authoritative.
            return "extension_not_selected"
        # Without a browser filename the check is repeated after the probe, using
        # the server's Content-Disposition filename (see _on_probe_finished).
        expiry = self._fallback_hashes.get(url_hash)
        if expiry and expiry > time.monotonic():
            return "browser_fallback"
        return None

    # ------------------------------------------------------------ handshake
    def prepare(self, payload: Dict[str, Any]) -> Result:
        req = validate_prepare_capture(dict(payload, action="prepare_capture"))
        self._seen(req.browser)
        self._require_db()
        url_hash = self._hash(req.url)

        existing = self._get(req.request_id)
        if existing is not None:
            if existing["url_sha256"] != url_hash:
                raise ValidationError("request_id_conflict", "This request ID was already used for a different link.")
            existing = self._check_expiry(existing)
            return "duplicate", self.view(existing), "This capture was already prepared."
        try:
            if self.db.get_browser_request(req.request_id) is not None:
                raise ValidationError("request_id_conflict", "This request ID was already used.")
        except sqlite3.Error:
            raise ValidationError("storage_error", "My Download Manager could not verify the request.") from None

        cfg = self.settings.get()
        reason = self._eligibility(req, cfg, url_hash)
        if reason:
            logger.info("Capture %s declined at prepare: %s", req.request_id, reason)
            return ("declined",
                    {"request_id": req.request_id, "state": "declined", "reason": reason, "download_id": None,
                     "filename": req.filename or "", "total_bytes": req.total_bytes, "mode": rules.effective_mode(cfg)},
                    DECLINE_MESSAGES.get(reason, "Not captured."))

        try:
            active = self.db.find_active_capture(req.browser, req.browser_download_id)
        except sqlite3.Error:
            raise ValidationError("storage_error", "My Download Manager could not verify the request.") from None
        if active is not None:
            raise ValidationError("duplicate_capture", "This browser download is already being captured.")

        now = datetime.now()
        record = {
            "request_id": req.request_id,
            "browser": req.browser,
            "browser_download_id": req.browser_download_id,
            "url_sha256": url_hash,
            "filename": req.filename,
            "total_bytes": req.total_bytes,
            "mode": rules.effective_mode(cfg),
            "state": "probing",
            "reason": None,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=self.pending_ttl_s)).isoformat(),
        }
        try:
            self.db.insert_capture(record)
        except sqlite3.IntegrityError:
            winner = self._get(req.request_id)
            if winner and winner["url_sha256"] == url_hash:
                return "duplicate", self.view(winner), "This capture was already prepared."
            raise ValidationError("request_id_conflict", "This request ID was already used.") from None
        except sqlite3.Error:
            logger.exception("Could not store capture %s", req.request_id)
            raise ValidationError("storage_error", "My Download Manager could not store the capture.") from None

        self._urls[req.request_id] = req.url
        self._prepare_info[req.request_id] = {
            "browser_filename": req.filename,
            "mime": req.mime,
            "website": rules.host_of(req.referrer) or rules.host_of(req.url) or "",
        }
        logger.info("Capture %s prepared (%s, %s, mode=%s)", req.request_id, req.browser,
                    redact_url(req.url), record["mode"])
        self.capture_changed.emit(req.request_id, "probing")
        self._submit_probe(req.request_id, req.url, req.mime)
        return "prepared", self.view(record), "Capture prepared."

    def _submit_probe(self, rid: str, url: str, mime: Optional[str]) -> None:
        def job() -> None:
            try:
                result = self._prober(url, browser_mime=mime)
            except Exception:  # noqa: BLE001 - never let a probe crash the worker
                logger.exception("Capture probe crashed.")
                result = {"ok": False, "reason": "probe_failed"}
            if self._stopped:
                return
            try:
                self._probe_finished.emit(rid, dict(result))
            except RuntimeError:
                pass  # manager destroyed during shutdown

        try:
            self._executor.submit(job)
        except RuntimeError:  # executor already shut down
            self._probe_finished.emit(rid, {"ok": False, "reason": "probe_failed"})

    @Slot(str, dict)
    def _on_probe_finished(self, rid: str, result: Dict[str, Any]) -> None:
        if self._stopped:
            return
        try:
            rec = self._get(rid)
        except ValidationError:
            return
        if not rec or rec["state"] != "probing":
            return
        info = self._prepare_info.get(rid, {})
        url = self._urls.get(rid, "")
        filename = (info.get("browser_filename") or rules.safe_basename(result.get("filename") or "")
                    or rules.filename_from_url(url) or rec.get("filename") or "download")
        total = result.get("total_bytes") if result.get("total_bytes") is not None else rec.get("total_bytes")

        if result.get("ok") and not info.get("browser_filename"):
            enabled = rules.enabled_extensions(self.settings.get())
            if rules.extension_matches(filename, enabled) is None:
                result = dict(result, ok=False, reason="extension_not_selected")

        if not result.get("ok"):
            reason = result.get("reason") or "probe_failed"
            self._transition(rid, ("probing",), "declined", reason=reason, filename=filename, total_bytes=total)
            self._forget(rid)
            return

        now = datetime.now()
        if rec["mode"] == "automatic":
            self._transition(rid, ("probing",), "approved", filename=filename, total_bytes=total,
                             expires_at=(now + timedelta(seconds=self.approved_ttl_s)).isoformat())
            return

        deadline = now + timedelta(seconds=self.ask_timeout_s + 30)
        if not self._transition(rid, ("probing",), "awaiting_user", filename=filename, total_bytes=total,
                                expires_at=deadline.isoformat()):
            return
        self._request_approval(rid, {
            "request_id": rid,
            "filename": filename,
            "website": info.get("website", ""),
            "total_bytes": total,
            "browser": BROWSER_LABELS.get(rec["browser"], rec["browser"]),
            "timeout_s": self.ask_timeout_s,
        })

    def _request_approval(self, rid: str, info: Dict[str, Any]) -> None:
        if self._approval_handler is None:
            self.resolve_decision(rid, False, reason="no_approval_ui")
            return

        def decide(approve: bool) -> None:
            self._approval_close.pop(rid, None)
            self.resolve_decision(rid, bool(approve))

        try:
            close = self._approval_handler(info, decide)
        except Exception:  # noqa: BLE001
            logger.exception("Approval prompt failed.")
            self.resolve_decision(rid, False, reason="no_approval_ui")
            return
        if close and rid not in self._approval_close and self._get_state(rid) == "awaiting_user":
            self._approval_close[rid] = close

    def _get_state(self, rid: str) -> Optional[str]:
        rec = self.db.get_capture(rid)
        return rec["state"] if rec else None

    def resolve_decision(self, rid: str, approve: bool, *, reason: str = "user_declined") -> bool:
        """Records the user's choice from the Ask dialog."""
        if approve:
            expires = (datetime.now() + timedelta(seconds=self.approved_ttl_s)).isoformat()
            changed = self._transition(rid, ("awaiting_user",), "approved", expires_at=expires)
        else:
            changed = self._transition(rid, ("awaiting_user",), "declined", reason=reason)
            if changed:
                self._forget(rid)
        self._close_approval(rid)
        return changed

    def status(self, payload: Dict[str, Any]) -> Result:
        ref = validate_capture_reference(dict(payload, action="capture_status"), action="capture_status")
        self._seen(ref.get("browser"))
        self._require_db()
        rec = self._get(ref["request_id"])
        if rec is None:
            raise ValidationError("capture_not_found", "This capture is unknown or has expired.")
        rec = self._check_expiry(rec)
        return rec["state"], self.view(rec), DECLINE_MESSAGES.get(rec.get("reason") or "", "")

    def commit(self, payload: Dict[str, Any]) -> Result:
        ref = validate_capture_reference(dict(payload, action="commit_capture"), allow_url=True,
                                         action="commit_capture")
        self._seen(ref.get("browser"))
        self._require_db()
        rid, url = ref["request_id"], ref["url"]
        rec = self._get(rid)
        if rec is None:
            raise ValidationError("capture_not_found", "This capture is unknown or has expired.")
        if rec["url_sha256"] != self._hash(url):
            raise ValidationError("request_id_conflict", "This request ID belongs to a different link.")
        if rec["state"] == "committed":
            return "duplicate", self._committed_view(rec), "This download was already transferred."
        rec = self._check_expiry(rec)
        if rec["state"] != "approved":
            raise ValidationError("capture_not_approved", f"The capture is not approved (state: {rec['state']}).")

        folder = Path(self._folder_provider())
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise ValidationError("queue_rejected", "The default download folder is not available.") from None

        browser_request = {
            "request_id": rid,
            "browser": rec["browser"],
            "url_sha256": rec["url_sha256"],
            "status": "accepted",
            "created_at": datetime.now().isoformat(),
            "capture_commit": True,
        }
        try:
            item = self.queue_mgr.add_download(
                url=url,
                folder=str(folder),
                suggested_filename=rec.get("filename") or None,
                auto_start=True,
                browser_request=browser_request,
            )
        except sqlite3.IntegrityError:
            # Lost a race or the state changed underneath us: nothing was committed.
            again = self._get(rid)
            if again and again["state"] == "committed":
                return "duplicate", self._committed_view(again), "This download was already transferred."
            raise ValidationError("capture_not_approved", "The capture is no longer approved.") from None
        except sqlite3.Error:
            logger.exception("Database error committing capture %s", rid)
            raise ValidationError("storage_error", "My Download Manager could not store the download. Try again.") from None
        except Exception:
            logger.exception("QueueManager rejected captured download %s", redact_url(url))
            raise ValidationError("queue_rejected", "My Download Manager could not add the download.") from None

        item["source"] = rec["browser"]
        item["source_label"] = BROWSER_LABELS.get(rec["browser"], rec["browser"])
        item["browser_request_id"] = rid
        item["captured"] = True
        item["referrer"] = None
        item["page_title"] = None
        self._forget(rid)
        self.capture_changed.emit(rid, "committed")
        logger.info("Capture %s committed as download %s (%s)", rid, item["id"], redact_url(url))
        self.download_captured.emit(item)
        committed = self._get(rid) or dict(rec, state="committed", download_id=item["id"])
        return "committed", self._committed_view(committed), f"Transferred to My Download Manager: {item.get('filename', '')}"

    def _committed_view(self, rec: Dict[str, Any]) -> Dict[str, Any]:
        view = self.view(rec)
        item = self.queue_mgr.get_item(rec.get("download_id") or "") if rec.get("download_id") else None
        if item:
            view["filename"] = item.get("filename", view["filename"])
        return view

    def abort(self, payload: Dict[str, Any]) -> Result:
        ref = validate_capture_reference(dict(payload, action="abort_capture"), allow_reason=True,
                                         action="abort_capture")
        self._seen(ref.get("browser"))
        self._require_db()
        rid = ref["request_id"]
        rec = self._get(rid)
        if rec is None:
            return "aborted", {"request_id": rid, "state": "not_found", "reason": ref["reason"],
                               "download_id": None, "filename": "", "total_bytes": None, "mode": None}, ""
        if rec["state"] in CAPTURE_ACTIVE_STATES:
            self._transition(rid, CAPTURE_ACTIVE_STATES, "aborted", reason=ref["reason"])
            self._close_approval(rid)
            self._forget(rid)
            rec = self._get(rid) or rec
        # A committed capture is reported as such: the browser must NOT resume.
        return rec["state"], self.view(rec), ""

    # ------------------------------------------------------- configuration API
    def config_payload(self) -> Dict[str, Any]:
        data = rules.public_config(self.settings.get())
        data["app_version"] = APP_VERSION
        data["capture_available"] = self.available
        data["pending"] = self.pending_count()
        data["fallbacks"] = self.fallbacks()
        return data

    def fallbacks(self) -> List[Dict[str, Any]]:
        if not self.available:
            return []
        try:
            recs = self.db.list_captures(("needs_browser",), limit=10)
        except sqlite3.Error:
            return []
        return [{"request_id": r["request_id"], "filename": r.get("filename") or "", "reason": r.get("reason")}
                for r in recs]

    def handle_get_config(self, payload: Dict[str, Any]) -> Result:
        info = validate_capture_config_request(dict(payload, action="get_capture_config"))
        self._seen(info.get("browser"), capture_permissions=info.get("capture_permissions"),
                   extension_version=info.get("extension_version"))
        return "ok", self.config_payload(), ""

    def handle_set_mode(self, payload: Dict[str, Any]) -> Result:
        info = validate_capture_mode(dict(payload, action="set_capture_mode"))
        self._seen(info.get("browser"))
        cfg = self.settings.get()
        if info["mode"] == "disabled":
            cfg["enabled"] = False
        else:
            cfg["enabled"] = True
            cfg["mode"] = info["mode"]
        self.update_config(cfg)
        logger.info("Capture mode set to %s from the browser.", info["mode"])
        return "ok", self.config_payload(), "Capture settings updated."

    def claim_fallback(self, payload: Dict[str, Any]) -> Result:
        """Hands a captured download that failed for lack of a browser session
        back to the browser, which re-downloads it with its own cookies."""
        ref = validate_capture_reference(dict(payload, action="claim_fallback"), action="claim_fallback")
        self._seen(ref.get("browser"))
        self._require_db()
        rec = self._get(ref["request_id"])
        if rec is None or rec["state"] != "needs_browser":
            raise ValidationError("fallback_not_available", "There is nothing to hand back for this download.")
        url = None
        item = self.queue_mgr.get_item(rec.get("download_id") or "")
        if item:
            url = item.get("url")
        if not url:
            try:
                d = self.db.get_download(rec.get("download_id") or "")
                url = d.url if d else None
            except sqlite3.Error:
                url = None
        if not url:
            raise ValidationError("fallback_not_available", "The original download is no longer available.")
        self._transition(rec["request_id"], ("needs_browser",), "fallback_claimed")
        self._fallback_hashes[rec["url_sha256"]] = time.monotonic() + self.fallback_ttl_s
        return "ok", {"request_id": rec["request_id"], "url": url, "filename": rec.get("filename") or ""}, \
            "Continue this download in the browser."

    # ------------------------------------------------------------ diagnostics
    def record_decision(self, payload: Dict[str, Any]) -> Result:
        """Stores a browser's privacy-safe summary of one capture decision
        (memory only, bounded) for the Browser Integration settings panel."""
        info = validate_capture_decision(dict(payload, action="report_capture_decision"))
        self._seen(info.get("browser"))
        entry = dict(info["decision"], received_at=time.time())
        if not entry.get("browser"):
            entry["browser"] = info.get("browser")
        self.decisions.append(entry)
        logger.info("Browser capture decision: %s/%s (%s, .%s, %s)", entry["decision"], entry.get("reason") or "-",
                    entry.get("browser") or "?", entry.get("extension") or "?", entry.get("method") or "method ?")
        self.diagnostics_changed.emit()
        return "ok", {"recorded": True}, ""

    def latest_decision(self) -> Optional[Dict[str, Any]]:
        return dict(self.decisions[-1]) if self.decisions else None

    def recent_decisions(self, limit: int = 10) -> List[Dict[str, Any]]:
        return [dict(d) for d in list(self.decisions)[-limit:]][::-1]

    # ------------------------------------------------------- queue integration
    @Slot(str, str)
    def _on_download_failed(self, item_id: str, error: str) -> None:
        if not self.available or not AUTH_FAILURE_RE.search(error or ""):
            return
        try:
            rec = self.db.find_capture_by_download(item_id)
            if rec and rec["state"] == "committed":
                self.db.transition_capture(rec["request_id"], ("committed",), "needs_browser", reason="auth_required")
                self.capture_changed.emit(rec["request_id"], "needs_browser")
                logger.info("Captured download %s needs the browser session (HTTP auth failure).", item_id)
        except sqlite3.Error:
            logger.warning("Could not record browser fallback for %s.", item_id)
