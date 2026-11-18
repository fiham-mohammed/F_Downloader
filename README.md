# My Download Manager — Stage 4: Pause, Resume, Persistence & Recovery

A modern Windows desktop application built with Python 3.12/3.13, PySide6, HTTPX, and SQLite.

Stage 4 introduces comprehensive **pause, resume, persistence, and crash recovery**, backed by a thread-safe SQLite database. Downloads interrupted by application closure, system reboot, network loss, or explicit pause can be resumed later without re-downloading previously completed bytes.

The application passes all **107 automated regression tests** (Stage 2: 27 tests, Stage 3: 33 tests, Stage 4: 47 tests) with 100% success.

---

## What's New in Stage 4

### 1. Persistent SQLite Database (`downloads.db`)
- **Database Location:** Stored securely in Windows `%LOCALAPPDATA%\MyDownloadManager\downloads.db` using `platformdirs`.
- **Database Schema Versioning:** Tracks schema versions with `PRAGMA user_version = 1`.
- **Foreign Keys & Cascade Deletes:** `FOREIGN KEY(download_id) REFERENCES downloads(id) ON DELETE CASCADE`. Removing a download record automatically removes its segments.
- **Thread Safety & High Concurrency:** Employs thread-local connections and SQLite WAL (`Write-Ahead Logging`) mode with re-entrant write locks.
- **Configurable for Testing:** Database path is configurable; unittests run socket-free and database-isolated without polluting user download history.

### 2. State Machine & Lifecycle Tracking
- Strict, validated state transitions governed by `mdm.models.VALID_TRANSITIONS`:
  - `Queued`: Waiting in line for sequential execution.
  - `Downloading`: Actively streaming segments or single connection.
  - `Paused`: Paused by the user; partial files and `.parts` segment files preserved on disk.
  - `Interrupted`: Automatically set on startup for downloads left active during an unexpected shutdown or crash.
  - `Completed`: Download verified and final file placed on disk.
  - `Failed`: Permanent network, server, or disk error.
  - `Cancelled`: Cancelled by user.

### 3. Pause & Resume for Single-Connection Downloads
- **Partial File Preservation:** Pausing a single-connection download flushes written bytes and preserves the `.part` file on disk.
- **HTTP Range Resume (`bytes={downloaded}-`):**
  - Sends HTTP Range header with the existing file offset.
  - Requires and validates `HTTP 206 Partial Content` and `Content-Range`.
  - Strictly checks remote `ETag` and `Last-Modified` validators using strong validator rules (RFC 7232/9110).
  - Appends to the `.part` file in append mode (`"ab"`).
- **Safe Fallback & Truncation:** If the remote server ignores the Range header (returning `HTTP 200 OK`) or returns `HTTP 412 / 416` or validator mismatch, the engine truncates the file (`"wb"`) and safely restarts from byte zero without corrupting the download.

### 4. Pause & Resume for Multi-Connection Downloads
- **Segment State Preservation:** Segment `.part` files inside the temporary `.parts` directory remain intact upon pause.
- **Skipping Completed Segments:** On resume, segments whose file size on disk equals `expected_size` are marked `Completed` and skipped without redundant network calls.
- **Resuming Partial Segments from Offset:** Segments with partial bytes resume from their exact byte offset:
  `Range: bytes={start + downloaded}-{end}`
- **Validation:** Validates HTTP 206, Content-Range, Content-Length, and remote ETag/Last-Modified consistency across all segments.
- **Interrupted Merge Recovery:** If an application terminates while merging segments, the original segment files in `.parts` are preserved, allowing safe re-merge upon restart.

### 5. Application Restart Recovery
- When My Download Manager starts, `recover_interrupted_downloads()` identifies any downloads left in `Downloading` (or `Retrying`) state and transitions them to `Interrupted`.
- Interrupted downloads never start automatically without user intent; the user can resume them at their convenience.
- Restores queue order and historical items into the dashboard table.

### 6. Download History Management
- Download history is persistent across application sessions.
- **Remove from History:** Removes the database record without deleting the user's downloaded file on disk.
- **Delete from Disk & History:** Explicit action that cleans up both the database record and the file on disk.

### 7. UI Controls & Status Badges
- **Toolbar Buttons:** Added **⏸ Pause Active** and **▶ Resume** buttons in the download table action bar.
- **State-Aware Context Menu:** Right-clicking any item provides actions specific to its state (`Pause`, `Resume`, `Start`, `Cancel`, `View Segments`, `Open File`, `Open Containing Folder`, `Remove from History`, `Delete from Disk & History`).
- **Visual Status Badges:**
  - `Completed`: Green (`#38c172`)
  - `Downloading`: Blue (`#4c8dff`)
  - `Retrying`: Orange (`#f6993f`)
  - `Paused`: Amber (`#f59e0b`)
  - `Interrupted`: Purple (`#a855f7`)
  - `Failed`: Red (`#e3342f`)
  - `Cancelled`: Muted Gray (`#9ca3af`)
  - `Queued`: Gold (`#e5b839`)

---

## Project Structure

```text
MyDownloadManager/
├── app.py                      # Application entry point
├── requirements.txt            # Python dependencies
├── README.md                   # Project documentation
│
├── mdm/
│   ├── __init__.py             # Package marker
│   ├── models.py               # Stage 4: DownloadModel, SegmentModel, DownloadStatus & state machine
│   ├── database.py             # Stage 4: SQLite DatabaseManager (WAL mode, migrations, recovery)
│   ├── main_window.py          # PySide6 dark UI, toolbar controls, context menus, history
│   ├── downloader.py           # Single-connection engine with HTTP Range resume & pause
│   ├── segment_manager.py      # Multi-connection engine with parallel segment resume & pause
│   ├── download_worker.py      # QThread QObject worker with paused/progress/segments signals
│   ├── queue_manager.py        # Sequential queue controller with DB persistence & recovery
│   ├── settings.py             # App settings (default connections, thresholds)
│   ├── utils.py                # Filename sanitization, format utilities, strong ETag check
│   ├── browser_host.py         # Browser host integration (future stage placeholder)
│   └── scheduler.py            # Download scheduler (future stage placeholder)
│
└── tests/
    ├── test_downloader.py              # Stage 2 regression test suite (27 tests)
    ├── test_segmented_downloader.py    # Stage 3 multi-connection test suite (33 tests)
    └── test_persistence_resume.py      # Stage 4 persistence & resume test suite (47 tests)
```

---

## How to Run on Windows PowerShell

### 1. Activate Virtual Environment
Open PowerShell in the project directory and activate the existing virtual environment:

```powershell
.venv\Scripts\Activate.ps1
```

### 2. Run the Application
```powershell
python app.py
```

---

## Running the Automated Test Suite

The automated test suite contains **107 automated tests** (Stage 2: 27 tests, Stage 3: 33 tests, Stage 4: 47 tests) executed socket-free and self-contained via `httpx.MockTransport`:

```powershell
python -m unittest discover -s tests -v
```

### Test Suite Breakdown:
- **`tests/test_downloader.py` (27 tests):** Stage 2 single-connection downloading, HTTP redirects, Content-Disposition priority, user filename overrides, responsive cancellation, graceful shutdown, shutdown timeout reference preservation, close-event rejection, transient error retries, Windows filename sanitization, and Stage 4 segmented download shutdown preserving `.parts` directory.
- **`tests/test_segmented_downloader.py` (33 tests):** Stage 3 multi-connection HTTP Range downloading, 2/4/8/16 connection concurrency, exact segment boundaries, segment merging, capability probe, automatic fallback, temporary file cleanup, and strong validator checks.
- **`tests/test_persistence_resume.py` (47 tests):**
  1. SQLite schema initialization (tables, columns, foreign keys, WAL mode).
  2. Migration user_version tracking (`PRAGMA user_version = 1`).
  3. State machine transitions (valid transitions allowed, invalid rejected).
  4. Adding download persists to DB with Queued status and queue_order.
  5. Updating download status in DB across lifecycle.
  6. Updating progress (downloaded_size, total_size, speed, eta) in DB.
  7. Updating validators (etag, last_modified, temp_path) in DB.
  8. Saving and retrieving segments (index, byte ranges, status, downloaded bytes).
  9. Segment cascade deletion when parent download is deleted.
  10. Application restart recovery: downloads left in `Downloading` become `Interrupted`.
  11. Preserving other download statuses (`Queued`, `Paused`, `Completed`, `Failed`, `Cancelled`) on restart.
  12. Restoring queue order from database.
  13. Pause single-connection download preserves partial `.part` file on disk.
  14. Resume single-connection download sends `Range: bytes={offset}-` with HTTP 206 and appends safely.
  15. Single-connection resume server returns HTTP 200: truncates file and restarts from byte 0.
  16. Single-connection resume HTTP 412 Precondition Failed: truncates and restarts safely.
  17. Single-connection resume HTTP 416 Range Not Satisfiable: handled cleanly.
  18. Single-connection resume ETag mismatch restarts fresh.
  19. Single-connection resume Last-Modified mismatch restarts fresh.
  20. Weak ETag detection according to RFC 7232/9110.
  21. Pause multi-connection download preserves `.parts` directory and segment files.
  22. Resume multi-connection download skips completed segments without network calls.
  23. Resume multi-connection download resumes partial segments from exact byte offset.
  24. Multi-connection resume validates Content-Range strictly.
  25. Multi-connection resume server returns HTTP 200: falls back cleanly.
  26. Multi-connection resume ETag mismatch restarts safely.
  27. Multi-connection resume Last-Modified mismatch restarts safely.
  28. Multi-connection resume oversized/corrupted segment reset.
  29. Interrupted merge recovery preserving segment files in `.parts`.
  30. History removal deletes database row without deleting downloaded file on disk.
  31. Explicit deletion (`delete_file=True`) removes both database row and file on disk.
  32. Concurrency and thread safety under simultaneous multi-threaded DB transactions.
  33. QueueManager `pause_active()` pauses running worker and emits `download_paused`.
  34. QueueManager `resume_download()` resumes paused download and processes queue sequentially.
  35. MainWindow loads download history from SQLite on startup and renders into dashboard table.
  36. End-to-End Single-connection restart: pause -> destroy QM -> fresh QM from DB -> resume -> byte-for-byte exact verification.
  37. End-to-End Multi-connection restart: pause mid-way -> verify `.parts` dir & segment rows -> destroy QM -> fresh QM from DB -> reuse same `.parts` dir -> skip completed segments -> byte-for-byte match -> clean `temp_path` and segment DB rows.
  38. End-to-End Application shutdown & restart: MainWindow close during download -> marks `Interrupted` & preserves partial files -> fresh MainWindow loads `Interrupted` status -> resume download -> completed.
  39. End-to-End Validator persistence & mismatch recovery: remote ETag changes while paused -> resume detects validator mismatch -> safely discards old partial file -> restarts fresh from byte 0.
  40. Single-connection strict 206 validation: invalid Content-Range closes 206 response, deletes stale partial data, issues fresh GET without Range from byte zero, and writes valid file.
  41. Database persistence callback receives expected arguments `(downloaded_bytes, speed, eta)` without exception.
  42. QueueManager persistence callback handles throttling (>= 1.0s interval), progress persistence, and thread-safe exception handling.
  43. Strict single-connection resume validation: Invalid Content-Range end (`end != total - 1`) closes 206 response, deletes stale partial data, and issues fresh GET from byte 0.
  44. Strict single-connection resume validation: Incorrect total file size (`cr_total != expected_total` or non-integer) closes 206 response, deletes stale partial data, and restarts from byte 0.
  45. Strict single-connection resume validation: Incorrect remaining Content-Length (`cl != total - existing_size`) closes 206 response, deletes stale partial data, and restarts from byte 0.
  46. Strict single-connection resume validation: Successful valid resume appends bytes correctly in `"ab"` mode and completes byte-for-byte.
  47. Strict single-connection resume validation: Malformed Content-Range where ending byte < starting byte and Content-Length is absent closes 206 response, deletes stale partial data, and restarts from byte 0.
