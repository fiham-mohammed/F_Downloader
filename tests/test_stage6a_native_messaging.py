"""
Stage 6A - Native Messaging host and request validation tests.

Covers Chromium native messaging framing (4-byte little-endian length + UTF-8
JSON), partial reads, multiple messages per process, invalid/oversized frames,
malformed JSON, unsupported actions, URL scheme/credential rejection and
metadata validation. No browser, application or network is required: the
desktop application is replaced by a mock IPC server where needed.
"""
import io
import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "native_host"))
sys.path.insert(0, str(ROOT))

import host  # noqa: E402  (native_host/host.py)
from verify_host import MockAppServer  # noqa: E402
from mdm.browser_integration import ipc_protocol as proto  # noqa: E402
from mdm.browser_integration.request_validator import (  # noqa: E402
    ValidationError,
    validate_add_download_request,
    validate_download_url,
    validate_filename,
    redact_url,
    MAX_URL_LENGTH,
)


def frame(obj) -> bytes:
    body = json.dumps(obj).encode("utf-8")
    return struct.pack("<I", len(body)) + body


def parse_frames(data: bytes):
    out, pos = [], 0
    while pos + 4 <= len(data):
        (n,) = struct.unpack_from("<I", data, pos)
        out.append(json.loads(data[pos + 4:pos + 4 + n].decode("utf-8")))
        pos += 4 + n
    assert pos == len(data), "trailing bytes on stdout"
    return out


class TrickleStream(io.RawIOBase):
    """A pipe that returns at most `chunk` bytes per read() - simulates partial reads."""

    def __init__(self, data: bytes, chunk: int = 1):
        self._data = data
        self._pos = 0
        self._chunk = chunk

    def readable(self):
        return True

    def read(self, n=-1):
        if self._pos >= len(self._data):
            return b""
        n = self._chunk if n < 0 else min(n, self._chunk)
        out = self._data[self._pos:self._pos + n]
        self._pos += len(out)
        return out


def valid_add(**overrides):
    msg = {
        "action": "add_download",
        "request_id": "req-0000-0001",
        "url": "https://example.com/files/file.zip",
        "filename": None,
        "referrer": "https://example.com/",
        "page_title": "Example page",
        "browser": "chrome",
    }
    msg.update(overrides)
    return msg


# ---------------------------------------------------------------------------
# Framing
# ---------------------------------------------------------------------------
class NativeMessagingFramingTests(unittest.TestCase):

    def test_valid_message_framing_roundtrip(self):
        msg = {"action": "ping", "request_id": "abc12345", "text": "héllo ✓"}
        self.assertEqual(host.read_message(io.BytesIO(frame(msg))), msg)

        out = io.BytesIO()
        host.write_message(out, {"ok": True, "message": "✓"})
        raw = out.getvalue()
        (length,) = struct.unpack("<I", raw[:4])
        self.assertEqual(length, len(raw) - 4, "length prefix must be little-endian byte count")
        self.assertEqual(json.loads(raw[4:].decode("utf-8")), {"ok": True, "message": "✓"})

    def test_length_prefix_is_little_endian(self):
        out = io.BytesIO()
        host.write_message(out, {"a": "x" * 300})
        body_len = len(out.getvalue()) - 4
        self.assertEqual(out.getvalue()[:4], body_len.to_bytes(4, "little"))

    def test_partial_reads_one_byte_at_a_time(self):
        msgs = [{"action": "ping", "request_id": f"id-{i:08d}"} for i in range(3)]
        stream = TrickleStream(b"".join(frame(m) for m in msgs), chunk=1)
        self.assertEqual([host.read_message(stream) for _ in range(3)], msgs)
        self.assertIsNone(host.read_message(stream), "clean EOF returns None")

    def test_multiple_messages_in_one_process(self):
        msgs = [{"action": "unknown_a"}, {"action": "unknown_b"}, {"action": "unknown_c"}]
        out = io.BytesIO()
        code = host.run(TrickleStream(b"".join(frame(m) for m in msgs), chunk=3), out)
        self.assertEqual(code, 0)
        responses = parse_frames(out.getvalue())
        self.assertEqual(len(responses), 3)
        self.assertTrue(all(r["error_code"] == "unsupported_action" for r in responses))

    def test_clean_eof_exits_zero_without_output(self):
        out = io.BytesIO()
        self.assertEqual(host.run(io.BytesIO(b""), out), 0)
        self.assertEqual(out.getvalue(), b"")

    def test_invalid_frame_zero_length(self):
        out = io.BytesIO()
        code = host.run(io.BytesIO(struct.pack("<I", 0)), out)
        self.assertEqual(code, 1)
        self.assertEqual(parse_frames(out.getvalue())[0]["error_code"], "invalid_frame")

    def test_invalid_frame_truncated_header_and_body(self):
        with self.assertRaises(host.NativeMessagingError) as ctx:
            host.read_message(io.BytesIO(b"\x05\x00"))
        self.assertEqual(ctx.exception.code, "invalid_frame")
        self.assertFalse(ctx.exception.recoverable)

        with self.assertRaises(host.NativeMessagingError) as ctx:
            host.read_message(io.BytesIO(struct.pack("<I", 50) + b'{"a":1}'))
        self.assertEqual(ctx.exception.code, "invalid_frame")

    def test_oversized_payload_rejected_without_reading_body(self):
        size = proto.MAX_MESSAGE_SIZE + 1
        stream = io.BytesIO(struct.pack("<I", size) + b"x" * 16)
        with self.assertRaises(host.NativeMessagingError) as ctx:
            host.read_message(stream)
        self.assertEqual(ctx.exception.code, "message_too_large")
        self.assertEqual(stream.tell(), 4, "oversized body must not be read into memory")

        out = io.BytesIO()
        self.assertEqual(host.run(io.BytesIO(struct.pack("<I", 0xFFFFFFFF)), out), 1)
        self.assertEqual(parse_frames(out.getvalue())[0]["error_code"], "message_too_large")

    def test_max_size_message_accepted(self):
        pad = "x" * (proto.MAX_MESSAGE_SIZE - 20)
        body = json.dumps({"p": pad}).encode()
        self.assertLessEqual(len(body), proto.MAX_MESSAGE_SIZE)
        self.assertEqual(host.read_message(io.BytesIO(struct.pack("<I", len(body)) + body))["p"], pad)

    def test_invalid_json_is_recoverable(self):
        bad = b"{not json"
        data = struct.pack("<I", len(bad)) + bad + frame({"action": "nope"})
        out = io.BytesIO()
        self.assertEqual(host.run(io.BytesIO(data), out), 0)
        r = parse_frames(out.getvalue())
        self.assertEqual([x["error_code"] for x in r], ["invalid_json", "unsupported_action"])

    def test_invalid_utf8_and_non_object_json(self):
        for body in (b"\xff\xfe\xfd", b"[1,2,3]", b'"string"'):
            with self.assertRaises(host.NativeMessagingError) as ctx:
                host.read_message(io.BytesIO(struct.pack("<I", len(body)) + body))
            self.assertTrue(ctx.exception.recoverable)

    def test_response_flushed_after_each_message(self):
        class FlushTracker(io.BytesIO):
            flushes = 0

            def flush(self):
                FlushTracker.flushes += 1
                super().flush()

        out = FlushTracker()
        host.run(io.BytesIO(frame({"action": "x"}) + frame({"action": "y"})), out)
        self.assertGreaterEqual(FlushTracker.flushes, 2)

    def test_nothing_but_frames_written_to_stdout(self):
        out = io.BytesIO()
        with tempfile.TemporaryDirectory() as tmp:
            host.run(io.BytesIO(frame(valid_add(url="data:text/plain,hi")) + frame({"action": "ping"})),
                     out, endpoint_path=Path(tmp) / "missing.json")
        parse_frames(out.getvalue())  # asserts no stray bytes


# ---------------------------------------------------------------------------
# Message handling
# ---------------------------------------------------------------------------
class NativeHostMessageHandlingTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.endpoint = Path(self._tmp.name) / "endpoint.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_unsupported_action(self):
        for msg in ({"action": "exec", "cmd": "calc.exe"}, {"action": "eval"}, {"action": 5}):
            r = host.handle_message(msg, self.endpoint)
            self.assertFalse(r["ok"])
            self.assertIn(r["error_code"], ("unsupported_action", "missing_field"))

    def test_missing_action_and_non_object(self):
        self.assertEqual(host.handle_message({}, self.endpoint)["error_code"], "missing_field")
        self.assertEqual(host.handle_message([1], self.endpoint)["error_code"], "invalid_message")

    def test_invalid_url_scheme_rejected_before_ipc(self):
        with MockAppServer() as app:
            proto.write_endpoint_atomic(self.endpoint, app.port, app.token)
            for url in ("javascript:alert(1)", "data:text/html,x", "blob:https://e.com/u", "file:///C:/x",
                        "chrome://settings", "edge://settings", "ftp://e.com/x"):
                r = host.handle_message(valid_add(url=url), self.endpoint)
                self.assertFalse(r["ok"], url)
                self.assertEqual(r["error_code"], "unsupported_scheme", url)
            self.assertEqual(app.received, [], "rejected requests must never reach the application")

    def test_embedded_credentials_rejected(self):
        for url in ("https://alice:S3cr3tPw@example.com/f.zip", "http://alice@example.com/f.zip"):
            r = host.handle_message(valid_add(url=url), self.endpoint)
            self.assertEqual(r["error_code"], "embedded_credentials")
            self.assertNotIn("S3cr3tPw", json.dumps(r))
            self.assertNotIn("alice", json.dumps(r))

    def test_malformed_metadata_rejected(self):
        cases = [
            valid_add(browser="firefox"),
            valid_add(browser=None),
            valid_add(page_title=["x"]),
            valid_add(referrer=123),
            valid_add(filename={"a": 1}),
            valid_add(filename="..\\..\\Windows\\evil.exe"),
            valid_add(filename="C:\\evil.exe"),
            valid_add(filename="sub/dir.zip"),
            valid_add(request_id="short"),
            valid_add(request_id="bad id with spaces!"),
            valid_add(extra_field="x"),
            {k: v for k, v in valid_add().items() if k != "url"},
        ]
        for msg in cases:
            r = host.handle_message(msg, self.endpoint)
            self.assertFalse(r["ok"], msg)
            self.assertIn(r["error_code"], ("invalid_field", "missing_field"), msg)

    def test_app_not_running(self):
        r = host.handle_message(valid_add(), self.endpoint)
        self.assertEqual(r["error_code"], "app_not_running")
        self.assertIn("not running", r["message"])

    def test_stale_endpoint_reports_app_not_running(self):
        with MockAppServer() as app:
            port, token = app.port, app.token
        proto.write_endpoint_atomic(self.endpoint, port, token)  # server is gone
        r = host.handle_message({"action": "ping"}, self.endpoint)
        self.assertEqual(r["error_code"], "app_not_running")

    def test_corrupt_endpoint_file(self):
        self.endpoint.write_text("{ not json", encoding="utf-8")
        self.assertEqual(host.handle_message({"action": "ping"}, self.endpoint)["error_code"], "invalid_endpoint")
        self.endpoint.write_text(json.dumps({"port": 1234, "token": "short"}), encoding="utf-8")
        self.assertEqual(host.handle_message({"action": "ping"}, self.endpoint)["error_code"], "invalid_endpoint")

    def test_valid_request_forwarded_with_authentication(self):
        with MockAppServer() as app:
            proto.write_endpoint_atomic(self.endpoint, app.port, app.token)
            url = "https://example.com/dl/My%20File.zip?sig=a%2Fb&exp=99"
            r = host.handle_message(valid_add(url=url), self.endpoint)
            self.assertTrue(r["ok"])
            self.assertEqual(r["status"], "accepted")
            self.assertEqual(r["download"]["id"], "mock0001")
            sent = app.received[0]
            self.assertEqual(sent["token"], app.token)
            self.assertEqual(sent["action"], "add_download")
            self.assertEqual(sent["request_id"], "req-0000-0001")
            self.assertEqual(sent["payload"]["url"], url, "query and encoded characters preserved")

    def test_ping_connected(self):
        with MockAppServer() as app:
            proto.write_endpoint_atomic(self.endpoint, app.port, app.token)
            r = host.handle_message({"action": "ping", "request_id": "p1"}, self.endpoint)
        self.assertTrue(r["ok"])
        self.assertEqual(r["status"], "connected")
        self.assertEqual(r["app_version"], "mock")
        self.assertTrue(r["open_app_supported"])

    def test_wrong_token_reports_unauthorized(self):
        with MockAppServer() as app:
            proto.write_endpoint_atomic(self.endpoint, app.port, "w" * 43)
            r = host.handle_message(valid_add(), self.endpoint)
        self.assertEqual(r["error_code"], "unauthorized")

    def test_ipc_timeout(self):
        import socket
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)  # accepts at OS level but never answers
        try:
            proto.write_endpoint_atomic(self.endpoint, srv.getsockname()[1], "t" * 43)
            with patch.object(host, "IPC_RESPONSE_TIMEOUT", 0.3):
                r = host.handle_message({"action": "ping"}, self.endpoint)
            self.assertEqual(r["error_code"], "timeout")
        finally:
            srv.close()

    def test_open_app_without_configuration(self):
        r = host.handle_message({"action": "open_app"}, self.endpoint, config={})
        self.assertFalse(r["ok"])
        self.assertEqual(r["error_code"], "open_not_supported")

    def test_open_app_ignores_browser_supplied_paths(self):
        with patch.object(host.subprocess, "Popen") as popen:
            r = host.handle_message({"action": "open_app", "app_path": "C:\\Windows\\System32\\calc.exe"},
                                    self.endpoint, config={})
        popen.assert_not_called()
        self.assertFalse(r["ok"])

    def test_open_app_launches_only_configured_exe(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "MyDownloadManager.exe"
            exe.write_bytes(b"MZ")
            with patch.object(host.subprocess, "Popen") as popen:
                r = host.handle_message({"action": "open_app"}, self.endpoint,
                                        config={"app_path": str(exe), "app_args": ["--flag"]})
            self.assertTrue(r["ok"])
            self.assertEqual(r["status"], "launched")
            args, kwargs = popen.call_args
            self.assertEqual(args[0], [str(exe), "--flag"])
            self.assertFalse(kwargs.get("shell"))

            with patch.object(host.subprocess, "Popen") as popen:
                host.handle_message({"action": "open_app"}, self.endpoint,
                                    config={"app_path": str(Path(tmp) / "script.bat")})
            popen.assert_not_called()

    def test_host_never_uses_eval_or_exec(self):
        src = (ROOT / "native_host" / "host.py").read_text(encoding="utf-8")
        for forbidden in ("eval(", "exec(", "shell=True", "os.system", "print("):
            self.assertNotIn(forbidden, src)


# ---------------------------------------------------------------------------
# Shared validator
# ---------------------------------------------------------------------------
class RequestValidatorTests(unittest.TestCase):

    def test_valid_request(self):
        req = validate_add_download_request(valid_add(browser="EDGE", page_title="  T\x07itle "))
        self.assertEqual(req.browser, "edge")
        self.assertEqual(req.page_title, "T itle")

    def test_rejected_schemes(self):
        for url in ("javascript:void(0)", "JAVASCRIPT:alert(1)", "data:,x", "blob:https://a/b", "file:///etc/passwd",
                    "chrome://downloads", "edge://downloads", "about:blank", "ws://e.com", "mailto:a@b.c"):
            with self.assertRaises(ValidationError) as ctx:
                validate_download_url(url)
            self.assertEqual(ctx.exception.code, "unsupported_scheme", url)

    def test_invalid_urls(self):
        for url in ("", "   ", "example.com/file", "http:/x.com", "http://", "https://exa mple.com",
                    "https://example.com:99999/x", "https://example.com:abc/x", "http://\x00evil.com",
                    "https://e.com/" + "a" * MAX_URL_LENGTH, None, 123):
            with self.assertRaises(ValidationError, msg=repr(url)):
                validate_download_url(url)

    def test_valid_urls_preserved_exactly(self):
        for url in ("http://example.com/a.zip", "https://example.com/My%20File%20(1).zip?x=1&y=%E2%9C%93#f",
                    "https://[2001:db8::1]:8443/file.iso", "HTTPS://Example.COM/X.BIN",
                    "https://example.com/?" + "&".join(f"k{i}=v%20{i}" for i in range(600))):
            self.assertEqual(validate_download_url(url), url)

    def test_filename_rules(self):
        self.assertIsNone(validate_filename(None))
        self.assertIsNone(validate_filename("   "))
        self.assertEqual(validate_filename("report (final).pdf"), "report (final).pdf")
        for bad in ("../x.zip", "..", "a/b.zip", "a\\b.zip", "C:x.zip", "x\x00.zip", "x" * 300):
            with self.assertRaises(ValidationError, msg=bad):
                validate_filename(bad)

    def test_referrer_dropped_not_rejected(self):
        for ref in ("file:///C:/page.html", "https://u:p@e.com/", "chrome://newtab", "not a url"):
            self.assertIsNone(validate_add_download_request(valid_add(referrer=ref)).referrer)

    def test_redact_url_never_leaks_path_or_query(self):
        red = redact_url("https://cdn.example.com/secret/path.zip?token=SECRET")
        self.assertNotIn("SECRET", red)
        self.assertNotIn("secret", red)
        self.assertIn("cdn.example.com", red)


class IpcProtocolTests(unittest.TestCase):

    def test_frame_decoder_handles_fragmentation(self):
        frames = b"".join(proto.encode_frame({"i": i}) for i in range(5))
        dec = proto.FrameDecoder()
        got = []
        for b in frames:
            got.extend(dec.feed(bytes([b])))
        self.assertEqual(got, [{"i": i} for i in range(5)])
        self.assertEqual(dec.pending_bytes, 0)

    def test_frame_decoder_rejects_oversized_and_zero(self):
        with self.assertRaises(proto.ProtocolError):
            proto.FrameDecoder().feed(struct.pack("<I", proto.MAX_MESSAGE_SIZE + 1))
        with self.assertRaises(proto.ProtocolError):
            proto.FrameDecoder().feed(struct.pack("<I", 0))

    def test_encode_rejects_oversized(self):
        with self.assertRaises(proto.ProtocolError):
            proto.encode_frame({"x": "y" * proto.MAX_MESSAGE_SIZE})

    def test_endpoint_roundtrip_and_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "sub" / "endpoint.json"
            proto.write_endpoint_atomic(p, 50123, "k" * 43)
            ep = proto.read_endpoint(p)
            self.assertEqual((ep.port, ep.token), (50123, "k" * 43))
            self.assertEqual(json.loads(p.read_text())["host"], "127.0.0.1")
            self.assertEqual(list(p.parent.glob("*.tmp")), [], "no temp files left behind")
            for bad in ({"port": 0, "token": "k" * 43}, {"port": "1", "token": "k" * 43},
                        {"port": 5000, "token": "short"}, []):
                p.write_text(json.dumps(bad))
                with self.assertRaises(proto.ProtocolError):
                    proto.read_endpoint(p)

    def test_default_endpoint_in_per_user_data_dir(self):
        with patch.dict("os.environ", {proto.ENDPOINT_ENV_OVERRIDE: ""}):
            path = proto.default_endpoint_path()
        self.assertEqual(path.name, "endpoint.json")
        self.assertIn("MyDownloadManager", str(path))
        if sys.platform == "win32":
            try:
                import platformdirs
                self.assertEqual(path.parent.parent,
                                 Path(platformdirs.user_data_dir("MyDownloadManager", appauthor=False)))
            except ImportError:
                pass


if __name__ == "__main__":
    unittest.main()
