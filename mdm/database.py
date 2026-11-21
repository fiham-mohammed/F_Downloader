"""
SQLite database persistence layer for My Download Manager (Stage 4).
Handles thread-safe persistence, schema migrations, state tracking, and recovery.
"""
import sqlite3
import threading
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime

try:
    import platformdirs
    DEFAULT_DATA_DIR = Path(platformdirs.user_data_dir("MyDownloadManager", appauthor=False))
except Exception:
    DEFAULT_DATA_DIR = Path.home() / ".local" / "share" / "MyDownloadManager"

DEFAULT_DB_FILE = DEFAULT_DATA_DIR / "downloads.db"
CURRENT_SCHEMA_VERSION = 3  # v3 (Stage 6A): browser_requests idempotency table

# Stage 6A: browser request idempotency retention policy.
BROWSER_REQUEST_RETENTION_DAYS = 30
BROWSER_REQUEST_MAX_ROWS = 10_000


from mdm.models import DownloadModel, SegmentModel, DownloadStatus, can_transition


class DatabaseManager:
    """
    Thread-safe SQLite database manager for downloads, segments, and settings.
    Uses thread-local connections and WAL mode for high concurrency.
    """

    def __init__(self, db_path: Optional[str | Path] = None):
        if db_path is not None:
            self.db_path = Path(db_path)
        else:
            self.db_path = DEFAULT_DB_FILE

        self._local = threading.local()
        self._write_lock = threading.Lock()
        self._init_lock = threading.Lock()
        self._all_connections: List[sqlite3.Connection] = []
        self._conns_lock = threading.Lock()

        # Ensure directory exists and initialize schema
        self._ensure_dir()
        self.init_db()

    def _ensure_dir(self):
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    def get_connection(self) -> sqlite3.Connection:
        """Returns a thread-local SQLite connection configured with WAL and foreign keys."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            self._ensure_dir()
            conn = sqlite3.connect(
                str(self.db_path),
                timeout=10.0,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON;")
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA busy_timeout = 5000;")
            self._local.conn = conn
            with self._conns_lock:
                self._all_connections.append(conn)
        return conn

    def close(self):
        """Closes all tracked SQLite connections."""
        with self._conns_lock:
            for conn in list(self._all_connections):
                try:
                    conn.close()
                except Exception:
                    pass
            self._all_connections.clear()
        self._local.conn = None

    def init_db(self):
        """Initializes tables and handles schema migrations."""
        with self._init_lock, self._write_lock:
            conn = self.get_connection()
            with conn:
                # Check schema version
                cur = conn.cursor()
                cur.execute("PRAGMA user_version;")
                version = cur.fetchone()[0]

                if version == 0:
                    self._create_schema_v1(conn)
                    self._migrate(conn, 1, CURRENT_SCHEMA_VERSION)
                    conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION};")
                elif version < CURRENT_SCHEMA_VERSION:
                    self._migrate(conn, version, CURRENT_SCHEMA_VERSION)
                    conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION};")

    def _create_schema_v1(self, conn: sqlite3.Connection):
        """Creates initial Version 1 schema."""
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS downloads (
                id TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                original_url TEXT,
                filename TEXT NOT NULL,
                destination_path TEXT NOT NULL,
                temp_path TEXT,
                status TEXT NOT NULL,
                download_mode TEXT NOT NULL DEFAULT 'single',
                connection_count INTEGER NOT NULL DEFAULT 1,
                total_size INTEGER,
                downloaded_size INTEGER NOT NULL DEFAULT 0,
                speed REAL DEFAULT 0.0,
                eta REAL,
                created_at TEXT NOT NULL,
                started_at TEXT,
                paused_at TEXT,
                completed_at TEXT,
                failed_at TEXT,
                error_message TEXT,
                etag TEXT,
                last_modified TEXT,
                supports_range INTEGER DEFAULT 1,
                queue_order INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS segments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                download_id TEXT NOT NULL,
                segment_index INTEGER NOT NULL,
                start_byte INTEGER NOT NULL,
                end_byte INTEGER NOT NULL,
                downloaded_bytes INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'Queued',
                temp_path TEXT,
                retry_count INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY(download_id) REFERENCES downloads(id) ON DELETE CASCADE,
                UNIQUE(download_id, segment_index)
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_downloads_status ON downloads(status);
            CREATE INDEX IF NOT EXISTS idx_downloads_queue_order ON downloads(queue_order);
            CREATE INDEX IF NOT EXISTS idx_segments_download_id ON segments(download_id);
        """)

    def _migrate(self, conn: sqlite3.Connection, from_version: int, to_version: int):
        """Handles sequential schema migrations."""
        if from_version < 2 and to_version >= 2:
            # Migration to Stage 5 schema (version 2)
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(downloads);")
            existing_cols = {row["name"] for row in cur.fetchall()}

            columns_to_add = [
                ("priority", "TEXT DEFAULT 'Normal'"),
                ("scheduled_start_at", "TEXT DEFAULT NULL"),
                ("scheduled_stop_at", "TEXT DEFAULT NULL"),
                ("schedule_enabled", "INTEGER DEFAULT 0"),
                ("individual_speed_limit", "INTEGER DEFAULT 0"),
                ("category", "TEXT DEFAULT 'Other'"),
                ("retry_count", "INTEGER DEFAULT 0"),
                ("max_retry_attempts", "INTEGER DEFAULT 3"),
                ("next_retry_at", "TEXT DEFAULT NULL"),
                ("retry_enabled", "INTEGER DEFAULT 1"),
            ]

            for col_name, col_def in columns_to_add:
                if col_name not in existing_cols:
                    conn.execute(f"ALTER TABLE downloads ADD COLUMN {col_name} {col_def};")

            conn.execute("CREATE INDEX IF NOT EXISTS idx_downloads_priority ON downloads(priority);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_downloads_category ON downloads(category);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_downloads_schedule ON downloads(schedule_enabled, scheduled_start_at);")

        if from_version < 3 and to_version >= 3:
            # Migration to Stage 6A schema (version 3): durable browser request
            # idempotency. Purely additive - existing tables and rows are untouched.
            # Only a SHA-256 fingerprint of the URL is stored, never the URL itself
            # (signed URLs can carry access tokens in their query string).
            # No foreign key: the record must outlive a download the user removes,
            # so a replayed request_id is still recognised.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS browser_requests (
                    request_id  TEXT PRIMARY KEY,
                    download_id TEXT,
                    browser     TEXT NOT NULL,
                    url_sha256  TEXT NOT NULL,
                    status      TEXT NOT NULL,
                    created_at  TEXT NOT NULL
                );
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_browser_requests_created ON browser_requests(created_at);"
            )

    # --- Browser request idempotency (Stage 6A) ---

    @staticmethod
    def _insert_browser_request(cur: sqlite3.Cursor, download_id: str, record: Dict[str, Any]):
        """Plain INSERT (no OR REPLACE): a duplicate request_id raises
        sqlite3.IntegrityError, which rolls back the enclosing transaction."""
        cur.execute(
            """
            INSERT INTO browser_requests (request_id, download_id, browser, url_sha256, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?);
            """,
            (
                record["request_id"],
                download_id,
                record["browser"],
                record["url_sha256"],
                record.get("status", "accepted"),
                record.get("created_at") or datetime.now().isoformat(),
            ),
        )

    def get_browser_request(self, request_id: str) -> Optional[Dict[str, Any]]:
        conn = self.get_connection()
        row = conn.execute(
            "SELECT request_id, download_id, browser, url_sha256, status, created_at "
            "FROM browser_requests WHERE request_id = ?;",
            (request_id,),
        ).fetchone()
        return dict(row) if row else None

    def purge_browser_requests(
        self,
        max_age_days: int = BROWSER_REQUEST_RETENTION_DAYS,
        max_rows: int = BROWSER_REQUEST_MAX_ROWS,
        now: Optional[datetime] = None,
    ) -> int:
        """Retention policy: drops idempotency records older than ``max_age_days``
        and keeps at most ``max_rows`` newest records. Download rows are never touched.
        Returns the number of records removed."""
        from datetime import timedelta
        cutoff = ((now or datetime.now()) - timedelta(days=max_age_days)).isoformat()
        with self._write_lock:
            conn = self.get_connection()
            with conn:
                removed = conn.execute(
                    "DELETE FROM browser_requests WHERE created_at < ?;", (cutoff,)
                ).rowcount
                removed += conn.execute(
                    """
                    DELETE FROM browser_requests WHERE request_id NOT IN (
                        SELECT request_id FROM browser_requests ORDER BY created_at DESC LIMIT ?
                    );
                    """,
                    (max_rows,),
                ).rowcount
        return removed

    # --- Downloads CRUD ---

    def add_download(self, d: DownloadModel, browser_request: Optional[Dict[str, Any]] = None) -> bool:
        """Inserts a new download record and optional initial segments.

        Stage 6A: when ``browser_request`` is given, its idempotency record is
        inserted in the SAME transaction. Either both rows are committed or
        neither is (e.g. a duplicate request_id raises sqlite3.IntegrityError
        and the download row is rolled back)."""
        with self._write_lock:
            conn = self.get_connection()
            with conn:
                # Determine next queue_order if not explicitly given
                cur = conn.cursor()
                if d.queue_order is None or d.queue_order <= 0:
                    cur.execute("SELECT COALESCE(MAX(queue_order), 0) + 1 FROM downloads;")
                    d.queue_order = cur.fetchone()[0]

                cur.execute(
                    """
                    INSERT OR REPLACE INTO downloads (
                        id, url, original_url, filename, destination_path, temp_path,
                        status, download_mode, connection_count, total_size,
                        downloaded_size, speed, eta, created_at, started_at,
                        paused_at, completed_at, failed_at, error_message,
                        etag, last_modified, supports_range, queue_order,
                        priority, scheduled_start_at, scheduled_stop_at, schedule_enabled,
                        individual_speed_limit, category, retry_count, max_retry_attempts, next_retry_at, retry_enabled
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    );
                    """,
                    (
                        d.id, d.url, d.original_url, d.filename, d.destination_path, d.temp_path,
                        d.status, d.download_mode, d.connection_count, d.total_size,
                        d.downloaded_size, d.speed, d.eta, d.created_at, d.started_at,
                        d.paused_at, d.completed_at, d.failed_at, d.error_message,
                        d.etag, d.last_modified, d.supports_range, d.queue_order,
                        getattr(d, "priority", "Normal"),
                        getattr(d, "scheduled_start_at", None),
                        getattr(d, "scheduled_stop_at", None),
                        getattr(d, "schedule_enabled", 0),
                        getattr(d, "individual_speed_limit", 0),
                        getattr(d, "category", "Other"),
                        getattr(d, "retry_count", 0) or 0,
                        getattr(d, "max_retry_attempts", 3),
                        getattr(d, "next_retry_at", None),
                        getattr(d, "retry_enabled", 1),
                    ),
                )

                if d.segments:
                    for s in d.segments:
                        cur.execute(
                            """
                            INSERT OR REPLACE INTO segments (
                                download_id, segment_index, start_byte, end_byte,
                                downloaded_bytes, status, temp_path, retry_count
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                            """,
                            (
                                d.id, s.segment_index, s.start_byte, s.end_byte,
                                s.downloaded_bytes, s.status, s.temp_path, s.retry_count
                            ),
                        )

                if browser_request is not None:
                    self._insert_browser_request(cur, d.id, browser_request)
            return True

    def get_download(self, download_id: str) -> Optional[DownloadModel]:
        """Retrieves a single download record with its segments."""
        conn = self.get_connection()
        cur = conn.cursor()
        cur.execute("SELECT * FROM downloads WHERE id = ?;", (download_id,))
        row = cur.fetchone()
        if not row:
            return None

        d = self._row_to_download_model(row)
        d.segments = self.get_segments(download_id)
        return d

    def get_all_downloads(self) -> List[DownloadModel]:
        """Retrieves all downloads sorted by queue_order, created_at."""
        conn = self.get_connection()
        cur = conn.cursor()
        cur.execute("SELECT * FROM downloads ORDER BY queue_order ASC, created_at ASC;")
        rows = cur.fetchall()
        result = []
        for row in rows:
            d = self._row_to_download_model(row)
            d.segments = self.get_segments(d.id)
            result.append(d)
        return result

    def _row_to_download_model(self, row: sqlite3.Row) -> DownloadModel:
        final_p = str(Path(row["destination_path"]) / row["filename"]) if (row["destination_path"] and row["filename"]) else None
        keys = row.keys()
        return DownloadModel(
            id=row["id"],
            url=row["url"],
            original_url=row["original_url"],
            filename=row["filename"],
            destination_path=row["destination_path"],
            temp_path=row["temp_path"],
            status=row["status"],
            download_mode=row["download_mode"],
            connection_count=row["connection_count"] or 1,
            connections=row["connection_count"] or 1,
            total_size=row["total_size"],
            downloaded_size=row["downloaded_size"],
            speed=row["speed"] or 0.0,
            eta=row["eta"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            paused_at=row["paused_at"],
            completed_at=row["completed_at"],
            failed_at=row["failed_at"],
            error_message=row["error_message"],
            final_path=final_p,
            etag=row["etag"],
            last_modified=row["last_modified"],
            supports_range=row["supports_range"],
            queue_order=row["queue_order"] or 0,
            priority=row["priority"] if "priority" in keys and row["priority"] else "Normal",
            scheduled_start_at=row["scheduled_start_at"] if "scheduled_start_at" in keys else None,
            scheduled_stop_at=row["scheduled_stop_at"] if "scheduled_stop_at" in keys else None,
            schedule_enabled=row["schedule_enabled"] if "schedule_enabled" in keys and row["schedule_enabled"] is not None else 0,
            individual_speed_limit=row["individual_speed_limit"] if "individual_speed_limit" in keys and row["individual_speed_limit"] is not None else 0,
            category=row["category"] if "category" in keys and row["category"] else "Other",
            retry_count=row["retry_count"] if "retry_count" in keys and row["retry_count"] is not None else 0,
            max_retry_attempts=row["max_retry_attempts"] if "max_retry_attempts" in keys and row["max_retry_attempts"] is not None else 3,
            next_retry_at=row["next_retry_at"] if "next_retry_at" in keys else None,
            retry_enabled=row["retry_enabled"] if "retry_enabled" in keys and row["retry_enabled"] is not None else 1,
        )

    def update_download_status(
        self,
        download_id: str,
        status: str,
        error_message: Optional[str] = None,
        final_path: Optional[str] = None,
        completed_at: Optional[str] = None,
        failed_at: Optional[str] = None,
        **kwargs,
    ) -> bool:
        """Updates download status with timestamp tracking and state validation."""
        now = completed_at or failed_at or datetime.now().isoformat()
        with self._write_lock:
            conn = self.get_connection()
            with conn:
                cur = conn.cursor()
                cur.execute("SELECT status FROM downloads WHERE id = ?;", (download_id,))
                row = cur.fetchone()
                if not row:
                    return False

                current_status = row["status"]
                if not can_transition(current_status, status):
                    # Reject invalid transitions (e.g. Completed -> Queued automatically)
                    return False

                updates = ["status = ?"]
                params: List[Any] = [status]

                if status == DownloadStatus.DOWNLOADING:
                    updates.append("started_at = COALESCE(started_at, ?)")
                    params.append(now)
                elif status == DownloadStatus.PAUSED:
                    updates.append("paused_at = ?")
                    params.append(now)
                elif status == DownloadStatus.COMPLETED:
                    updates.append("completed_at = ?")
                    params.append(now)
                    updates.append("temp_path = NULL")
                    if final_path:
                        updates.append("destination_path = ?")
                        params.append(str(Path(final_path).parent))
                        updates.append("filename = ?")
                        params.append(Path(final_path).name)
                elif status == DownloadStatus.FAILED:
                    updates.append("failed_at = ?")
                    params.append(now)
                    if error_message:
                        updates.append("error_message = ?")
                        params.append(error_message)
                elif status == DownloadStatus.INTERRUPTED:
                    updates.append("paused_at = ?")
                    params.append(now)

                params.append(download_id)
                query = f"UPDATE downloads SET {', '.join(updates)} WHERE id = ?;"
                cur.execute(query, params)
            return True

    def update_download_progress(
        self,
        download_id: str,
        downloaded_size: int,
        *args,
        speed: float = 0.0,
        eta: Optional[float] = None,
        total_size: Optional[int] = None,
    ) -> bool:
        """Updates progress fields for a download without locking other transactions for long."""
        if len(args) == 3:
            # Check if called as (total_size, speed, eta) or (speed, eta, total_size)
            if (isinstance(args[0], int) or args[0] is None) and isinstance(args[1], (int, float)):
                total_size = args[0]
                speed = float(args[1]) if args[1] is not None else 0.0
                eta = args[2]
            else:
                speed = float(args[0]) if args[0] is not None else 0.0
                eta = args[1]
                total_size = args[2]
        elif len(args) == 1:
            total_size = args[0]
        elif len(args) == 2:
            speed = float(args[0]) if args[0] is not None else 0.0
            eta = args[1]

        with self._write_lock:
            conn = self.get_connection()
            with conn:
                cur = conn.cursor()
                if total_size is not None and total_size > 0:
                    cur.execute(
                        """
                        UPDATE downloads
                        SET downloaded_size = ?, speed = ?, eta = ?, total_size = ?
                        WHERE id = ?;
                        """,
                        (downloaded_size, speed, eta, total_size, download_id),
                    )
                else:
                    cur.execute(
                        """
                        UPDATE downloads
                        SET downloaded_size = ?, speed = ?, eta = ?
                        WHERE id = ?;
                        """,
                        (downloaded_size, speed, eta, download_id),
                    )
            return True

    def update_download_metadata(
        self,
        download_id: str,
        filename: Optional[str] = None,
        temp_path: Optional[str] = None,
        etag: Optional[str] = None,
        last_modified: Optional[str] = None,
        supports_range: Optional[int] = None,
        total_size: Optional[int] = None,
        download_mode: Optional[str] = None,
        connection_count: Optional[int] = None,
        priority: Optional[str] = None,
        scheduled_start_at: Optional[str] = None,
        scheduled_stop_at: Optional[str] = None,
        schedule_enabled: Optional[int] = None,
        individual_speed_limit: Optional[int] = None,
        category: Optional[str] = None,
        max_retry_attempts: Optional[int] = None,
        next_retry_at: Optional[str] = None,
        retry_enabled: Optional[int] = None,
        queue_order: Optional[int] = None,
        retry_count: Optional[int] = None,
        error_message: Optional[str] = None,
    ) -> bool:
        """Updates detected server metadata (ETag, filename, temp_path, total size) or Stage 5 fields."""
        updates = []
        params = []
        if filename is not None:
            updates.append("filename = ?")
            params.append(filename)
        if temp_path is not None:
            if temp_path == "":
                updates.append("temp_path = NULL")
            else:
                updates.append("temp_path = ?")
                params.append(temp_path)
        if etag is not None:
            updates.append("etag = ?")
            params.append(etag)
        if last_modified is not None:
            updates.append("last_modified = ?")
            params.append(last_modified)
        if supports_range is not None:
            updates.append("supports_range = ?")
            params.append(supports_range)
        if total_size is not None:
            updates.append("total_size = ?")
            params.append(total_size)
        if download_mode is not None:
            updates.append("download_mode = ?")
            params.append(download_mode)
        if connection_count is not None:
            updates.append("connection_count = ?")
            params.append(connection_count)
        if priority is not None:
            updates.append("priority = ?")
            params.append(priority)
        if scheduled_start_at is not None:
            updates.append("scheduled_start_at = ?")
            params.append(scheduled_start_at)
        if scheduled_stop_at is not None:
            updates.append("scheduled_stop_at = ?")
            params.append(scheduled_stop_at)
        if schedule_enabled is not None:
            updates.append("schedule_enabled = ?")
            params.append(schedule_enabled)
        if individual_speed_limit is not None:
            updates.append("individual_speed_limit = ?")
            params.append(individual_speed_limit)
        if category is not None:
            updates.append("category = ?")
            params.append(category)
        if max_retry_attempts is not None:
            updates.append("max_retry_attempts = ?")
            params.append(max_retry_attempts)
        if next_retry_at is not None:
            updates.append("next_retry_at = ?")
            params.append(next_retry_at)
        if retry_enabled is not None:
            updates.append("retry_enabled = ?")
            params.append(retry_enabled)
        if queue_order is not None:
            updates.append("queue_order = ?")
            params.append(queue_order)
        if retry_count is not None:
            updates.append("retry_count = ?")
            params.append(retry_count)
        if error_message is not None:
            updates.append("error_message = ?")
            params.append(error_message)

        if not updates:
            return False

        params.append(download_id)
        with self._write_lock:
            conn = self.get_connection()
            with conn:
                conn.execute(f"UPDATE downloads SET {', '.join(updates)} WHERE id = ?;", params)
            return True

    def update_priority(self, download_id: str, priority: str) -> bool:
        return self.update_download_metadata(download_id, priority=priority)

    def update_schedule(
        self,
        download_id: str,
        start_at: Optional[str],
        stop_at: Optional[str] = None,
        enabled: bool = True,
    ) -> bool:
        return self.update_download_metadata(
            download_id,
            scheduled_start_at=start_at,
            scheduled_stop_at=stop_at,
            schedule_enabled=1 if enabled else 0,
        )

    def update_speed_limit(self, download_id: str, limit_bps: int) -> bool:
        return self.update_download_metadata(download_id, individual_speed_limit=limit_bps)

    def update_category(self, download_id: str, category: str) -> bool:
        return self.update_download_metadata(download_id, category=category)

    def update_retry_info(
        self,
        download_id: str,
        retry_count: int,
        next_retry_at: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> bool:
        return self.update_download_metadata(
            download_id,
            retry_count=retry_count,
            next_retry_at=next_retry_at,
            error_message=error_message,
        )

    def update_queue_order(self, download_id: str, queue_order: int) -> bool:
        return self.update_download_metadata(download_id, queue_order=queue_order)

    def remove_download(self, download_id: str) -> bool:
        """Removes a download and cascades deletion of its segments."""
        with self._write_lock:
            conn = self.get_connection()
            with conn:
                cur = conn.cursor()
                cur.execute("DELETE FROM downloads WHERE id = ?;", (download_id,))
                return cur.rowcount > 0

    # --- Segments CRUD ---

    def add_or_update_segments(self, download_id: str, segments: List[SegmentModel]) -> bool:
        """Inserts or replaces segment records for a multi-connection download."""
        with self._write_lock:
            conn = self.get_connection()
            with conn:
                cur = conn.cursor()
                for s in segments:
                    cur.execute(
                        """
                        INSERT OR REPLACE INTO segments (
                            download_id, segment_index, start_byte, end_byte,
                            downloaded_bytes, status, temp_path, retry_count
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                        """,
                        (
                            download_id, s.segment_index, s.start_byte, s.end_byte,
                            s.downloaded_bytes, s.status, s.temp_path, s.retry_count
                        ),
                    )
            return True

    def update_segment_progress(
        self,
        download_id: str,
        segment_index: int,
        downloaded_bytes: int,
        status: Optional[str] = None,
        temp_path: Optional[str] = None,
    ) -> bool:
        """Updates progress and status for a specific segment."""
        updates = ["downloaded_bytes = ?"]
        params: List[Any] = [downloaded_bytes]
        if status is not None:
            updates.append("status = ?")
            params.append(status)
        if temp_path is not None:
            if temp_path == "":
                updates.append("temp_path = NULL")
            else:
                updates.append("temp_path = ?")
                params.append(temp_path)

        params.extend([download_id, segment_index])
        with self._write_lock:
            conn = self.get_connection()
            with conn:
                conn.execute(
                    f"UPDATE segments SET {', '.join(updates)} WHERE download_id = ? AND segment_index = ?;",
                    params,
                )
            return True

    def get_segments(self, download_id: str) -> List[SegmentModel]:
        """Retrieves all segments for a download ordered by segment_index."""
        conn = self.get_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM segments WHERE download_id = ? ORDER BY segment_index ASC;",
            (download_id,),
        )
        rows = cur.fetchall()
        segments = []
        for row in rows:
            segments.append(
                SegmentModel(
                    id=row["id"],
                    download_id=row["download_id"],
                    segment_index=row["segment_index"],
                    start_byte=row["start_byte"],
                    end_byte=row["end_byte"],
                    downloaded_bytes=row["downloaded_bytes"],
                    status=row["status"],
                    temp_path=row["temp_path"],
                    retry_count=row["retry_count"],
                )
            )
        return segments

    def reset_segments(self, download_id: str) -> bool:
        """Deletes all segments for a download (used when server content changes and restart from 0 is required)."""
        with self._write_lock:
            conn = self.get_connection()
            with conn:
                conn.execute("DELETE FROM segments WHERE download_id = ?;", (download_id,))
            return True

    # --- Recovery and Settings ---

    def recover_interrupted_downloads(self) -> List[str]:
        """
        Finds downloads previously left in 'Downloading' (or retrying states)
        and transitions them safely to 'Interrupted'. Returns list of recovered download IDs.
        """
        with self._write_lock:
            conn = self.get_connection()
            with conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT id FROM downloads WHERE status = 'Downloading' OR status LIKE 'Retrying%';"
                )
                rows = cur.fetchall()
                recovered_ids = [row["id"] for row in rows]
                if recovered_ids:
                    now = datetime.now().isoformat()
                    cur.execute(
                        """
                        UPDATE downloads
                        SET status = 'Interrupted', paused_at = ?, speed = 0.0, eta = NULL
                        WHERE status = 'Downloading' OR status LIKE 'Retrying%';
                        """,
                        (now,),
                    )
                return recovered_ids

    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Reads a setting value from SQLite."""
        conn = self.get_connection()
        cur = conn.cursor()
        cur.execute("SELECT value FROM settings WHERE key = ?;", (key,))
        row = cur.fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str):
        """Writes a setting value to SQLite."""
        with self._write_lock:
            conn = self.get_connection()
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?);",
                    (key, value),
                )

    # Aliases for QueueManager convenience
    update_status = update_download_status
    update_progress = update_download_progress
    update_validators = update_download_metadata
    update_metadata = update_download_metadata
    delete_download = remove_download
    save_segments = add_or_update_segments
    delete_segments = reset_segments
