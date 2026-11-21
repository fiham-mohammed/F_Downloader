import json
from pathlib import Path
from typing import Dict, Any, Optional

try:
    import platformdirs
    CONFIG_DIR = Path(platformdirs.user_config_dir("MyDownloadManager", appauthor=False))
except Exception:
    CONFIG_DIR = Path.home() / ".config" / "MyDownloadManager"

CONFIG_FILE = CONFIG_DIR / "settings.json"

VALID_CONNECTION_COUNTS = [1, 2, 4, 8, 16]
DEFAULT_CONNECTIONS = 8
DEFAULT_MIN_SEGMENT_SIZE = 2 * 1024 * 1024  # 2 MB

DEFAULT_MAX_SIMULTANEOUS = 2
DEFAULT_PRIORITY = "Normal"
DEFAULT_GLOBAL_SPEED_LIMIT = 0
DEFAULT_GLOBAL_SPEED_ENABLED = False
DEFAULT_INDIVIDUAL_SPEED_LIMIT = 0
DEFAULT_AUTO_RETRY_ENABLED = True
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_DELAY_BASE = 5


class Settings:
    """
    Manages application settings with persistent SQLite database storage,
    JSON file fallback, and in-memory defaults.
    """

    def __init__(self, db: Optional[Any] = None):
        self._db = db
        self._data: Dict[str, Any] = {
            "default_connections": DEFAULT_CONNECTIONS,
            "min_segment_size": DEFAULT_MIN_SEGMENT_SIZE,
            "max_simultaneous_downloads": DEFAULT_MAX_SIMULTANEOUS,
            "default_priority": DEFAULT_PRIORITY,
            "global_speed_limit": DEFAULT_GLOBAL_SPEED_LIMIT,
            "global_speed_limit_enabled": DEFAULT_GLOBAL_SPEED_ENABLED,
            "default_individual_speed_limit": DEFAULT_INDIVIDUAL_SPEED_LIMIT,
            "automatic_retry_enabled": DEFAULT_AUTO_RETRY_ENABLED,
            "maximum_retry_attempts": DEFAULT_MAX_RETRIES,
            "retry_delay_base": DEFAULT_RETRY_DELAY_BASE,
        }
        self.load()

    def bind_database(self, db: Optional[Any]):
        """Binds an active DatabaseManager instance for SQLite-backed settings persistence."""
        self._db = db
        if db is not None:
            self.load()

    def get_database(self) -> Optional[Any]:
        return self._db

    def load(self):
        # 1. Load from JSON if present
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
                        max_sim = loaded.get("max_simultaneous_downloads")
                        if isinstance(max_sim, int) and 1 <= max_sim <= 10:
                            self._data["max_simultaneous_downloads"] = max_sim
                        if "default_priority" in loaded:
                            self._data["default_priority"] = loaded["default_priority"]
                        if "global_speed_limit" in loaded:
                            self._data["global_speed_limit"] = int(loaded["global_speed_limit"])
                        if "global_speed_limit_enabled" in loaded:
                            self._data["global_speed_limit_enabled"] = bool(loaded["global_speed_limit_enabled"])
                        if "default_individual_speed_limit" in loaded:
                            self._data["default_individual_speed_limit"] = int(loaded["default_individual_speed_limit"])
                        if "automatic_retry_enabled" in loaded:
                            self._data["automatic_retry_enabled"] = bool(loaded["automatic_retry_enabled"])
                        if "maximum_retry_attempts" in loaded:
                            self._data["maximum_retry_attempts"] = int(loaded["maximum_retry_attempts"])
                        if "retry_delay_base" in loaded:
                            self._data["retry_delay_base"] = int(loaded["retry_delay_base"])
        except Exception:
            pass  # Fall back to defaults on read error

        # 2. If database is available, load and synchronize settings from SQLite
        if self._db is not None:
            try:
                for key, default_val in list(self._data.items()):
                    val_str = self._db.get_setting(key, None)
                    if val_str is not None:
                        # Parse according to default_val type
                        if isinstance(default_val, bool):
                            self._data[key] = val_str.lower() in ("true", "1", "yes")
                        elif isinstance(default_val, int):
                            self._data[key] = int(val_str)
                        else:
                            self._data[key] = val_str
                    else:
                        # Not yet in DB, write current value to migrate into SQLite
                        self._db.set_setting(key, str(self._data[key]))
            except Exception:
                pass

    def save(self):
        # 1. Save to SQLite database if bound
        if self._db is not None:
            try:
                for key, val in self._data.items():
                    self._db.set_setting(key, str(val))
            except Exception:
                pass

        # 2. Save to JSON file as fallback
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

    @property
    def max_simultaneous_downloads(self) -> int:
        return self._data.get("max_simultaneous_downloads", DEFAULT_MAX_SIMULTANEOUS)

    @max_simultaneous_downloads.setter
    def max_simultaneous_downloads(self, val: int):
        if isinstance(val, int) and 1 <= val <= 10:
            self._data["max_simultaneous_downloads"] = val
            self.save()

    @property
    def default_priority(self) -> str:
        return self._data.get("default_priority", DEFAULT_PRIORITY)

    @default_priority.setter
    def default_priority(self, val: str):
        if val in ("High", "Normal", "Low"):
            self._data["default_priority"] = val
            self.save()

    @property
    def global_speed_limit(self) -> int:
        return self._data.get("global_speed_limit", DEFAULT_GLOBAL_SPEED_LIMIT)

    @global_speed_limit.setter
    def global_speed_limit(self, val: int):
        self._data["global_speed_limit"] = max(0, int(val))
        self.save()

    @property
    def global_speed_limit_enabled(self) -> bool:
        return self._data.get("global_speed_limit_enabled", DEFAULT_GLOBAL_SPEED_ENABLED)

    @global_speed_limit_enabled.setter
    def global_speed_limit_enabled(self, val: bool):
        self._data["global_speed_limit_enabled"] = bool(val)
        self.save()

    @property
    def default_individual_speed_limit(self) -> int:
        return self._data.get("default_individual_speed_limit", DEFAULT_INDIVIDUAL_SPEED_LIMIT)

    @default_individual_speed_limit.setter
    def default_individual_speed_limit(self, val: int):
        self._data["default_individual_speed_limit"] = max(0, int(val))
        self.save()

    @property
    def automatic_retry_enabled(self) -> bool:
        return self._data.get("automatic_retry_enabled", DEFAULT_AUTO_RETRY_ENABLED)

    @automatic_retry_enabled.setter
    def automatic_retry_enabled(self, val: bool):
        self._data["automatic_retry_enabled"] = bool(val)
        self.save()

    @property
    def maximum_retry_attempts(self) -> int:
        return self._data.get("maximum_retry_attempts", DEFAULT_MAX_RETRIES)

    @maximum_retry_attempts.setter
    def maximum_retry_attempts(self, val: int):
        if isinstance(val, int) and val >= 0:
            self._data["maximum_retry_attempts"] = val
            self.save()

    @property
    def retry_delay_base(self) -> int:
        return self._data.get("retry_delay_base", DEFAULT_RETRY_DELAY_BASE)

    @retry_delay_base.setter
    def retry_delay_base(self, val: int):
        if isinstance(val, int) and val > 0:
            self._data["retry_delay_base"] = val
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


def get_max_simultaneous_downloads() -> int:
    return app_settings.max_simultaneous_downloads


def set_max_simultaneous_downloads(count: int):
    app_settings.max_simultaneous_downloads = count


def get_global_speed_limit() -> int:
    return app_settings.global_speed_limit


def set_global_speed_limit(limit_bps: int):
    app_settings.global_speed_limit = limit_bps


def is_global_speed_limit_enabled() -> bool:
    return app_settings.global_speed_limit_enabled


def set_global_speed_limit_enabled(enabled: bool):
    app_settings.global_speed_limit_enabled = enabled


def get_default_priority() -> str:
    return app_settings.default_priority


def set_default_priority(priority: str):
    app_settings.default_priority = priority


def is_auto_retry_enabled() -> bool:
    return app_settings.automatic_retry_enabled


def set_auto_retry_enabled(enabled: bool):
    app_settings.automatic_retry_enabled = enabled


def get_max_retry_attempts() -> int:
    return app_settings.maximum_retry_attempts


def set_max_retry_attempts(attempts: int):
    app_settings.maximum_retry_attempts = attempts


def get_default_individual_speed_limit() -> int:
    return app_settings.default_individual_speed_limit


def set_default_individual_speed_limit(limit_bps: int):
    app_settings.default_individual_speed_limit = limit_bps
