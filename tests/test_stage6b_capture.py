"""
Stage 6B - automatic capture: rules, schema v4, CaptureManager handshake,
persistence/recovery, concurrency, failure handling, approval UI, settings
panel and native-host forwarding.

Probes are replaced by deterministic fakes here; tests/test_stage6b_integration.py
exercises the real probe and the real download engine against a local HTTP server.
"""
import hashlib
import json
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QThread, QTimer
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "native_host"))

from mdm.browser_integration import capture_rules as rules  # noqa: E402
from mdm.browser_integration import ipc_protocol as proto  # noqa: E402
from mdm.browser_integration.capture import CaptureManager, probe_url  # noqa: E402
from mdm.browser_integration.capture_dialog import CaptureApprovalDialog  # noqa: E402
from mdm.browser_integration.host_registration import query_registration  # noqa: E402
from mdm.browser_integration.request_validator import ValidationError  # noqa: E402
from mdm.browser_integration.service import BrowserIntegrationService  # noqa: E402
from mdm.browser_integration.settings_widget import BrowserCaptureSettingsPanel, describe_browser  # noqa: E402
from mdm.database import CURRENT_SCHEMA_VERSION, DatabaseManager  # noqa: E402
from mdm.models import DownloadModel, DownloadStatus  # noqa: E402
from mdm.queue_manager import QueueManager  # noqa: E402
import host  # noqa: E402
from verify_host import MockAppServer  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

URL = "https://downloads.example.test/releases/tool-3.1.zip?X-Amz-Signature=SECRETSIG&token=t0p53cr3t"


def pump_until(predicate, timeout=10.0):
    deadline = time.time() + timeout
    while not predicate():
        if time.time() > deadline:
            raise AssertionError("Timed out waiting for condition")
        QCoreApplication.processEvents()
        time.sleep(0.002)


def in_thread(fn, timeout=20.0):
    box = {}

    def target():
        try:
            box["result"] = fn()
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    t = threading.Thread(target=target, daemon=True)
    t.start()
    pump_until(lambda: not t.is_alive(), timeout)
    if "error" in box:
        raise box["error"]
    return box.get("result")


def ok_probe(url, browser_mime=None):
    return {"ok": True, "reason": None, "status": 206, "total_bytes": 4096, "filename": None, "content_type": "application/zip"}


def prepare_payload(request_id="capture-req-0001", url=URL, **over):
    p = {"request_id": request_id, "url": url, "browser": "chrome", "browser_download_id": 17,
         "filename": "tool-3.1.zip", "mime": "application/zip", "total_bytes": None,
         "referrer": "https://example.test/downloads", "method": "GET", "danger": "safe", "incognito": False}
    p.update(over)
    return p


# ===========================================================================
# Rules
# ===========================================================================
class CaptureRulesTests(unittest.TestCase):

    def test_extension_normalisation(self):
        self.assertEqual(rules.normalize_extension(" .ZIP "), "zip")
        self.assertEqual(rules.normalize_extension("*.Tar.Gz"), "tar.gz")
        for bad in ("", "z ip", "zip!", "a.b.c", "x" * 11, "../exe", None, 5):
            with self.assertRaises(rules.RuleError):
                rules.normalize_extension(bad)

    def test_extension_matching(self):
        self.assertEqual(rules.extension_matches("C:\\Users\\me\\Downloads\\Setup.EXE", {"exe"}), "exe")
        self.assertEqual(rules.extension_matches("archive.tar.gz", {"gz", "tar.gz"}), "tar.gz")
        self.assertEqual(rules.extension_matches("archive.tar.gz", {"gz"}), "gz")
        self.assertIsNone(rules.extension_matches("README", {"txt"}))
        self.assertIsNone(rules.extension_matches(".zip", {"zip"}))
        # Stage 6B repair: images are default extensions now; unsupported formats still never match.
        self.assertEqual(rules.extension_matches("photo.jpg", set(rules.DEFAULT_EXTENSIONS)), "jpg")
        self.assertIsNone(rules.extension_matches("photo.heic", set(rules.DEFAULT_EXTENSIONS)))

    def test_filename_from_url_path_only(self):
        self.assertEqual(rules.filename_from_url("https://e.com/a/My%20File.zip?name=evil.exe&sig=x"), "My File.zip")
        self.assertEqual(rules.filename_from_url("https://e.com/download?file=a.zip"), "download")
        self.assertEqual(rules.filename_from_url("https://e.com/"), "")

    def test_default_extensions_match_specification(self):
        expected = {"zip", "rar", "7z", "tar", "gz", "exe", "msi", "msix", "iso", "img", "mp4", "mkv", "avi",
                    "mov", "wmv", "webm", "mp3", "wav", "flac", "aac", "pdf", "doc", "docx", "xls", "xlsx",
                    "ppt", "pptx", "apk", "csv", "txt",
                    # Stage 6B repair: Images group
                    "jpg", "jpeg", "png", "gif", "webp", "bmp", "tif", "tiff", "ico"}
        self.assertEqual(set(rules.DEFAULT_EXTENSIONS), expected)

    def test_site_normalisation(self):
        self.assertEqual(rules.normalize_site("Example.COM"), "example.com")
        self.assertEqual(rules.normalize_site("https://www.YouTube.com/watch?v=1"), "www.youtube.com")
        self.assertEqual(rules.normalize_site("*.google.com"), "google.com")
        self.assertEqual(rules.normalize_site("example.com:8443/path"), "example.com")
        self.assertEqual(rules.normalize_site("bücher.example"), "xn--bcher-kva.example")
        self.assertEqual(rules.normalize_site("192.168.1.10"), "192.168.1.10")
        for bad in ("", "com", "*", "a*.example.com", "exa mple.com", "-bad.com", "user@example.com",
                    "a..b.com", "x" * 64 + ".com", None):
            with self.assertRaises(rules.RuleError, msg=repr(bad)):
                rules.normalize_site(bad)

    def test_site_matching(self):
        sites = ["example.com", "xn--bcher-kva.example"]
        self.assertTrue(rules.host_is_excluded("example.com", sites))
        self.assertTrue(rules.host_is_excluded("a.b.example.com", sites))
        self.assertTrue(rules.host_is_excluded("EXAMPLE.com.", sites))
        self.assertFalse(rules.host_is_excluded("notexample.com", sites))
        self.assertFalse(rules.host_is_excluded("example.com.evil.net", sites))
        self.assertTrue(rules.host_is_excluded(rules.host_of("https://bücher.example/x"), sites))

    def test_config_normalisation_never_raises(self):
        cfg = rules.normalize_config({"enabled": "yes", "mode": "hack", "extensions": {"zip": False, "evil": True},
                                      "custom_extensions": ["EPUB", "bad ext", "zip", "epub"],
                                      "excluded_sites": ["Example.com", "*", "example.com"]})
        self.assertFalse(cfg["enabled"], "only a real boolean enables capture")
        self.assertEqual(cfg["mode"], "ask")
        self.assertFalse(cfg["extensions"]["zip"])
        self.assertNotIn("evil", cfg["extensions"])
        self.assertEqual(cfg["custom_extensions"], ["epub"])
        self.assertEqual(cfg["excluded_sites"], ["example.com"])
        self.assertEqual(rules.effective_mode(rules.default_config()), "disabled", "Mode A is the default")
        self.assertIsInstance(rules.normalize_config("garbage"), dict)

    def test_settings_persist_across_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = DatabaseManager(Path(tmp) / "s.db")
            store = rules.CaptureSettingsStore(db)
            cfg = store.get()
            cfg.update(enabled=True, mode="automatic", custom_extensions=["epub"], excluded_sites=["youtube.com"])
            cfg["extensions"]["txt"] = False
            store.save(cfg)
            db.close()
            db2 = DatabaseManager(Path(tmp) / "s.db")
            again = rules.CaptureSettingsStore(db2).get()
            self.assertEqual(rules.effective_mode(again), "automatic")
            self.assertEqual(again["custom_extensions"], ["epub"])
            self.assertFalse(again["extensions"]["txt"])
            self.assertEqual(rules.CaptureSettingsStore(db2).reset()["enabled"], False)
            db2.close()


# ===========================================================================
# Schema v4
# ===========================================================================
class SchemaV4MigrationTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "old.db"

    def tearDown(self):
        self._tmp.cleanup()

    def _make_db(self, version):
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        builder = DatabaseManager.__new__(DatabaseManager)
        with conn:
            builder._create_schema_v1(conn)
            builder._migrate(conn, 1, version)
            conn.execute(f"PRAGMA user_version = {version};")
            conn.execute("INSERT INTO downloads (id, url, filename, destination_path, status, created_at, total_size, "
                         "downloaded_size) VALUES ('done0001','https://e.test/a.iso','a.iso','C:/dl','Completed',"
                         "'2026-01-01T00:00:00',10,10)")
            conn.execute("INSERT INTO settings (key, value) VALUES ('max_simultaneous_downloads', '4')")
            if version >= 3:
                conn.execute("INSERT INTO browser_requests VALUES ('old-request-01','done0001','edge','"
                             + "0" * 64 + "','accepted','2026-01-01T00:00:00')")
        conn.close()

    def _check(self, expect_request_row):
        db = DatabaseManager(self.path)
        conn = db.get_connection()
        self.assertEqual(CURRENT_SCHEMA_VERSION, 4)
        self.assertEqual(conn.execute("PRAGMA user_version;").fetchone()[0], 4)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(browser_captures);")}
        self.assertEqual(cols, {"request_id", "browser", "browser_download_id", "url_sha256", "filename",
                                "total_bytes", "mode", "state", "reason", "download_id", "created_at",
                                "updated_at", "expires_at"})
        self.assertNotIn("url", cols, "the URL is never stored in the capture table")
        done = db.get_download("done0001")
        self.assertEqual((done.status, done.total_size), ("Completed", 10), "completed history preserved")
        self.assertEqual(db.get_setting("max_simultaneous_downloads"), "4")
        if expect_request_row:
            self.assertIsNotNone(db.get_browser_request("old-request-01"), "6A dedupe records preserved")
        db.close()

    def test_migration_from_stage6a_v3(self):
        self._make_db(3)
        self._check(expect_request_row=True)

    def test_migration_from_stage5_v2(self):
        self._make_db(2)
        self._check(expect_request_row=False)

    def test_reopen_is_noop(self):
        self._make_db(3)
        DatabaseManager(self.path).close()
        self._check(expect_request_row=True)


# ===========================================================================
# CaptureManager
# ===========================================================================
class _Stack:
    def __init__(self, tmp: Path, *, prober=ok_probe, approval=None, auto_queue=False, **capture_kwargs):
        self.tmp = tmp
        self.db = DatabaseManager(tmp / "downloads.db")
        self.queue = QueueManager(db=self.db)
        self.queue.is_auto_queue_running = auto_queue
        self.capture = CaptureManager(self.queue, folder_provider=lambda: tmp / "dl", approval_handler=approval,
                                      prober=prober, **capture_kwargs)
        self.capture.start()

    def set_mode(self, mode, **cfg_over):
        cfg = self.capture.get_config()
        cfg["enabled"] = mode != "disabled"
        if mode != "disabled":
            cfg["mode"] = mode
        cfg.update(cfg_over)
        self.capture.update_config(cfg)

    def close(self):
        self.capture.stop()
        self.queue.shutdown()
        self.capture.deleteLater()
        self.queue.deleteLater()
        QCoreApplication.processEvents()
        self.db.close()


class CaptureManagerTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.stacks = []

    def tearDown(self):
        for s in self.stacks:
            s.close()
        self._tmp.cleanup()

    def stack(self, mode="automatic", **kw):
        s = _Stack(self.tmp, **kw)
        s.set_mode(mode)
        self.stacks.append(s)
        return s

    def restart(self, s, **kw):
        s.close()
        self.stacks.remove(s)
        s2 = _Stack(self.tmp, **kw)
        self.stacks.append(s2)
        return s2

    def wait_state(self, s, rid, states=("approved", "awaiting_user", "declined")):
        pump_until(lambda: s.db.get_capture(rid)["state"] in states)
        return s.db.get_capture(rid)

    # --- prepare -----------------------------------------------------------
    def test_prepare_capture_automatic(self):
        s = self.stack()
        status, data, _ = s.capture.prepare(prepare_payload())
        self.assertEqual((status, data["state"]), ("prepared", "probing"))
        rec = self.wait_state(s, "capture-req-0001")
        self.assertEqual(rec["state"], "approved")
        self.assertEqual(rec["total_bytes"], 4096)
        self.assertEqual(rec["url_sha256"], hashlib.sha256(URL.encode()).hexdigest())
        dump = json.dumps(dict(rec))
        for secret in ("SECRETSIG", "t0p53cr3t", "downloads.example.test"):
            self.assertNotIn(secret, dump, "URL and its signed query are never stored")

    def test_prepare_declined_by_rules(self):
        s = self.stack("disabled")
        self.assertEqual(s.capture.prepare(prepare_payload())[1]["reason"], "capture_disabled")
        s.set_mode("automatic", excluded_sites=["example.test"])
        self.assertEqual(s.capture.prepare(prepare_payload("capture-req-0002"))[1]["reason"], "site_excluded",
                         "downloads.example.test is a subdomain of an excluded site")
        s.set_mode("automatic", excluded_sites=[])
        r = s.capture.prepare(prepare_payload("capture-req-0003", filename="photo.heic",
                                              url="https://e.test/photo.heic"))
        self.assertEqual(r[1]["reason"], "extension_not_selected")
        self.assertIsNone(s.db.get_capture("capture-req-0003"), "declined at prepare: no record kept")

        # No browser filename: decided after the probe from Content-Disposition.
        s.capture._prober = lambda url, browser_mime=None: dict(ok_probe(url), filename="diagram.svg")
        s.capture.prepare(prepare_payload("capture-req-0004", filename=None, url="https://e.test/get?id=4",
                                          browser_download_id=44))
        rec = self.wait_state(s, "capture-req-0004")
        self.assertEqual((rec["state"], rec["reason"]), ("declined", "extension_not_selected"))
        s.capture._prober = lambda url, browser_mime=None: dict(ok_probe(url), filename="Q3 Report.pdf")
        s.capture.prepare(prepare_payload("capture-req-0005", filename=None, url="https://e.test/get?id=5",
                                          browser_download_id=45))
        rec = self.wait_state(s, "capture-req-0005")
        self.assertEqual((rec["state"], rec["filename"]), ("approved", "Q3 Report.pdf"))

    def test_prepare_validation(self):
        s = self.stack()
        for over, code in (({"method": "POST"}, "unsupported_method"), ({"danger": "file"}, "dangerous_download"),
                           ({"incognito": True}, "incognito_excluded"), ({"url": "blob:https://e/x"}, "unsupported_scheme"),
                           ({"url": "data:,x"}, "unsupported_scheme"), ({"filename": "..\\evil.exe"}, "invalid_field"),
                           ({"url": "https://u:p@e.com/x.zip"}, "embedded_credentials"), ({"cookies": "a=b"}, "invalid_field")):
            with self.assertRaises(ValidationError) as ctx:
                s.capture.prepare(prepare_payload(**over))
            self.assertEqual(ctx.exception.code, code, over)

    def test_prepare_idempotent_and_conflicts(self):
        s = self.stack()
        s.capture.prepare(prepare_payload())
        self.assertEqual(s.capture.prepare(prepare_payload())[0], "duplicate")
        with self.assertRaises(ValidationError) as ctx:
            s.capture.prepare(prepare_payload(url="https://e.test/other.zip"))
        self.assertEqual(ctx.exception.code, "request_id_conflict")
        with self.assertRaises(ValidationError) as ctx:
            s.capture.prepare(prepare_payload("capture-req-0009"))  # same browser download id, still active
        self.assertEqual(ctx.exception.code, "duplicate_capture")

    def test_request_id_shared_with_6a_dedupe(self):
        s = self.stack()
        s.queue.add_download("https://e.test/m.zip", str(self.tmp), auto_start=False,
                             browser_request={"request_id": "manual-req-0001", "browser": "chrome",
                                              "url_sha256": "1" * 64})
        with self.assertRaises(ValidationError) as ctx:
            s.capture.prepare(prepare_payload("manual-req-0001"))
        self.assertEqual(ctx.exception.code, "request_id_conflict")

    # --- commit ------------------------------------------------------------
    def test_commit_capture(self):
        s = self.stack()
        s.capture.prepare(prepare_payload())
        self.wait_state(s, "capture-req-0001")
        status, data, _ = s.capture.commit({"request_id": "capture-req-0001", "url": URL, "browser": "chrome"})
        self.assertEqual(status, "committed")
        item = s.queue.get_item(data["download_id"])
        self.assertEqual(item["url"], URL, "signed query preserved for the download engine")
        self.assertEqual(item["filename"], "tool-3.1.zip", "browser-provided filename preferred")
        self.assertTrue(item["captured"])
        self.assertEqual(item["source_label"], "Google Chrome")
        self.assertEqual(s.db.get_capture("capture-req-0001")["download_id"], data["download_id"])
        self.assertIsNotNone(s.db.get_browser_request("capture-req-0001"))
        again = s.capture.commit({"request_id": "capture-req-0001", "url": URL})
        self.assertEqual((again[0], again[1]["download_id"]), ("duplicate", data["download_id"]))
        self.assertEqual(len(s.db.get_all_downloads()), 1)

    def test_commit_requires_approval_and_matching_url(self):
        s = self.stack("ask", approval=lambda info, decide: None)
        s.capture.prepare(prepare_payload())
        self.wait_state(s, "capture-req-0001", ("awaiting_user",))
        with self.assertRaises(ValidationError) as ctx:
            s.capture.commit({"request_id": "capture-req-0001", "url": URL})
        self.assertEqual(ctx.exception.code, "capture_not_approved")
        with self.assertRaises(ValidationError) as ctx:
            s.capture.commit({"request_id": "capture-req-0001", "url": "https://e.test/other.zip"})
        self.assertEqual(ctx.exception.code, "request_id_conflict")
        with self.assertRaises(ValidationError) as ctx:
            s.capture.commit({"request_id": "unknown-req-0001", "url": URL})
        self.assertEqual(ctx.exception.code, "capture_not_found")
        self.assertEqual(s.db.get_all_downloads(), [])

    def test_commit_after_expiry_refused(self):
        s = self.stack(approved_ttl_s=0)
        s.capture.prepare(prepare_payload())
        self.wait_state(s, "capture-req-0001")
        time.sleep(0.01)
        with self.assertRaises(ValidationError) as ctx:
            s.capture.commit({"request_id": "capture-req-0001", "url": URL})
        self.assertEqual(ctx.exception.code, "capture_not_approved")
        self.assertEqual(s.db.get_capture("capture-req-0001")["state"], "expired")

    # --- abort -------------------------------------------------------------
    def test_abort_capture(self):
        s = self.stack()
        s.capture.prepare(prepare_payload())
        self.wait_state(s, "capture-req-0001")
        status, data, _ = s.capture.abort({"request_id": "capture-req-0001", "reason": "cancel_failed"})
        self.assertEqual((status, data["reason"]), ("aborted", "cancel_failed"))
        self.assertEqual(s.capture.abort({"request_id": "capture-req-0001", "reason": "x"})[0], "aborted", "idempotent")
        with self.assertRaises(ValidationError):
            s.capture.commit({"request_id": "capture-req-0001", "url": URL})
        self.assertEqual(s.capture.abort({"request_id": "unknown-req-0002"})[1]["state"], "not_found")

    def test_abort_after_commit_reports_committed(self):
        s = self.stack()
        s.capture.prepare(prepare_payload())
        self.wait_state(s, "capture-req-0001")
        s.capture.commit({"request_id": "capture-req-0001", "url": URL})
        status, data, _ = s.capture.abort({"request_id": "capture-req-0001", "reason": "timeout"})
        self.assertEqual(status, "committed", "the browser must not resume a committed capture")

    # --- persistence / recovery -------------------------------------------
    def test_persistent_pending_records_and_recovery(self):
        s = self.stack()
        s.capture.prepare(prepare_payload())
        self.wait_state(s, "capture-req-0001")
        s.set_mode("ask")
        hold = []
        s.capture._approval_handler = lambda info, decide: hold.append(decide)
        s.capture.prepare(prepare_payload("capture-req-0002", browser_download_id=18,
                                          url="https://e.test/b.zip", filename="b.zip"))
        self.wait_state(s, "capture-req-0002", ("awaiting_user",))

        s2 = self.restart(s)
        self.assertEqual(s2.db.get_capture("capture-req-0001")["state"], "approved", "approved survives restart")
        self.assertEqual(s2.db.get_capture("capture-req-0002")["state"], "expired",
                         "a lost dialog expires so the browser resumes")
        status, data, _ = s2.capture.commit({"request_id": "capture-req-0001", "url": URL})
        self.assertEqual(status, "committed")
        s3 = self.restart(s2)
        self.assertEqual(len(s3.queue.items), 1, "committed download reloaded from SQLite")
        self.assertEqual(s3.capture.commit({"request_id": "capture-req-0001", "url": URL})[0], "duplicate")

    def test_maintenance_expires_and_purges(self):
        s = self.stack(pending_ttl_s=0, prober=lambda *a, **k: (time.sleep(0.3), ok_probe(""))[1])
        s.capture.prepare(prepare_payload())
        time.sleep(0.01)
        s.capture.maintenance()
        self.assertEqual(s.db.get_capture("capture-req-0001")["state"], "expired")
        old = (datetime.now() + timedelta(days=30)).isoformat()
        self.assertEqual(s.db.purge_captures(now=datetime.fromisoformat(old)), 1)
        self.assertIsNone(s.db.get_capture("capture-req-0001"))

    # --- concurrency / failures -------------------------------------------
    def test_concurrent_duplicate_commit_at_database_level(self):
        s = self.stack()
        s.capture.prepare(prepare_payload())
        self.wait_state(s, "capture-req-0001")
        rec = {"request_id": "capture-req-0001", "browser": "chrome", "url_sha256": "x", "capture_commit": True}
        barrier = threading.Barrier(2)
        out = []

        def writer(i):
            m = DownloadModel(id=f"race{i:04d}", url=URL, destination_folder=str(self.tmp), filename="f")
            barrier.wait()
            try:
                s.db.add_download(m, browser_request=rec)
                out.append("ok")
            except sqlite3.IntegrityError:
                out.append("conflict")

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(2)]
        [t.start() for t in threads]
        [t.join(10) for t in threads]
        self.assertEqual(sorted(out), ["conflict", "ok"])
        self.assertEqual(len(s.db.get_all_downloads()), 1, "the losing download row was rolled back")
        self.assertEqual(s.db.get_capture("capture-req-0001")["state"], "committed")

    def test_db_commit_requires_approved_state(self):
        """Last line of defence: even if the capture changes state between the
        service's check and the write (e.g. aborted concurrently), the SQL guard
        rolls back the download row and the dedupe row."""
        from mdm.database import CaptureStateConflict
        s = self.stack()
        s.capture.prepare(prepare_payload())
        self.wait_state(s, "capture-req-0001")
        s.capture.abort({"request_id": "capture-req-0001", "reason": "browser_resumed"})
        rec = {"request_id": "capture-req-0001", "browser": "chrome", "url_sha256": "x", "capture_commit": True}
        with self.assertRaises(CaptureStateConflict):
            s.db.add_download(DownloadModel(id="late0001", url=URL, destination_folder=str(self.tmp), filename="f"),
                              browser_request=rec)
        self.assertEqual(s.db.get_all_downloads(), [])
        self.assertIsNone(s.db.get_browser_request("capture-req-0001"))
        self.assertEqual(s.db.get_capture("capture-req-0001")["state"], "aborted")

        # Service path: state flips after the Python-level check -> refused, nothing added.
        s.capture.prepare(prepare_payload("capture-req-0002", browser_download_id=77))
        self.wait_state(s, "capture-req-0002")
        real_add = s.queue.add_download

        def abort_first(*a, **kw):
            s.db.transition_capture("capture-req-0002", ("approved",), "aborted", reason="browser_resumed")
            return real_add(*a, **kw)

        with patch.object(s.queue, "add_download", side_effect=abort_first):
            with self.assertRaises(ValidationError) as ctx:
                s.capture.commit({"request_id": "capture-req-0002", "url": URL})
        self.assertEqual(ctx.exception.code, "capture_not_approved")
        self.assertEqual(s.queue.items, [])
        self.assertEqual(s.db.get_all_downloads(), [])

    def test_database_transaction_failure(self):
        s = self.stack()
        s.capture.prepare(prepare_payload())
        self.wait_state(s, "capture-req-0001")
        with patch.object(DatabaseManager, "_insert_browser_request", side_effect=sqlite3.OperationalError("disk I/O")):
            with self.assertRaises(ValidationError) as ctx:
                s.capture.commit({"request_id": "capture-req-0001", "url": URL})
        self.assertEqual(ctx.exception.code, "storage_error")
        self.assertEqual(s.db.get_all_downloads(), [])
        self.assertEqual(s.queue.items, [])
        self.assertEqual(s.db.get_capture("capture-req-0001")["state"], "approved", "nothing half-committed")
        self.assertEqual(s.capture.commit({"request_id": "capture-req-0001", "url": URL})[0], "committed")

    def test_queue_insertion_failure(self):
        s = self.stack()
        s.capture.prepare(prepare_payload())
        self.wait_state(s, "capture-req-0001")
        with patch.object(s.queue, "add_download", side_effect=RuntimeError("queue full")):
            with self.assertRaises(ValidationError) as ctx:
                s.capture.commit({"request_id": "capture-req-0001", "url": URL})
        self.assertEqual(ctx.exception.code, "queue_rejected")
        self.assertNotIn("queue full", ctx.exception.message)
        self.assertEqual(s.db.get_capture("capture-req-0001")["state"], "approved")
        self.assertEqual(s.db.get_all_downloads(), [])

    def test_probe_failure_declines(self):
        s = self.stack(prober=lambda url, browser_mime=None: {"ok": False, "reason": "auth_required"})
        s.capture.prepare(prepare_payload())
        rec = self.wait_state(s, "capture-req-0001")
        self.assertEqual((rec["state"], rec["reason"]), ("declined", "auth_required"))
        crashing = self.stack(prober=lambda *a, **k: 1 / 0)
        crashing.capture.prepare(prepare_payload("capture-req-0005", browser_download_id=99))
        self.assertEqual(self.wait_state(crashing, "capture-req-0005")["reason"], "probe_failed")

    # --- approval ----------------------------------------------------------
    def test_user_approval(self):
        decisions = []
        s = self.stack("ask", approval=lambda info, decide: decisions.append((info, decide)))
        s.capture.prepare(prepare_payload())
        self.wait_state(s, "capture-req-0001", ("awaiting_user",))
        info, decide = decisions[0]
        self.assertEqual(info["filename"], "tool-3.1.zip")
        self.assertEqual(info["website"], "example.test")
        self.assertEqual(info["total_bytes"], 4096)
        decide(True)
        self.assertEqual(s.db.get_capture("capture-req-0001")["state"], "approved")

    def test_user_rejection(self):
        s = self.stack("ask", approval=lambda info, decide: decide(False))
        s.capture.prepare(prepare_payload())
        rec = self.wait_state(s, "capture-req-0001", ("declined",))
        self.assertEqual(rec["reason"], "user_declined")
        status, data, msg = s.capture.status({"request_id": "capture-req-0001"})
        self.assertEqual(status, "declined")
        self.assertIn("browser", msg)

    def test_approval_dialog_buttons_and_timeout(self):
        results = []
        d = CaptureApprovalDialog({"filename": "a.zip", "website": "e.test", "total_bytes": 2048,
                                   "browser": "Microsoft Edge", "timeout_s": 5})
        d.decided.connect(results.append)
        d.show()
        d.mdm_button.click()
        self.assertEqual(results, [True])
        d2 = CaptureApprovalDialog({"filename": "b.zip", "timeout_s": 5})
        d2.decided.connect(results.append)
        d2.show()
        for _ in range(5):
            d2._tick()
        self.assertEqual(results, [True, False], "timeout = continue in browser")
        d3 = CaptureApprovalDialog({"filename": "c.zip", "timeout_s": 60})
        d3.decided.connect(results.append)
        d3.show()
        d3.close()
        self.assertEqual(results, [True, False, False], "closing the window = continue in browser")
        QCoreApplication.processEvents()

    # --- GUI responsiveness --------------------------------------------------
    def test_gui_not_blocked_by_probe(self):
        def slow_probe(url, browser_mime=None):
            time.sleep(1.0)
            return ok_probe(url)

        s = self.stack(prober=slow_probe)
        ticks = []
        timer = QTimer()
        timer.setInterval(20)
        timer.timeout.connect(lambda: ticks.append(1))
        timer.start()
        t0 = time.monotonic()
        s.capture.prepare(prepare_payload())
        self.assertLess(time.monotonic() - t0, 0.3, "prepare returns immediately")
        self.wait_state(s, "capture-req-0001")
        timer.stop()
        self.assertGreater(len(ticks), 20, "the event loop kept running during the probe")

    def test_handler_runs_on_main_thread(self):
        threads = []
        s = self.stack("ask", approval=lambda info, decide: threads.append(QThread.currentThread()))
        s.capture.prepare(prepare_payload())
        self.wait_state(s, "capture-req-0001", ("awaiting_user",))
        self.assertEqual(threads, [app.thread()], "dialogs are only ever created on the GUI thread")

    # --- auth failure fallback --------------------------------------------
    def test_auth_failure_hands_back_to_browser(self):
        s = self.stack()
        s.capture.prepare(prepare_payload())
        self.wait_state(s, "capture-req-0001")
        _, data, _ = s.capture.commit({"request_id": "capture-req-0001", "url": URL})
        s.queue.download_failed.emit(data["download_id"], "HTTP 404 Not Found")
        self.assertEqual(s.db.get_capture("capture-req-0001")["state"], "committed", "only auth failures")
        s.queue.download_failed.emit(data["download_id"], "HTTP 403 Forbidden: Permanent error, download failed.")
        self.assertEqual(s.db.get_capture("capture-req-0001")["state"], "needs_browser")
        cfg = s.capture.config_payload()
        self.assertEqual([f["request_id"] for f in cfg["fallbacks"]], ["capture-req-0001"])
        status, out, _ = s.capture.claim_fallback({"request_id": "capture-req-0001"})
        self.assertEqual(out["url"], URL)
        self.assertEqual(s.db.get_capture("capture-req-0001")["state"], "fallback_claimed")
        with self.assertRaises(ValidationError):
            s.capture.claim_fallback({"request_id": "capture-req-0001"})
        r = s.capture.prepare(prepare_payload("capture-req-0002", browser_download_id=40))
        self.assertEqual(r[1]["reason"], "browser_fallback", "the browser's re-download is not captured again")

    # --- configuration over IPC -------------------------------------------
    def test_config_and_mode_from_browser(self):
        s = self.stack("disabled")
        _, data, _ = s.capture.handle_get_config({"browser": "edge", "capture_permissions": True,
                                                  "extension_version": "0.7.0"})
        self.assertEqual(data["mode"], "disabled")
        self.assertIn("zip", data["extensions"])
        self.assertEqual(s.capture.browser_status["edge"]["capture_permissions"], True)
        _, data, _ = s.capture.handle_set_mode({"mode": "automatic", "browser": "chrome"})
        self.assertEqual(data["mode"], "automatic")
        self.assertTrue(rules.CaptureSettingsStore(s.db).get()["enabled"], "persisted")
        with self.assertRaises(ValidationError):
            s.capture.handle_set_mode({"mode": "everything"})


# ===========================================================================
# Settings panel, registration status, MainWindow
# ===========================================================================
class SettingsPanelTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.s = _Stack(Path(self._tmp.name))
        self.panel = BrowserCaptureSettingsPanel(self.s.capture, registration_reader=lambda: {})

    def tearDown(self):
        self.panel.deleteLater()
        self.s.close()
        self._tmp.cleanup()

    def test_mode_controls_persist(self):
        self.assertFalse(self.panel.enable_cb.isChecked(), "disabled by default")
        self.assertFalse(self.panel.mode_combo.isEnabled())
        self.panel.enable_cb.setChecked(True)
        self.panel.mode_combo.setCurrentIndex(self.panel.mode_combo.findData("automatic"))
        cfg = rules.CaptureSettingsStore(self.s.db).get()
        self.assertEqual(rules.effective_mode(cfg), "automatic")

    def test_extension_and_site_editing(self):
        self.panel.ext_checkboxes["txt"].setChecked(False)
        self.panel.custom_input.setText(".EPUB")
        self.panel.add_custom_extension()
        self.panel.custom_input.setText("bad ext!")
        self.panel.add_custom_extension()
        self.assertIn("not a valid", self.panel.error_label.text())
        self.panel.site_input.setText("https://WWW.Example.com/page")
        self.panel.add_site()
        cfg = self.s.capture.get_config()
        self.assertFalse(cfg["extensions"]["txt"])
        self.assertEqual(cfg["custom_extensions"], ["epub"])
        self.assertEqual(cfg["excluded_sites"], ["www.example.com"])
        self.assertEqual(self.panel.sites_list.count(), 1)
        self.panel.sites_list.setCurrentRow(0)
        self.panel.remove_site()
        self.panel.custom_list.setCurrentRow(0)
        self.panel.remove_custom_extension()
        cfg = self.s.capture.get_config()
        self.assertEqual((cfg["excluded_sites"], cfg["custom_extensions"]), ([], []))

    def test_reset_defaults(self):
        self.panel.enable_cb.setChecked(True)
        self.panel.ext_checkboxes["pdf"].setChecked(False)
        self.panel.reset_button.click()
        self.assertFalse(self.panel.enable_cb.isChecked())
        self.assertTrue(self.panel.ext_checkboxes["pdf"].isChecked())

    def test_status_and_test_connection_without_host(self):
        self.assertIn("Not registered", self.panel.host_status.text())
        self.panel.test_connection()
        self.assertIn("not registered", self.panel.test_result.text())
        reg = {"chrome": {"registered": True}}
        self.assertIn("Connected", describe_browser("chrome", {"last_seen": time.time(), "extension_version": "0.7.0",
                                                               "capture_permissions": False}, reg))
        self.assertIn("NOT granted", describe_browser("chrome", {"last_seen": time.time(),
                                                                 "capture_permissions": False}, reg))
        self.assertIn("no contact", describe_browser("chrome", None, reg))
        self.assertIn("not registered", describe_browser("edge", None, reg))

    def test_registration_parsing(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "MyDownloadManagerNativeHost.exe"
            exe.write_bytes(b"MZ")
            manifest = Path(tmp) / "m.json"
            manifest.write_text(json.dumps({"name": "com.mydownloadmanager.native", "path": str(exe),
                                            "allowed_origins": ["chrome-extension://" + "a" * 32 + "/"]}))
            other = Path(tmp) / "o.json"
            other.write_text(json.dumps({"name": "com.other.host"}))
            values = {"chrome": str(manifest), "edge": str(other)}
            reg = query_registration(lambda key: values["chrome" if "Google" in key else "edge"])
            self.assertTrue(reg["chrome"]["registered"])
            self.assertFalse(reg["edge"]["registered"])
            self.assertIn("different host", reg["edge"]["problem"])


class MainWindowCaptureTests(unittest.TestCase):

    def test_panel_attached_and_settings_page_opened(self):
        from mdm.main_window import MainWindow
        with tempfile.TemporaryDirectory() as tmp:
            db = DatabaseManager(Path(tmp) / "w.db")
            win = MainWindow(db=db)
            service = BrowserIntegrationService(win.queue_mgr, endpoint_path=Path(tmp) / "ep.json",
                                                show_window=win.bring_to_front, parent=win)
            win.attach_browser_integration(service)
            self.assertIsNotNone(win.browser_capture_panel)
            self.assertTrue(service.start())
            r = in_thread(lambda: host.handle_message({"action": "open_app", "page": "settings"},
                                                      Path(tmp) / "ep.json", config={}))
            self.assertTrue(r["ok"], r)
            self.assertIs(win.pages.currentWidget(), win.settings_page)
            bad = host.handle_message({"action": "open_app", "page": "C:\\evil"}, Path(tmp) / "ep.json", config={})
            self.assertEqual(bad["error_code"], "invalid_field")
            service.stop()
            win.queue_mgr.shutdown()
            win.deleteLater()
            QCoreApplication.processEvents()
            db.close()


# ===========================================================================
# Native host
# ===========================================================================
class NativeHostCaptureTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.endpoint = Path(self._tmp.name) / "endpoint.json"

    def tearDown(self):
        self._tmp.cleanup()

    def msg(self, **over):
        m = dict(prepare_payload(), action="prepare_capture")
        m.update(over)
        return m

    def test_host_rejects_unsafe_capture_before_ipc(self):
        with MockAppServer() as app_srv:
            proto.write_endpoint_atomic(self.endpoint, app_srv.port, app_srv.token)
            for over, code in (({"method": "POST"}, "unsupported_method"), ({"method": None}, "unsupported_method"),
                               ({"danger": "uncommon"}, "dangerous_download"), ({"incognito": True}, "incognito_excluded"),
                               ({"url": "file:///C:/x.zip"}, "unsupported_scheme"),
                               ({"filename": "C:\\Windows\\x.exe"}, "invalid_field"),
                               ({"browser_download_id": -1}, "invalid_field")):
                r = host.handle_message(self.msg(**over), self.endpoint, config={})
                self.assertEqual(r["error_code"], code, over)
            self.assertEqual(app_srv.received, [], "nothing unsafe reached the application")

    def test_host_forwards_with_fresh_ipc_request_ids(self):
        with MockAppServer() as app_srv:
            proto.write_endpoint_atomic(self.endpoint, app_srv.port, app_srv.token)
            host.handle_message(self.msg(), self.endpoint, config={})
            host.handle_message({"action": "capture_status", "request_id": "capture-req-0001"}, self.endpoint, config={})
            host.handle_message({"action": "capture_status", "request_id": "capture-req-0001"}, self.endpoint, config={})
        envelopes = app_srv.received
        self.assertEqual([e["action"] for e in envelopes], ["prepare_capture", "capture_status", "capture_status"])
        self.assertEqual(len({e["request_id"] for e in envelopes}), 3, "never answered from the replay cache")
        self.assertTrue(all(e["payload"]["request_id"] == "capture-req-0001" for e in envelopes))
        self.assertEqual(envelopes[0]["payload"]["method"], "GET")

    def test_host_capture_unsupported_fields(self):
        r = host.handle_message({"action": "commit_capture", "request_id": "capture-req-0001", "url": URL,
                                 "cookie": "sid=1"}, self.endpoint, config={})
        self.assertEqual(r["error_code"], "invalid_field")
        r = host.handle_message({"action": "set_capture_mode", "mode": "always"}, self.endpoint, config={})
        self.assertEqual(r["error_code"], "invalid_field")

    def test_capture_when_app_unavailable(self):
        r = host.handle_message(self.msg(), self.endpoint, config={})
        self.assertEqual(r["error_code"], "app_not_running")


if __name__ == "__main__":
    unittest.main()
