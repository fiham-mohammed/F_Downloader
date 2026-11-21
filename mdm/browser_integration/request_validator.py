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
