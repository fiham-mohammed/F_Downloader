"""
My Download Manager - Chromium Native Messaging host (Stage 6A).

Host name: com.mydownloadmanager.native

Launched by Chrome / Edge with stdin/stdout connected to the extension.

Native Messaging framing (both directions):
    4-byte unsigned length, little-endian, native byte order on Windows x86/x64
    followed by exactly that many bytes of UTF-8 JSON.

IMPORTANT: stdout is the protocol channel. Nothing except framed responses is
ever written to it. Diagnostics go to a rotating log file (or stderr).

Supported extension messages ("action"):
    ping          -> check that the desktop application is reachable
    add_download  -> validate and forward a download request over secure IPC
    open_app      -> bring the running application to the front, or launch
                     the executable configured at install time (if any)

Nothing received from the browser is ever executed, evaluated or used as a
file path.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import secrets
import subprocess
import sys
from pathlib import Path
from typing import Any, BinaryIO, Dict, Optional

if not getattr(sys, "frozen", False):
    # Running from source: make the shared ``mdm.browser_integration`` package importable.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mdm.browser_integration import ipc_protocol as proto  # noqa: E402
from mdm.browser_integration.request_validator import (  # noqa: E402
    ValidationError,
    redact_url,
    validate_add_download_request,
)

HOST_NAME = "com.mydownloadmanager.native"
HOST_VERSION = "0.6.0"
MAX_MESSAGE_SIZE = proto.MAX_MESSAGE_SIZE           # browser -> host (1 MB)
MAX_RESPONSE_SIZE = proto.MAX_MESSAGE_SIZE          # host -> browser (Chromium limit is 1 MB)
HOST_ACTIONS = frozenset({"ping", "add_download", "open_app"})
CONFIG_FILENAME = "host_config.json"

IPC_CONNECT_TIMEOUT = 3.0
IPC_RESPONSE_TIMEOUT = 12.0

logger = logging.getLogger("mdm.native_host")
logger.addHandler(logging.NullHandler())  # real handler is added in main(); never stdout

USER_MESSAGES = {
    "app_not_running": "My Download Manager is not running. Start the application and try again.",
    "invalid_endpoint": "My Download Manager connection details are invalid. Restart the application.",
    "timeout": "My Download Manager did not respond in time.",
    "connection_lost": "My Download Manager closed the connection (it may be shutting down).",
    "connection_failed": "Could not communicate with My Download Manager.",
    "unauthorized": "My Download Manager rejected the connection. Restart the application.",
    "shutting_down": "My Download Manager is shutting down.",
}


class NativeMessagingError(Exception):
    """Framing error on the browser <-> host channel.

    ``recoverable`` is True when the frame boundary is still intact (e.g. the
    payload was not valid JSON) and the host can keep reading messages.
    """

    def __init__(self, code: str, message: str, recoverable: bool):
        super().__init__(message)
        self.code = code
        self.message = message
        self.recoverable = recoverable


# ---------------------------------------------------------------------------
# Framing
# ---------------------------------------------------------------------------

def _read_exact(stream: BinaryIO, size: int) -> bytes:
    """Reads exactly ``size`` bytes, tolerating partial reads from pipes."""
    buf = bytearray()
    while len(buf) < size:
        chunk = stream.read(size - len(buf))
        if not chunk:
            break
        buf.extend(chunk)
    return bytes(buf)


def read_message(stream: BinaryIO, max_size: int = MAX_MESSAGE_SIZE) -> Optional[Dict[str, Any]]:
    """Reads one framed message. Returns None on a clean EOF (browser closed the port)."""
    header = _read_exact(stream, proto.HEADER_SIZE)
    if not header:
        return None
    if len(header) < proto.HEADER_SIZE:
        raise NativeMessagingError("invalid_frame", "Truncated message header.", recoverable=False)
    (length,) = proto.HEADER.unpack(header)
    if length == 0:
        raise NativeMessagingError("invalid_frame", "Zero-length message.", recoverable=False)
    if length > max_size:
        # The oversized body is never read into memory; the stream cannot be resynchronised.
        raise NativeMessagingError("message_too_large", "Message exceeds the 1 MB limit.", recoverable=False)
    body = _read_exact(stream, length)
    if len(body) < length:
        raise NativeMessagingError("invalid_frame", "Truncated message body.", recoverable=False)
    try:
        return proto.decode_body(body)
    except proto.ProtocolError as exc:
        raise NativeMessagingError(exc.code, exc.message, recoverable=True) from None


def write_message(stream: BinaryIO, message: Dict[str, Any], max_size: int = MAX_RESPONSE_SIZE) -> None:
    """Writes one framed message and flushes immediately."""
    try:
        frame = proto.encode_frame(message, max_size)
    except proto.ProtocolError:
        frame = proto.encode_frame(_error(message.get("action"), message.get("request_id"),
                                          "message_too_large", "Response too large."))
    stream.write(frame)
    stream.flush()


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------

def _base(action: Any, request_id: Any) -> Dict[str, Any]:
    return {
        "ok": False,
        "action": action if isinstance(action, str) and action in HOST_ACTIONS else None,
        "request_id": request_id if isinstance(request_id, str) and len(request_id) <= 128 else None,
        "status": "error",
        "error_code": None,
        "message": "",
        "host_version": HOST_VERSION,
    }


def _error(action: Any, request_id: Any, code: str, message: str) -> Dict[str, Any]:
    resp = _base(action, request_id)
    resp.update(error_code=code, message=USER_MESSAGES.get(code, message))
    return resp


def _from_ipc(action: str, request_id: Optional[str], ipc: Dict[str, Any]) -> Dict[str, Any]:
    resp = _base(action, request_id)
    data = ipc.get("data") if isinstance(ipc.get("data"), dict) else {}
    if ipc.get("ok"):
        resp.update(ok=True, status=str(ipc.get("status") or "ok"), message=str(ipc.get("message") or ""))
    else:
        code = str(ipc.get("error_code") or "app_error")
        resp.update(error_code=code, message=USER_MESSAGES.get(code, str(ipc.get("message") or "Request failed.")))
    if "version" in data:
        resp["app_version"] = str(data["version"])
    if "download_id" in data:
        resp["download"] = {
            "id": str(data.get("download_id")),
            "filename": str(data.get("filename") or ""),
        }
    return resp


# ---------------------------------------------------------------------------
# Configuration (written by install_native_host.ps1, never by the browser)
# ---------------------------------------------------------------------------

def _host_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def load_host_config(path: Optional[Path] = None) -> Dict[str, Any]:
    p = path or (_host_dir() / CONFIG_FILENAME)
    try:
        raw = p.read_text(encoding="utf-8-sig")
        if len(raw) > 64 * 1024:
            return {}
        cfg = json.loads(raw)
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, ValueError):
        return {}


def _launch_application(config: Dict[str, Any]) -> bool:
    """Launches the executable configured at install time. Browser input is never used."""
    app_path = config.get("app_path")
    app_args = config.get("app_args") or []
    if not isinstance(app_path, str) or not app_path:
        return False
    exe = Path(app_path)
    if not exe.is_absolute() or exe.suffix.lower() != ".exe" or not exe.is_file():
        logger.warning("Configured application path is not a valid executable.")
        return False
    if not isinstance(app_args, list) or not all(isinstance(a, str) for a in app_args):
        app_args = []
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(  # noqa: S603 - fixed, installer-provided argv; shell=False
        [str(exe), *app_args],
        cwd=str(exe.parent),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        close_fds=True, creationflags=flags, shell=False,
    )
    return True


# ---------------------------------------------------------------------------
# Message handling
# ---------------------------------------------------------------------------

def _forward(action: str, request_id: str, payload: Dict[str, Any], endpoint_path: Optional[Path],
             ipc_action: Optional[str] = None) -> Dict[str, Any]:
    try:
        endpoint = proto.read_endpoint(endpoint_path)
        ipc = proto.send_request(
            endpoint, ipc_action or action, request_id, payload,
            connect_timeout=IPC_CONNECT_TIMEOUT, response_timeout=IPC_RESPONSE_TIMEOUT,
        )
    except proto.ProtocolError as exc:
        logger.info("IPC %s failed: %s", ipc_action or action, exc.code)
        return _error(action, request_id, exc.code, exc.message)
    return _from_ipc(action, request_id, ipc)


def handle_message(message: Any, endpoint_path: Optional[Path] = None,
                   config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Handles one decoded extension message. Never raises."""
    try:
        if not isinstance(message, dict):
            return _error(None, None, "invalid_message", "Message must be a JSON object.")
        action = message.get("action")
        request_id = message.get("request_id")
        if not isinstance(action, str) or not action:
            return _error(None, request_id, "missing_field", "action is required.")
        if action not in HOST_ACTIONS:
            return _error(None, request_id, "unsupported_action", "Unsupported action.")

        if action == "ping":
            rid = secrets.token_hex(16)
            resp = _forward("ping", rid, {}, endpoint_path)
            resp["request_id"] = request_id if isinstance(request_id, str) else None
            if resp["ok"]:
                resp["status"] = "connected"
            resp["open_app_supported"] = resp["ok"] or bool((config or {}).get("app_path"))
            return resp

        if action == "add_download":
            try:
                req = validate_add_download_request(message)
            except ValidationError as exc:
                logger.info("Rejected add_download: %s", exc.code)
                return _error(action, request_id, exc.code, exc.message)
            logger.info("Forwarding add_download request=%s browser=%s url=%s",
                        req.request_id, req.browser, redact_url(req.url))
            return _forward("add_download", req.request_id, req.to_payload(), endpoint_path)

        # open_app
        rid = secrets.token_hex(16)
        resp = _forward("open_app", rid, {}, endpoint_path, ipc_action="show_window")
        resp["request_id"] = request_id if isinstance(request_id, str) else None
        if resp["ok"] or resp["error_code"] not in ("app_not_running", "invalid_endpoint"):
            return resp
        cfg = config if config is not None else load_host_config()
        try:
            launched = _launch_application(cfg)
        except OSError:
            logger.exception("Launching the application failed.")
            launched = False
        if launched:
            ok = _base("open_app", resp["request_id"])
            ok.update(ok=True, status="launched", message="Starting My Download Manager…")
            return ok
        return _error("open_app", resp["request_id"], "open_not_supported",
                      "My Download Manager is not running and no application path was configured "
                      "during installation. Start it from the Start menu or your shortcut.")
    except Exception:  # pragma: no cover - last-resort guard, the host must always answer
        logger.exception("Unexpected error while handling a message.")
        return _error(None, None, "internal_error", "The native host encountered an internal error.")


def run(stdin: BinaryIO, stdout: BinaryIO, endpoint_path: Optional[Path] = None,
        config: Optional[Dict[str, Any]] = None) -> int:
    """Processes messages until the browser closes the port. Returns an exit code."""
    while True:
        try:
            message = read_message(stdin)
        except NativeMessagingError as exc:
            logger.warning("Invalid frame from browser: %s", exc.code)
            try:
                write_message(stdout, _error(None, None, exc.code, exc.message))
            except OSError:
                return 1
            if exc.recoverable:
                continue
            return 1
        if message is None:
            return 0
        response = handle_message(message, endpoint_path, config)
        try:
            write_message(stdout, response)
        except OSError:
            return 1  # browser went away


def _configure_logging() -> None:
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        log_dir = proto.default_data_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = logging.handlers.RotatingFileHandler(
            log_dir / "native_host.log", maxBytes=512 * 1024, backupCount=3, encoding="utf-8"
        )
    except OSError:
        handler = logging.StreamHandler(sys.stderr)  # never stdout
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)


def main() -> int:
    _configure_logging()
    stdin = sys.stdin.buffer if sys.stdin is not None else open(0, "rb", buffering=0, closefd=False)
    stdout = sys.stdout.buffer if sys.stdout is not None else open(1, "wb", buffering=0, closefd=False)
    # Defensive: anything accidentally printed must never corrupt the protocol stream.
    sys.stdout = sys.stderr
    if sys.platform == "win32":
        import msvcrt
        msvcrt.setmode(stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(stdout.fileno(), os.O_BINARY)
    logger.info("Native host started (pid %d).", os.getpid())
    try:
        code = run(stdin, stdout, config=load_host_config())
    except KeyboardInterrupt:
        code = 0
    logger.info("Native host exiting with code %d.", code)
    return code


if __name__ == "__main__":
    sys.exit(main())
