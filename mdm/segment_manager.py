import os
import re
import time
import errno
import shutil
import uuid
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Tuple, List, Dict, Any
import httpx

from mdm.utils import (
    sanitize_filename,
    safe_finalize_file,
    parse_content_disposition,
    extract_filename_from_url,
)
from mdm.downloader import (
    HttpDownloader,
    DownloadError,
    DownloadCancelledError,
    RETRYABLE_STATUS_CODES,
    RETRYABLE_EXCEPTIONS,
    is_local_disk_error,
)
from mdm.settings import get_min_segment_size


class RangeNotSupportedError(Exception):
    """Raised when server does not support HTTP Range requests or returns invalid ranges."""
    pass


class SegmentValidationError(DownloadError):
    """Raised when a downloaded segment fails size or integrity validation."""
    pass


def is_strong_etag(etag: Optional[str]) -> bool:
    """
    Checks if an ETag is a strong validator according to RFC 7232/9110.
    Weak ETags start with W/ or w/ and must not be used for sub-range requests with If-Range.
    """
    if not etag:
        return False
    val = etag.strip()
    if val.startswith(('W/"', 'w/"', "W/'", "w/'")):
        return False
    return True


def calculate_segments(
    total_size: int,
    requested_connections: int,
    min_segment_size: int = 2 * 1024 * 1024,
) -> List[Tuple[int, int]]:
    """
    Calculates exact (start_byte, end_byte) ranges for segmented downloading.
    Offsets are inclusive: 0 to total_size - 1.
    Guarantees no gaps and no overlapping bytes.
    """
    if total_size <= 0:
        return []
    if total_size < min_segment_size or requested_connections <= 1:
        return [(0, total_size - 1)]

    # Do not create more segments than useful for smaller files (each segment >= 256 KB)
    max_useful = max(1, total_size // (256 * 1024))
    num_segments = min(requested_connections, max_useful)
    num_segments = max(1, num_segments)

    if num_segments == 1:
        return [(0, total_size - 1)]

    seg_size = total_size // num_segments
    segments = []
    current_start = 0

    for i in range(num_segments):
        if i == num_segments - 1:
            current_end = total_size - 1
        else:
            current_end = current_start + seg_size - 1
        segments.append((current_start, current_end))
        current_start = current_end + 1

    return segments


@dataclass
class Segment:
    index: int
    start_byte: int
    end_byte: int
    expected_size: int
    downloaded_bytes: int = 0
    status: str = "Queued"  # "Queued", "Downloading", "Completed", "Failed", "Retrying"
    part_file: Optional[Path] = None
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        pct = (self.downloaded_bytes / self.expected_size * 100.0) if self.expected_size > 0 else 0.0
        return {
            "index": self.index,
            "start": self.start_byte,
            "end": self.end_byte,
            "expected_size": self.expected_size,
            "downloaded": self.downloaded_bytes,
            "progress": f"{min(100.0, pct):.1f}%",
            "progress_value": int(round(min(100.0, pct))),
            "status": self.status,
            "error": self.error,
        }


class SegmentedDownloader:
    """
    Advanced multi-connection HTTP/HTTPS download engine with:
    - Server HTTP Range capability detection (bytes=0-0)
    - Automatic fallback to single-connection download when unsupported
    - Exact segment boundary calculation without gaps or overlaps
    - Parallel streaming downloads using ThreadPoolExecutor
    - Low memory footprint (streams chunks directly to disk)
    - ETag and Last-Modified validator consistency
    - Per-segment retries (up to 3 attempts) for transient errors
    - Atomic byte-ordered segment merging and size verification
    - Safe no-overwrite file finalisation
    - Clean cancellation and temporary file cleanup
    """

    def __init__(
        self,
        url: str,
        destination_folder: str | Path,
        connections: int = 8,
        explicit_filename: Optional[str] = None,
        suggested_filename: Optional[str] = None,
        max_retries: int = 3,
        chunk_size: int = 65536,
        timeout_seconds: float = 20.0,
        min_segment_size: Optional[int] = None,
        is_cancelled: Optional[Callable[[], bool]] = None,
        on_progress: Optional[Callable[[int, Optional[int], float, Optional[float]], None]] = None,
        on_filename_detected: Optional[Callable[[str], None]] = None,
        on_retry: Optional[Callable[[int, int, str], None]] = None,
        on_segments_updated: Optional[Callable[[List[Dict[str, Any]]], None]] = None,
        on_connection_status: Optional[Callable[[int, int, str], None]] = None,
        transport: Optional[httpx.BaseTransport] = None,
    ):
        self.url = url.strip()
        self.destination_folder = Path(destination_folder)
        self.requested_connections = max(1, min(16, connections))
        self.explicit_filename = explicit_filename if explicit_filename is not None else suggested_filename
        self.max_retries = max_retries
        self.chunk_size = chunk_size
        self.timeout = httpx.Timeout(timeout_seconds, connect=10.0, read=timeout_seconds)
        self.min_segment_size = min_segment_size if min_segment_size is not None else get_min_segment_size()
        self.transport = transport

        self.is_cancelled_cb = is_cancelled or (lambda: False)
        self.on_progress_cb = on_progress
        self.on_filename_detected_cb = on_filename_detected
        self.on_retry_cb = on_retry
        self.on_segments_updated_cb = on_segments_updated
        self.on_connection_status_cb = on_connection_status

        self._is_cancelled = False
        self._lock = threading.Lock()
        self._active_clients: List[httpx.Client] = []
        self._active_responses: List[httpx.Response] = []
        self._active_fallback_downloader: Optional[HttpDownloader] = None

        self.segments: List[Segment] = []
        self.parts_dir: Optional[Path] = None
        self.merged_part_path: Optional[Path] = None
        self.final_path: Optional[Path] = None

        self.total_bytes: Optional[int] = None
        self.downloaded_bytes: int = 0
        self.speed: float = 0.0
        self.eta: Optional[float] = None
        self.effective_filename: str = ""
        self.active_connections: int = 0
        self.mode_description: str = "Segmented"

        self.probed_etag: Optional[str] = None
        self.probed_last_modified: Optional[str] = None
        self._wire_bytes_received: int = 0
        self._fatal_error: bool = False
        self._fatal_exception: Optional[Exception] = None

    def _abort_active_streams(self):
        """Immediately closes all active HTTP responses and clients to unblock threads."""
        with self._lock:
            for resp in list(self._active_responses):
                try:
                    resp.close()
                except Exception:
                    pass
            for client in list(self._active_clients):
                try:
                    client.close()
                except Exception:
                    pass

    def cancel(self):
        """Thread-safe cancellation of all segment downloads and active connections."""
        self._is_cancelled = True
        with self._lock:
            if self._active_fallback_downloader is not None:
                try:
                    self._active_fallback_downloader.cancel()
                except Exception:
                    pass
        self._abort_active_streams()
        self._cleanup_parts()

    def _check_cancelled(self):
        if self._is_cancelled or self.is_cancelled_cb():
            self._cleanup_parts()
            raise DownloadCancelledError("Download cancelled by user.")

    def _check_segment_aborted(self):
        self._check_cancelled()
        if self._fatal_error:
            raise self._fatal_exception or DownloadError("Download aborted due to fatal error in a segment.")

    def _cleanup_parts(self):
        """Cleans up temporary segment files and temporary directories."""
        if self.parts_dir and self.parts_dir.exists():
            try:
                shutil.rmtree(self.parts_dir, ignore_errors=True)
            except Exception:
                pass
        if self.merged_part_path and self.merged_part_path.exists():
            try:
                self.merged_part_path.unlink(missing_ok=True)
            except Exception:
                pass

    def _emit_segments(self):
        if self.on_segments_updated_cb:
            with self._lock:
                data = [s.to_dict() for s in self.segments]
            try:
                self.on_segments_updated_cb(data)
            except Exception:
                pass

    def _emit_connection_status(self):
        if self.on_connection_status_cb:
            with self._lock:
                active = sum(1 for s in self.segments if s.status == "Downloading")
                total_conns = len(self.segments) if self.segments else self.requested_connections
                desc = self.mode_description
            try:
                self.on_connection_status_cb(active, total_conns, desc)
            except Exception:
                pass

    def download(self) -> Tuple[Path, int]:
        """
        Executes download. Probes server for Range support; if supported, downloads
        in parallel segments and merges. Otherwise falls back to single-connection mode.
        """
        self._check_cancelled()
        try:
            self.destination_folder.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            raise DownloadError(f"Cannot create destination folder: {e}") from e

        # Validate URL scheme
        if not (self.url.startswith("http://") or self.url.startswith("https://")):
            raise DownloadError("Invalid URL scheme. Only HTTP and HTTPS are supported.")

        # If user explicitly requested only 1 connection, use single-connection directly
        if self.requested_connections <= 1:
            return self._fallback_to_single_connection("Single connection (1 connection selected)")

        # 1. Server capability detection via Range probe
        supports_range, total_size, detected_name, etag, last_modified = self._probe_range_capability()

        self._check_cancelled()

        if detected_name:
            self.effective_filename = detected_name
            if self.on_filename_detected_cb:
                self.on_filename_detected_cb(detected_name)

        # Check if fallback to single-connection mode is necessary
        if not supports_range:
            return self._fallback_to_single_connection("Single connection mode (Server does not support Range requests)")

        if total_size is None or total_size <= 0:
            return self._fallback_to_single_connection("Single connection mode (Unknown file size)")

        if total_size < self.min_segment_size:
            return self._fallback_to_single_connection(
                f"Single connection mode (File size {total_size} bytes below {self.min_segment_size} bytes threshold)"
            )

        # 2. Segment calculation
        raw_segments = calculate_segments(total_size, self.requested_connections, self.min_segment_size)
        if len(raw_segments) <= 1:
            return self._fallback_to_single_connection("Single connection mode (1 segment calculated)")

        self.total_bytes = total_size
        self.mode_description = f"Multi-connection ({len(raw_segments)} connections)"
        self._emit_connection_status()

        # 3. Setup temporary working directory for segments
        safe_name = sanitize_filename(self.effective_filename or extract_filename_from_url(self.url, "download"))
        unique_token = uuid.uuid4().hex[:8]
        self.parts_dir = self.destination_folder / f".{safe_name}_{unique_token}.parts"
        self.parts_dir.mkdir(parents=True, exist_ok=True)

        self.segments = [
            Segment(
                index=i,
                start_byte=start,
                end_byte=end,
                expected_size=(end - start + 1),
                part_file=self.parts_dir / f"segment_{i:02d}.part",
            )
            for i, (start, end) in enumerate(raw_segments)
        ]
        self._emit_segments()

        # 4. Execute parallel segment download with graceful fallback if server fails on ranges
        try:
            return self._download_segments_parallel(etag, last_modified)
        except RangeNotSupportedError as rne:
            # Server accepted initial probe but rejected subsequent range requests: fallback cleanly
            self._cleanup_parts()
            return self._fallback_to_single_connection(f"Single connection fallback ({rne})")
        except DownloadCancelledError:
            self._cleanup_parts()
            raise
        except Exception:
            self._cleanup_parts()
            raise

    def _probe_range_capability(self) -> Tuple[bool, Optional[int], str, Optional[str], Optional[str]]:
        """
        Sends an HTTP Range request with bytes=0-0 to test:
        1. Whether server responds with HTTP 206 Partial Content.
        2. Validates Content-Range and extracts total file size.
        3. Extracts ETag and Last-Modified validators.
        4. Detects filename from Content-Disposition or final redirected URL.
        """
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) MyDownloadManager/3.0",
            "Range": "bytes=0-0",
            "Accept": "*/*",
            "Accept-Encoding": "identity",
        }

        client_kwargs = {"follow_redirects": True, "timeout": self.timeout}
        if self.transport is not None:
            client_kwargs["transport"] = self.transport

        try:
            with httpx.Client(**client_kwargs) as client:
                with self._lock:
                    self._active_clients.append(client)
                try:
                    with client.stream("GET", self.url, headers=headers) as response:
                        self._check_cancelled()

                        # Determine filename
                        header_filename = parse_content_disposition(response.headers.get("content-disposition"))
                        if header_filename:
                            detected_name = sanitize_filename(header_filename)
                        elif self.explicit_filename:
                            detected_name = sanitize_filename(self.explicit_filename)
                        else:
                            redirected_name = extract_filename_from_url(str(response.url))
                            if redirected_name and redirected_name != "download":
                                detected_name = redirected_name
                            else:
                                detected_name = extract_filename_from_url(self.url, fallback="download")

                        etag = response.headers.get("etag")
                        last_modified = response.headers.get("last-modified")

                        # If server returns HTTP 200 instead of HTTP 206, Range is NOT supported
                        if response.status_code == 200:
                            # It ignored Range: bytes=0-0 and tried to send full content
                            total_size = None
                            cl = response.headers.get("content-length")
                            if cl and cl.isdigit():
                                total_size = int(cl)
                            return False, total_size, detected_name, etag, last_modified

                        if response.status_code != 206:
                            # Range rejected or error
                            return False, None, detected_name, etag, last_modified

                        # Server returned 206! Strictly validate Content-Range header
                        content_range = response.headers.get("content-range", "").strip()
                        # Strict format: "bytes 0-0/TOTAL"
                        match = re.match(r"^bytes\s+(\d+)-(\d+)/(\d+)$", content_range, re.IGNORECASE)
                        if not match:
                            return False, None, detected_name, etag, last_modified

                        start_b = int(match.group(1))
                        end_b = int(match.group(2))
                        total_b = int(match.group(3))

                        # Strict probe validation: start must be 0, end must be 0, total must be positive and > end_b
                        if start_b != 0 or end_b != 0 or total_b <= 0 or total_b <= end_b:
                            return False, None, detected_name, etag, last_modified

                        self.probed_etag = etag
                        self.probed_last_modified = last_modified
                        return True, total_b, detected_name, etag, last_modified
                finally:
                    with self._lock:
                        if client in self._active_clients:
                            self._active_clients.remove(client)
        except Exception:
            return False, None, "", None, None

    def _fallback_to_single_connection(self, reason: str) -> Tuple[Path, int]:
        """Falls back gracefully to the Stage 2 single-connection engine."""
        with self._lock:
            self._fatal_error = False
            self._fatal_exception = None
        self._check_cancelled()
        self.mode_description = reason
        self.active_connections = 1
        self._emit_connection_status()

        downloader = HttpDownloader(
            url=self.url,
            destination_folder=self.destination_folder,
            explicit_filename=self.effective_filename or self.explicit_filename,
            max_retries=self.max_retries,
            chunk_size=self.chunk_size,
            timeout_seconds=self.timeout.read or 20.0,
            is_cancelled=self.is_cancelled_cb,
            on_progress=self.on_progress_cb,
            on_filename_detected=self.on_filename_detected_cb,
            on_retry=self.on_retry_cb,
            transport=self.transport,
        )

        with self._lock:
            self._active_fallback_downloader = downloader

        try:
            final_path, total_downloaded = downloader.download()
            self.final_path = final_path
            self.downloaded_bytes = total_downloaded
            return final_path, total_downloaded
        finally:
            with self._lock:
                self._active_fallback_downloader = None

    def _download_segments_parallel(
        self,
        etag: Optional[str],
        last_modified: Optional[str],
    ) -> Tuple[Path, int]:
        """
        Manages parallel download of all segments using ThreadPoolExecutor.
        Merges completed segments and verifies final output.
        """
        num_workers = len(self.segments)
        start_time = time.time()
        last_speed_check_time = start_time
        bytes_at_last_check = 0
        speed_samples = []

        def update_progress():
            nonlocal last_speed_check_time, bytes_at_last_check, speed_samples
            now = time.time()
            with self._lock:
                wire_now = self._wire_bytes_received
                total_dl = sum(min(s.expected_size, s.downloaded_bytes) for s in self.segments)
                self.downloaded_bytes = total_dl
                time_delta = now - last_speed_check_time
                if time_delta >= 0.25:
                    delta_b = max(0, wire_now - bytes_at_last_check)
                    instant_speed = delta_b / time_delta
                    speed_samples.append(instant_speed)
                    if len(speed_samples) > 5:
                        speed_samples.pop(0)
                    self.speed = sum(speed_samples) / len(speed_samples)
                    last_speed_check_time = now
                    bytes_at_last_check = wire_now
                    if self.total_bytes and self.speed > 0:
                        rem = max(0, self.total_bytes - total_dl)
                        self.eta = rem / self.speed
                    else:
                        self.eta = None

            if self.on_progress_cb:
                self.on_progress_cb(total_dl, self.total_bytes, self.speed, self.eta)
            self._emit_segments()
            self._emit_connection_status()

        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = [
                executor.submit(
                    self._download_single_segment,
                    segment,
                    etag,
                    last_modified,
                    update_progress,
                )
                for segment in self.segments
            ]

            try:
                for future in as_completed(futures):
                    self._check_cancelled()
                    future.result()
            except Exception as exc:
                with self._lock:
                    self._fatal_error = True
                    if not self._fatal_exception:
                        self._fatal_exception = exc
                self._abort_active_streams()
                for f in futures:
                    f.cancel()
                raise

        # All segments completed successfully! Now merge them.
        self._check_cancelled()
        final_path, total_size = self._merge_segments()

        # Final progress update at 100%
        if self.on_progress_cb:
            self.on_progress_cb(total_size, total_size, 0.0, 0.0)
        self._emit_segments()
        self._emit_connection_status()

        return final_path, total_size

    def _download_single_segment(
        self,
        segment: Segment,
        etag: Optional[str],
        last_modified: Optional[str],
        progress_callback: Callable[[], None],
    ):
        """
        Downloads a single segment with independent retries (up to max_retries).
        Validates HTTP 206, Content-Range, and exact byte counts.
        """
        attempt = 0
        last_error = ""

        while attempt < self.max_retries:
            self._check_segment_aborted()
            attempt += 1

            # Check if this segment was already completed in a prior attempt
            if segment.part_file and segment.part_file.exists():
                try:
                    if segment.part_file.stat().st_size == segment.expected_size:
                        segment.status = "Completed"
                        segment.downloaded_bytes = segment.expected_size
                        progress_callback()
                        return
                except OSError:
                    pass

            segment.status = f"Retrying ({attempt}/{self.max_retries})" if attempt > 1 else "Downloading"
            self._emit_segments()
            self._emit_connection_status()

            try:
                self._stream_segment_to_file(segment, etag, last_modified, progress_callback)
                segment.status = "Completed"
                segment.downloaded_bytes = segment.expected_size
                segment.error = ""
                progress_callback()
                return

            except DownloadCancelledError:
                segment.status = "Cancelled"
                self.cancel()
                raise
            except RangeNotSupportedError as rne:
                segment.status = "Failed"
                with self._lock:
                    self._fatal_error = True
                    self._fatal_exception = rne
                self._abort_active_streams()
                raise
            except SegmentValidationError as sve:
                segment.status = "Failed"
                segment.error = str(sve)
                if segment.part_file and segment.part_file.exists():
                    try:
                        segment.part_file.unlink(missing_ok=True)
                    except Exception:
                        pass
                with self._lock:
                    self._fatal_error = True
                    self._fatal_exception = sve
                self._abort_active_streams()
                raise sve
            except Exception as exc:
                self._check_segment_aborted()

                if is_local_disk_error(exc):
                    segment.status = "Failed"
                    segment.error = f"Disk error: {exc}"
                    with self._lock:
                        self._fatal_error = True
                        self._fatal_exception = exc
                    self._abort_active_streams()
                    raise DownloadError(f"Disk error on segment {segment.index}: {exc}") from exc

                is_retryable = False
                if isinstance(exc, httpx.HTTPStatusError):
                    code = exc.response.status_code
                    if code == 200:
                        rne = RangeNotSupportedError(f"Server returned 200 OK instead of 206 for segment {segment.index}")
                        segment.status = "Failed"
                        with self._lock:
                            self._fatal_error = True
                            self._fatal_exception = rne
                        self._abort_active_streams()
                        raise rne
                    if code in RETRYABLE_STATUS_CODES:
                        is_retryable = True
                        last_error = f"HTTP {code}"
                    else:
                        segment.status = "Failed"
                        de = DownloadError(f"Permanent HTTP error {code} on segment {segment.index}")
                        with self._lock:
                            self._fatal_error = True
                            self._fatal_exception = de
                        self._abort_active_streams()
                        raise de from exc

                elif isinstance(exc, RETRYABLE_EXCEPTIONS):
                    is_retryable = True
                    last_error = f"Network error: {exc}"
                else:
                    last_error = str(exc)

                segment.error = last_error
                if is_retryable and attempt < self.max_retries:
                    if segment.part_file and segment.part_file.exists():
                        try:
                            segment.part_file.unlink(missing_ok=True)
                        except Exception:
                            pass
                    segment.downloaded_bytes = 0
                    time.sleep(min(attempt * 0.5, 2.0))
                else:
                    segment.status = "Failed"
                    de = DownloadError(
                        f"Segment {segment.index} failed after {self.max_retries} attempts: {last_error}"
                    )
                    with self._lock:
                        self._fatal_error = True
                        self._fatal_exception = de
                    self._abort_active_streams()
                    raise de

    def _stream_segment_to_file(
        self,
        segment: Segment,
        etag: Optional[str],
        last_modified: Optional[str],
        progress_callback: Callable[[], None],
    ):
        """Streams a single segment range into its dedicated part file."""
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) MyDownloadManager/3.0",
            "Range": f"bytes={segment.start_byte}-{segment.end_byte}",
            "Accept": "*/*",
            "Accept-Encoding": "identity",
        }

        # Validator consistency with If-Range:
        # RFC 7232/9110: Weak ETags must NOT be sent in If-Range for sub-range requests!
        target_etag = self.probed_etag or etag
        target_lm = self.probed_last_modified or last_modified
        if is_strong_etag(target_etag):
            headers["If-Range"] = target_etag.strip()
        elif target_lm:
            headers["If-Range"] = target_lm.strip()

        client_kwargs = {"follow_redirects": True, "timeout": self.timeout}
        if self.transport is not None:
            client_kwargs["transport"] = self.transport

        with httpx.Client(**client_kwargs) as client:
            with self._lock:
                self._active_clients.append(client)
            try:
                with client.stream("GET", self.url, headers=headers) as response:
                    with self._lock:
                        self._active_responses.append(response)
                    try:
                        self._check_segment_aborted()

                        if response.status_code == 200:
                            raise RangeNotSupportedError(
                                f"Server sent 200 OK (ignored Range header) for segment {segment.index}"
                            )

                        if response.status_code != 206:
                            response.raise_for_status()

                        # Check validator consistency across segments
                        resp_etag = response.headers.get("etag")
                        if target_etag and resp_etag and resp_etag.strip() != target_etag.strip():
                            raise RangeNotSupportedError(
                                f"Validator mismatch on segment {segment.index}: ETag changed from '{target_etag}' to '{resp_etag}'. Resource was modified on server."
                            )

                        resp_lm = response.headers.get("last-modified")
                        if target_lm and resp_lm and resp_lm.strip() != target_lm.strip():
                            raise RangeNotSupportedError(
                                f"Validator mismatch on segment {segment.index}: Last-Modified changed from '{target_lm}' to '{resp_lm}'. Resource was modified on server."
                            )

                        # Validate Content-Range header strictly: bytes START-END/TOTAL
                        content_range = response.headers.get("content-range", "").strip()
                        match = re.match(r"^bytes\s+(\d+)-(\d+)/(\d+)$", content_range, re.IGNORECASE)
                        if not match:
                            raise SegmentValidationError(
                                f"Malformed or missing Content-Range header '{content_range}' for segment {segment.index}"
                            )

                        start_b = int(match.group(1))
                        end_b = int(match.group(2))
                        total_b = int(match.group(3))

                        if start_b != segment.start_byte or end_b != segment.end_byte:
                            raise SegmentValidationError(
                                f"Content-Range mismatch for segment {segment.index}: got bytes {start_b}-{end_b}, expected {segment.start_byte}-{segment.end_byte}"
                            )

                        if self.total_bytes is not None and total_b != self.total_bytes:
                            raise SegmentValidationError(
                                f"Content-Range total size mismatch for segment {segment.index}: got {total_b}, expected {self.total_bytes}"
                            )

                        # Content-Length check if header present
                        cl_header = response.headers.get("content-length")
                        if cl_header and cl_header.strip().isdigit():
                            cl_val = int(cl_header.strip())
                            if cl_val != segment.expected_size:
                                raise SegmentValidationError(
                                    f"Content-Length header mismatch for segment {segment.index}: got {cl_val}, expected {segment.expected_size}"
                                )

                        # Write segment bytes to part file
                        segment.downloaded_bytes = 0
                        with open(segment.part_file, "wb") as f:
                            for chunk in response.iter_bytes(chunk_size=self.chunk_size):
                                self._check_segment_aborted()
                                if not chunk:
                                    continue
                                f.write(chunk)
                                chunk_len = len(chunk)
                                with self._lock:
                                    self._wire_bytes_received += chunk_len
                                segment.downloaded_bytes += chunk_len
                                progress_callback()

                            f.flush()
                            os.fsync(f.fileno())

                        # Validate final written size
                        actual_size = segment.part_file.stat().st_size
                        if actual_size != segment.expected_size:
                            raise SegmentValidationError(
                                f"Segment {segment.index} size mismatch: got {actual_size} bytes, expected {segment.expected_size} bytes"
                            )

                    finally:
                        with self._lock:
                            if response in self._active_responses:
                                self._active_responses.remove(response)
            finally:
                with self._lock:
                    if client in self._active_clients:
                        self._active_clients.remove(client)

    def _merge_segments(self) -> Tuple[Path, int]:
        """
        Verifies all segments, merges them sequentially into a single temporary output file,
        validates the final merged file size, safely finalizes destination filename without
        overwriting, and removes temporary segment files.
        """
        self._check_cancelled()

        # 1. Verify all segment files exist and have exact expected size
        total_expected = 0
        for seg in self.segments:
            if not seg.part_file or not seg.part_file.exists():
                raise SegmentValidationError(f"Segment file {seg.part_file} is missing before merge.")
            actual_size = seg.part_file.stat().st_size
            if actual_size != seg.expected_size:
                raise SegmentValidationError(
                    f"Segment {seg.index} size verification failed: {actual_size} != {seg.expected_size}"
                )
            total_expected += seg.expected_size

        if self.total_bytes is not None and total_expected != self.total_bytes:
            raise SegmentValidationError(
                f"Combined segment size {total_expected} does not match expected total size {self.total_bytes}."
            )

        # 2. Merge into temporary output file
        safe_name = sanitize_filename(self.effective_filename or "download")
        self.merged_part_path = self.destination_folder / f"{safe_name}.part"

        with open(self.merged_part_path, "wb") as out_f:
            for seg in sorted(self.segments, key=lambda s: s.index):
                self._check_cancelled()
                with open(seg.part_file, "rb") as in_f:
                    shutil.copyfileobj(in_f, out_f, length=self.chunk_size)

            out_f.flush()
            os.fsync(out_f.fileno())

        # 3. Verify final merged size
        merged_size = self.merged_part_path.stat().st_size
        if merged_size != total_expected:
            self._cleanup_parts()
            raise SegmentValidationError(
                f"Merged file size {merged_size} does not match expected size {total_expected}."
            )

        # 4. Clean up temporary segment files and parts folder
        if self.parts_dir and self.parts_dir.exists():
            try:
                shutil.rmtree(self.parts_dir, ignore_errors=True)
            except Exception:
                pass

        # 5. Finalize file safely without overwriting
        self.final_path = safe_finalize_file(
            self.merged_part_path,
            self.destination_folder,
            self.effective_filename or safe_name,
        )

        return self.final_path, merged_size
