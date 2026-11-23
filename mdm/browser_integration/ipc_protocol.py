"""
Local IPC protocol shared by My Download Manager and its native messaging host.

Pure standard-library module (no Qt) so the native host executable stays small.

Wire format (identical framing to Chromium Native Messaging):

    +----------------------+------------------------------+
    | uint32 little-endian |  UTF-8 encoded JSON object   |
    |   payload length     |  (exactly `length` bytes)    |
    +----------------------+------------------------------+

Request envelope (host -> application)::

    {
      "v": 1,
      "token": "<endpoint auth token>",
      "request_id": "<unique id>",
      "ts": 1732172400.123,
      "action": "ping" | "add_download" | "show_window",
      "payload": { ... }
    }

Response envelope (application -> host)::

    {
      "v": 1,
      "request_id": "<echoed>",
      "ok": true | false,
      "status": "ok" | "accepted" | "duplicate" | "error",
      "error_code": null | "<code>",
      "message": "<human readable>",
      "data": { ... }
    }

The application only ever listens on 127.0.0.1. The port and a per-launch
random token are published in a per-user endpoint file; the host never reads a
host address from that file, it always connects to 127.0.0.1.
"""
from __future__ import annotations

import json
import os
import socket
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

PROTOCOL_VERSION = 1
LOOPBACK_HOST = "127.0.0.1"
MAX_MESSAGE_SIZE = 1024 * 1024  # 1 MB, applies to both directions
HEADER = struct.Struct("<I")
HEADER_SIZE = HEADER.size

# Requests whose timestamp is further than this from the application's clock
# are rejected; combined with the request-id cache this prevents replay.
MAX_CLOCK_SKEW_SECONDS = 300

IPC_ACTIONS = frozenset({
    "ping", "add_download", "show_window",
    # Stage 6B automatic capture (two-phase handshake + configuration)
    "prepare_capture", "capture_status", "commit_capture", "abort_capture",
    "get_capture_config", "set_capture_mode", "claim_fallback",
    "report_capture_decision",  # privacy-safe diagnostics (no URL)
})
ENVELOPE_FIELDS = frozenset({"v", "token", "request_id", "ts", "action", "payload"})

APP_DIR_NAME = "MyDownloadManager"
ENDPOINT_SUBDIR = "browser_integration"
ENDPOINT_FILENAME = "endpoint.json"
ENDPOINT_ENV_OVERRIDE = "MDM_BROWSER_ENDPOINT_FILE"  # used by automated tests only

MIN_TOKEN_LENGTH = 32


class ProtocolError(Exception):
    """Framing / envelope level error."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------
# Framing
# --------------------------------------------------------------------------

def encode_frame(obj: Dict[str, Any], max_size: int = MAX_MESSAGE_SIZE) -> bytes:
    body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(body) > max_size:
        raise ProtocolError("message_too_large", "Message exceeds the maximum allowed size.")
    return HEADER.pack(len(body)) + body


def decode_body(body: bytes) -> Dict[str, Any]:
    try:
        obj = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ProtocolError("invalid_json", "Message is not valid UTF-8 JSON.") from None
    if not isinstance(obj, dict):
        raise ProtocolError("invalid_message", "Message must be a JSON object.")
    return obj


def check_length(length: int, max_size: int = MAX_MESSAGE_SIZE) -> None:
    if length == 0:
        raise ProtocolError("invalid_frame", "Zero-length message frame.")
    if length > max_size:
        raise ProtocolError("message_too_large", "Message exceeds the maximum allowed size.")


class FrameDecoder:
    """Incremental decoder that tolerates arbitrary fragmentation.

    ``feed()`` returns every complete message decoded so far. After a
    ``ProtocolError`` the stream is unrecoverable and the decoder must be
    discarded (the caller closes the connection).
    """

    def __init__(self, max_size: int = MAX_MESSAGE_SIZE):
        self.max_size = max_size
        self._buffer = bytearray()

    @property
    def pending_bytes(self) -> int:
        return len(self._buffer)

    def feed(self, data: bytes) -> List[Dict[str, Any]]:
        self._buffer.extend(data)
        messages: List[Dict[str, Any]] = []
        while len(self._buffer) >= HEADER_SIZE:
            (length,) = HEADER.unpack_from(self._buffer, 0)
            check_length(length, self.max_size)
            if len(self._buffer) < HEADER_SIZE + length:
                break
            body = bytes(self._buffer[HEADER_SIZE:HEADER_SIZE + length])
            del self._buffer[:HEADER_SIZE + length]
            messages.append(decode_body(body))
        return messages


class BoundedFrameReader:
    """Pull-style reader that never holds more than one frame's worth of data.

    The caller asks ``bytes_needed()`` and reads *at most* that many bytes from
    its transport before calling ``feed()``. Consequently:

    * the declared length is validated as soon as the 4-byte header is
      complete - an oversized or zero-length frame is rejected before a single
      body byte is consumed;
    * the internal buffer never exceeds ``HEADER_SIZE + declared length``
      (itself capped at ``max_size``);
    * arbitrary fragmentation (down to one byte per read) is supported.

    ``feed()`` returns the decoded message once a frame is complete, otherwise
    None. After a ``ProtocolError`` the reader must be discarded.
    """

    def __init__(self, max_size: int = MAX_MESSAGE_SIZE):
        self.max_size = max_size
        self._header = bytearray()
        self._length: Optional[int] = None
        self._body = bytearray()

    @property
    def declared_length(self) -> Optional[int]:
        return self._length

    @property
    def buffered_bytes(self) -> int:
        return len(self._header) + len(self._body)

    def bytes_needed(self) -> int:
        if self._length is None:
            return HEADER_SIZE - len(self._header)
        return self._length - len(self._body)

    def feed(self, chunk: bytes) -> Optional[Dict[str, Any]]:
        if len(chunk) > self.bytes_needed():
            raise ValueError("feed() received more bytes than bytes_needed()")
        if self._length is None:
            self._header.extend(chunk)
            if len(self._header) == HEADER_SIZE:
                (length,) = HEADER.unpack(bytes(self._header))
                check_length(length, self.max_size)
                self._length = length
            return None
        self._body.extend(chunk)
        if len(self._body) < self._length:
            return None
        body = bytes(self._body)
        self._header.clear()
        self._body = bytearray()
        self._length = None
        return decode_body(body)


def recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = sock.recv(size - len(chunks))
        if not chunk:
            raise ProtocolError("connection_lost", "Connection closed before the full message was received.")
        chunks.extend(chunk)
    return bytes(chunks)


def recv_frame(sock: socket.socket, max_size: int = MAX_MESSAGE_SIZE) -> Dict[str, Any]:
    (length,) = HEADER.unpack(recv_exact(sock, HEADER_SIZE))
    check_length(length, max_size)
    return decode_body(recv_exact(sock, length))


# --------------------------------------------------------------------------
# Envelopes
# --------------------------------------------------------------------------

def build_request(token: str, action: str, request_id: str, payload: Optional[Dict[str, Any]] = None,
                  ts: Optional[float] = None) -> Dict[str, Any]:
    return {
        "v": PROTOCOL_VERSION,
        "token": token,
        "request_id": request_id,
        "ts": time.time() if ts is None else ts,
        "action": action,
        "payload": payload or {},
    }


def build_response(request_id: Optional[str], ok: bool, status: str, *, error_code: Optional[str] = None,
                   message: str = "", data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "v": PROTOCOL_VERSION,
        "request_id": request_id,
        "ok": ok,
        "status": status,
        "error_code": error_code,
        "message": message,
        "data": data or {},
    }


def error_response(request_id: Optional[str], code: str, message: str) -> Dict[str, Any]:
    return build_response(request_id, False, "error", error_code=code, message=message)


# --------------------------------------------------------------------------
# Endpoint file
# --------------------------------------------------------------------------

def default_data_dir() -> Path:
    """Same per-user directory that ``mdm.database`` uses via platformdirs
    (``%LOCALAPPDATA%\\MyDownloadManager`` on Windows), computed without
    third-party dependencies so the native host stays standard-library only."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / APP_DIR_NAME
        return Path.home() / "AppData" / "Local" / APP_DIR_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "share") / APP_DIR_NAME


def default_endpoint_path() -> Path:
    override = os.environ.get(ENDPOINT_ENV_OVERRIDE)
    if override:
        return Path(override)
    return default_data_dir() / ENDPOINT_SUBDIR / ENDPOINT_FILENAME


@dataclass(frozen=True)
class Endpoint:
    port: int
    token: str
    pid: Optional[int] = None


def parse_endpoint(obj: Any) -> Endpoint:
    if not isinstance(obj, dict):
        raise ProtocolError("invalid_endpoint", "Endpoint file is malformed.")
    port = obj.get("port")
    token = obj.get("token")
    pid = obj.get("pid")
    if not isinstance(port, int) or isinstance(port, bool) or not (1 <= port <= 65535):
        raise ProtocolError("invalid_endpoint", "Endpoint file has an invalid port.")
    if not isinstance(token, str) or len(token) < MIN_TOKEN_LENGTH:
        raise ProtocolError("invalid_endpoint", "Endpoint file has an invalid token.")
    return Endpoint(port=port, token=token, pid=pid if isinstance(pid, int) else None)


def read_endpoint(path: Optional[Path] = None) -> Endpoint:
    p = Path(path) if path is not None else default_endpoint_path()
    try:
        raw = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ProtocolError("app_not_running", "My Download Manager is not running.") from None
    except OSError:
        raise ProtocolError("app_not_running", "My Download Manager endpoint is not readable.") from None
    if len(raw) > 4096:
        raise ProtocolError("invalid_endpoint", "Endpoint file is malformed.")
    try:
        return parse_endpoint(json.loads(raw))
    except ValueError:
        raise ProtocolError("invalid_endpoint", "Endpoint file is malformed.") from None


def write_endpoint_atomic(path: Path, port: int, token: str) -> None:
    """Writes the endpoint file atomically with owner-only permissions.

    On Windows the file inherits the ACL of ``%LOCALAPPDATA%`` which grants
    access only to the current user, SYSTEM and Administrators.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps({
        "v": PROTOCOL_VERSION,
        "host": LOOPBACK_HOST,
        "port": port,
        "token": token,
        "pid": os.getpid(),
        "created_at": time.time(),
    }).encode("utf-8")
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
    fd = os.open(str(tmp), flags, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.replace(str(tmp), str(path))


# --------------------------------------------------------------------------
# Blocking client (used by the native host - never by the GUI thread)
# --------------------------------------------------------------------------

def send_request(endpoint: Endpoint, action: str, request_id: str, payload: Optional[Dict[str, Any]] = None,
                 *, connect_timeout: float = 3.0, response_timeout: float = 10.0) -> Dict[str, Any]:
    """Sends one authenticated request to the application and returns the
    response envelope. Raises ``ProtocolError`` with a stable code on failure:
    ``app_not_running``, ``timeout``, ``connection_lost``, ``connection_failed``,
    ``invalid_json``, ``message_too_large``."""
    frame = encode_frame(build_request(endpoint.token, action, request_id, payload))
    try:
        sock = socket.create_connection((LOOPBACK_HOST, endpoint.port), timeout=connect_timeout)
    except ConnectionRefusedError:
        raise ProtocolError("app_not_running", "My Download Manager is not running.") from None
    except socket.timeout:
        raise ProtocolError("timeout", "Timed out connecting to My Download Manager.") from None
    except OSError:
        raise ProtocolError("connection_failed", "Could not connect to My Download Manager.") from None

    try:
        sock.settimeout(response_timeout)
        try:
            sock.sendall(frame)
            response = recv_frame(sock)
        except socket.timeout:
            raise ProtocolError("timeout", "My Download Manager did not respond in time.") from None
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            raise ProtocolError("connection_lost", "My Download Manager closed the connection.") from None
        except OSError:
            raise ProtocolError("connection_failed", "Communication with My Download Manager failed.") from None
    finally:
        try:
            sock.close()
        except OSError:
            pass

    if response.get("request_id") not in (request_id, None):
        raise ProtocolError("invalid_message", "Response does not match the request.")
    return response
