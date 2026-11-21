"""
End-to-end verification of the native messaging host (source or built .exe).

Starts a *mock* application IPC server on 127.0.0.1 with a temporary endpoint
file, launches the host exactly as Chrome/Edge would (stdin/stdout pipes),
sends framed messages and checks the framed responses.

Usage:
    python native_host\\verify_host.py native_host\\dist\\MyDownloadManagerNativeHost.exe
    python native_host\\verify_host.py            # verifies host.py from source
"""
from __future__ import annotations

import json
import os
import secrets
import socket
import struct
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mdm.browser_integration import ipc_protocol as proto  # noqa: E402


class MockAppServer:
    """Minimal stand-in for My Download Manager's authenticated IPC server."""

    def __init__(self):
        self.token = secrets.token_urlsafe(32)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind((proto.LOOPBACK_HOST, 0))
        self.sock.listen(8)
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.received: List[Dict[str, Any]] = []
        self.seen: Dict[str, Dict[str, Any]] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=2)
        self.sock.close()

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with conn:
                conn.settimeout(5)
                try:
                    req = proto.recv_frame(conn)
                except (proto.ProtocolError, OSError):
                    continue
                self.received.append(req)
                conn.sendall(proto.encode_frame(self._respond(req)))

    def _respond(self, req: Dict[str, Any]) -> Dict[str, Any]:
        rid = req.get("request_id")
        if req.get("token") != self.token:
            return proto.error_response(rid, "unauthorized", "Authentication failed.")
        if rid in self.seen:
            return dict(self.seen[rid], status="duplicate")
        action = req.get("action")
        if action == "ping":
            resp = proto.build_response(rid, True, "ok", data={"app": "My Download Manager", "version": "mock"})
        elif action == "add_download":
            resp = proto.build_response(rid, True, "accepted", message="Added",
                                        data={"download_id": "mock0001", "filename": "file.zip"})
        else:
            resp = proto.error_response(rid, "unsupported_action", "Unsupported action.")
        self.seen[rid] = resp
        return resp


def frame(obj: Any) -> bytes:
    body = json.dumps(obj).encode("utf-8")
    return struct.pack("<I", len(body)) + body


def parse_frames(data: bytes) -> List[Dict[str, Any]]:
    out, pos = [], 0
    while pos + 4 <= len(data):
        (n,) = struct.unpack_from("<I", data, pos)
        out.append(json.loads(data[pos + 4:pos + 4 + n].decode("utf-8")))
        pos += 4 + n
    return out


def run_host(command: List[str], stdin_bytes: bytes, env: Dict[str, str], timeout: float = 30.0) -> Tuple[int, bytes, bytes]:
    proc = subprocess.run(command, input=stdin_bytes, capture_output=True, env=env, timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


def verify(exe: Optional[str] = None) -> Tuple[bool, List[str]]:
    command = [exe] if exe else [sys.executable, str(ROOT / "native_host" / "host.py")]
    report: List[str] = []
    ok = True
    with tempfile.TemporaryDirectory() as tmp, MockAppServer() as app:
        endpoint = Path(tmp) / "endpoint.json"
        proto.write_endpoint_atomic(endpoint, app.port, app.token)
        env = dict(os.environ, **{proto.ENDPOINT_ENV_OVERRIDE: str(endpoint), "LOCALAPPDATA": tmp})

        messages = [
            {"action": "ping", "request_id": "ping-0001"},
            {"action": "add_download", "request_id": "req-verify-0001",
             "url": "https://example.com/files/file.zip?token=a%20b&x=1", "filename": None,
             "referrer": "https://example.com/", "page_title": "Example", "browser": "chrome"},
            {"action": "add_download", "request_id": "req-verify-0001",
             "url": "https://example.com/files/file.zip?token=a%20b&x=1", "filename": None,
             "referrer": "https://example.com/", "page_title": "Example", "browser": "chrome"},
            {"action": "add_download", "request_id": "req-verify-0002",
             "url": "javascript:alert(1)", "browser": "edge"},
        ]
        code, out, err = run_host(command, b"".join(frame(m) for m in messages), env)
        responses = parse_frames(out)

        def check(cond: bool, label: str):
            nonlocal ok
            ok = ok and cond
            report.append(f"[{'PASS' if cond else 'FAIL'}] {label}")

        check(code == 0, f"host exited cleanly (exit code {code})")
        check(len(responses) == 4, f"received 4 framed responses (got {len(responses)})")
        if len(responses) == 4:
            check(responses[0].get("ok") and responses[0].get("status") == "connected", "ping -> connected")
            check(responses[1].get("ok") and responses[1].get("status") == "accepted"
                  and responses[1].get("download", {}).get("id") == "mock0001", "add_download forwarded and accepted")
            check(responses[2].get("ok") and responses[2].get("status") == "duplicate", "duplicate request_id not re-added")
            check(not responses[3].get("ok") and responses[3].get("error_code") == "unsupported_scheme",
                  "javascript: URL rejected by host")
        adds = [r for r in app.received if r.get("action") == "add_download"]
        check(len(adds) == 2 and all(r.get("token") == app.token for r in adds),
              "IPC requests were authenticated with the endpoint token")
        check(all(r["payload"].get("url", "").startswith("https://") for r in adds),
              "rejected URL never reached the application")
        if err.strip():
            report.append("stderr: " + err.decode("utf-8", "replace").strip()[:500])

        # Application not running: endpoint file absent.
        endpoint.unlink()
        code2, out2, _ = run_host(command, frame({"action": "ping", "request_id": "ping-0002"}), env)
        r2 = parse_frames(out2)
        check(code2 == 0 and len(r2) == 1 and r2[0].get("error_code") == "app_not_running",
              "app not running reported clearly")
    return ok, report


def main(argv: List[str]) -> int:
    exe = argv[1] if len(argv) > 1 else None
    if exe and not Path(exe).is_file():
        print(f"Host executable not found: {exe}", file=sys.stderr)
        return 2
    ok, report = verify(exe)
    print("\n".join(report))
    print("VERIFICATION PASSED" if ok else "VERIFICATION FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
