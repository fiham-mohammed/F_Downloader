"""
Authenticated localhost IPC server for browser integration (Stage 6A).

Design notes
------------
* ``QTcpServer`` / ``QTcpSocket`` are event driven and live on the Qt main
  thread, so no socket threads exist at all and nothing can block the GUI.
* Binding is to ``QHostAddress.LocalHost`` (127.0.0.1) on a random port only.
  Peers that are not loopback are dropped immediately.
* Every request carries the per-launch random token, compared with
  ``hmac.compare_digest``.
* One request per connection; a per-connection timer closes idle or slow
  connections; frames larger than 1 MB are rejected.
* Memory per connection is bounded: the QTcpSocket read buffer is capped
  (``READ_BUFFER_SIZE``) so Qt stops pulling data from the OS once it is full
  (TCP flow control pushes back on the peer), and data is *pulled* from the
  socket only in the amounts the current frame still needs
  (``BoundedFrameReader``). The declared length is checked as soon as the
  4-byte header arrives, so oversized frames are rejected before any body
  bytes are consumed. Worst case per connection is
  ``HEADER_SIZE + max_message_size + READ_BUFFER_SIZE``.
* Validated requests are delivered to the handler through a queued Qt signal,
  so the handler (which touches the QueueManager) always runs on the main
  thread from a clean event-loop iteration.
* Successful responses are cached in memory by ``request_id`` (plus a digest of
  the payload) as an optimisation; the durable, restart-proof idempotency
  record lives in SQLite (see ``BrowserIntegrationService``).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from PySide6.QtCore import QObject, QTimer, Signal, Slot, Qt
from PySide6.QtNetwork import QHostAddress, QTcpServer, QTcpSocket, QAbstractSocket

from mdm.browser_integration import ipc_protocol as proto
from mdm.browser_integration.request_validator import ValidationError, REQUEST_ID_PATTERN

logger = logging.getLogger("mdm.browser_integration.ipc")

# Cap on Qt's internal per-socket read buffer. Once full, Qt stops reading from
# the OS socket until we consume data, so a peer cannot make us buffer more.
READ_BUFFER_SIZE = 64 * 1024

# Handler signature: handler(action, payload) -> (status, data, message)
Handler = Callable[[str, Dict[str, Any]], "tuple[str, Dict[str, Any], str]"]


class _Connection:
    __slots__ = ("cid", "socket", "reader", "timer", "busy", "closed")

    def __init__(self, cid: int, sock: QTcpSocket, reader: proto.BoundedFrameReader, timer: QTimer):
        self.cid = cid
        self.socket = sock
        self.reader = reader
        self.timer = timer
        self.busy = False
        self.closed = False


class BrowserIpcServer(QObject):
    """Localhost-only, token-authenticated, length-prefixed JSON IPC server."""

    _request_ready = Signal(int, str, str, str, dict)  # connection id, request id, dedupe key, action, payload
    request_handled = Signal(str, str, bool)      # action, request id, ok

    def __init__(
        self,
        handler: Handler,
        endpoint_path: Optional[Path] = None,
        parent: Optional[QObject] = None,
        *,
        request_timeout_ms: int = 10_000,
        max_connections: int = 16,
        max_message_size: int = proto.MAX_MESSAGE_SIZE,
        read_buffer_size: int = READ_BUFFER_SIZE,
        dedupe_capacity: int = 1000,
        dedupe_ttl_seconds: float = 900.0,
        clock: Callable[[], float] = time.time,
    ):
        super().__init__(parent)
        self._handler = handler
        self.endpoint_path = Path(endpoint_path) if endpoint_path else proto.default_endpoint_path()
        self.request_timeout_ms = request_timeout_ms
        self.max_connections = max_connections
        self.max_message_size = max_message_size
        self.read_buffer_size = read_buffer_size
        # Highest number of request bytes held for any single connection
        # (frame reader buffer + unread Qt socket buffer). For tests/diagnostics.
        self.peak_connection_bytes = 0
        self._dedupe_capacity = dedupe_capacity
        self._dedupe_ttl = dedupe_ttl_seconds
        self._clock = clock

        self._server: Optional[QTcpServer] = None
        self._token: Optional[str] = None
        self._connections: Dict[int, _Connection] = {}
        self._next_cid = 1
        # request_id -> (timestamp, payload digest, response)
        self._completed: "OrderedDict[str, tuple[float, str, Dict[str, Any]]]" = OrderedDict()
        self._in_flight: Dict[str, list] = {}
        self._stopping = False

        # Queued connection: the handler never runs re-entrantly inside readyRead.
        self._request_ready.connect(self._dispatch, Qt.QueuedConnection)

    # ------------------------------------------------------------------ state
    @property
    def is_listening(self) -> bool:
        return bool(self._server and self._server.isListening())

    @property
    def port(self) -> Optional[int]:
        return int(self._server.serverPort()) if self.is_listening else None

    @property
    def server_address(self) -> Optional[str]:
        return self._server.serverAddress().toString() if self.is_listening else None

    @property
    def active_connection_count(self) -> int:
        return len(self._connections)

    @property
    def accepted_connection_count(self) -> int:
        """Total connections accepted since construction (diagnostics/tests)."""
        return self._next_cid - 1

    @property
    def max_bytes_per_connection(self) -> int:
        """Upper bound on request bytes buffered for one connection."""
        return proto.HEADER_SIZE + self.max_message_size + self.read_buffer_size

    # -------------------------------------------------------------- lifecycle
    def start(self) -> bool:
        """Starts listening and publishes the endpoint file.

        Returns False (without raising) if another live instance already owns
        the endpoint or the server cannot listen.
        """
        if self.is_listening:
            return True
        self._stopping = False

        if self._another_instance_is_live():
            logger.warning("Browser integration endpoint is owned by another running instance; not starting.")
            return False
        self._remove_endpoint_file(only_if_ours=False)

        server = QTcpServer(self)
        server.setMaxPendingConnections(self.max_connections)
        if not server.listen(QHostAddress(QHostAddress.LocalHost), 0):
            logger.error("Browser integration IPC server could not listen: %s", server.errorString())
            server.deleteLater()
            return False
        server.newConnection.connect(self._on_new_connection)
        self._server = server
        self._token = secrets.token_urlsafe(32)

        try:
            proto.write_endpoint_atomic(self.endpoint_path, int(server.serverPort()), self._token)
        except OSError as exc:
            logger.error("Could not write browser integration endpoint file: %s", exc.__class__.__name__)
            self.stop()
            return False

        logger.info("Browser integration IPC listening on 127.0.0.1:%d", server.serverPort())
        return True

    def stop(self) -> None:
        """Stops accepting connections, closes sockets and removes the endpoint file."""
        self._stopping = True
        if self._server is not None:
            try:
                self._server.newConnection.disconnect(self._on_new_connection)
            except (RuntimeError, TypeError):
                pass
            self._server.close()
            self._server.deleteLater()
            self._server = None
        for conn in list(self._connections.values()):
            self._close(conn, abort=True)
        self._connections.clear()
        self._in_flight.clear()
        self._remove_endpoint_file(only_if_ours=True)
        self._token = None

    # --------------------------------------------------------------- internals
    def _another_instance_is_live(self) -> bool:
        """Checks whether an existing endpoint file belongs to a responsive instance."""
        try:
            endpoint = proto.read_endpoint(self.endpoint_path)
        except proto.ProtocolError:
            return False
        try:
            response = proto.send_request(
                endpoint, "ping", secrets.token_hex(16), {}, connect_timeout=0.5, response_timeout=1.0
            )
        except proto.ProtocolError:
            return False
        return bool(response.get("ok"))

    def _remove_endpoint_file(self, only_if_ours: bool) -> None:
        try:
            if not self.endpoint_path.exists():
                return
            if only_if_ours:
                try:
                    current = proto.read_endpoint(self.endpoint_path)
                except proto.ProtocolError:
                    return
                if not self._token or not hmac.compare_digest(current.token.encode(), self._token.encode()):
                    return
            self.endpoint_path.unlink()
        except OSError:
            logger.warning("Could not remove browser integration endpoint file.")

    @Slot()
    def _on_new_connection(self) -> None:
        server = self._server
        if server is None:
            return
        while server.hasPendingConnections():
            sock = server.nextPendingConnection()
            if sock is None:
                break
            if self._stopping or len(self._connections) >= self.max_connections:
                sock.abort()
                sock.deleteLater()
                continue
            if not sock.peerAddress().isLoopback():
                logger.warning("Rejected non-loopback IPC peer.")
                sock.abort()
                sock.deleteLater()
                continue

            cid = self._next_cid
            self._next_cid += 1
            sock.setReadBufferSize(self.read_buffer_size)
            timer = QTimer(self)
            timer.setSingleShot(True)
            conn = _Connection(cid, sock, proto.BoundedFrameReader(self.max_message_size), timer)
            self._connections[cid] = conn

            timer.timeout.connect(lambda c=conn: self._on_timeout(c))
            sock.readyRead.connect(lambda c=conn: self._on_ready_read(c))
            sock.disconnected.connect(lambda c=conn: self._close(c))
            sock.errorOccurred.connect(lambda _err, c=conn: self._on_socket_error(c))
            timer.start(self.request_timeout_ms)

    def _on_socket_error(self, conn: _Connection) -> None:
        if conn.socket.error() != QAbstractSocket.RemoteHostClosedError:
            logger.debug("IPC socket error on connection %d", conn.cid)
        self._close(conn)

    def _on_timeout(self, conn: _Connection) -> None:
        if conn.closed:
            return
        logger.info("IPC connection %d timed out.", conn.cid)
        if not conn.busy:
            self._send(conn, proto.error_response(None, "timeout", "Request was not received in time."))
        self._close(conn)

    def _on_ready_read(self, conn: _Connection) -> None:
        # One request per connection: once a frame is complete nothing more is
        # read; extra bytes stay in Qt's capped buffer until the socket closes.
        sock = conn.socket
        while not conn.closed and not conn.busy:
            available = int(sock.bytesAvailable())
            self._track_peak(conn, available)
            if available <= 0:
                return
            want = min(available, conn.reader.bytes_needed())
            chunk = bytes(sock.read(want).data())
            if not chunk:
                return
            try:
                message = conn.reader.feed(chunk)
            except proto.ProtocolError as exc:
                logger.info("IPC connection %d rejected: %s", conn.cid, exc.code)
                self._reply_and_close(conn, proto.error_response(None, exc.code, exc.message))
                return
            self._track_peak(conn, int(sock.bytesAvailable()))
            if message is not None:
                conn.busy = True
                self._process(conn, message)
                return

    def _track_peak(self, conn: _Connection, unread: int) -> None:
        held = conn.reader.buffered_bytes + max(0, unread)
        if held > self.peak_connection_bytes:
            self.peak_connection_bytes = held

    def _process(self, conn: _Connection, envelope: Dict[str, Any]) -> None:
        raw_rid = envelope.get("request_id")
        rid = raw_rid if isinstance(raw_rid, str) and REQUEST_ID_PATTERN.match(raw_rid) else None

        # 1. Authenticate before anything else is interpreted.
        token = envelope.get("token")
        if (
            not isinstance(token, str)
            or self._token is None
            or not hmac.compare_digest(token.encode("utf-8"), self._token.encode("utf-8"))
        ):
            logger.warning("Rejected unauthenticated IPC request.")
            self._reply_and_close(conn, proto.error_response(rid, "unauthorized", "Authentication failed."))
            return

        # 2. Envelope schema.
        try:
            action, payload = self._validate_envelope(envelope, rid)
        except ValidationError as exc:
            self._reply_and_close(conn, proto.error_response(rid, exc.code, exc.message))
            return

        # 3. Replay / duplicate protection (in-memory fast path). Only an exact
        #    repeat (same request_id AND same payload) is answered from cache;
        #    anything else reaches the handler, which owns the durable record.
        digest = self._payload_digest(action, payload)
        self._prune_completed()
        cached = self._completed.get(rid)
        if cached is not None and cached[1] == digest:
            response = dict(cached[2])
            response["status"] = "duplicate"
            response["message"] = "This request was already processed."
            self._reply_and_close(conn, response)
            return
        key = f"{rid}:{digest}"
        if key in self._in_flight:
            self._in_flight[key].append(conn.cid)
            return

        self._in_flight[key] = [conn.cid]
        self._request_ready.emit(conn.cid, rid, key, action, payload)

    @staticmethod
    def _payload_digest(action: str, payload: Dict[str, Any]) -> str:
        canonical = json.dumps([action, payload], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _validate_envelope(self, envelope: Dict[str, Any], rid: Optional[str]) -> "tuple[str, Dict[str, Any]]":
        if set(envelope.keys()) - proto.ENVELOPE_FIELDS:
            raise ValidationError("invalid_message", "Request envelope contains unsupported fields.")
        if envelope.get("v") != proto.PROTOCOL_VERSION:
            raise ValidationError("unsupported_version", "Unsupported protocol version.")
        if rid is None:
            raise ValidationError("invalid_field", "request_id is missing or invalid.")
        ts = envelope.get("ts")
        if not isinstance(ts, (int, float)) or isinstance(ts, bool):
            raise ValidationError("invalid_field", "ts is missing or invalid.")
        if abs(self._clock() - float(ts)) > proto.MAX_CLOCK_SKEW_SECONDS:
            raise ValidationError("stale_request", "Request timestamp is outside the accepted window.")
        action = envelope.get("action")
        if action not in proto.IPC_ACTIONS:
            raise ValidationError("unsupported_action", "Unsupported action.")
        payload = envelope.get("payload", {})
        if not isinstance(payload, dict):
            raise ValidationError("invalid_field", "payload must be an object.")
        return action, payload

    @Slot(int, str, str, str, dict)
    def _dispatch(self, cid: int, rid: str, key: str, action: str, payload: Dict[str, Any]) -> None:
        waiting = self._in_flight.pop(key, [cid])
        if self._stopping:
            response = proto.error_response(rid, "shutting_down", "My Download Manager is shutting down.")
        else:
            try:
                status, data, message = self._handler(action, payload)
                response = proto.build_response(rid, True, status, message=message, data=data)
            except ValidationError as exc:
                response = proto.error_response(rid, exc.code, exc.message)
            except Exception:  # pragma: no cover - defensive, details go to the log only
                logger.exception("Browser integration handler failed for action '%s'.", action)
                response = proto.error_response(rid, "internal_error", "My Download Manager could not process the request.")

        if response["ok"]:
            self._completed[rid] = (self._clock(), key.rsplit(":", 1)[1], response)
            self._completed.move_to_end(rid)
            while len(self._completed) > self._dedupe_capacity:
                self._completed.popitem(last=False)
        self.request_handled.emit(action, rid, bool(response["ok"]))

        for i, waiting_cid in enumerate(waiting):
            conn = self._connections.get(waiting_cid)
            if conn is None or conn.closed:
                continue
            reply = response
            if i > 0 and response["ok"]:
                reply = dict(response, status="duplicate", message="This request was already processed.")
            self._reply_and_close(conn, reply)

    def _prune_completed(self) -> None:
        cutoff = self._clock() - self._dedupe_ttl
        while self._completed:
            rid, (stamp, _digest, _resp) = next(iter(self._completed.items()))
            if stamp >= cutoff:
                break
            self._completed.popitem(last=False)

    def _send(self, conn: _Connection, response: Dict[str, Any]) -> None:
        if conn.closed:
            return
        try:
            frame = proto.encode_frame(response, self.max_message_size)
        except proto.ProtocolError:
            frame = proto.encode_frame(proto.error_response(response.get("request_id"), "internal_error",
                                                            "Response too large."))
        conn.socket.write(frame)
        conn.socket.flush()

    def _reply_and_close(self, conn: _Connection, response: Dict[str, Any]) -> None:
        self._send(conn, response)
        if not conn.closed:
            conn.socket.disconnectFromHost()  # graceful: pending bytes are flushed first
            conn.timer.stop()

    def _close(self, conn: _Connection, abort: bool = False) -> None:
        if conn.closed:
            return
        conn.closed = True
        conn.timer.stop()
        conn.timer.deleteLater()
        self._connections.pop(conn.cid, None)
        sock = conn.socket
        try:
            if abort:
                sock.abort()
            elif sock.state() != QAbstractSocket.UnconnectedState:
                sock.disconnectFromHost()
        except RuntimeError:
            pass
        sock.deleteLater()
