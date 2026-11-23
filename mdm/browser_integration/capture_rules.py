"""
Automatic-capture rules (Stage 6B): file-extension filtering, website
exclusions and the persisted capture configuration.

Pure standard-library code; mirrored in ``browser_extension/lib/capture_rules.js``.
The desktop application is the source of truth for the configuration; the
extension caches it only to avoid pausing downloads that cannot be captured.
"""
from __future__ import annotations

import copy
import ipaddress
import json
import re
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Dict, Iterable, List, Optional, Set
from urllib.parse import unquote, urlsplit

CAPTURE_MODES = ("disabled", "ask", "automatic")
CONFIG_SETTING_KEY = "browser_capture_config"
# v2 (Stage 6B repair): adds the "Images" group. Older saved configurations are
# upgraded by normalize_config(): every preference the user saved is kept, and
# extensions that did not exist yet start with their default (enabled).
CONFIG_VERSION = 2

IMAGE_EXTENSIONS: List[str] = ["jpg", "jpeg", "png", "gif", "webp", "bmp", "tif", "tiff", "ico"]

DEFAULT_EXTENSION_GROUPS: Dict[str, List[str]] = {
    "Archives": ["zip", "rar", "7z", "tar", "gz"],
    "Executables": ["exe", "msi", "msix"],
    "Disk images": ["iso", "img"],
    "Video": ["mp4", "mkv", "avi", "mov", "wmv", "webm"],
    "Audio": ["mp3", "wav", "flac", "aac"],
    "Documents": ["pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx"],
    "Images": list(IMAGE_EXTENSIONS),
    "Other": ["apk", "csv", "txt"],
}
DEFAULT_EXTENSIONS: List[str] = [e for group in DEFAULT_EXTENSION_GROUPS.values() for e in group]

MAX_CUSTOM_EXTENSIONS = 100
MAX_EXCLUDED_SITES = 500

# One or two dot-separated parts ("zip", "tar.gz"), letters/digits only.
_EXTENSION_RE = re.compile(r"^[a-z0-9]{1,10}(\.[a-z0-9]{1,10})?$")
_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class RuleError(ValueError):
    """User-supplied extension or website is malformed."""


# ---------------------------------------------------------------------------
# File extensions
# ---------------------------------------------------------------------------

def normalize_extension(value: Any) -> str:
    """'.ZIP' -> 'zip', '*.tar.gz' -> 'tar.gz'. Raises RuleError when malformed."""
    if not isinstance(value, str):
        raise RuleError("Extension must be text.")
    ext = value.strip().lower()
    if ext.startswith("*"):
        ext = ext[1:]
    ext = ext.lstrip(".")
    if not _EXTENSION_RE.match(ext):
        raise RuleError(
            f"'{value.strip()[:30]}' is not a valid file extension "
            "(use letters and digits, e.g. 'zip' or 'tar.gz')."
        )
    return ext


def safe_basename(name: Any) -> str:
    """Last path component of a Windows or POSIX path (browser filenames are full paths)."""
    if not isinstance(name, str) or not name:
        return ""
    return PureWindowsPath(PurePosixPath(name).name).name.strip()


def filename_from_url(url: str) -> str:
    """Filename from the URL *path* only - the query string (which may carry a
    signed token) is never inspected or modified."""
    try:
        path = urlsplit(url).path
    except ValueError:
        return ""
    return safe_basename(unquote(path.rstrip("/").rsplit("/", 1)[-1]))


def extension_matches(filename: str, enabled: Iterable[str]) -> Optional[str]:
    """Returns the matching enabled extension for ``filename`` or None.

    Longest match wins, so 'tar.gz' is preferred over 'gz'. A file must have a
    stem: '.zip' alone or 'zip' (no dot) never matches.
    """
    name = safe_basename(filename).lower().rstrip(". ")
    if not name:
        return None
    for ext in sorted(set(enabled), key=len, reverse=True):
        suffix = "." + ext
        if name.endswith(suffix) and len(name) > len(suffix):
            return ext
    return None


# ---------------------------------------------------------------------------
# Websites
# ---------------------------------------------------------------------------

def _to_ascii_host(host: str) -> str:
    host = host.strip().strip(".").lower()
    if not host:
        raise RuleError("Website is empty.")
    try:
        # IDNA 2003 via the standard library; also lowercases and validates.
        return host.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        raise RuleError(f"'{host[:60]}' is not a valid website name.") from None


def normalize_site(value: Any) -> str:
    """Normalises a user-entered website to a lowercase ASCII (punycode) hostname.

    Accepts 'Example.COM', 'https://example.com/path', '*.example.com',
    'bücher.example'. Rejects anything with other wildcards, bare TLDs,
    empty labels or invalid characters.
    """
    if not isinstance(value, str):
        raise RuleError("Website must be text.")
    text = value.strip()
    if not text:
        raise RuleError("Website is empty.")
    if "://" in text:
        try:
            text = urlsplit(text).hostname or ""
        except ValueError:
            raise RuleError("Website address could not be parsed.") from None
    else:
        text = text.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
        if "@" in text:
            raise RuleError("Website must not contain '@'.")
        if text.count(":") == 1:  # host:port
            text = text.split(":", 1)[0]
    text = text.strip().lower()
    if text.startswith("*."):
        text = text[2:]
    text = text.lstrip(".")
    if "*" in text:
        raise RuleError("Wildcards are not supported; 'example.com' already covers its subdomains.")

    try:
        ipaddress.IPv4Address(text)
        return text
    except ValueError:
        pass

    host = _to_ascii_host(text)
    if len(host) > 253:
        raise RuleError("Website name is too long.")
    labels = host.split(".")
    if host != "localhost" and len(labels) < 2:
        raise RuleError("Enter a full website name such as 'example.com' (top-level domains alone are not allowed).")
    for label in labels:
        if not _LABEL_RE.match(label):
            raise RuleError(f"'{value.strip()[:60]}' is not a valid website name.")
    return host


def host_of(url: Optional[str]) -> Optional[str]:
    if not url or not isinstance(url, str):
        return None
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    if not host:
        return None
    try:
        return _to_ascii_host(host)
    except RuleError:
        return None


def host_is_excluded(host: Optional[str], excluded: Iterable[str]) -> bool:
    """Exact host or any subdomain of an excluded site. 'example.com' matches
    'example.com' and 'a.b.example.com' but never 'notexample.com'."""
    if not host:
        return False
    try:
        h = _to_ascii_host(host)
    except RuleError:
        return False
    for site in excluded:
        if h == site or h.endswith("." + site):
            return True
    return False


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def default_config() -> Dict[str, Any]:
    return {
        "version": CONFIG_VERSION,
        "enabled": False,          # Mode A (disabled) is the default
        "mode": "ask",             # used once the user enables capture
        "extensions": {e: True for e in DEFAULT_EXTENSIONS},
        "custom_extensions": [],
        "excluded_sites": [],
    }


def normalize_config(raw: Any) -> Dict[str, Any]:
    """Coerces persisted or user-supplied data into a valid config. Never raises."""
    cfg = default_config()
    if not isinstance(raw, dict):
        return cfg
    cfg["enabled"] = raw.get("enabled") is True
    if raw.get("mode") in ("ask", "automatic"):
        cfg["mode"] = raw["mode"]
    exts = raw.get("extensions")
    saved = set()
    if isinstance(exts, dict):
        for e in DEFAULT_EXTENSIONS:
            if isinstance(exts.get(e), bool):
                cfg["extensions"][e] = exts[e]
                saved.add(e)
    # Extensions missing from an older saved configuration keep their default
    # (enabled) - the user never had the chance to turn them off.
    customs: List[str] = []
    raw_customs = raw.get("custom_extensions")
    for item in raw_customs if isinstance(raw_customs, list) else []:
        try:
            e = normalize_extension(item)
        except RuleError:
            continue
        if e in DEFAULT_EXTENSIONS:
            # A custom extension that is now a built-in one (e.g. a custom "webp"
            # from before v2) stays enabled unless the user saved a choice for it.
            if e not in saved:
                cfg["extensions"][e] = True
        elif e not in customs:
            customs.append(e)
    cfg["custom_extensions"] = customs[:MAX_CUSTOM_EXTENSIONS]
    sites: List[str] = []
    for item in raw.get("excluded_sites") or []:
        try:
            s = normalize_site(item)
        except RuleError:
            continue
        if s not in sites:
            sites.append(s)
    cfg["excluded_sites"] = sites[:MAX_EXCLUDED_SITES]
    return cfg


def effective_mode(cfg: Dict[str, Any]) -> str:
    return cfg["mode"] if cfg.get("enabled") else "disabled"


def enabled_extensions(cfg: Dict[str, Any]) -> Set[str]:
    on = {e for e, flag in cfg["extensions"].items() if flag}
    return on | set(cfg["custom_extensions"])


def public_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """The subset sent to the browser extension."""
    return {
        "version": CONFIG_VERSION,
        "mode": effective_mode(cfg),
        "preferred_mode": cfg["mode"],
        "extensions": sorted(enabled_extensions(cfg)),
        "excluded_sites": list(cfg["excluded_sites"]),
    }


class CaptureSettingsStore:
    """Persists the capture configuration as JSON in the existing SQLite
    ``settings`` table (no new database or file)."""

    def __init__(self, db: Any = None):
        self._db = db
        self._cfg = default_config()
        self.load()

    def load(self) -> Dict[str, Any]:
        if self._db is not None:
            try:
                raw = self._db.get_setting(CONFIG_SETTING_KEY, None)
                if raw:
                    self._cfg = normalize_config(json.loads(raw))
            except (ValueError, TypeError):
                self._cfg = default_config()
            except Exception:  # noqa: BLE001 - unreadable storage falls back to safe defaults
                self._cfg = default_config()
        return self.get()

    def get(self) -> Dict[str, Any]:
        return copy.deepcopy(self._cfg)

    def save(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
        self._cfg = normalize_config(cfg)
        if self._db is not None:
            self._db.set_setting(CONFIG_SETTING_KEY, json.dumps(self._cfg, sort_keys=True))
        return self.get()

    def reset(self) -> Dict[str, Any]:
        return self.save(default_config())
