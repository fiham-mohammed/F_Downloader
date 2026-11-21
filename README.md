# My Download Manager — Stage 5: Advanced Download Management

A modern, high-performance Windows desktop download manager built with Python 3.12/3.13, PySide6, HTTPX, and SQLite.

Stage 5 delivers advanced download control capabilities on top of the approved Stage 1–4 foundation:
- **Global & Individual Speed Limiter** using the token bucket algorithm with smooth, responsive rate control.
- **Advanced Download Scheduler** with start/stop schedules, overdue restart handling, and event-driven timer evaluation.
- **Concurrent Queue Management** supporting 1–5 simultaneous downloads (default 2), strict priority ordering (High, Normal, Low), FIFO within same priority, and full queue controls (Start All, Pause All, Resume All, Stop Queue, Clear Completed, Move Up, Move Down, Set Priority).
- **Automatic Retry Scheduling** with exponential backoff (5s, 15s, 45s), `Retry-After` header parsing, transient vs permanent error classification, and state persistence across application restarts.
- **File Categories** with extension-based automatic classification (Videos, Audio, Documents, Images, Archives, Software, Other), manual category override, and sidebar filtering.
- **SQLite Schema v2 Migration** with full backward compatibility, automatic column and index additions, and preserved download history.
- **Modernized PySide6 UI** with Settings view, Download Details dialog, Speed Limit dialog, Schedule dialog, category filters, and queue control toolbars.

The application passes all **204 automated regression tests** (Stage 2: 27 tests, Stage 3: 33 tests, Stage 4: 47 tests, Stage 5: 97 tests) with 100% success.

---

## What's New in Stage 5

### 1. Global & Individual Download Speed Limiter
- **Token Bucket Algorithm:** Implemented in `mdm/bandwidth_controller.py` with smooth micro-slice token consumption and zero CPU spinning.
- **Global & Per-Download Limits:** Supports independent global limits and per-download individual overrides. When both are active, the more restrictive limit takes precedence.
- **Parallel Segment Sharing:** Multiple worker threads for segmented downloads draw collectively from the download's individual token bucket.
- **Dynamic Updates:** Speed limits can be updated on the fly without interrupting or pausing active downloads.
- **Generation Tracking & Exactly-Once Reservation Accounting:** Bandwidth controller uses generational tracking and atomic reconciliation for outstanding global token reservations. When the global limiter is disabled or re-enabled while waiting on an individual bucket, reservations are reconciled exactly once—forgiving accumulated debt without phantom token inflation and preventing double-charging or token leakage across dynamic limiter transitions.
- **Cancellation & Pause Responsiveness:** Slices sleeps into small (~50ms) intervals and checks cancellation/pause flags, ensuring immediate reaction during rate-limiting pauses.

### 2. Advanced Download Scheduler
- **Event-Driven Periodic Checks:** Implemented in `mdm/scheduler.py` using `QTimer` periodic checks. Avoids thread-per-download overhead and uses weak references to avoid memory cycles.
- **Scheduled Start:** Queued downloads configured with a future start time wait in the queue until the target time arrives.
- **Scheduled Stop:** Active downloads reaching a configured stop time are automatically and safely paused.
- **Overdue Handling:** If a scheduled start time elapsed while the application was closed, the download is detected and executed upon application restart.
- **Timezone Awareness:** Supports ISO-8601 timestamps and compares them accurately against local and UTC datetimes.

### 3. Advanced Concurrent Queue Management
- **Configurable Concurrency:** Configurable from 1 to 5 simultaneous downloads (default 2), managed by `QueueManager`.
- **Collision Protection:** Automatically serializes downloads targeting the same destination file to prevent partial file collisions.
- **Priority Dispatching:** Downloads are prioritized by `High` > `Normal` > `Low`. Within the same priority level, items are dispatched FIFO based on `queue_order`.
- **Queue Controls:**
  - **Start All / Resume All:** Activates eligible queued or paused items up to concurrency limit.
  - **Pause All / Stop Queue:** Safely pauses running downloads and halts auto-dispatching.
  - **Clear Completed:** Cleans up completed downloads from history and table.
  - **Move Up / Move Down:** Reorders items in the queue with persistent `queue_order`.
  - **Set Priority:** Changes item priority dynamically and immediately re-evaluates the queue.

### 4. Two-Layer Automatic Retry Architecture
- **Layer A (Internal Worker Retries):** Fast, short network and segment retries within the running `DownloadWorker` (configured for up to 3 fast attempts). Keeps the worker thread active without re-dispatching from the queue. Slices retry backoff into 50ms intervals for responsive cancellation and pause. If a server returns any retryable status code (HTTP 408, 429, 500, 502, 503, 504) with a significant `Retry-After` delay (>3 seconds), the worker does not block the thread; it captures the header, stops internal retries immediately, and raises a structured `DownloadError(status_code=..., retry_after=...)` to promote the failure immediately to Layer B.
- **Layer B (QueueManager Whole-Download Retries):** Whole-download queue-level scheduling managed by `QueueManager` with persistence in SQLite (`retry_count`, `next_retry_at`). Applies exponential backoff (5s, 15s, 45s) or honors server `Retry-After` headers (both delta seconds and RFC 7231 HTTP-date). Concurrency slots are immediately released for other queue downloads, and `DownloadScheduler` autonomously dispatches the download when `next_retry_at` arrives (even across application restarts).
- **Error Classification:** Distinguishes retryable transient failures (HTTP 408, 429, 500, 502, 503, 504, connection timeouts) from non-retryable permanent errors (HTTP 400, 401, 403, 404, disk full, permission denied, invalid URLs).
- **Safe Cancellation:** Cancelled downloads permanently clear `next_retry_at` and are never scheduled or retried.
- **Download Details Inspection:** Accurate retry status, current retry attempt out of maximum, and next scheduled retry timestamp are clearly displayed in the Download Details dialog.
- **Manual Actions:** Users can trigger immediate manual retry or cancel pending retries at any time.

### 5. Download Categories
- **Extension-Based Auto-Detection:** Automatically categorizes downloads into:
  - **Videos:** `.mp4`, `.mkv`, `.avi`, `.mov`, `.wmv`, `.flv`, `.webm`, `.m4v`, `.mpg`, `.mpeg`
  - **Audio:** `.mp3`, `.wav`, `.flac`, `.aac`, `.ogg`, `.wma`, `.m4a`, `.opus`
  - **Documents:** `.pdf`, `.doc`, `.docx`, `.xls`, `.xlsx`, `.ppt`, `.pptx`, `.txt`, `.epub`, `.csv`
  - **Images:** `.jpg`, `.jpeg`, `.png`, `.gif`, `.bmp`, `.webp`, `.svg`, `.ico`, `.tiff`
  - **Archives:** `.zip`, `.rar`, `.7z`, `.tar`, `.gz`, `.bz2`, `.xz`
  - **Software:** `.exe`, `.msi`, `.dmg`, `.pkg`, `.deb`, `.rpm`, `.apk`, `.appimage`, `.iso`
  - **Other:** Default fallback for unrecognized file types.
- **Manual Category Override:** Users can manually reassign categories via the context menu or Download Details dialog.
- **Sidebar Filters:** Filter the dashboard table by category (`All Downloads`, `Videos`, `Audio`, `Documents`, `Images`, `Archives`, `Software`, `Other`).

### 6. SQLite Schema v2 Migration
- Automatically migrates existing version 1 databases to version 2 (`PRAGMA user_version = 2`).
- Adds columns to `downloads`: `priority`, `category`, `scheduled_start_at`, `scheduled_stop_at`, `schedule_enabled`, `individual_speed_limit`, `retry_count`, `max_retry_attempts`, `next_retry_at`, `retry_enabled`.
- Adds performance indexes: `idx_downloads_priority`, `idx_downloads_category`, `idx_downloads_schedule`.
- Retains 100% of historical download records, segment states, and settings without data loss.

### 7. Modernized PySide6 User Interface
- **Sidebar Categories:** Quick-filtering by All, Videos, Audio, Documents, Images, Archives, Software, and Other.
- **Queue Action Toolbar:** Convenient buttons for Add Download, Start All, Pause All, Resume All, Stop Queue, Clear Completed, Move Up, and Move Down.
- **Table Columns:** 10 comprehensive columns (`FILE NAME`, `STATUS`, `SIZE`, `PROGRESS`, `SPEED`, `ETA`, `PRIORITY`, `CATEGORY`, `SCHEDULE`, `ADDED`).
- **Context Menu:** Actions for Set Priority, Change Category, Speed Limit, Schedule, Details, Retry, Cancel, Open File, Open Folder, and Remove/Delete.
- **Dialogs & Settings:** Dedicated Speed Limit dialog, Schedule Download dialog, Download Details dialog, and a full Settings page for queue concurrency, default priority, global speed limits, and auto-retry policies.

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
│   ├── models.py               # DownloadModel, SegmentModel, DownloadPriority, DownloadStatus
│   ├── database.py             # SQLite DatabaseManager (schema v2, WAL mode, migrations)
│   ├── bandwidth_controller.py # TokenBucket & BandwidthController rate limiting
│   ├── scheduler.py            # DownloadScheduler with event-driven QTimer evaluation
│   ├── categories.py           # Extension categorization and mapping utilities
│   ├── main_window.py          # PySide6 desktop interface, settings, dialogs, queue toolbars
│   ├── downloader.py           # Single-connection engine with bandwidth throttling & resume
│   ├── segment_manager.py      # Multi-connection engine with shared bandwidth & segment resume
│   ├── download_worker.py      # QThread QObject worker managing download execution & signals
│   ├── queue_manager.py        # Multi-worker queue manager (1-5 concurrency, priorities, retries)
│   ├── settings.py             # Configuration settings with database persistence
│   ├── utils.py                # Filename sanitization, formatting, ETag validators
│   └── browser_host.py         # Browser host integration (future stage placeholder)
│
└── tests/
    ├── test_downloader.py              # Stage 2 regression test suite (27 tests)
    ├── test_segmented_downloader.py    # Stage 3 multi-connection test suite (33 tests)
    ├── test_persistence_resume.py      # Stage 4 persistence & resume test suite (47 tests)
    └── test_stage5_advanced.py         # Stage 5 advanced download management test suite (97 tests)
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

The automated test suite contains **204 automated tests** executed socket-free and self-contained via `httpx.MockTransport`:

```powershell
python -m unittest discover -s tests -v
```

### Test Suite Breakdown (204 tests total):

- **`tests/test_downloader.py` (27 tests):**
  Stage 2 single-connection downloading, HTTP redirects, Content-Disposition priority, user filename overrides, responsive cancellation, graceful shutdown, shutdown timeout reference preservation, close-event rejection, transient error retries, Windows filename sanitization, and Stage 4 segmented download shutdown preserving `.parts` directory.

- **`tests/test_segmented_downloader.py` (33 tests):**
  Stage 3 multi-connection HTTP Range downloading, 2/4/8/16 connection concurrency, exact segment boundaries, segment merging, capability probe, automatic fallback, temporary file cleanup, and strong validator checks.

- **`tests/test_persistence_resume.py` (47 tests):**
  Stage 4 SQLite schema initialization (WAL mode, foreign keys, cascades), migration tracking, state machine validation, download and segment persistence, application restart recovery (interrupted downloads), queue order restoration, single-connection and multi-connection pause and resume, strict HTTP 206 validation, weak ETag handling, and history deletion.

- **`tests/test_stage5_advanced.py` (97 tests):**
  1. Global speed limiter accuracy.
  2. Individual speed limiter accuracy.
  3. Combined global and individual speed limits (most restrictive wins).
  4. Multi-segment bandwidth sharing from individual token bucket.
  5. Multi-download bandwidth sharing from global pool.
  6. Dynamic speed limit adjustments on active downloads.
  7. Unlimited speed mode (zero-overhead pass-through).
  8. Speed limiter cancellation responsiveness during rate-limit pauses.
  9. Speed limiter pause responsiveness during rate-limit pauses.
  10. Speed limiter UI chunk-size adaptation for responsiveness.
  11. Scheduled start skips download until future timestamp.
  12. Scheduled start dispatches immediately if timestamp is due/overdue.
  13. Overdue scheduled downloads start automatically upon application restart.
  14. Scheduled stop safely pauses active download when time expires.
  15. Schedule metadata persists across application restarts.
  16. Invalid or unparseable schedule timestamp handling.
  17. Scheduler avoids duplicate triggers on active downloads.
  18. Scheduled downloads respect simultaneous download limits.
  19. Queue respects simultaneous download limit of 1.
  20. Queue respects default simultaneous download limit of 2.
  21. Queue respects simultaneous download limit of 4.
  22. Priority ordering: High priority downloads before Normal, and Normal before Low.
  23. FIFO dispatching for downloads with identical priority based on queue order.
  24. Dynamic priority change immediately updates queue dispatch ordering.
  25. Manual queue reordering via Move Up and Move Down.
  26. Destination file collision avoidance: prevents concurrent writes to same destination.
  27. Start All and Pause All queue controls.
  28. Resume All and Stop Queue queue controls.
  29. Duplicate worker instance prevention.
  30. Error classification: transient network failures qualify for retry.
  31. Error classification: HTTP 503 triggers retry scheduling.
  32. Error classification: HTTP 429 parses and honors `Retry-After` header.
  33. Error classification: HTTP 404 permanent failure fails immediately without retry.
  34. Error classification: Disk full / permission errors fail immediately without retry.
  35. Maximum retry attempt enforcement: fails permanently when limit is exhausted.
  36. Exponential backoff timing verification (5s, 15s, 45s).
  37. Retry count and next retry timestamp persist in database across app restarts.
  38. Manual retry clears retry timestamp and queues immediately.
  39. Cancel pending retry stops scheduling and marks item Failed.
  40. Auto-categorization: Videos by file extension.
  41. Auto-categorization: Audio by file extension.
  42. Auto-categorization: Documents by file extension.
  43. Auto-categorization: Images by file extension.
  44. Auto-categorization: Archives by file extension.
  45. Auto-categorization: Software by file extension.
  46. Auto-categorization: Unrecognized extensions mapped to Other.
  47. Manual category override takes precedence over auto-detection.
  48. Download category persists in database across app restarts.
  49. Schema v1 to v2 migration adds all Stage 5 columns and indexes.
  50. Existing v1 download records remain intact during v2 migration.
  51. Interrupted download recovery preserves Stage 5 priority, category, and speed limit.
  52. Stage 5 settings persist in the database settings table.
  53. Queue order and priority helper updates persist directly in SQLite database.
  54. Automatic retry dispatch when next_retry_at becomes due.
  55. Retry dispatch autonomous execution after application restart.
  56. Cancelled downloads are never scheduled or retried.
  57. Strict error classification: permanent errors and unknown errors fail permanently.
  58. Retry-After header parsing for delta seconds and HTTP-date formats.
  59. Proportional burst token bucket capacity and condition variable dynamic wake.
  60. Default individual speed limit application, override, and unlimited mode.
  61. Add Download dialog priority and category data passing.
  62. Production settings persistence via DatabaseManager get_setting and set_setting.
  63. Automatic category recalculation on server Content-Disposition filename with manual preservation.
  64. Scheduler filter in MainWindow and strict schedule validation (stop > start).
  65. Dynamic queue concurrency slot filling without duplicate workers.
  66. Scheduler timezone normalisation: naive local Windows time, aware UTC, and invalid timestamp rejection.
  67. HTTP 429 Retry-After header delta seconds propagation and immediate slot release.
  68. HTTP 429 Retry-After RFC 7231 HTTP-date parsing and persistent retry timestamp.
  69. HTTP 408 Request Timeout transient failure classification and exponential backoff retry.
  70. Segmented download HTTP 429 Retry-After propagation through worker to queue scheduler.
  71. HTTP 503 and malformed Retry-After header fallback to exponential backoff.
  72. Scheduled stop safely pauses active download during internal Retrying worker state.
  73. Bandwidth controller token reservation lifecycle, cancellation, pause, and dynamic limit changes.
  74. Layer A internal worker retries vs Layer B whole-download queue retries distinction and cancellation safety.
  75. HTTP 503 with long Retry-After (>3s) promotes to QueueManager and frees concurrency slot immediately.
  76. HTTP 500 with Retry-After promotes to QueueManager without internal delay.
  77. HTTP 502 with RFC 7231 HTTP-date calculates accurate delay and persists next_retry_at.
  78. HTTP 504 and HTTP 408 with Retry-After headers promote to queue-level retry.
  79. Segmented download HTTP 503 with Retry-After on segment request aborts siblings and promotes to queue retry.
  80. Short Retry-After (<=3s) retried internally; missing Retry-After uses exponential backoff; resets metadata per attempt.
  81. Global limiter generation control: disabling and immediate re-enabling forces re-reservation without bandwidth bypass.
  82. Global limiter dynamic rate increases wake waiting workers promptly.
  83. Removing individual limit while global limit remains active continues enforcing global rate limit.
  84. Generation control cancellation/pause token refunds and multi-worker global token sharing.
  85. Short Retry-After handling (0.5s, 1s, 2s, 2.5s, 3s) retried internally via shared `INTERNAL_RETRY_AFTER_MAX_SECONDS = 3.0`, >3s (3.1s) promoted to queue retry, missing and invalid headers fallback across single and segmented downloads.
  86. Concurrent segments thread safety: Segment B network timeout does not erase Segment A's HTTP 503 Retry-After metadata.
  87. Concurrent segments fatal error arbitration: Selected fatal exception retains its own response metadata without cross-segment overwriting regardless of arrival order.
  88. Segment attempt clears stale Retry-After metadata: Subsequent network errors do not reuse stale HTTP response headers.
  89. Segmented engine preserves status_code and retry_after for all retryable status codes (HTTP 429, 500, 502, 503, 504).
  90. QueueManager receives fatal metadata from concurrent segments and persists correct next_retry_at into SQLite database.
  91. Positive global token balance with individual waiting: global disable and immediate re-enable does not double charge tokens.
  92. Negative global token balance with disable/re-enable: debt is reset cleanly without phantom token inflation or unearned refund.
  93. Multiple concurrent workers with outstanding global reservations: reconciled accurately upon limiter enable/disable without over-crediting or lost tokens.
  94. Cancellation after reservation invalidation: cleanly reconciles invalidated reservation without double refunding.
  95. Pause after reservation invalidation: cleanly reconciles invalidated reservation without double refunding.
  96. Individual limit removal while global limiting remains active: preserves outstanding global reservation and continuous global enforcement.
  97. Repeated enable/disable cycles: reconciles reservations exactly once per cycle without token leakage or negative balance drift.
