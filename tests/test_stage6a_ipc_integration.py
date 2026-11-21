"""
Stage 6A - authenticated localhost IPC server and application integration tests.

The real ``BrowserIpcServer`` (QTcpServer on the Qt main thread) is exercised
by blocking clients running in helper threads while the main thread pumps the
Qt event loop. Application tests use the real ``QueueManager`` with a
temporary SQLite database and automatic queue start disabled, so nothing is
downloaded and no public website is contacted.
"""
import json
import logging
import os
import secrets
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QThread
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "native_host"))

from mdm.browser_integration import ipc_protocol as proto  # noqa: E402
from mdm.browser_integration.ipc_server import BrowserIpcServer  # noqa: E402
from mdm.browser_integration.service import BrowserIntegrationService  # noqa: E402
from mdm.browser_integration.request_validator import ValidationError  # noqa: E402
from mdm.database import DatabaseManager  # noqa: E402
from mdm.queue_manager import QueueManager  # noqa: E402
from mdm.models import DownloadStatus  # noqa: E402
from mdm.settings import get_default_connections, get_default_priority, get_max_simultaneous_downloads  # noqa: E402
import host  # noqa: E402  (native_host/host.py)
from verify_host import MockAppServer  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)


def pump_until(predicate, timeout=10.0):
    deadline = time.time() + timeout
    while not predicate():
        if time.time() > deadline:
            raise AssertionError("Timed out waiting for condition")
        QCoreApplication.processEvents()
        time.sleep(0.002)


def in_thread(fn, timeout=10.0):
    """Runs a blocking client in a helper thread while the Qt loop keeps serving."""
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


def raw_exchange(port, payload: bytes, read_timeout=5.0):
    """Sends raw bytes and returns the decoded response frame (or None if closed)."""
    with socket.create_connection(("127.0.0.1", port), timeout=3) as s:
        s.settimeout(read_timeout)
        s.sendall(payload)
        try:
            return proto.recv_frame(s)
        except proto.ProtocolError:
            return None


class _ServerTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.endpoint_path = Path(self._tmp.name) / "browser_integration" / "endpoint.json"
        self.calls = []
        self.threads_seen = []

    def tearDown(self):
        server = getattr(self, "server", None)
        if server is not None:
            server.stop()
            server.deleteLater()
        QCoreApplication.processEvents()
        self._tmp.cleanup()

    def make_server(self, handler=None, **kwargs):
        def default_handler(action, payload):
            self.calls.append((action, payload))
            self.threads_seen.append(QThread.currentThread())
            if action == "ping":
                return "ok", {"version": "test"}, "Connected"
            if action == "add_download":
                return "accepted", {"download_id": f"id{len(self.calls):06d}", "filename": "f.zip"}, "Added"
            raise ValidationError("unsupported_action", "Unsupported action.")

        self.server = BrowserIpcServer(handler or default_handler, endpoint_path=self.endpoint_path, **kwargs)
        self.assertTrue(self.server.start())
        self.endpoint = proto.read_endpoint(self.endpoint_path)
        return self.server

    def request(self, action, request_id=None, payload=None, token=None, **kw):
        ep = proto.Endpoint(self.endpoint.port, token if token is not None else self.endpoint.token)
        rid = request_id or secrets.token_hex(8)
        return in_thread(lambda: proto.send_request(ep, action, rid, payload or {}, **kw))


# ---------------------------------------------------------------------------
class IpcServerSecurityTests(_ServerTestBase):

    def test_valid_authentication(self):
        self.make_server()
        r = self.request("ping")
        self.assertTrue(r["ok"])
        self.assertEqual(r["data"]["version"], "test")

    def test_invalid_authentication_missing_token(self):
        self.make_server()
        env = proto.build_request("x", "ping", "abcdefgh12")
        del env["token"]
        r = in_thread(lambda: raw_exchange(self.endpoint.port, proto.encode_frame(env)))
        self.assertFalse(r["ok"])
        self.assertEqual(r["error_code"], "unauthorized")
        self.assertEqual(self.calls, [])

    def test_wrong_token(self):
        self.make_server()
        for bad in ("", "x" * 43, self.endpoint.token[:-1] + ("A" if self.endpoint.token[-1] != "A" else "B")):
            r = self.request("add_download", payload={"url": "https://e.com/x"}, token=bad)
            self.assertEqual(r["error_code"], "unauthorized")
        self.assertEqual(self.calls, [], "unauthenticated requests must never reach the handler")

    def test_constant_time_comparison_used(self):
        import mdm.browser_integration.ipc_server as mod
        with patch.object(mod.hmac, "compare_digest", wraps=mod.hmac.compare_digest) as cd:
            self.make_server()
            self.request("ping")
        self.assertTrue(cd.called)

    def test_invalid_request_schema(self):
        self.make_server()
        tok = self.endpoint.token
        bad_envelopes = [
            dict(proto.build_request(tok, "ping", "abcdefgh01"), extra="x"),
            dict(proto.build_request(tok, "ping", "abcdefgh02"), v=99),
            dict(proto.build_request(tok, "ping", "bad id!")),
            dict(proto.build_request(tok, "ping", "abcdefgh03"), ts="now"),
            dict(proto.build_request(tok, "ping", "abcdefgh04"), payload=[1]),
            dict(proto.build_request(tok, "shell", "abcdefgh05")),
        ]
        codes = [in_thread(lambda e=e: raw_exchange(self.endpoint.port, proto.encode_frame(e)))["error_code"]
                 for e in bad_envelopes]
        self.assertEqual(codes, ["invalid_message", "unsupported_version", "invalid_field",
                                 "invalid_field", "invalid_field", "unsupported_action"])
        self.assertEqual(self.calls, [])

    def test_stale_timestamp_rejected(self):
        self.make_server()
        env = proto.build_request(self.endpoint.token, "ping", "abcdefgh99", ts=time.time() - 3600)
        r = in_thread(lambda: raw_exchange(self.endpoint.port, proto.encode_frame(env)))
        self.assertEqual(r["error_code"], "stale_request")

    def test_malformed_json_and_oversized_frames(self):
        self.make_server()
        bad = b"{nope"
        r = in_thread(lambda: raw_exchange(self.endpoint.port, struct.pack("<I", len(bad)) + bad))
        self.assertEqual(r["error_code"], "invalid_json")
        r = in_thread(lambda: raw_exchange(self.endpoint.port, struct.pack("<I", proto.MAX_MESSAGE_SIZE + 1)))
        self.assertEqual(r["error_code"], "message_too_large")
        r = in_thread(lambda: raw_exchange(self.endpoint.port, struct.pack("<I", 0)))
        self.assertEqual(r["error_code"], "invalid_frame")

    def test_fragmented_request_accepted(self):
        self.make_server()
        data = proto.encode_frame(proto.build_request(self.endpoint.token, "ping", "fragment01"))

        def client():
            with socket.create_connection(("127.0.0.1", self.endpoint.port), timeout=3) as s:
                s.settimeout(5)
                for i in range(0, len(data), 7):
                    s.sendall(data[i:i + 7])
                    time.sleep(0.001)
                return proto.recv_frame(s)

        self.assertTrue(in_thread(client)["ok"])

    def test_request_deduplication(self):
        self.make_server()
        payload = {"url": "https://example.com/x.zip"}
        first = self.request("add_download", "dup-request-1", payload)
        second = self.request("add_download", "dup-request-1", payload)
        self.assertEqual(first["status"], "accepted")
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(first["data"]["download_id"], second["data"]["download_id"])
        self.assertEqual(len(self.calls), 1)

    def test_duplicate_request_id_concurrent(self):
        """Two connections with the same request_id before the first is handled."""
        self.make_server()
        env = proto.build_request(self.endpoint.token, "add_download", "concurrent-1", {"url": "https://e.com/x"})
        frame = proto.encode_frame(env)
        socks = []

        def open_and_send():
            for _ in range(2):
                s = socket.create_connection(("127.0.0.1", self.endpoint.port), timeout=3)
                s.settimeout(5)
                s.sendall(frame)
                socks.append(s)

        in_thread(open_and_send)
        responses = in_thread(lambda: [proto.recv_frame(s) for s in socks])
        for s in socks:
            s.close()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(sorted(r["status"] for r in responses), ["accepted", "duplicate"])

    def test_failed_requests_are_not_cached(self):
        attempts = []

        def flaky(action, payload):
            attempts.append(1)
            if len(attempts) == 1:
                raise ValidationError("queue_rejected", "temporary")
            return "accepted", {"download_id": "ok000001"}, "Added"

        self.make_server(flaky)
        self.assertFalse(self.request("add_download", "retry-me-01")["ok"])
        self.assertEqual(self.request("add_download", "retry-me-01")["status"], "accepted")

    def test_handler_runs_on_main_thread(self):
        self.make_server()
        self.request("ping")
        self.request("add_download", payload={})
        self.assertTrue(self.threads_seen)
        self.assertTrue(all(t is app.thread() for t in self.threads_seen))

    def test_socket_timeout_on_incomplete_request(self):
        self.make_server(request_timeout_ms=300)
        partial = proto.encode_frame(proto.build_request(self.endpoint.token, "ping", "partial01"))[:10]
        start = time.time()
        r = in_thread(lambda: raw_exchange(self.endpoint.port, partial, read_timeout=5))
        self.assertEqual(r["error_code"], "timeout")
        self.assertLess(time.time() - start, 3)
        pump_until(lambda: self.server.active_connection_count == 0)

    def test_connection_limit(self):
        self.make_server(max_connections=2)
        held = in_thread(lambda: [socket.create_connection(("127.0.0.1", self.endpoint.port), timeout=3)
                                  for _ in range(2)])
        pump_until(lambda: self.server.active_connection_count == 2)
        try:
            def third():
                with socket.create_connection(("127.0.0.1", self.endpoint.port), timeout=3) as s:
                    s.settimeout(3)
                    return s.recv(1)
            try:
                data = in_thread(third)
            except (ConnectionResetError, ConnectionAbortedError):
                data = b""
            self.assertEqual(data, b"", "excess connection must be closed")
        finally:
            for s in held:
                s.close()

    def test_localhost_only_binding(self):
        self.make_server()
        self.assertEqual(self.server.server_address, "127.0.0.1")
        data = json.loads(self.endpoint_path.read_text())
        self.assertEqual(data["host"], "127.0.0.1")
        try:
            lan_ip = next(ip for ip in socket.gethostbyname_ex(socket.gethostname())[2]
                          if not ip.startswith("127."))
        except (StopIteration, OSError):
            self.skipTest("No non-loopback interface available")

        def connect_lan():
            try:
                socket.create_connection((lan_ip, self.endpoint.port), timeout=1).close()
                return True
            except OSError:
                return False

        self.assertFalse(in_thread(connect_lan), "server must not be reachable on non-loopback interfaces")

    def test_endpoint_file_contents_and_token_strength(self):
        self.make_server()
        data = json.loads(self.endpoint_path.read_text())
        self.assertEqual(data["port"], self.server.port)
        self.assertGreaterEqual(len(data["token"]), 43)  # 32 random bytes, urlsafe base64
        self.assertEqual(data["pid"], os.getpid())

    def test_token_regenerated_per_start(self):
        self.make_server()
        first = self.endpoint.token
        self.server.stop()
        self.assertTrue(self.server.start())
        self.assertNotEqual(proto.read_endpoint(self.endpoint_path).token, first)

    def test_token_never_logged(self):
        with self.assertLogs("mdm.browser_integration", level="DEBUG") as logs:
            self.make_server()
            self.request("ping")
            self.request("ping", token="w" * 43)
            logging.getLogger("mdm.browser_integration").debug("marker")
        self.assertNotIn(self.endpoint.token, "\n".join(logs.output))

    def test_application_shutdown_removes_endpoint_and_closes_port(self):
        self.make_server()
        port = self.endpoint.port
        self.server.stop()
        self.assertFalse(self.endpoint_path.exists())
        self.assertFalse(self.server.is_listening)
        with self.assertRaises(proto.ProtocolError) as ctx:
            in_thread(lambda: proto.send_request(proto.Endpoint(port, self.endpoint.token), "ping", "after-stop1",
                                                 connect_timeout=5))  # Windows refusal takes ~2 s
        self.assertEqual(ctx.exception.code, "app_not_running")

    def test_shutdown_during_inflight_request(self):
        """Request accepted by the socket but app stops before dispatch: no handler call, clean error."""
        self.make_server()
        sock = in_thread(lambda: socket.create_connection(("127.0.0.1", self.endpoint.port), timeout=3))
        sock.settimeout(3)
        sock.sendall(proto.encode_frame(proto.build_request(self.endpoint.token, "add_download", "inflight01")))
        pump_until(lambda: self.server._in_flight)  # received + queued, not yet dispatched
        self.server.stop()
        QCoreApplication.processEvents()

        def read():
            try:
                return sock.recv(4)
            except OSError:
                return b""
        self.assertEqual(in_thread(read), b"")
        sock.close()
        self.assertEqual(self.calls, [])

    def test_stale_endpoint_file_replaced(self):
        self.endpoint_path.parent.mkdir(parents=True)
        proto.write_endpoint_atomic(self.endpoint_path, 1, "s" * 43)
        self.make_server()
        self.assertNotEqual(self.endpoint.port, 1)

    def test_does_not_hijack_live_instance(self):
        with MockAppServer() as other:
            self.endpoint_path.parent.mkdir(parents=True)
            proto.write_endpoint_atomic(self.endpoint_path, other.port, other.token)
            server = BrowserIpcServer(lambda a, p: ("ok", {}, ""), endpoint_path=self.endpoint_path)
            started = server.start()  # the mock answers from its own thread
            self.assertFalse(started)
            self.assertEqual(proto.read_endpoint(self.endpoint_path).token, other.token)
            server.stop()
            self.assertTrue(self.endpoint_path.exists(), "must not delete another instance's endpoint")


# ---------------------------------------------------------------------------
class BrowserIntegrationServiceTests(unittest.TestCase):
    """Full path: native host handler -> IPC -> service -> real QueueManager."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.db = DatabaseManager(tmp / "test.db")
        self.queue = QueueManager(db=self.db)
        self.queue.is_auto_queue_running = False  # never start real network downloads
        self.download_dir = tmp / "Downloads"
        self.endpoint_path = tmp / "endpoint.json"
        self.service = BrowserIntegrationService(
            self.queue, folder_provider=lambda: self.download_dir, endpoint_path=self.endpoint_path,
            show_window=self._show_window,
        )
        self.shown = 0
        self.added_signals = []
        self.queue.item_added.connect(lambda item: self.added_signals.append(item))
        self.assertTrue(self.service.start())

    def tearDown(self):
        self.service.stop()
        self.queue.shutdown()
        self.service.deleteLater()
        self.queue.deleteLater()
        QCoreApplication.processEvents()
        self.db.close()
        self._tmp.cleanup()

    def _show_window(self):
        self.shown += 1

    def host_send(self, message):
        return in_thread(lambda: host.handle_message(message, self.endpoint_path, config={}))

    def add(self, url, **kw):
        msg = {"action": "add_download", "request_id": kw.pop("request_id", secrets.token_hex(8)),
               "url": url, "filename": None, "referrer": None, "page_title": None, "browser": "chrome"}
        msg.update(kw)
        return self.host_send(msg)

    def test_valid_url_enters_existing_queue(self):
        r = self.add("https://downloads.example.test/pkg/tool-1.2.3.zip?mirror=eu&sig=a%2Fb")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["status"], "accepted")
        item = self.queue.get_item(r["download"]["id"])
        self.assertIsNotNone(item)
        self.assertEqual(item["url"], "https://downloads.example.test/pkg/tool-1.2.3.zip?mirror=eu&sig=a%2Fb")
        self.assertEqual(item["status"], DownloadStatus.QUEUED)
        self.assertEqual(item["filename"], "tool-1.2.3.zip")
        self.assertEqual(item["folder"], str(self.download_dir))
        self.assertEqual(len(self.added_signals), 1, "existing item_added signal drives the UI table")
        persisted = self.db.get_download(item["id"])
        self.assertIsNotNone(persisted, "persisted through the existing QueueManager -> DatabaseManager path")
        self.assertEqual(persisted.url, item["url"])

    def test_invalid_url_rejected(self):
        for url in ("javascript:alert(1)", "file:///C:/Windows/win.ini", "https://u:p@e.com/x", "not a url"):
            r = self.add(url)
            self.assertFalse(r["ok"], url)
        self.assertEqual(self.queue.items, [])
        self.assertEqual(self.db.get_all_downloads(), [])

    def test_service_revalidates_even_if_host_is_bypassed(self):
        ep = proto.read_endpoint(self.endpoint_path)
        r = in_thread(lambda: proto.send_request(ep, "add_download", "bypass-0001",
                                                 {"request_id": "bypass-0001", "url": "javascript:alert(1)",
                                                  "browser": "chrome"}))
        self.assertFalse(r["ok"])
        self.assertEqual(r["error_code"], "unsupported_scheme")
        r = in_thread(lambda: proto.send_request(ep, "add_download", "bypass-0002",
                                                 {"request_id": "bypass-0002", "url": "https://e.com/x",
                                                  "browser": "chrome", "filename": "..\\..\\evil.exe"}))
        self.assertFalse(r["ok"])
        self.assertEqual(r["error_code"], "invalid_field")
        self.assertEqual(self.queue.items, [])

    def test_filename_sanitisation_uses_existing_logic(self):
        r = self.add("https://e.test/download?id=1", filename="CON.txt")
        self.assertEqual(self.queue.get_item(r["download"]["id"])["filename"], "_CON.txt")
        r = self.add("https://e.test/download?id=2", filename='bad<name>|"x".zip')
        self.assertEqual(self.queue.get_item(r["download"]["id"])["filename"], "bad_name___x_.zip")
        r = self.add("https://e.test/files/My%20Report%20%282026%29.pdf?x=1")
        self.assertEqual(self.queue.get_item(r["download"]["id"])["filename"], "My Report (2026).pdf")
        r = self.add("https://e.test/a", filename="../../evil.exe")
        self.assertFalse(r["ok"])

    def test_existing_queue_settings_preserved(self):
        before = (get_default_connections(), get_default_priority(), get_max_simultaneous_downloads(),
                  self.queue.max_simultaneous_downloads)
        r = self.add("https://e.test/big.iso")
        item = self.queue.get_item(r["download"]["id"])
        self.assertEqual(item["connections"], get_default_connections())
        self.assertEqual(item["priority"], self.queue.default_priority)
        self.assertEqual(item["max_retry_attempts"], self.queue.max_retry_attempts)
        self.assertFalse(item["category_manual"], "category comes from the existing auto-classification")
        after = (get_default_connections(), get_default_priority(), get_max_simultaneous_downloads(),
                 self.queue.max_simultaneous_downloads)
        self.assertEqual(before, after)

    def test_no_duplicate_download(self):
        msg = dict(request_id="same-request-0001")
        first = self.add("https://e.test/one.zip", **msg)
        second = self.add("https://e.test/one.zip", **msg)
        self.assertEqual(first["status"], "accepted")
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(first["download"]["id"], second["download"]["id"])
        self.assertEqual(len(self.queue.items), 1)
        self.assertEqual(len(self.db.get_all_downloads()), 1)

    def test_browser_metadata_preservation(self):
        r = self.add("https://e.test/a.zip", browser="edge", referrer="https://e.test/page?q=1",
                     page_title="Release\tdownloads")
        item = self.queue.get_item(r["download"]["id"])
        self.assertEqual(item["source"], "edge")
        self.assertEqual(item["source_label"], "Microsoft Edge")
        self.assertEqual(item["referrer"], "https://e.test/page?q=1")
        self.assertEqual(item["page_title"], "Release downloads")

    def test_ui_thread_safety(self):
        seen = []
        original = self.queue.add_download

        def spy(*a, **kw):
            seen.append(QThread.currentThread())
            return original(*a, **kw)

        with patch.object(self.queue, "add_download", side_effect=spy):
            self.assertTrue(self.add("https://e.test/t.zip")["ok"])
        self.assertEqual(seen, [app.thread()])

        box = {}

        def call_from_worker():
            try:
                self.service.handle_request("ping", {})
            except RuntimeError as exc:
                box["err"] = exc
        t = threading.Thread(target=call_from_worker)
        t.start()
        t.join()
        self.assertIn("main thread", str(box.get("err")))

    def test_queue_rejection_reported(self):
        with patch.object(self.queue, "add_download", side_effect=RuntimeError("boom")):
            r = self.add("https://e.test/x.zip")
        self.assertFalse(r["ok"])
        self.assertEqual(r["error_code"], "queue_rejected")
        self.assertNotIn("boom", r["message"])

    def test_ping_and_show_window(self):
        r = self.host_send({"action": "ping"})
        self.assertEqual(r["status"], "connected")
        r = self.host_send({"action": "open_app"})
        self.assertTrue(r["ok"])
        self.assertEqual(self.shown, 1)

    def test_application_unavailable_after_shutdown(self):
        self.service.stop()
        r = self.add("https://e.test/x.zip")
        self.assertEqual(r["error_code"], "app_not_running")
        self.assertEqual(self.queue.items, [])


# ---------------------------------------------------------------------------
class MainWindowIntegrationTests(unittest.TestCase):

    def setUp(self):
        from mdm.main_window import MainWindow
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.db = DatabaseManager(tmp / "win.db")
        self.win = MainWindow(db=self.db)
        self.win.queue_mgr.is_auto_queue_running = False
        self.endpoint_path = tmp / "endpoint.json"
        self.service = BrowserIntegrationService(
            self.win.queue_mgr, folder_provider=lambda: tmp / "dl", endpoint_path=self.endpoint_path,
            show_window=self.win.bring_to_front, parent=self.win,
        )
        self.win.attach_browser_integration(self.service)
        self.assertTrue(self.service.start())
        self.win.update_browser_status()

    def tearDown(self):
        self.service.stop()
        self.win.queue_mgr.shutdown()
        self.win.deleteLater()
        QCoreApplication.processEvents()
        self.db.close()
        self._tmp.cleanup()

    def test_download_appears_in_existing_table(self):
        rows_before = self.win.table.rowCount()
        r = in_thread(lambda: host.handle_message(
            {"action": "add_download", "request_id": "window-test-01", "url": "https://e.test/video.mp4",
             "browser": "chrome"}, self.endpoint_path, config={}))
        self.assertTrue(r["ok"])
        QCoreApplication.processEvents()
        self.assertEqual(self.win.table.rowCount(), rows_before + 1)
        names = [self.win.table.item(i, 0).text() for i in range(self.win.table.rowCount())
                 if self.win.table.item(i, 0)]
        self.assertTrue(any("video.mp4" in n for n in names), names)
        self.assertIn("Google Chrome", self.win.browser_status_label.text())

    def test_status_indicator(self):
        self.assertTrue(self.win.browser_status_label.isVisibleTo(self.win))
        self.assertIn("ready", self.win.browser_status_label.text())

    def test_close_event_stops_service_and_removes_endpoint(self):
        self.assertTrue(self.endpoint_path.exists())
        self.win.close()
        QCoreApplication.processEvents()
        self.assertFalse(self.service.is_running)
        self.assertFalse(self.endpoint_path.exists())

    def test_close_vetoed_restarts_browser_integration(self):
        with patch.object(self.win.queue_mgr, "is_busy", return_value=True), \
                patch.object(self.win.queue_mgr, "shutdown", return_value=False), \
                patch("mdm.main_window.QMessageBox.warning"):
            self.win.close()
        QCoreApplication.processEvents()
        self.assertTrue(self.service.is_running, "app stayed open, so integration must resume")
        self.assertTrue(self.endpoint_path.exists())


if __name__ == "__main__":
    unittest.main()
