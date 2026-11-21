import os
import re
import errno
from typing import Optional, Any, Union
from pathlib import Path
from urllib.parse import urlparse, unquote, unquote_to_bytes
import urllib.parse


# Windows reserved base names
WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9"
}

# Illegal characters on Windows filesystem
WINDOWS_ILLEGAL_CHARS_PATTERN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_filename(filename: str, fallback: str = "download") -> str:
    """
    Sanitize a filename to make it safe for the Windows filesystem.
    Removes illegal characters, handles reserved names, strips trailing dots/spaces,
    and enforces safe length.
    """
    if not filename:
        return fallback

    # Strip whitespace
    name = filename.strip()

    # Replace illegal characters with underscore
    name = WINDOWS_ILLEGAL_CHARS_PATTERN.sub("_", name)

    # Windows does not allow filenames ending in dots or spaces
    name = name.rstrip(". ")

    if not name:
        return fallback

    # Split stem and extension
    p = Path(name)
    stem = p.stem
    suffix = p.suffix

    # Check for Windows reserved names.
    # Windows treats the component before the FIRST dot as the device name,
    # e.g. "CON.foo.bar" is still the CON device.  Path.stem strips only
    # the last extension, so we must check the part before the first dot.
    first_dot = name.find(".")
    if first_dot >= 0:
        base_component = name[:first_dot]
    else:
        base_component = stem
    if base_component.upper() in WINDOWS_RESERVED_NAMES:
        name = f"_{name}"
        # Recalculate stem/suffix after prefixing
        p = Path(name)
        stem = p.stem
        suffix = p.suffix

    # Truncate length to avoid exceeding Windows path limits (max 200 chars for filename)
    max_len = 200
    if len(name) > max_len:
        allowed_stem_len = max(1, max_len - len(suffix))
        stem = stem[:allowed_stem_len].rstrip(". ")
        name = f"{stem}{suffix}"

    return name if name else fallback


def get_unique_filepath(folder: Path | str, filename: str) -> Path:
    """
    Returns a unique file path in the target folder by appending (1), (2), etc.
    if the destination file already exists.
    """
    folder_path = Path(folder)
    folder_path.mkdir(parents=True, exist_ok=True)

    sanitized = sanitize_filename(filename)
    target = folder_path / sanitized

    if not target.exists():
        return target

    stem = target.stem
    suffix = target.suffix
    counter = 1

    while True:
        candidate = folder_path / f"{stem} ({counter}){suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def safe_finalize_file(part_path: Path | str, destination_folder: Path | str, preferred_filename: str) -> Path:
    """
    Atomically moves the temporary .part file to its final destination in destination_folder
    without overwriting any existing file, even if another process or thread creates a collision
    concurrently.

    On Windows, os.rename fails with FileExistsError (WinError 183) if the target file exists,
    providing an atomic no-overwrite guarantee. We use this in a loop with safe fallback numbering
    (e.g., 'file (1).ext', 'file (2).ext').
    """
    folder = Path(destination_folder)
    folder.mkdir(parents=True, exist_ok=True)

    sanitized = sanitize_filename(preferred_filename)
    stem = Path(sanitized).stem
    suffix = Path(sanitized).suffix

    candidate = folder / sanitized
    counter = 1

    part = Path(part_path)
    if not part.exists():
        raise FileNotFoundError(f"Temporary file '{part}' does not exist.")

    while True:
        if candidate.exists():
            candidate = folder / f"{stem} ({counter}){suffix}"
            counter += 1
            continue

        try:
            os.rename(str(part), str(candidate))
            return candidate
        except (FileExistsError, OSError) as exc:
            is_collision = (
                isinstance(exc, FileExistsError)
                or getattr(exc, "winerror", None) == 183
                or getattr(exc, "errno", None) in (errno.EEXIST, 17)
            )
            if is_collision:
                candidate = folder / f"{stem} ({counter}){suffix}"
                counter += 1
                continue
            raise


def parse_content_disposition(header_val: str | None) -> str | None:
    """
    Parses the Content-Disposition header and extracts the filename if present.
    Supports standard 'filename=' and RFC 5987 / RFC 6266 'filename*=UTF-8''...'.
    """
    if not header_val:
        return None

    # Try RFC 5987 encoded filename first: filename*=charset'lang'encoded-value
    rfc5987_match = re.search(r"filename\*\s*=\s*([^;'\"]+)'([^']*)'([^;\s]+)", header_val, re.IGNORECASE)
    if rfc5987_match:
        encoding, _lang, encoded_val = rfc5987_match.groups()
        try:
            return urllib.parse.unquote(encoded_val, encoding=encoding or "utf-8")
        except Exception:
            try:
                return urllib.parse.unquote(encoded_val)
            except Exception:
                pass

    # Try standard filename="value" or filename=value
    filename_match = re.search(r'filename\s*=\s*(?:"([^"]+)"|([^;\s]+))', header_val, re.IGNORECASE)
    if filename_match:
        raw_name = filename_match.group(1) or filename_match.group(2)
        if raw_name:
            # Unquote in case it's URL-encoded
            return unquote(raw_name.strip())

    return None


def extract_filename_from_url(url: str, fallback: str = "download") -> str:
    """
    Extracts the filename from a URL path, stripping query parameters and unquoting.
    """
    try:
        parsed = urlparse(url)
        path = unquote(parsed.path)
        name = Path(path).name
        if name:
            return sanitize_filename(name, fallback=fallback)
    except Exception:
        pass
    return fallback


def format_bytes(num_bytes: int | float | None) -> str:
    """
    Formats a byte count into a human-readable string (B, KB, MB, GB).
    """
    if num_bytes is None or num_bytes < 0:
        return "Unknown"

    num = float(num_bytes)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if num < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(num)} B"
            return f"{num:.2f} {unit}"
        num /= 1024.0
    return f"{num:.2f} TB"


def format_speed(bytes_per_sec: float | None) -> str:
    """
    Formats download speed in KB/s or MB/s.
    """
    if bytes_per_sec is None or bytes_per_sec <= 0:
        return "—"

    if bytes_per_sec < 1024 * 1024:
        kb = bytes_per_sec / 1024.0
        return f"{kb:.1f} KB/s"
    mb = bytes_per_sec / (1024.0 * 1024.0)
    return f"{mb:.2f} MB/s"


def format_eta(seconds: float | None) -> str:
    """
    Formats remaining time in seconds into HH:MM:SS or MM:SS.
    """
    if seconds is None or seconds < 0 or seconds > 86400 * 7:
        return "—"

    secs = int(round(seconds))
    hrs = secs // 3600
    mins = (secs % 3600) // 60
    rem_secs = secs % 60

    if hrs > 0:
        return f"{hrs:02d}:{mins:02d}:{rem_secs:02d}"
    return f"{mins:02d}:{rem_secs:02d}"


def is_strong_etag(etag: str | None) -> bool:
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


def parse_retry_after(value_or_msg: Any) -> float | None:
    """
    Parses Retry-After header or string containing Retry-After.
    Supports both delta seconds (e.g. 120, '30 seconds', '45s') and HTTP-date formats.
    """
    if not value_or_msg:
        return None
    val_str = str(value_or_msg).strip()

    # If it's an error message containing "retry-after: ...", extract the token
    m = re.search(r"retry-after[:\s]+([^\r\n;)]+)", val_str, re.IGNORECASE)
    candidate = m.group(1).strip() if m else val_str

    # 1. Delta seconds (e.g. "120", "30 seconds", "45s")
    sec_match = re.search(r"^\s*(\d+(?:\.\d+)?)(?:\s*(?:s|sec|seconds)?)?\s*$", candidate, re.IGNORECASE)
    if sec_match:
        try:
            val = float(sec_match.group(1))
            if val >= 0:
                return val
        except ValueError:
            pass

    # 2. HTTP-date format (e.g. "Wed, 21 Oct 2026 07:28:00 GMT")
    try:
        import email.utils
        from datetime import datetime
        dt = email.utils.parsedate_to_datetime(candidate)
        if dt:
            now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
            delay = (dt - now).total_seconds()
            return max(0.0, delay)
    except Exception:
        pass

    return None



