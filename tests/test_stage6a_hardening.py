"""
Stage 6A final hardening tests.

1. Bounded IPC memory: capped QTcpSocket read buffer + pull-based frame reader.
2. Durable request idempotency: SQLite ``browser_requests`` table (schema v3),
   committed atomically with the download row; survives restart.
3. Signed URLs are forwarded intact but never stored in the idempotency table
   or written to logs.

Everything runs against loopback sockets, temporary SQLite databases and a
mocked/real-in-process application. No public website is contacted and no
real download is started (automatic queue start is disabled).
"""
import hashlib
import logging
import secrets
import socket
import sqlite3
import struct
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "native_host"))

from mdm.browser_integration import ipc_protocol as proto  # noqa: E402
from mdm.browser_integration.ipc_server import BrowserIpcServer, READ_BUFFER_SIZE  # noqa: E402
from mdm.browser_integration.service import BrowserIntegrationService  # noqa: E402
from mdm.database import (  # noqa: E402
    DatabaseManager, CURRENT_SCHEMA_VERSION, BROWSER_REQUEST_RETENTION_DAYS,
)
from mdm.models import DownloadModel, DownloadStatus  # noqa: E402
from mdm.queue_manager import QueueManager  # noqa: E402
import host  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

MAX = proto.MAX_MESSAGE_SIZE


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


def try_recv_frame(sock):
    """Returns the response frame, or None if the server reset the connection first."""
    try:
        return proto.recv_frame(sock)
    except (proto.ProtocolError, OSError):
        return None


# ===========================================================================
# 1. Bounded frame reader (pure Python)
# ===========================================================================
class BoundedFrameReaderTests(unittest.TestCase):

    def test_one_byte_fragments_and_bounded_buffer(self):
        frame = proto.encode_frame({"action": "ping", "pad": "x" * 5000})
        reader = proto.BoundedFrameReader()
        peak, result = 0, None
        for i in range(len(frame)):
            self.assertGreaterEqual(reader.bytes_needed(), 1)
            result = reader.feed(frame[i:i + 1])
            peak = max(peak, reader.buffered_bytes)
        self.assertEqual(result["pad"], "x" * 5000)
        self.assertLessEqual(peak, len(frame))
        self.assertEqual(reader.buffered_bytes, 0, "buffer released after a complete frame")

    def test_oversized_rejected_on_header_before_any_body_byte(self):
        reader = proto.BoundedFrameReader()
        header = struct.pack("<I", MAX + 1)
        for b in header[:3]:
            self.assertIsNone(reader.feed(bytes([b])))
        with self.assertRaises(proto.ProtocolError) as ctx:
            reader.feed(header[3:])
        self.assertEqual(ctx.exception.code, "message_too_large")
        self.assertLessEqual(reader.buffered_bytes, proto.HEADER_SIZE)

    def test_zero_length_rejected(self):
        with self.assertRaises(proto.ProtocolError) as ctx:
            proto.BoundedFrameReader().feed(struct.pack("<I", 0))
        self.assertEqual(ctx.exception.code, "invalid_frame")

    def test_never_accepts_more_than_needed(self):
        reader = proto.BoundedFrameReader()
        with self.assertRaises(ValueError):
            reader.feed(b"\x01\x00\x00\x00\x7b")  # header + 1 extra byte

    def test_exact_maximum_size_accepted(self):
        body = b'"' + b"a" * (MAX - 2) + b'"'
        reader = proto.BoundedFrameReader()
        self.assertIsNone(reader.feed(struct.pack("<I", len(body))))
        self.assertEqual(reader.bytes_needed(), MAX)
        with self.assertRaises(proto.ProtocolError):  # a JSON string, not an object
            reader.feed(body)
        reader = proto.BoundedFrameReader()
        obj_body = b'{"p":"' + b"a" * (MAX - 8) + b'"}'
        self.assertEqual(len(obj_body), MAX)
        reader.feed(struct.pack("<I", MAX))
        self.assertEqual(len(reader.feed(obj_body)["p"]), MAX - 8)


# ===========================================================================
# 1b. Bounded memory on the real QTcpServer
# ===========================================================================
class IpcMemoryBoundTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.endpoint_path = Path(self._tmp.name) / "endpoint.json"
        self.calls = []

    def tearDown(self):
        self.server.stop()
        self.server.deleteLater()
        QCoreApplication.processEvents()
        self._tmp.cleanup()

    def make_server(self, **kw):
        def handler(action, payload):
            self.calls.append(action)
            return "ok", {"echo_len": len(str(payload))}, "ok"

        self.server = BrowserIpcServer(handler, endpoint_path=self.endpoint_path, **kw)
        self.assertTrue(self.server.start())
        self.ep = proto.read_endpoint(self.endpoint_path)
        return self.server

    def connect(self):
        s = socket.create_connection(("127.0.0.1", self.ep.port), timeout=5)
        s.settimeout(10)
        return s

    def max_size_envelope(self):
        env = proto.build_request(self.ep.token, "ping", "maxsize-0001", {"pad": ""})
        base = len(proto.encode_frame(env)) - proto.HEADER_SIZE
        env["payload"]["pad"] = "x" * (MAX - base)
        frame = proto.encode_frame(env)
        self.assertEqual(len(frame) - proto.HEADER_SIZE, MAX)
        return frame

    def test_read_buffer_is_capped(self):
        self.make_server()
        self.assertEqual(self.server.read_buffer_size, READ_BUFFER_SIZE)
        held = in_thread(self.connect)
        try:
            pump_until(lambda: self.server.active_connection_count == 1)
            conn = next(iter(self.server._connections.values()))
            self.assertEqual(conn.socket.readBufferSize(), READ_BUFFER_SIZE)
        finally:
            held.close()

    def test_valid_maximum_size_message(self):
        self.make_server()
        frame = self.max_size_envelope()

        def client():
            with self.connect() as s:
                s.sendall(frame)
                return proto.recv_frame(s)

        r = in_thread(client)
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.calls, ["ping"])
        self.assertLessEqual(self.server.peak_connection_bytes, self.server.max_bytes_per_connection)

    def test_message_exceeding_maximum_size_rejected_promptly(self):
        self.make_server()

        def client():
            with self.connect() as s:
                start = time.time()
                s.sendall(struct.pack("<I", MAX + 1))  # header only - no body ever sent
                r = proto.recv_frame(s)
                return r, time.time() - start

        r, elapsed = in_thread(client)
        self.assertEqual(r["error_code"], "message_too_large")
        self.assertLess(elapsed, 2.0, "must not wait for the body or the connection timeout")
        self.assertEqual(self.calls, [])
        self.assertLessEqual(self.server.peak_connection_bytes, proto.HEADER_SIZE)

    def test_oversized_header_sent_in_fragments(self):
        self.make_server()

        def client():
            with self.connect() as s:
                for b in struct.pack("<I", 0x7FFFFFFF):
                    s.sendall(bytes([b]))
                    time.sleep(0.01)
                return proto.recv_frame(s)

        self.assertEqual(in_thread(client)["error_code"], "message_too_large")
        self.assertEqual(self.calls, [])

    def test_oversized_data_in_fragments_is_never_buffered(self):
        """Valid 1 MB header followed by 3 MB of data in 16 KB fragments: the server
        consumes exactly one frame's worth, rejects it, and never buffers the rest."""
        self.make_server()

        def client():
            with self.connect() as s:
                s.settimeout(5)
                try:
                    s.sendall(struct.pack("<I", MAX))
                    junk = b"\xff" * (16 * 1024)
                    for _ in range(3 * 64):
                        s.sendall(junk)
                except OSError:
                    pass  # server closed the connection after rejecting the frame
                return try_recv_frame(s)

        with self.assertLogs("mdm.browser_integration.ipc", level="INFO") as logs:
            r = in_thread(client)
            pump_until(lambda: self.server.active_connection_count == 0)
        self.assertIn("rejected: invalid_json", "\n".join(logs.output))
        if r is not None:
            self.assertEqual(r["error_code"], "invalid_json")
        self.assertEqual(self.calls, [])
        self.assertLessEqual(self.server.peak_connection_bytes, self.server.max_bytes_per_connection)

    def test_incomplete_large_message_times_out(self):
        self.make_server(request_timeout_ms=400)

        def client():
            with self.connect() as s:
                s.sendall(struct.pack("<I", 900_000) + b"{" + b"a" * 99_999)
                return proto.recv_frame(s)  # body never completed

        r = in_thread(client)
        self.assertEqual(r["error_code"], "timeout")
        pump_until(lambda: self.server.active_connection_count == 0)
        self.assertEqual(self.calls, [])
        self.assertLessEqual(self.server.peak_connection_bytes, 100_004 + READ_BUFFER_SIZE)

    def test_incomplete_header_times_out(self):
        self.make_server(request_timeout_ms=300)

        def client():
            with self.connect() as s:
                s.sendall(b"\x10\x00")
                return proto.recv_frame(s)

        self.assertEqual(in_thread(client)["error_code"], "timeout")
        pump_until(lambda: self.server.active_connection_count == 0)

    def test_zero_length_frame_rejected(self):
        self.make_server()

        def client():
            with self.connect() as s:
                s.sendall(struct.pack("<I", 0))
                return proto.recv_frame(s)

        self.assertEqual(in_thread(client)["error_code"], "invalid_frame")

    def test_multiple_simultaneous_oversized_connections(self):
        self.make_server(max_connections=16)
        n = 10

        def client():
            socks = [self.connect() for _ in range(n)]
            for s in socks:
                s.sendall(struct.pack("<I", MAX + 1 + len(socks)))
            out = [proto.recv_frame(s)["error_code"] for s in socks]
            for s in socks:
                s.close()
            return out

        self.assertEqual(in_thread(client), ["message_too_large"] * n)
        pump_until(lambda: self.server.active_connection_count == 0)
        self.assertEqual(self.calls, [])

    def test_memory_bounded_with_concurrent_streams(self):
        """Four peers each stream 3 MB at once; per-connection usage stays bounded."""
        self.make_server(request_timeout_ms=5000)
        payload = b"\x00" * (3 * 1024 * 1024)

        def flood():
            with self.connect() as s:
                s.settimeout(5)
                try:
                    s.sendall(struct.pack("<I", MAX) + payload)
                except OSError:
                    pass

        threads = [threading.Thread(target=flood, daemon=True) for _ in range(4)]
        for t in threads:
            t.start()
        pump_until(lambda: all(not t.is_alive() for t in threads), 20)
        pump_until(lambda: self.server.accepted_connection_count == 4
                   and self.server.active_connection_count == 0, 20)
        self.assertGreater(self.server.peak_connection_bytes, READ_BUFFER_SIZE // 2,
                           "the server really did read the streamed data")
        self.assertLessEqual(self.server.peak_connection_bytes, self.server.max_bytes_per_connection)
        self.assertLess(self.server.max_bytes_per_connection, 2 * MAX)
        self.assertEqual(self.calls, [])

    def test_valid_fragmented_message_after_changes(self):
        self.make_server()
        frame = proto.encode_frame(proto.build_request(self.ep.token, "ping", "fragmented-1", {"k": "v" * 3000}))

        def client():
            with self.connect() as s:
                for i in range(len(frame)):
                    s.sendall(frame[i:i + 1])
                return proto.recv_frame(s)

        r = in_thread(client)
        self.assertTrue(r["ok"])
        self.assertEqual(self.calls, ["ping"])

    def test_extra_bytes_after_frame_ignored(self):
        self.make_server()
        frame = proto.encode_frame(proto.build_request(self.ep.token, "ping", "trailing-01"))

        def client():
            with self.connect() as s:
                s.sendall(frame + b"\x00" * 200_000)
                return try_recv_frame(s)

        r = in_thread(client)
        self.assertEqual(self.calls, ["ping"], "exactly one request handled per connection")
        if r is not None:
            self.assertTrue(r["ok"])


# ===========================================================================
# 2. Durable idempotency
# ===========================================================================
SIGNED_URL = ("https://cdn.example.test/builds/tool-2.0.zip"
              "?X-Amz-Signature=SECRETSIG%2Fabc&X-Amz-Credential=AKIASECRET%2F20261121&token=t0p53cr3t")


class _StackMixin:
    """Builds DatabaseManager + QueueManager + BrowserIntegrationService on a file DB."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.db_path = self.tmp / "downloads.db"
        self.endpoint_path = self.tmp / "endpoint.json"
        self.stack = None

    def tearDown(self):
        self.stop_stack()
        self._tmp.cleanup()

    def start_stack(self, with_db=True):
        db = DatabaseManager(self.db_path) if with_db else None
        queue = QueueManager(db=db)
        queue.is_auto_queue_running = False
        service = BrowserIntegrationService(queue, folder_provider=lambda: self.tmp / "dl",
                                            endpoint_path=self.endpoint_path)
        self.assertTrue(service.start())
        self.stack = (db, queue, service)
        return self.stack

    def stop_stack(self):
        if not self.stack:
            return
        db, queue, service = self.stack
        service.stop()
        queue.shutdown()  # closes the database like the real application
        service.deleteLater()
        queue.deleteLater()
        QCoreApplication.processEvents()
        if db is not None:
            db.close()
        self.stack = None

    def restart(self):
        self.stop_stack()
        return self.start_stack()

    def send(self, url, request_id, **extra):
        msg = {"action": "add_download", "request_id": request_id, "url": url, "filename": None,
               "referrer": None, "page_title": None, "browser": "chrome"}
        msg.update(extra)
        return in_thread(lambda: host.handle_message(msg, self.endpoint_path, config={}))

    def raw_rows(self, sql):
        conn = sqlite3.connect(str(self.db_path))
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()


class PersistentIdempotencyTests(_StackMixin, unittest.TestCase):

    def test_first_request_accepted_and_recorded(self):
        db, queue, _ = self.start_stack()
        r = self.send(SIGNED_URL, "first-request-01")
        self.assertEqual(r["status"], "accepted")
        rec = db.get_browser_request("first-request-01")
        self.assertEqual(rec["download_id"], r["download"]["id"])
        self.assertEqual(rec["browser"], "chrome")
        self.assertEqual(rec["status"], "accepted")
        self.assertEqual(rec["url_sha256"], hashlib.sha256(SIGNED_URL.encode()).hexdigest())
        datetime.fromisoformat(rec["created_at"])
        self.assertEqual(queue.get_item(r["download"]["id"])["url"], SIGNED_URL,
                         "signed URL forwarded to the queue intact (query string preserved)")

    def test_signed_url_never_stored_in_idempotency_table(self):
        self.start_stack()
        self.send(SIGNED_URL, "signed-url-0001")
        dump = repr(self.raw_rows("SELECT * FROM browser_requests"))
        for secret in ("SECRETSIG", "AKIASECRET", "t0p53cr3t", "cdn.example.test"):
            self.assertNotIn(secret, dump)

    def test_signed_url_parameters_never_logged(self):
        with self.assertLogs("mdm", level="DEBUG") as logs:
            self.start_stack()
            self.send(SIGNED_URL, "signed-log-0001")
            self.send(SIGNED_URL, "signed-log-0001")
            logging.getLogger("mdm.browser_integration").debug("marker")
        text = "\n".join(logs.output)
        for secret in ("SECRETSIG", "AKIASECRET", "t0p53cr3t", "X-Amz"):
            self.assertNotIn(secret, text)

    def test_duplicate_returns_original_download_id(self):
        _, queue, service = self.start_stack()
        first = self.send(SIGNED_URL, "dup-original-01")
        service.server._completed.clear()  # bypass the in-memory fast path: exercise SQLite
        second = self.send(SIGNED_URL, "dup-original-01")
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(second["download"]["id"], first["download"]["id"])
        self.assertEqual(second["download"]["filename"], "tool-2.0.zip")
        self.assertEqual(len(queue.items), 1)

    def test_duplicate_after_application_restart(self):
        self.start_stack()
        first = self.send(SIGNED_URL, "restart-req-001")
        self.assertEqual(first["status"], "accepted")

        db, queue, _ = self.restart()
        self.assertEqual(len(queue.items), 1, "download reloaded from SQLite")
        second = self.send(SIGNED_URL, "restart-req-001")
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(second["download"]["id"], first["download"]["id"])
        self.assertEqual(len(queue.items), 1)
        self.assertEqual(len(db.get_all_downloads()), 1)

    def test_same_request_id_with_different_url(self):
        _, queue, service = self.start_stack()
        self.send("https://e.test/a.zip", "conflict-req-01")
        r = self.send("https://e.test/b.zip", "conflict-req-01")
        self.assertFalse(r["ok"])
        self.assertEqual(r["error_code"], "request_id_conflict")
        self.assertEqual(len(queue.items), 1)

        _, queue, _ = self.restart()
        r = self.send("https://e.test/c.zip", "conflict-req-01")
        self.assertEqual(r["error_code"], "request_id_conflict")
        self.assertEqual(len(queue.items), 1)

    def test_concurrent_duplicates_at_database_level(self):
        db = DatabaseManager(self.db_path)
        record = {"request_id": "race-request-01", "browser": "edge", "url_sha256": "0" * 64,
                  "status": "accepted", "created_at": datetime.now().isoformat()}
        barrier = threading.Barrier(2)
        outcomes = {}

        def writer(i):
            model = DownloadModel(id=f"race000{i}", url="https://e.test/x", destination_folder=str(self.tmp),
                                  filename="x", status=DownloadStatus.QUEUED)
            barrier.wait()
            try:
                db.add_download(model, browser_request=record)
                outcomes[i] = "ok"
            except sqlite3.IntegrityError:
                outcomes[i] = "integrity"

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(sorted(outcomes.values()), ["integrity", "ok"])
        winner = next(i for i, o in outcomes.items() if o == "ok")
        self.assertEqual([d.id for d in db.get_all_downloads()], [f"race000{winner}"],
                         "the losing download row must be rolled back")
        self.assertEqual(db.get_browser_request("race-request-01")["download_id"], f"race000{winner}")
        db.close()

    def test_concurrent_duplicates_at_service_level(self):
        """Lookup misses (another writer not yet committed), insert then collides:
        the service answers with the winner's download id and adds nothing."""
        db, queue, service = self.start_stack()
        winner = self.send("https://e.test/w.zip", "race-service-01")
        service.server._completed.clear()
        real_lookup = service._lookup_request
        calls = {"n": 0}

        def stale_first_lookup(rid):
            calls["n"] += 1
            return None if calls["n"] == 1 else real_lookup(rid)

        with patch.object(service, "_lookup_request", side_effect=stale_first_lookup):
            r = self.send("https://e.test/w.zip", "race-service-01")
        self.assertEqual(r["status"], "duplicate")
        self.assertEqual(r["download"]["id"], winner["download"]["id"])
        self.assertEqual(len(queue.items), 1)
        self.assertEqual(len(db.get_all_downloads()), 1)

    def test_concurrent_duplicate_connections(self):
        _, queue, service = self.start_stack()
        ep = proto.read_endpoint(self.endpoint_path)
        payload = {"request_id": "parallel-req-01", "url": "https://e.test/p.zip", "filename": None,
                   "referrer": None, "page_title": None, "browser": "chrome"}
        frame = proto.encode_frame(proto.build_request(ep.token, "add_download", "parallel-req-01", payload))

        def clients():
            socks = []
            for _ in range(5):
                s = socket.create_connection(("127.0.0.1", ep.port), timeout=5)
                s.settimeout(10)
                s.sendall(frame)
                socks.append(s)
            out = [proto.recv_frame(s) for s in socks]
            for s in socks:
                s.close()
            return out

        responses = in_thread(clients)
        self.assertEqual(sorted(r["status"] for r in responses), ["accepted"] + ["duplicate"] * 4)
        self.assertEqual(len({r["data"]["download_id"] for r in responses}), 1)
        self.assertEqual(len(queue.items), 1)

    def test_database_transaction_failure_rolls_back_download(self):
        db, queue, _ = self.start_stack()
        with patch.object(DatabaseManager, "_insert_browser_request",
                          side_effect=sqlite3.OperationalError("disk I/O error")):
            r = self.send("https://e.test/fail.zip", "tx-failure-001")
        self.assertFalse(r["ok"])
        self.assertEqual(r["error_code"], "storage_error")
        self.assertEqual(queue.items, [], "no in-memory item without a committed record")
        self.assertEqual(db.get_all_downloads(), [], "download row rolled back with the failed record")
        self.assertIsNone(db.get_browser_request("tx-failure-001"))

        r = self.send("https://e.test/fail.zip", "tx-failure-001")  # retry after recovery
        self.assertEqual(r["status"], "accepted")
        self.assertEqual(len(db.get_all_downloads()), 1)

    def test_integrity_failure_rolls_back_download_row(self):
        db = DatabaseManager(self.db_path)
        rec = {"request_id": "integrity-0001", "browser": "chrome", "url_sha256": "1" * 64}
        db.add_download(DownloadModel(id="keep0001", url="https://e.test/1", destination_folder=str(self.tmp),
                                      filename="1"), browser_request=rec)
        with self.assertRaises(sqlite3.IntegrityError):
            db.add_download(DownloadModel(id="drop0002", url="https://e.test/2", destination_folder=str(self.tmp),
                                          filename="2"), browser_request=rec)
        self.assertEqual([d.id for d in db.get_all_downloads()], ["keep0001"])
        db.close()

    def test_lookup_failure_refuses_request(self):
        db, queue, _ = self.start_stack()
        with patch.object(DatabaseManager, "get_browser_request", side_effect=sqlite3.DatabaseError("corrupt")):
            r = self.send("https://e.test/x.zip", "lookup-fail-001")
        self.assertEqual(r["error_code"], "storage_error")
        self.assertEqual(queue.items, [])

    def test_cleanup_policy(self):
        db = DatabaseManager(self.db_path)
        now = datetime.now()

        def add(rid, age_days):
            db.add_download(
                DownloadModel(id=f"d{rid}", url="https://e.test/x", destination_folder=str(self.tmp), filename="x"),
                browser_request={"request_id": rid, "browser": "chrome", "url_sha256": "2" * 64,
                                 "created_at": (now - timedelta(days=age_days)).isoformat()})

        add("old-record-0001", BROWSER_REQUEST_RETENTION_DAYS + 5)
        add("old-record-0002", BROWSER_REQUEST_RETENTION_DAYS + 1)
        for i in range(5):
            add(f"new-record-000{i}", i)
        self.assertEqual(db.purge_browser_requests(now=now), 2)
        self.assertIsNone(db.get_browser_request("old-record-0001"))
        self.assertIsNotNone(db.get_browser_request("new-record-0004"))

        self.assertEqual(db.purge_browser_requests(max_rows=3, now=now), 2)
        kept = {r[0] for r in self.raw_rows("SELECT request_id FROM browser_requests")}
        self.assertEqual(kept, {"new-record-0000", "new-record-0001", "new-record-0002"})
        self.assertEqual(len(db.get_all_downloads()), 7, "retention never deletes downloads")
        db.close()

    def test_service_start_applies_retention(self):
        db = DatabaseManager(self.db_path)
        db.add_download(
            DownloadModel(id="expired1", url="https://e.test/x", destination_folder=str(self.tmp), filename="x"),
            browser_request={"request_id": "expired-req-01", "browser": "edge", "url_sha256": "3" * 64,
                             "created_at": (datetime.now() - timedelta(days=365)).isoformat()})
        db.close()
        db, queue, _ = self.start_stack()
        self.assertIsNone(db.get_browser_request("expired-req-01"))
        self.assertIsNotNone(db.get_download("expired1"))

    def test_existing_download_history_preserved(self):
        db, queue, _ = self.start_stack()
        manual = queue.add_download("https://e.test/manual.iso", str(self.tmp), auto_start=False)
        self.send("https://e.test/browser.zip", "history-req-001")
        db, queue, _ = self.restart()
        ids = {d.id for d in db.get_all_downloads()}
        self.assertIn(manual["id"], ids)
        self.assertEqual(len(ids), 2)
        self.assertEqual(db.purge_browser_requests(max_age_days=0, now=datetime.now() + timedelta(days=1)), 1)
        self.assertEqual({d.id for d in db.get_all_downloads()}, ids)

    def test_in_memory_fallback_without_database(self):
        _, queue, service = self.start_stack(with_db=False)
        first = self.send("https://e.test/m.zip", "memory-req-001")
        service.server._completed.clear()
        dup = self.send("https://e.test/m.zip", "memory-req-001")
        conflict = self.send("https://e.test/other.zip", "memory-req-001")
        self.assertEqual(dup["download"]["id"], first["download"]["id"])
        self.assertEqual(conflict["error_code"], "request_id_conflict")
        self.assertEqual(len(queue.items), 1)


class SchemaMigrationTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "stage5.db"

    def tearDown(self):
        self._tmp.cleanup()

    def make_stage5_db(self):
        """Creates a genuine schema-v2 (Stage 5) database with history rows."""
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        builder = DatabaseManager.__new__(DatabaseManager)
        with conn:
            builder._create_schema_v1(conn)
            builder._migrate(conn, 1, 2)
            conn.execute("PRAGMA user_version = 2;")
            conn.execute(
                "INSERT INTO downloads (id, url, filename, destination_path, status, created_at, total_size, "
                "downloaded_size, priority, category, retry_count) VALUES "
                "('hist0001','https://e.test/old.iso','old.iso','C:/dl','Completed','2026-01-01T10:00:00',"
                "1000,1000,'High','Disk Images',2)")
            conn.execute(
                "INSERT INTO downloads (id, url, filename, destination_path, status, created_at, downloaded_size) "
                "VALUES ('hist0002','https://e.test/p.bin','p.bin','C:/dl','Paused','2026-02-01T10:00:00',500)")
            conn.execute(
                "INSERT INTO segments (download_id, segment_index, start_byte, end_byte, downloaded_bytes, status) "
                "VALUES ('hist0002', 0, 0, 999, 500, 'Paused')")
            conn.execute("INSERT INTO settings (key, value) VALUES ('max_simultaneous_downloads', '3')")
        self.assertNotIn("browser_requests",
                         {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")})
        conn.close()

    def test_migration_from_stage5_preserves_data(self):
        self.make_stage5_db()
        db = DatabaseManager(self.path)
        conn = db.get_connection()
        # Stage 6B evolution: v2 now migrates through v3 to the current version (v4);
        # the v3 browser_requests checks below are unchanged. Exact v4 checks live
        # in tests/test_stage6b_capture.py (SchemaV4MigrationTests).
        self.assertGreaterEqual(CURRENT_SCHEMA_VERSION, 3)
        self.assertEqual(conn.execute("PRAGMA user_version;").fetchone()[0], CURRENT_SCHEMA_VERSION)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(browser_requests);")}
        self.assertEqual(cols, {"request_id", "download_id", "browser", "url_sha256", "status", "created_at"})
        pk = [r["name"] for r in conn.execute("PRAGMA table_info(browser_requests);") if r["pk"]]
        self.assertEqual(pk, ["request_id"])

        old = db.get_download("hist0001")
        self.assertEqual((old.url, old.status, old.priority, old.category, old.retry_count, old.total_size),
                         ("https://e.test/old.iso", "Completed", "High", "Disk Images", 2, 1000))
        paused = db.get_download("hist0002")
        self.assertEqual(paused.downloaded_size, 500)
        self.assertEqual(len(db.get_segments("hist0002")), 1)
        self.assertEqual(db.get_setting("max_simultaneous_downloads"), "3")
        db.close()

        db2 = DatabaseManager(self.path)  # re-opening is a no-op
        self.assertEqual(len(db2.get_all_downloads()), 2)
        db2.close()

    def test_fresh_database_has_v3_schema(self):
        db = DatabaseManager(self.path)
        conn = db.get_connection()
        self.assertEqual(conn.execute("PRAGMA user_version;").fetchone()[0], CURRENT_SCHEMA_VERSION)
        self.assertIsNone(db.get_browser_request("nothing-here-1"))
        db.close()


# ===========================================================================
# 3. Documentation accuracy
# ===========================================================================
class SecurityDocumentationTests(unittest.TestCase):

    def test_service_worker_does_not_claim_tokens_are_never_forwarded(self):
        src = (ROOT / "browser_extension" / "service_worker.js").read_text(encoding="utf-8")
        self.assertNotIn("tokens are ever read or forwarded", src)
        self.assertIn("query string", src)

    def test_readmes_explain_signed_url_distinction(self):
        for path in (ROOT / "README.md", ROOT / "native_host" / "README.md"):
            text = path.read_text(encoding="utf-8")
            self.assertIn("signed", text.lower(), path)
            self.assertIn("query", text.lower(), path)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("browser_requests", readme)
        self.assertIn("schema v3", readme.lower())


if __name__ == "__main__":
    unittest.main()
