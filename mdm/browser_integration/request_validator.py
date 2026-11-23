"""
Validation of browser-originated download requests (Stage 6A).

This module is deliberately pure standard-library Python with no Qt imports so
that it can be shared by both the desktop application (authoritative check
before anything reaches the QueueManager) and the lightweight native messaging
host (early rejection before anything is forwarded over IPC).

Nothing received from the browser is trusted:
  * Only http:// and https:// URLs are accepted.
  * URLs carrying embedded user-info credentials are rejected.
  * Filenames are names only - any path component is rejected outright; the
    application then applies its existing ``sanitize_filename`` logic.
  * Free-text metadata is type-checked, stripped of control characters and
    length-limited.
  * Unknown fields are rejected so the schema cannot be silently extended.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

ALLOWED_SCHEMES = frozenset({"http", "https"})
SUPPORTED_BROWSERS = frozenset({"chrome", "edge"})

# Generous enough for long signed CDN URLs with many query parameters, far
# below the 1 MB transport limit.
MAX_URL_LENGTH = 32 * 1024
MAX_REFERRER_LENGTH = 8 * 1024
MAX_PAGE_TITLE_LENGTH = 512
MAX_FILENAME_LENGTH = 255

REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_URL_FORBIDDEN_CHARS = re.compile(r"[\x00-\x20\x7f]")
_FILENAME_FORBIDDEN = re.compile(r"[\\/:\x00-\x1f\x7f]")
_HOSTNAME_PATTERN = re.compile(r"^[A-Za-z0-9\-._~%!$&'()*+,;=\[\]: -￿]+$")

ADD_DOWNLOAD_FIELDS = frozenset(
    {"action", "request_id", "url", "filename", "referrer", "page_title", "browser"}
)


class ValidationError(ValueError):
    """Raised when a browser request fails validation.

    ``code`` is a stable machine-readable identifier that is returned to the
    extension; ``message`` is a short human-readable explanation that never
    echoes back sensitive input.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class BrowserDownloadRequest:
    request_id: str
    url: str
    browser: str
    filename: Optional[str] = None
    referrer: Optional[str] = None
    page_title: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id,
            "url": self.url,
            "filename": self.filename,
            "referrer": self.referrer,
            "page_title": self.page_title,
            "browser": self.browser,
        }


def validate_request_id(value: Any) -> str:
    if value is None:
        raise ValidationError("missing_field", "request_id is required.")
    if not isinstance(value, str) or not REQUEST_ID_PATTERN.match(value):
        raise ValidationError(
            "invalid_field",
            "request_id must be 8-128 characters of letters, digits, '-' or '_'.",
        )
    return value


def validate_download_url(value: Any, *, field_name: str = "url", max_length: int = MAX_URL_LENGTH) -> str:
    """Validates and returns a normalised (whitespace-stripped) HTTP/HTTPS URL.

    Percent-encoded characters and query strings are preserved exactly.
    """
    if value is None:
        raise ValidationError("missing_field", f"{field_name} is required.")
    if not isinstance(value, str):
        raise ValidationError("invalid_field", f"{field_name} must be a string.")

    url = value.strip()
    if not url:
        raise ValidationError("missing_field", f"{field_name} is empty.")
    if len(url) > max_length:
        raise ValidationError("invalid_url", f"{field_name} is too long.")
    if _URL_FORBIDDEN_CHARS.search(url):
        raise ValidationError("invalid_url", f"{field_name} contains whitespace or control characters.")

    scheme_end = url.find(":")
    scheme = url[:scheme_end].lower() if scheme_end > 0 else ""
    if scheme not in ALLOWED_SCHEMES:
        if scheme:
            raise ValidationError(
                "unsupported_scheme",
                f"Only HTTP and HTTPS links are supported (got '{scheme[:20]}:').",
            )
        raise ValidationError("invalid_url", f"{field_name} is not an absolute HTTP or HTTPS URL.")

    try:
        parts = urlsplit(url)
    except ValueError:
        raise ValidationError("invalid_url", f"{field_name} could not be parsed.") from None

    if not url[scheme_end:].startswith("://"):
        raise ValidationError("invalid_url", f"{field_name} is not an absolute HTTP or HTTPS URL.")
    if not parts.netloc:
        raise ValidationError("invalid_url", f"{field_name} has no host.")
    if "@" in parts.netloc or parts.username is not None or parts.password is not None:
        raise ValidationError(
            "embedded_credentials",
            "Links containing an embedded username or password are not accepted.",
        )

    hostname = parts.hostname
    if not hostname or not _HOSTNAME_PATTERN.match(hostname):
        raise ValidationError("invalid_url", f"{field_name} has an invalid host.")
    try:
        port = parts.port
    except ValueError:
        raise ValidationError("invalid_url", f"{field_name} has an invalid port.") from None
    if port is not None and not (1 <= port <= 65535):
        raise ValidationError("invalid_url", f"{field_name} has an invalid port.")

    return url


def validate_filename(value: Any) -> Optional[str]:
    """Accepts an optional bare filename. Any path component is rejected.

    Final sanitisation (reserved names, illegal characters, length) is done by
    the application's existing ``mdm.utils.sanitize_filename``.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError("invalid_field", "filename must be a string or null.")
    name = value.strip()
    if not name:
        return None
    if len(name) > MAX_FILENAME_LENGTH:
        raise ValidationError("invalid_field", "filename is too long.")
    if _FILENAME_FORBIDDEN.search(name) or name in (".", "..") or name.startswith(".."):
        raise ValidationError("invalid_field", "filename must be a plain file name, not a path.")
    return name


def _clean_optional_text(value: Any, field_name: str, max_length: int) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError("invalid_field", f"{field_name} must be a string or null.")
    text = _CONTROL_CHARS.sub(" ", value).strip()
    if not text:
        return None
    return text[:max_length]


def clean_referrer(value: Any) -> Optional[str]:
    """Referrer is informational only. A non-HTTP(S) or credential-bearing
    referrer is dropped (the download itself is still allowed)."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError("invalid_field", "referrer must be a string or null.")
    if not value.strip():
        return None
    try:
        return validate_download_url(value, field_name="referrer", max_length=MAX_REFERRER_LENGTH)
    except ValidationError:
        return None


def validate_browser(value: Any) -> str:
    if value is None:
        raise ValidationError("missing_field", "browser is required.")
    if not isinstance(value, str) or value.lower() not in SUPPORTED_BROWSERS:
        raise ValidationError("invalid_field", "browser must be 'chrome' or 'edge'.")
    return value.lower()


def validate_add_download_request(message: Any) -> BrowserDownloadRequest:
    """Validates a complete ``add_download`` request object."""
    if not isinstance(message, dict):
        raise ValidationError("invalid_message", "Request must be a JSON object.")

    unknown = set(message.keys()) - ADD_DOWNLOAD_FIELDS
    if unknown:
        raise ValidationError("invalid_field", "Request contains unsupported fields.")

    action = message.get("action", "add_download")
    if action != "add_download":
        raise ValidationError("unsupported_action", "Unsupported action.")

    return BrowserDownloadRequest(
        request_id=validate_request_id(message.get("request_id")),
        url=validate_download_url(message.get("url")),
        browser=validate_browser(message.get("browser")),
        filename=validate_filename(message.get("filename")),
        referrer=clean_referrer(message.get("referrer")),
        page_title=_clean_optional_text(message.get("page_title"), "page_title", MAX_PAGE_TITLE_LENGTH),
    )


def redact_url(url: Any) -> str:
    """Returns a log-safe representation of a URL: scheme and host only.

    Paths and query strings can contain signed tokens, so they are never logged.
    """
    if not isinstance(url, str):
        return "<invalid>"
    try:
        parts = urlsplit(url.strip())
        host = parts.hostname or "?"
        return f"{parts.scheme or '?'}://{host}/…"
    except ValueError:
        return "<unparseable>"


# ---------------------------------------------------------------------------
# Stage 6B: automatic capture messages
# ---------------------------------------------------------------------------

PREPARE_CAPTURE_FIELDS = frozenset({
    "action", "request_id", "url", "filename", "mime", "total_bytes", "referrer",
    "browser", "browser_download_id", "method", "danger", "incognito",
})
CAPTURE_MODES = frozenset({"disabled", "ask", "automatic"})
OPEN_APP_PAGES = frozenset({"settings"})
MAX_BROWSER_DOWNLOAD_ID = 2 ** 53
_MIME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}$")
_REASON_PATTERN = re.compile(r"^[a-z_]{1,40}$")
_VERSION_PATTERN = re.compile(r"^[0-9]{1,5}(\.[0-9]{1,5}){0,3}$")


@dataclass(frozen=True)
class CapturePrepareRequest:
    request_id: str
    url: str
    browser: str
    browser_download_id: int
    filename: Optional[str] = None
    mime: Optional[str] = None
    total_bytes: Optional[int] = None
    referrer: Optional[str] = None

    def to_payload(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id, "url": self.url, "browser": self.browser,
            "browser_download_id": self.browser_download_id, "filename": self.filename,
            "mime": self.mime, "total_bytes": self.total_bytes, "referrer": self.referrer,
            "method": "GET", "danger": "safe", "incognito": False,
        }


def _int_field(value: Any, name: str, *, minimum: int = 0, maximum: int = 2 ** 63 - 1) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not (minimum <= value <= maximum):
        raise ValidationError("invalid_field", f"{name} must be a non-negative integer.")
    return value


def validate_prepare_capture(message: Any) -> CapturePrepareRequest:
    """Validates a ``prepare_capture`` message. Only safe, non-incognito GET
    downloads with an http(s) URL and a bare filename are accepted."""
    if not isinstance(message, dict):
        raise ValidationError("invalid_message", "Request must be a JSON object.")
    if set(message.keys()) - PREPARE_CAPTURE_FIELDS:
        raise ValidationError("invalid_field", "Request contains unsupported fields.")
    if message.get("action", "prepare_capture") != "prepare_capture":
        raise ValidationError("unsupported_action", "Unsupported action.")

    method = message.get("method")
    if method != "GET":
        raise ValidationError("unsupported_method",
                              "Only downloads made with a plain GET request can be captured.")
    if message.get("danger") != "safe":
        raise ValidationError("dangerous_download",
                              "The browser has not marked this download as safe; it stays in the browser.")
    if message.get("incognito", False) is not False:
        raise ValidationError("incognito_excluded", "Private-window downloads are never captured.")

    mime = message.get("mime")
    if mime is not None:
        if not isinstance(mime, str):
            raise ValidationError("invalid_field", "mime must be a string or null.")
        mime = mime.split(";", 1)[0].strip().lower() or None
        if mime is not None and not _MIME_PATTERN.match(mime):
            mime = None  # informational only - drop rather than reject

    total = message.get("total_bytes")
    if total is not None:
        if not isinstance(total, int) or isinstance(total, bool):
            raise ValidationError("invalid_field", "total_bytes must be an integer or null.")
        total = total if total >= 0 else None

    return CapturePrepareRequest(
        request_id=validate_request_id(message.get("request_id")),
        url=validate_download_url(message.get("url")),
        browser=validate_browser(message.get("browser")),
        browser_download_id=_int_field(message.get("browser_download_id"), "browser_download_id",
                                       maximum=MAX_BROWSER_DOWNLOAD_ID),
        filename=validate_filename(message.get("filename")),
        mime=mime,
        total_bytes=total,
        referrer=clean_referrer(message.get("referrer")),
    )


def validate_capture_reference(message: Any, *, allow_url: bool = False, allow_reason: bool = False,
                               action: Optional[str] = None) -> Dict[str, Any]:
    """Validates capture_status / commit_capture / abort_capture / claim_fallback."""
    if not isinstance(message, dict):
        raise ValidationError("invalid_message", "Request must be a JSON object.")
    allowed = {"action", "request_id", "browser"}
    if allow_url:
        allowed.add("url")
    if allow_reason:
        allowed.add("reason")
    if set(message.keys()) - allowed:
        raise ValidationError("invalid_field", "Request contains unsupported fields.")
    if action is not None and message.get("action", action) != action:
        raise ValidationError("unsupported_action", "Unsupported action.")
    out: Dict[str, Any] = {"request_id": validate_request_id(message.get("request_id"))}
    if message.get("browser") is not None:
        out["browser"] = validate_browser(message.get("browser"))
    if allow_url:
        out["url"] = validate_download_url(message.get("url"))
    if allow_reason:
        reason = message.get("reason", "other")
        if not isinstance(reason, str) or not _REASON_PATTERN.match(reason):
            reason = "other"
        out["reason"] = reason
    return out


def validate_capture_config_request(message: Any) -> Dict[str, Any]:
    """get_capture_config: who is asking and whether capture permissions are granted."""
    if not isinstance(message, dict):
        raise ValidationError("invalid_message", "Request must be a JSON object.")
    if set(message.keys()) - {"action", "request_id", "browser", "capture_permissions", "extension_version"}:
        raise ValidationError("invalid_field", "Request contains unsupported fields.")
    out: Dict[str, Any] = {}
    if message.get("browser") is not None:
        out["browser"] = validate_browser(message.get("browser"))
    perms = message.get("capture_permissions")
    if perms is not None and not isinstance(perms, bool):
        raise ValidationError("invalid_field", "capture_permissions must be a boolean.")
    out["capture_permissions"] = perms
    version = message.get("extension_version")
    out["extension_version"] = version if isinstance(version, str) and _VERSION_PATTERN.match(version) else None
    return out


CAPTURE_DECISION_FIELDS = frozenset({
    "at", "download_id", "browser", "resource_type", "method", "method_source", "from_cache", "filename",
    "extension", "mime", "state", "paused", "by_extension", "initiator", "mode", "decision", "reason",
})
_TOKEN_PATTERN = re.compile(r"^[a-z_]{1,40}$")
_METHOD_PATTERN = re.compile(r"^[A-Z]{1,12}$")
_DECISION_EXTENSION_PATTERN = re.compile(r"^[a-z0-9]{1,10}$")
MAX_DECISION_FILENAME = 120


def validate_capture_decision(message: Any) -> Dict[str, Any]:
    """``report_capture_decision``: a privacy-safe summary of one browser-side
    capture decision, shown in the Browser Integration settings.

    Only an allowlisted set of short, typed facts survives. There is no URL
    field at all, and anything that looks like a URL is dropped, so a full
    link, signed query string, cookie or token can never be stored.
    """
    if not isinstance(message, dict):
        raise ValidationError("invalid_message", "Request must be a JSON object.")
    if set(message.keys()) - {"action", "request_id", "browser", "decision"}:
        raise ValidationError("invalid_field", "Request contains unsupported fields.")
    raw = message.get("decision")
    if not isinstance(raw, dict):
        raise ValidationError("invalid_field", "decision must be an object.")
    if set(raw.keys()) - CAPTURE_DECISION_FIELDS:
        raise ValidationError("invalid_field", "decision contains unsupported fields.")

    def token(name: str) -> Optional[str]:
        v = raw.get(name)
        return v if isinstance(v, str) and _TOKEN_PATTERN.match(v) else None

    def flag(name: str) -> Optional[bool]:
        v = raw.get(name)
        return v if isinstance(v, bool) else None

    filename = raw.get("filename")
    if isinstance(filename, str):
        filename = _CONTROL_CHARS.sub(" ", filename).strip()[:MAX_DECISION_FILENAME]
        if "://" in filename or _FILENAME_FORBIDDEN.search(filename.replace(":", "")):
            filename = None
    else:
        filename = None
    method = raw.get("method")
    mime = raw.get("mime")
    ext = raw.get("extension")
    did = raw.get("download_id")
    at = raw.get("at")
    out: Dict[str, Any] = {
        "browser": validate_browser(message.get("browser")) if message.get("browser") is not None else None,
        "download_id": did if isinstance(did, int) and not isinstance(did, bool) and 0 <= did <= MAX_BROWSER_DOWNLOAD_ID else None,
        "at": at if isinstance(at, (int, float)) and not isinstance(at, bool) and at > 0 else None,
        "resource_type": token("resource_type"),
        "method": method if isinstance(method, str) and _METHOD_PATTERN.match(method) else None,
        "method_source": token("method_source"),
        "from_cache": flag("from_cache"),
        "filename": filename or None,
        "extension": ext if isinstance(ext, str) and _DECISION_EXTENSION_PATTERN.match(ext) else None,
        "mime": mime if isinstance(mime, str) and _MIME_PATTERN.match(mime) else None,
        "state": token("state"),
        "paused": flag("paused"),
        "by_extension": flag("by_extension"),
        "initiator": token("initiator"),
        "mode": token("mode"),
        "decision": token("decision") or "other",
        "reason": token("reason"),
    }
    return {"browser": out["browser"], "decision": out}


def validate_capture_mode(message: Any) -> Dict[str, Any]:
    if not isinstance(message, dict):
        raise ValidationError("invalid_message", "Request must be a JSON object.")
    if set(message.keys()) - {"action", "request_id", "browser", "mode"}:
        raise ValidationError("invalid_field", "Request contains unsupported fields.")
    mode = message.get("mode")
    if mode not in CAPTURE_MODES:
        raise ValidationError("invalid_field", "mode must be 'disabled', 'ask' or 'automatic'.")
    out = {"mode": mode}
    if message.get("browser") is not None:
        out["browser"] = validate_browser(message.get("browser"))
    return out
