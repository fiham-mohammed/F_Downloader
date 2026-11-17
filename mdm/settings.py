import json
from pathlib import Path
from typing import Dict, Any

try:
    import platformdirs
    CONFIG_DIR = Path(platformdirs.user_config_dir("MyDownloadManager", appauthor=False))
except Exception:
    CONFIG_DIR = Path.home() / ".config" / "MyDownloadManager"

CONFIG_FILE = CONFIG_DIR / "settings.json"

VALID_CONNECTION_COUNTS = [1, 2, 4, 8, 16]
DEFAULT_CONNECTIONS = 8
DEFAULT_MIN_SEGMENT_SIZE = 2 * 1024 * 1024  # 2 MB


class Settings:
    """Manages application settings with JSON file persistence and in-memory fallback."""

    def __init__(self):
        self._data: Dict[str, Any] = {
            "default_connections": DEFAULT_CONNECTIONS,
            "min_segment_size": DEFAULT_MIN_SEGMENT_SIZE,
        }
        self.load()

    def load(self):
        try:
            if CONFIG_FILE.exists():
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    if isinstance(loaded, dict):
                        conn = loaded.get("default_connections")
                        if conn in VALID_CONNECTION_COUNTS:
                            self._data["default_connections"] = conn
                        min_size = loaded.get("min_segment_size")
                        if isinstance(min_size, int) and min_size > 0:
                            self._data["min_segment_size"] = min_size
        except Exception:
            pass  # Fall back to defaults on read error

    def save(self):
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2)
        except Exception:
            pass  # Fail gracefully in restricted environments

    @property
    def default_connections(self) -> int:
        return self._data.get("default_connections", DEFAULT_CONNECTIONS)

    @default_connections.setter
    def default_connections(self, val: int):
        if val in VALID_CONNECTION_COUNTS:
            self._data["default_connections"] = val
            self.save()

    @property
    def min_segment_size(self) -> int:
        return self._data.get("min_segment_size", DEFAULT_MIN_SEGMENT_SIZE)

    @min_segment_size.setter
    def min_segment_size(self, val: int):
        if isinstance(val, int) and val > 0:
            self._data["min_segment_size"] = val
            self.save()


# Global settings instance
app_settings = Settings()


def get_default_connections() -> int:
    return app_settings.default_connections


def set_default_connections(count: int):
    app_settings.default_connections = count


def get_min_segment_size() -> int:
    return app_settings.min_segment_size


def set_min_segment_size(size_bytes: int):
    app_settings.min_segment_size = size_bytes
