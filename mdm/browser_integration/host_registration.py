"""
Reads the native messaging host registration written by
``native_host/install_native_host.ps1`` (Stage 6B settings status).

Read-only: this module never writes to the registry.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

HOST_NAME = "com.mydownloadmanager.native"
REGISTRY_KEYS = {
    "chrome": rf"Software\Google\Chrome\NativeMessagingHosts\{HOST_NAME}",
    "edge": rf"Software\Microsoft\Edge\NativeMessagingHosts\{HOST_NAME}",
}


def _read_default_value(subkey: str) -> Optional[str]:
    if sys.platform != "win32":
        return None
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey) as key:
            value, _type = winreg.QueryValueEx(key, "")
            return value if isinstance(value, str) else None
    except OSError:
        return None


def query_registration(reader=_read_default_value) -> Dict[str, Dict[str, Any]]:
    """Returns per-browser registration status:

    {"chrome": {"registered": bool, "manifest_path": str|None, "host_path": str|None,
                "host_exists": bool, "allowed_origins": [...], "problem": str|None}, "edge": {...}}
    """
    result: Dict[str, Dict[str, Any]] = {}
    for browser, subkey in REGISTRY_KEYS.items():
        info: Dict[str, Any] = {"registered": False, "manifest_path": None, "host_path": None,
                                "host_exists": False, "allowed_origins": [], "problem": None}
        manifest_path = reader(subkey)
        if not manifest_path:
            info["problem"] = "not registered"
            result[browser] = info
            continue
        info["manifest_path"] = manifest_path
        try:
            manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            info["problem"] = "host manifest missing or unreadable"
            result[browser] = info
            continue
        if not isinstance(manifest, dict) or manifest.get("name") != HOST_NAME:
            info["problem"] = "registry points to a different host"
            result[browser] = info
            continue
        host_path = manifest.get("path") if isinstance(manifest.get("path"), str) else None
        info["host_path"] = host_path
        info["host_exists"] = bool(host_path) and Path(host_path).is_file()
        origins = manifest.get("allowed_origins")
        info["allowed_origins"] = [o for o in origins if isinstance(o, str)] if isinstance(origins, list) else []
        if not info["host_exists"]:
            info["problem"] = "host executable missing"
        elif not info["allowed_origins"]:
            info["problem"] = "no extension IDs allowed"
        else:
            info["registered"] = True
        result[browser] = info
    return result
