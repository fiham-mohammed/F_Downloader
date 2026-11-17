import os
import time
import errno
import httpx
from pathlib import Path
from typing import Callable, Optional, Tuple

from mdm.utils import (
    sanitize_filename,
    safe_finalize_file,
    parse_content_disposition,
    extract_filename_from_url,
)


class DownloadError(Exception):
    """Raised when a download encounters an unrecoverable error."""
    pass


class DownloadCancelledError(Exception):
    """Raised when a download is cancelled by the user."""
    pass


# Status codes that should trigger an automatic retry
RETRYABLE_STATUS_CODES = {500, 502, 503, 504}

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
        on_progress: Optional[Callable[[int, Optional[int], float, Optional[float]], None]] = None,
        on_filename_detected: Optional[Callable[[str], None]] = None,
        on_retry: Optional[Callable[[int, int, str], None]] = None,
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
        self.is_cancelled_cb = is_cancelled or (lambda: False)
        self.on_progress_cb = on_progress
        self.on_filename_detected_cb = on_filename_detected
        self.on_retry_cb = on_retry

        self._is_cancelled = False
        self._active_client: Optional[httpx.Client] = None
        self._active_response: Optional[httpx.Response] = None

        self.final_path: Optional[Path] = None
        self.part_path: Optional[Path] = None
        self.downloaded_bytes: int = 0
        self.total_bytes: Optional[int] = None
        self.speed: float = 0.0
        self.eta: Optional[float] = None

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
            except DownloadCancelledError:
                self._cleanup_part_file()
                raise
            except Exception as exc:
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
                if isinstance(exc, httpx.HTTPStatusError):
                    status = exc.response.status_code
                    if status in RETRYABLE_STATUS_CODES:
                        is_retryable = True
                        last_error_message = f"HTTP {status} {exc.response.reason_phrase or 'Server Error'}"
                    else:
                        reason = exc.response.reason_phrase or "Error"
                        raise DownloadError(f"HTTP {status} {reason}: Permanent error, download failed.") from exc

                # 4. Transient network errors from HTTPX
                elif isinstance(exc, RETRYABLE_EXCEPTIONS):
                    is_retryable = True
                    last_error_message = f"Network error: {exc}"

                # 5. Any other unexpected error (code bugs, value errors): NEVER retry
                else:
                    raise DownloadError(f"Download failed: {exc}") from exc

                if is_retryable and attempt < self.max_retries:
                    if self.on_retry_cb:
                        self.on_retry_cb(attempt, self.max_retries, last_error_message)
                    # Short backoff while listening for cancellation
                    sleep_time = min(attempt * 1.0, 3.0)
                    start_sleep = time.time()
                    while time.time() - start_sleep < sleep_time:
                        self._check_cancelled()
                        time.sleep(0.05)
                else:
                    raise DownloadError(
                        f"Download failed after {self.max_retries} attempts. Last error: {last_error_message}"
                    )

        raise DownloadError(f"Download failed: {last_error_message or 'Max retries exceeded'}")

    def _attempt_download(self) -> Tuple[Path, int]:
        """
        Single download attempt. Streams response into a temporary .part file,
        then safely finalizes it without overwriting any existing file.
        """
        # Validate URL scheme
        if not (self.url.startswith("http://") or self.url.startswith("https://")):
            raise DownloadError("Invalid URL scheme. Only HTTP and HTTPS are supported.")

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) MyDownloadManager/2.0",
            "Accept": "*/*",
            "Accept-Encoding": "identity",
        }

        client_kwargs = {"follow_redirects": True, "timeout": self.timeout}
        if self.transport is not None:
            client_kwargs["transport"] = self.transport

        with httpx.Client(**client_kwargs) as client:
            self._active_client = client
            try:
                with client.stream("GET", self.url, headers=headers) as response:
                    self._active_response = response
                    try:
                        self._check_cancelled()

                        # Check for HTTP error codes
                        if response.status_code >= 400:
                            response.raise_for_status()

                        # Determine filename priority:
                        # 1. Content-Disposition filename from the final HTTP response
                        # 2. Explicit user-provided filename, if one exists
                        # 3. Filename extracted from the final redirected response URL
                        # 4. Fallback filename
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

                        # Determine temporary .part path
                        safe_stem = sanitize_filename(effective_filename)
                        self.part_path = self.destination_folder / f"{safe_stem}.part"

                        # Clean up any stale partial download for this specific file
                        self._cleanup_part_file()

                        # Determine Content-Length
                        content_length_header = response.headers.get("content-length")
                        if content_length_header and content_length_header.isdigit():
                            self.total_bytes = int(content_length_header)
                        else:
                            self.total_bytes = None

                        self.downloaded_bytes = 0

                        # Stream response to disk in chunks
                        start_time = time.time()
                        last_speed_check_time = start_time
                        bytes_at_last_check = 0
                        speed_samples = []

                        with open(self.part_path, "wb") as f:
                            for chunk in response.iter_bytes(chunk_size=self.chunk_size):
                                self._check_cancelled()

                                if not chunk:
                                    continue

                                f.write(chunk)
                                chunk_len = len(chunk)
                                self.downloaded_bytes += chunk_len

                                # Periodic speed calculation
                                current_time = time.time()
                                time_delta = current_time - last_speed_check_time

                                if time_delta >= 0.3:
                                    bytes_delta = self.downloaded_bytes - bytes_at_last_check
                                    instant_speed = bytes_delta / time_delta
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
                        self._active_response = None
            finally:
                self._active_client = None
