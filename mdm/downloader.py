import os
import re
import time
import errno
import logging
import httpx
from pathlib import Path
from typing import Callable, Optional, Tuple, Any

logger = logging.getLogger("mdm.downloader")

from mdm.utils import (
    sanitize_filename,
    safe_finalize_file,
    parse_content_disposition,
    extract_filename_from_url,
    is_strong_etag,
    parse_retry_after,
)


class DownloadError(Exception):
    """Raised when a download encounters an error."""
    def __init__(self, message: str, status_code: Optional[int] = None, retry_after: Optional[str] = None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class DownloadCancelledError(Exception):
    """Raised when a download is cancelled by the user."""
    pass


class DownloadPausedError(Exception):
    """Raised when a download is paused by the user."""
    pass


# Status codes that should trigger an automatic retry
RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}

# Maximum Retry-After delay (in seconds) that is handled internally by the worker.
# Values above this threshold promote the failure to queue-level retry scheduling.
INTERNAL_RETRY_AFTER_MAX_SECONDS = 3.0

# Exceptions from httpx that represent temporary network errors
RETRYABLE_EXCEPTIONS = (
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.ConnectError,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
)

# Local disk/filesystem errors that should NEVER be retried
LOCAL_DISK_ERRORS = (
    PermissionError,
    FileNotFoundError,
    NotADirectoryError,
    IsADirectoryError,
    FileExistsError,
)


def is_local_disk_error(exc: Exception) -> bool:
    """Detects if an exception is caused by a local disk/filesystem failure."""
    if isinstance(exc, LOCAL_DISK_ERRORS):
        return True
    if isinstance(exc, OSError):
        disk_errnos = {
            errno.ENOSPC,        # No space left on device
            errno.EACCES,        # Permission denied
            errno.EPERM,         # Operation not permitted
            errno.EROFS,         # Read-only file system
            errno.ENAMETOOLONG,  # Filename too long
            errno.EIO,           # I/O error
        }
        if exc.errno in disk_errnos:
            return True
        # Windows-specific winerror codes
        winerror = getattr(exc, "winerror", None)
        if winerror in (5, 32, 112):  # 5=Access denied, 32=Sharing violation, 112=Disk full
            return True
    return False


class HttpDownloader:
    """
    HTTP/HTTPS file downloader with support for:
    - Streaming large files (low memory footprint)
    - Content-Disposition & redirected filename extraction
    - Content-Length detection & unknown size handling
    - Temporary .part file safety & safe no-overwrite completion rename
    - Automatic retries for transient errors only (up to 3 attempts)
    - Immediate failure for permanent HTTP & local disk errors
    - Responsive cancellation and cleanup
    - Real-time speed and ETA calculation
    """

    def __init__(
        self,
        url: str,
        destination_folder: str | Path,
        explicit_filename: Optional[str] = None,
        suggested_filename: Optional[str] = None,
        max_retries: int = 3,
        chunk_size: int = 65536,
        timeout_seconds: float = 20.0,
        is_cancelled: Optional[Callable[[], bool]] = None,
        is_paused: Optional[Callable[[], bool]] = None,
        etag: Optional[str] = None,
        last_modified: Optional[str] = None,
        allow_resume: bool = True,
        resumed_part_path: Optional[str | Path] = None,
        on_progress: Optional[Callable[[int, Optional[int], float, Optional[float]], None]] = None,
        on_filename_detected: Optional[Callable[[str], None]] = None,
        on_retry: Optional[Callable[[int, int, str], None]] = None,
        on_db_persist: Optional[Callable[[int, float, Optional[float]], None]] = None,
        on_metadata_detected: Optional[Callable[[Optional[str], Optional[str], str, str, Optional[int], bool], None]] = None,
        bandwidth_controller: Optional[Any] = None,
        item_id: Optional[str] = None,
        transport: Optional[httpx.BaseTransport] = None,
    ):
        self.url = url.strip()
        self.destination_folder = Path(destination_folder)
        # Use explicit_filename if given; suggested_filename is accepted for backwards compatibility
        self.explicit_filename = explicit_filename if explicit_filename is not None else suggested_filename
        self.max_retries = max_retries
        self.chunk_size = chunk_size
        self.timeout = httpx.Timeout(timeout_seconds, connect=10.0, read=timeout_seconds)
        self.transport = transport
        self.bandwidth_controller = bandwidth_controller
        self.item_id = item_id
        self.is_cancelled_cb = is_cancelled or (lambda: False)
        self.is_paused_cb = is_paused or (lambda: False)
        self.probed_etag = etag
        self.probed_last_modified = last_modified
        self.allow_resume = allow_resume
        self.resumed_part_path = Path(resumed_part_path) if resumed_part_path else None
        self.on_progress_cb = on_progress
        self.on_filename_detected_cb = on_filename_detected
        self.on_retry_cb = on_retry
        self.on_db_persist_cb = on_db_persist
        self.on_metadata_detected_cb = on_metadata_detected

        self._is_cancelled = False
        self._is_paused = False
        self._active_client: Optional[httpx.Client] = None
        self._active_response: Optional[httpx.Response] = None

        self.final_path: Optional[Path] = None
        self.part_path: Optional[Path] = self.resumed_part_path
        self.downloaded_bytes: int = 0
        self.total_bytes: Optional[int] = None
        self.speed: float = 0.0
        self.last_retry_after: Optional[str] = None
        self.eta: Optional[float] = None

    def pause(self):
        """Thread-safe request to pause active download, preserving partial data on disk."""
        self._is_paused = True
        if self._active_response is not None:
            try:
                self._active_response.close()
            except Exception:
                pass
        if self._active_client is not None:
            try:
                self._active_client.close()
            except Exception:
                pass

    def cancel(self):
        """Thread-safe request to cancel active download and abort active network requests."""
        self._is_cancelled = True
        if self._active_response is not None:
            try:
                self._active_response.close()
            except Exception:
                pass
        if self._active_client is not None:
            try:
                self._active_client.close()
            except Exception:
                pass

    def _check_cancelled(self):
        if self._is_paused or self.is_paused_cb():
            raise DownloadPausedError("Download paused by user.")
        if self._is_cancelled or self.is_cancelled_cb():
            self._cleanup_part_file()
            raise DownloadCancelledError("Download cancelled by user.")

    def _cleanup_part_file(self):
        if self.part_path and self.part_path.exists():
            try:
                self.part_path.unlink()
            except OSError:
                pass

    def download(self) -> Tuple[Path, int]:
        """
        Executes the download with automatic retry logic for transient errors.
        Returns (final_file_path, total_downloaded_bytes).
        """
        self._check_cancelled()
        try:
            self.destination_folder.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            raise DownloadError(f"Cannot create destination folder: {e}") from e

        attempt = 0
        last_error_message = ""

        while attempt < self.max_retries:
            attempt += 1
            self._check_cancelled()

            try:
                return self._attempt_download()
            except DownloadPausedError:
                # Do NOT clean up partial file on pause!
                raise
            except DownloadCancelledError:
                self._cleanup_part_file()
                raise
            except Exception as exc:
                if self._is_paused or self.is_paused_cb():
                    raise DownloadPausedError("Download paused by user.")

                self._cleanup_part_file()

                # If cancelled during failure, propagate cancellation immediately
                if self._is_cancelled or self.is_cancelled_cb():
                    raise DownloadCancelledError("Download cancelled by user.")

                # 1. Local disk/filesystem error: NEVER retry
                if is_local_disk_error(exc):
                    raise DownloadError(f"Disk error: {exc}") from exc

                # 2. Logic or explicit DownloadError: NEVER retry
                if isinstance(exc, DownloadError):
                    raise

                # 3. HTTP status error
                is_retryable = False
                last_status_code = None
                self.last_retry_after = None
                if isinstance(exc, httpx.HTTPStatusError):
                    status = exc.response.status_code
                    last_status_code = status
                    retry_after_hdr = exc.response.headers.get("retry-after") or exc.response.headers.get("Retry-After")
                    if retry_after_hdr:
                        self.last_retry_after = retry_after_hdr.strip()

                    if status in RETRYABLE_STATUS_CODES:
                        is_retryable = True
                        reason = exc.response.reason_phrase or ("Too Many Requests" if status == 429 else ("Request Timeout" if status == 408 else "Server Error"))
                        last_error_message = f"HTTP {status} {reason}"
                        if self.last_retry_after:
                            last_error_message += f" (Retry-After: {self.last_retry_after})"
                    else:
                        reason = exc.response.reason_phrase or "Error"
                        raise DownloadError(f"HTTP {status} {reason}: Permanent error, download failed.", status_code=status, retry_after=self.last_retry_after) from exc

                # 4. Transient network errors from HTTPX
                elif isinstance(exc, RETRYABLE_EXCEPTIONS):
                    is_retryable = True
                    last_error_message = f"Network error: {exc}"

                # 5. Any other unexpected error (code bugs, value errors): NEVER retry
                else:
                    raise DownloadError(f"Download failed: {exc}") from exc

                if is_retryable:
                    parsed_delay = parse_retry_after(self.last_retry_after) if self.last_retry_after else None
                    if last_status_code in RETRYABLE_STATUS_CODES and parsed_delay is not None and parsed_delay > INTERNAL_RETRY_AFTER_MAX_SECONDS:
                        raise DownloadError(
                            last_error_message,
                            status_code=last_status_code,
                            retry_after=self.last_retry_after,
                        )

                    if attempt < self.max_retries:
                        if self.on_retry_cb:
                            self.on_retry_cb(attempt, self.max_retries, last_error_message)
                        # Short backoff while listening for cancellation
                        sleep_time = min(attempt * 1.0, 3.0)
                        if parsed_delay is not None and 0 <= parsed_delay <= INTERNAL_RETRY_AFTER_MAX_SECONDS:
                            sleep_time = parsed_delay
                        start_sleep = time.time()
                        while time.time() - start_sleep < sleep_time:
                            self._check_cancelled()
                            time.sleep(0.05)
                    else:
                        raise DownloadError(
                            f"Download failed after {self.max_retries} attempts. Last error: {last_error_message}",
                            status_code=last_status_code,
                            retry_after=self.last_retry_after,
                        )

        raise DownloadError(f"Download failed: {last_error_message or 'Max retries exceeded'}", status_code=last_status_code, retry_after=self.last_retry_after)

    def _attempt_download(self) -> Tuple[Path, int]:
        """
        Single download attempt. Supports resuming existing .part files via HTTP Range requests.
        Streams response into a temporary .part file, then safely finalizes it without overwriting.
        """
        # Validate URL scheme
        if not (self.url.startswith("http://") or self.url.startswith("https://")):
            raise DownloadError("Invalid URL scheme. Only HTTP and HTTPS are supported.")

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) MyDownloadManager/4.0",
            "Accept": "*/*",
            "Accept-Encoding": "identity",
        }

        # Check if an existing .part file is present for resume
        candidate_filename = self.explicit_filename or extract_filename_from_url(self.url, fallback="download")
        candidate_part = self.destination_folder / f"{sanitize_filename(candidate_filename)}.part"
        existing_part = self.part_path or (candidate_part if candidate_part.exists() else None)

        existing_size = 0
        if self.allow_resume and existing_part and existing_part.exists():
            try:
                existing_size = existing_part.stat().st_size
            except OSError:
                existing_size = 0

        if existing_size > 0:
            headers["Range"] = f"bytes={existing_size}-"
            if is_strong_etag(self.probed_etag):
                headers["If-Range"] = self.probed_etag.strip()
            elif self.probed_last_modified:
                headers["If-Range"] = self.probed_last_modified.strip()

        client_kwargs = {"follow_redirects": True, "timeout": self.timeout}
        if self.transport is not None:
            client_kwargs["transport"] = self.transport

        with httpx.Client(**client_kwargs) as client:
            self._active_client = client
            try:
                # If resume headers fail with 416 or 412, handle gracefully
                response_ctx = client.stream("GET", self.url, headers=headers)
                response = response_ctx.__enter__()
                self._active_response = response
                try:
                    self._check_cancelled()

                    if existing_size > 0 and response.status_code in (412, 416):
                        # Precondition Failed or Range Not Satisfiable:
                        # Remote file changed or partial range is invalid. Restart from byte 0.
                        response_ctx.__exit__(None, None, None)
                        if existing_part and existing_part.exists():
                            existing_part.unlink(missing_ok=True)
                        existing_size = 0
                        headers.pop("Range", None)
                        headers.pop("If-Range", None)

                        response_ctx = client.stream("GET", self.url, headers=headers)
                        response = response_ctx.__enter__()
                        self._active_response = response
                        self._check_cancelled()

                    # Handle 206 partial response validation & validator consistency
                    restart_required = False
                    cr_total = None
                    if response.status_code == 206 and existing_size > 0:
                        resp_etag = response.headers.get("etag")
                        if self.probed_etag and resp_etag and resp_etag.strip() != self.probed_etag.strip():
                            restart_required = True
                        resp_lm = response.headers.get("last-modified")
                        if not restart_required and self.probed_last_modified and resp_lm and resp_lm.strip() != self.probed_last_modified.strip():
                            restart_required = True

                        if not restart_required:
                            cr = response.headers.get("content-range", "").strip()
                            # Strict validation: format bytes START-END/TOTAL with positive integer total (never '*')
                            match = re.match(r"^bytes\s+(\d+)-(\d+)/(\d+)$", cr, re.IGNORECASE)
                            if not match:
                                restart_required = True
                            else:
                                cr_start = int(match.group(1))
                                cr_end = int(match.group(2))
                                cr_total = int(match.group(3))

                                # 1. Total size must be a valid positive integer
                                if cr_total <= 0:
                                    restart_required = True
                                # 2. Ending byte must not be smaller than starting byte
                                elif cr_end < cr_start:
                                    restart_required = True
                                # 3. Starting byte must be strictly less than total size
                                elif cr_start >= cr_total:
                                    restart_required = True
                                # 4. Ending byte must be strictly less than total size
                                elif cr_end >= cr_total:
                                    restart_required = True
                                # 5. Content-Range start = EXISTING_SIZE
                                elif cr_start != existing_size:
                                    restart_required = True
                                # 6. Content-Range end = TOTAL_SIZE - 1
                                elif cr_end != cr_total - 1:
                                    restart_required = True
                                # 7. Content-Range total = expected total size (must match self.total_bytes if already known)
                                elif self.total_bytes is not None and self.total_bytes > 0 and cr_total != self.total_bytes:
                                    restart_required = True
                                else:
                                    # 8. Content-Length = TOTAL_SIZE - EXISTING_SIZE when supplied
                                    cl = response.headers.get("content-length")
                                    if cl is not None:
                                        cl_clean = cl.strip()
                                        if not cl_clean.isdigit():
                                            restart_required = True
                                        elif int(cl_clean) != (cr_total - existing_size):
                                            restart_required = True

                        if restart_required:
                            logger.warning(
                                "Resume validation failed for %s. Discarding stale partial data and restarting from byte 0.",
                                self.url,
                            )
                            # Close 206 response, delete stale partial file, make fresh GET from byte zero
                            response_ctx.__exit__(None, None, None)
                            if existing_part and existing_part.exists():
                                existing_part.unlink(missing_ok=True)
                            if candidate_part and candidate_part.exists():
                                candidate_part.unlink(missing_ok=True)
                            existing_size = 0
                            headers.pop("Range", None)
                            headers.pop("If-Range", None)

                            response_ctx = client.stream("GET", self.url, headers=headers)
                            response = response_ctx.__enter__()
                            self._active_response = response
                            self._check_cancelled()

                    elif response.status_code == 200:
                        # Server returned whole file; discard stale partial file
                        if existing_part and existing_part.exists():
                            existing_part.unlink(missing_ok=True)
                        if candidate_part and candidate_part.exists():
                            candidate_part.unlink(missing_ok=True)
                        existing_size = 0

                    # Check for HTTP error codes
                    if response.status_code >= 400:
                        response.raise_for_status()

                    # Determine filename priority
                    header_filename = parse_content_disposition(response.headers.get("content-disposition"))
                    if header_filename:
                        effective_filename = sanitize_filename(header_filename)
                    elif self.explicit_filename:
                        effective_filename = sanitize_filename(self.explicit_filename)
                    else:
                        redirected_name = extract_filename_from_url(str(response.url))
                        if redirected_name and redirected_name != "download":
                            effective_filename = redirected_name
                        else:
                            effective_filename = extract_filename_from_url(self.url, fallback="download")

                    if self.on_filename_detected_cb:
                        self.on_filename_detected_cb(effective_filename)

                    # Determine destination .part path
                    safe_stem = sanitize_filename(effective_filename)
                    target_part = self.destination_folder / f"{safe_stem}.part"

                    if existing_part and existing_part.exists() and existing_part != target_part:
                        try:
                            existing_part.rename(target_part)
                        except OSError:
                            pass
                    self.part_path = target_part

                    open_mode = "wb"
                    self.downloaded_bytes = 0

                    if response.status_code == 206 and existing_size > 0 and not restart_required:
                        open_mode = "ab"
                        self.downloaded_bytes = existing_size
                        if cr_total is not None:
                            self.total_bytes = cr_total
                    else:
                        # Fresh or restarted download
                        self.downloaded_bytes = 0
                        cl_header = response.headers.get("content-length")
                        if cl_header and cl_header.isdigit():
                            self.total_bytes = int(cl_header)
                        else:
                            self.total_bytes = None
                        self.probed_etag = response.headers.get("etag")
                        self.probed_last_modified = response.headers.get("last-modified")

                    supports_range = (response.status_code == 206) or (response.headers.get("accept-ranges") == "bytes")
                    if self.on_metadata_detected_cb:
                        self.on_metadata_detected_cb(
                            self.probed_etag,
                            self.probed_last_modified,
                            effective_filename,
                            str(self.part_path),
                            self.total_bytes,
                            supports_range,
                        )

                    # Stream response to disk in chunks
                    start_time = time.time()
                    last_speed_check_time = start_time
                    last_db_persist_time = start_time
                    bytes_at_last_check = self.downloaded_bytes
                    speed_samples = []

                    effective_chunk = self.chunk_size
                    if self.bandwidth_controller:
                        effective_chunk = self.bandwidth_controller.get_effective_chunk_size(
                            self.item_id, self.chunk_size
                        )

                    with open(self.part_path, open_mode) as f:
                        for chunk in response.iter_bytes(chunk_size=effective_chunk):
                            self._check_cancelled()

                            if not chunk:
                                continue

                            if self.bandwidth_controller:
                                self.bandwidth_controller.acquire(
                                    len(chunk),
                                    item_id=self.item_id,
                                    is_cancelled=self.is_cancelled_cb,
                                    is_paused=self.is_paused_cb,
                                )

                            f.write(chunk)
                            chunk_len = len(chunk)
                            self.downloaded_bytes += chunk_len

                            # Periodic speed calculation
                            current_time = time.time()
                            time_delta = current_time - last_speed_check_time

                            if time_delta >= 0.3:
                                bytes_delta = self.downloaded_bytes - bytes_at_last_check
                                instant_speed = max(0.0, bytes_delta / time_delta)
                                speed_samples.append(instant_speed)
                                if len(speed_samples) > 5:
                                    speed_samples.pop(0)

                                self.speed = sum(speed_samples) / len(speed_samples)
                                last_speed_check_time = current_time
                                bytes_at_last_check = self.downloaded_bytes

                                if self.total_bytes and self.speed > 0:
                                    remaining_bytes = max(0, self.total_bytes - self.downloaded_bytes)
                                    self.eta = remaining_bytes / self.speed
                                else:
                                    self.eta = None

                            if self.on_progress_cb:
                                self.on_progress_cb(
                                    self.downloaded_bytes,
                                    self.total_bytes,
                                    self.speed,
                                    self.eta,
                                )

                            # Throttled DB persistence callback (~1.5s interval)
                            if self.on_db_persist_cb and (current_time - last_db_persist_time >= 1.5):
                                try:
                                    self.on_db_persist_cb(self.downloaded_bytes, self.speed, self.eta)
                                except Exception as exc:
                                    logger.warning("Error in DB persist callback: %s", exc)
                                last_db_persist_time = current_time

                        # Flush and sync to disk
                        f.flush()
                        os.fsync(f.fileno())

                    # Validate downloaded size
                    if self.total_bytes is not None and self.downloaded_bytes < self.total_bytes:
                        raise DownloadError(
                            f"Incomplete download: received {self.downloaded_bytes} of {self.total_bytes} bytes."
                        )

                    # Check cancellation before final rename
                    self._check_cancelled()

                    # Safe finalization without overwriting any existing file
                    self.final_path = safe_finalize_file(
                        self.part_path,
                        self.destination_folder,
                        effective_filename,
                    )

                    # Final progress callback at 100%
                    if self.on_progress_cb:
                        self.on_progress_cb(
                            self.downloaded_bytes,
                            self.downloaded_bytes if self.total_bytes is None else self.total_bytes,
                            self.speed,
                            0.0 if self.total_bytes else None,
                        )

                    return self.final_path, self.downloaded_bytes
                finally:
                    response_ctx.__exit__(None, None, None)
                    self._active_response = None
            finally:
                self._active_client = None
