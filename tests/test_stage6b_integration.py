"""
Stage 6B end-to-end integration tests against a LOCAL HTTP server.

Real chain, nothing mocked between the browser boundary and the disk:
  native host handle_message()  (what the extension sends)
    -> authenticated localhost IPC -> BrowserIntegrationService
    -> CaptureManager (real cookie-less probe, real SQLite)
    -> QueueManager -> DownloadWorker -> existing download engines -> file on disk

The browser side of the handshake (pause/cancel) is simulated by calling the
same native-host actions the extension uses, in the same order.
"""
import hashlib
import logging
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "native_host"))

from mdm.browser_integration import capture_rules as rules  # noqa: E402
from mdm.browser_integration.service import BrowserIntegrationService  # noqa: E402
from mdm.database import DatabaseManager  # noqa: E402
from mdm.models import DownloadStatus  # noqa: E402
from mdm.queue_manager import QueueManager  # noqa: E402
import host  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

SMALL = os.urandom(64 * 1024)
LARGE = os.urandom(6 * 1024 * 1024 + 123)
REPORT = b"%PDF-1.7\n" + os.urandom(20_000)
STREAM = os.urandom(150_000)
SIGNATURE = "abc/def=="


def pump_until(predicate, timeout=30.0):
    deadline = time.time() + timeout
    while not predicate():
        if time.time() > deadline:
            raise AssertionError("Timed out waiting for condition")
        QCoreApplication.processEvents()
        time.sleep(0.005)


def in_thread(fn, timeout=30.0):
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


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, status, body=b"", headers=None):
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        if "Content-Length" not in (headers or {}):
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _serve(self, data, ctype="application/octet-stream", extra=None):
        headers = {"Content-Type": ctype, "Accept-Ranges": "bytes", **(extra or {})}
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            start_s, _, end_s = rng[6:].partition("-")
            start = int(start_s)
            end = min(int(end_s) if end_s else len(data) - 1, len(data) - 1)
            headers["Content-Range"] = f"bytes {start}-{end}/{len(data)}"
            return self._send(206, data[start:end + 1], headers)
        return self._send(200, data, headers)

    def do_GET(self):  # noqa: N802
        state = self.server.state
        parts = urlsplit(self.path)
        q = parse_qs(parts.query)
        state["requests"].append((self.command, self.path, self.headers.get("Range"), self.headers.get("Cookie"),
                                  self.headers.get("User-Agent") or ""))
        path = parts.path
        if path == "/files/small.bin":
            return self._serve(SMALL)
        if path == "/files/large.bin":
            return self._serve(LARGE)
        if path == "/redirect":
            return self._send(302, b"", {"Location": "/files/small.bin"})
        if path == "/report":
            return self._serve(REPORT, "application/pdf",
                               {"Content-Disposition": 'attachment; filename="Quarterly Report.pdf"'})
        if path == "/stream.bin":
            # Unknown size: no Content-Length, no ranges, body ends when the connection closes.
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(STREAM)
            self.close_connection = True
            return None
        if path == "/signed/build.zip":
            if q.get("X-Amz-Signature") != [SIGNATURE] or q.get("token") != ["t0p53cr3t"]:
                return self._send(403, b"bad signature")
            return self._serve(SMALL, "application/zip")
        if path == "/private.zip":
            if not self.headers.get("Cookie"):
                return self._send(403, b"login required")
            return self._serve(SMALL, "application/zip")
        if path == "/login.zip":
            return self._send(200, b"<html><body>Please sign in</body></html>", {"Content-Type": "text/html"})
        if path == "/expiring.zip":
            state["expiring_hits"] += 1
            if state["expiring_hits"] == 1:
                return self._serve(SMALL, "application/zip")  # the probe succeeds...
            return self._send(403, b"session expired")      # ...the real download does not
        return self._send(404, b"not found")

    def do_POST(self):  # noqa: N802
        self.server.state["requests"].append(("POST", self.path, None, None, ""))
        return self._send(405, b"")


class _QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass  # clients (the 1-byte probe) legitimately close early


class CaptureIntegrationTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.httpd = _QuietServer(("127.0.0.1", 0), _Handler)
        cls.httpd.state = {"requests": [], "expiring_hits": 0}
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.server_thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.httpd.state["requests"].clear()
        self.httpd.state["expiring_hits"] = 0
        self.endpoint = self.tmp / "endpoint.json"
        self.start_app()
        self._bdid = 100

    def start_app(self):
        self.db = DatabaseManager(self.tmp / "downloads.db")
        self.queue = QueueManager(db=self.db)
        self.service = BrowserIntegrationService(self.queue, folder_provider=lambda: self.tmp / "dl",
                                                 endpoint_path=self.endpoint)
        cfg = self.service.capture.get_config()
        cfg.update(enabled=True, mode="automatic", custom_extensions=["bin"])
        self.service.capture.update_config(cfg)
        self.assertTrue(self.service.start())

    def stop_app(self):
        self.service.stop()
        self.queue.shutdown(timeout_ms=10_000)
        self.service.deleteLater()
        self.queue.deleteLater()
        QCoreApplication.processEvents()
        self.db.close()

    def tearDown(self):
        self.stop_app()
        self._tmp.cleanup()

    # ---------------------------------------------------- browser emulation
    def native(self, msg):
        return in_thread(lambda: host.handle_message(msg, self.endpoint, config={}))

    def prepare(self, url, rid, filename=None, mime=None):
        self._bdid += 1
        return self.native({"action": "prepare_capture", "request_id": rid, "url": url, "filename": filename,
                            "mime": mime, "total_bytes": None, "referrer": "http://127.0.0.1/page",
                            "browser": "chrome", "browser_download_id": self._bdid,
                            "method": "GET", "danger": "safe", "incognito": False})

    def wait_decision(self, rid):
        deadline = time.time() + 30
        while time.time() < deadline:
            r = self.native({"action": "capture_status", "request_id": rid, "browser": "chrome"})
            if r["ok"] and r["data"]["state"] not in ("probing", "awaiting_user"):
                return r
            time.sleep(0.05)
        raise AssertionError("no decision")

    def capture(self, url, rid, **kw):
        """prepare -> (browser pauses) -> decision -> (browser cancels) -> commit."""
        p = self.prepare(url, rid, **kw)
        self.assertTrue(p["ok"], p)
        d = self.wait_decision(rid)
        if d["data"]["state"] != "approved":
            return d, None
        c = self.native({"action": "commit_capture", "request_id": rid, "url": url, "browser": "chrome"})
        self.assertTrue(c["ok"], c)
        return d, c

    def wait_download(self, download_id, timeout=60):
        pump_until(lambda: self.queue.get_item(download_id)["status"] in (DownloadStatus.COMPLETED, DownloadStatus.FAILED),
                   timeout)
        return self.queue.get_item(download_id)

    def assert_file(self, item, expected_bytes, name=None):
        self.assertEqual(item["status"], DownloadStatus.COMPLETED, item.get("error"))
        path = Path(item["final_path"])
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), hashlib.sha256(expected_bytes).hexdigest())
        if name:
            self.assertEqual(path.name, name)

    # ------------------------------------------------------------- cases
    def test_small_direct_download(self):
        d, c = self.capture(f"{self.base}/files/small.bin", "it-small-00001", filename="small.bin")
        self.assertEqual(d["data"]["total_bytes"], len(SMALL), "size from the cookie-less probe")
        item = self.wait_download(c["data"]["download_id"])
        self.assert_file(item, SMALL, "small.bin")
        probe = [r for r in self.httpd.state["requests"] if r[4].startswith("MyDownloadManager/")]
        self.assertEqual(len(probe), 1, "exactly one capture probe request")
        self.assertEqual(probe[0][2], "bytes=0-0", "the probe asks for a single byte")
        self.assertTrue(all(r[3] is None for r in self.httpd.state["requests"]), "no cookies are ever sent")

    def test_large_download_multi_connection(self):
        d, c = self.capture(f"{self.base}/files/large.bin", "it-large-00001", filename="large.bin")
        item = self.wait_download(c["data"]["download_id"], timeout=120)
        self.assert_file(item, LARGE, "large.bin")
        ranged = [r for r in self.httpd.state["requests"] if r[1] == "/files/large.bin" and r[2] and r[2] != "bytes=0-0"]
        self.assertGreater(len(ranged), 1, "existing multi-connection engine used")

    def test_redirected_download(self):
        d, c = self.capture(f"{self.base}/redirect", "it-redirect-001", filename="small.bin")
        self.assertEqual(d["data"]["state"], "approved")
        item = self.wait_download(c["data"]["download_id"])
        self.assert_file(item, SMALL)
        self.assertEqual(item["url"], f"{self.base}/redirect", "original URL kept; redirect followed by the engine")

    def test_content_disposition_filename(self):
        d, c = self.capture(f"{self.base}/report", "it-cd-00000001")  # no browser filename
        self.assertEqual(d["data"]["filename"], "Quarterly Report.pdf")
        item = self.wait_download(c["data"]["download_id"])
        self.assert_file(item, REPORT, "Quarterly Report.pdf")

    def test_unknown_file_size(self):
        d, c = self.capture(f"{self.base}/stream.bin", "it-unknown-0001", filename="stream.bin")
        self.assertIsNone(d["data"]["total_bytes"], "size unknown is reported as unknown")
        item = self.wait_download(c["data"]["download_id"])
        self.assert_file(item, STREAM, "stream.bin")

    def test_signed_url_query_parameters(self):
        url = f"{self.base}/signed/build.zip?X-Amz-Signature=abc%2Fdef%3D%3D&token=t0p53cr3t&X-Amz-Expires=300"
        with self.assertLogs("mdm", level="INFO") as logs:
            d, c = self.capture(url, "it-signed-00001", filename="build.zip")
            item = self.wait_download(c["data"]["download_id"])
        self.assert_file(item, SMALL, "build.zip")
        self.assertEqual(item["url"], url, "query string forwarded byte-for-byte")
        text = "\n".join(logs.output)
        for secret in ("abc%2Fdef", "t0p53cr3t", "X-Amz-Signature"):
            self.assertNotIn(secret, text, "signed parameters never logged")
        stored = repr(self.db.get_capture("it-signed-00001"))
        self.assertNotIn("t0p53cr3t", stored)

    def test_unsupported_request_methods(self):
        for method in ("POST", "PUT", None):
            msg = {"action": "prepare_capture", "request_id": f"it-method-{method or 'none'}0", "url": f"{self.base}/files/small.bin",
                   "filename": "small.bin", "browser": "chrome", "browser_download_id": 1, "method": method,
                   "danger": "safe", "incognito": False}
            r = self.native(msg)
            self.assertEqual(r["error_code"], "unsupported_method", method)
        self.assertEqual(self.httpd.state["requests"], [], "never replayed: the server saw nothing")
        self.assertEqual(self.queue.items, [])

    def test_auth_required_stays_in_browser(self):
        d, c = self.capture(f"{self.base}/private.zip", "it-private-0001", filename="private.zip")
        self.assertEqual((d["data"]["state"], d["data"]["reason"]), ("declined", "auth_required"))
        self.assertIsNone(c, "never committed: the browser resumes its own download")
        self.assertEqual(self.queue.items, [])
        d, _ = self.capture(f"{self.base}/login.zip", "it-login-00001", filename="login.zip", mime="application/zip")
        self.assertEqual(d["data"]["reason"], "html_response", "login page instead of the file")

    def test_browser_fallback_after_native_auth_failure(self):
        url = f"{self.base}/expiring.zip?sig=keepme"
        d, c = self.capture(url, "it-expiring-001", filename="expiring.zip")
        item = self.wait_download(c["data"]["download_id"])
        self.assertEqual(item["status"], DownloadStatus.FAILED)
        pump_until(lambda: self.db.get_capture("it-expiring-001")["state"] == "needs_browser")
        cfg = self.native({"action": "get_capture_config", "browser": "chrome", "capture_permissions": True})
        self.assertEqual([f["request_id"] for f in cfg["data"]["fallbacks"]], ["it-expiring-001"])
        claim = self.native({"action": "claim_fallback", "request_id": "it-expiring-001", "browser": "chrome"})
        self.assertEqual(claim["data"]["url"], url, "the browser gets the exact URL to download itself")
        again = self.prepare(url, "it-expiring-002", filename="expiring.zip")
        self.assertEqual(again["data"]["reason"], "browser_fallback", "the browser's own retry is not recaptured")

    def test_duplicate_requests(self):
        url = f"{self.base}/files/small.bin"
        d, c = self.capture(url, "it-dup-0000001", filename="small.bin")
        p2 = self.prepare(url, "it-dup-0000001", filename="small.bin")
        self.assertEqual(p2["status"], "duplicate")
        c2 = self.native({"action": "commit_capture", "request_id": "it-dup-0000001", "url": url, "browser": "chrome"})
        self.assertEqual(c2["status"], "duplicate")
        self.assertEqual(c2["download"]["id"], c["download"]["id"])
        self.wait_download(c["data"]["download_id"])
        self.assertEqual(len(self.queue.items), 1)
        self.assertEqual(len(self.db.get_all_downloads()), 1)
        self.assertEqual(len(list((self.tmp / "dl").iterdir())), 1, "one file on disk")

    def test_recovery_after_restart_between_approval_and_commit(self):
        url = f"{self.base}/files/small.bin"
        self.prepare(url, "it-restart-0001", filename="small.bin")
        self.assertEqual(self.wait_decision("it-restart-0001")["data"]["state"], "approved")
        self.stop_app()      # app closes after approval, before the browser commits
        self.start_app()
        c = self.native({"action": "commit_capture", "request_id": "it-restart-0001", "url": url, "browser": "chrome"})
        self.assertEqual(c["status"], "committed")
        self.assert_file(self.wait_download(c["data"]["download_id"]), SMALL)

    def test_application_unavailable(self):
        self.service.stop()
        r = self.prepare(f"{self.base}/files/small.bin", "it-down-000001", filename="small.bin")
        self.assertEqual(r["error_code"], "app_not_running")
        self.assertEqual(self.httpd.state["requests"], [])
        self.service.start()

    def test_ask_mode_with_user_approval_and_rejection(self):
        decisions = {}
        self.service.capture._approval_handler = lambda info, decide: decisions.setdefault(info["request_id"], decide)
        self.service.capture.handle_set_mode({"mode": "ask"})
        url = f"{self.base}/files/small.bin"
        self.prepare(url, "it-ask-yes-0001", filename="small.bin")
        self.prepare(url, "it-ask-no-00001", filename="small.bin")
        pump_until(lambda: len(decisions) == 2)
        decisions["it-ask-yes-0001"](True)
        decisions["it-ask-no-00001"](False)
        self.assertEqual(self.wait_decision("it-ask-no-00001")["data"]["reason"], "user_declined")
        self.assertEqual(self.wait_decision("it-ask-yes-0001")["data"]["state"], "approved")
        c = self.native({"action": "commit_capture", "request_id": "it-ask-yes-0001", "url": url, "browser": "chrome"})
        self.assert_file(self.wait_download(c["data"]["download_id"]), SMALL)
        refused = self.native({"action": "commit_capture", "request_id": "it-ask-no-00001", "url": url, "browser": "chrome"})
        self.assertEqual(refused["error_code"], "capture_not_approved")
        self.assertEqual(len(self.queue.items), 1)

    def test_existing_manual_and_queue_functionality_unaffected(self):
        r = self.native({"action": "add_download", "request_id": "it-manual-00001", "url": f"{self.base}/files/small.bin",
                         "filename": None, "referrer": None, "page_title": None, "browser": "edge"})
        self.assertEqual(r["status"], "accepted")
        item = self.queue.add_download(f"{self.base}/files/large.bin", str(self.tmp / "manual"), auto_start=True)
        self.assert_file(self.wait_download(r["download"]["id"]), SMALL)
        self.assert_file(self.wait_download(item["id"], timeout=120), LARGE)


if __name__ == "__main__":
    unittest.main()
