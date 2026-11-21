# My Download Manager — Stage 6A: Chrome & Edge Browser Integration

A modern, high-performance Windows desktop download manager built with Python 3.12/3.13, PySide6, HTTPX, and SQLite.

Stage 5 delivers advanced download control capabilities on top of the approved Stage 1–4 foundation:
- **Global & Individual Speed Limiter** using the token bucket algorithm with smooth, responsive rate control.
- **Advanced Download Scheduler** with start/stop schedules, overdue restart handling, and event-driven timer evaluation.
- **Concurrent Queue Management** supporting 1–5 simultaneous downloads (default 2), strict priority ordering (High, Normal, Low), FIFO within same priority, and full queue controls (Start All, Pause All, Resume All, Stop Queue, Clear Completed, Move Up, Move Down, Set Priority).
- **Automatic Retry Scheduling** with exponential backoff (5s, 15s, 45s), `Retry-After` header parsing, transient vs permanent error classification, and state persistence across application restarts.
- **File Categories** with extension-based automatic classification (Videos, Audio, Documents, Images, Archives, Software, Other), manual category override, and sidebar filtering.
- **SQLite Schema v2 Migration** with full backward compatibility, automatic column and index additions, and preserved download history.
- **Modernized PySide6 UI** with Settings view, Download Details dialog, Speed Limit dialog, Schedule dialog, category filters, and queue control toolbars.

**Stage 6A** adds Chrome and Microsoft Edge integration: a Manifest V3 extension with a *Download with My Download Manager* link context menu and a popup, a standard-library Native Messaging host, and an authenticated localhost IPC service that feeds the **existing** `QueueManager`. See [Browser Integration (Stage 6A)](#browser-integration-stage-6a).

The application passes **339 automated Python tests**: the 204 Stage 1–5 regression tests (Stage 2: 27, Stage 3: 33, Stage 4: 47, Stage 5: 97) plus 135 Stage 6A browser-integration tests. A further 21 extension behaviour tests run under `node --test`.

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
│   ├── browser_host.py         # Superseded placeholder (see native_host/ and browser_integration/)
│   └── browser_integration/    # Stage 6A application-side integration
│       ├── __init__.py
│       ├── ipc_protocol.py     # Length-prefixed JSON protocol, endpoint file, blocking client (stdlib only)
│       ├── request_validator.py# URL / filename / metadata validation (stdlib only, shared with host)
│       ├── ipc_server.py       # QTcpServer on 127.0.0.1, token auth, dedupe, timeouts (Qt main thread)
│       └── service.py          # Bridges validated requests to QueueManager.add_download()
│
├── browser_extension/          # Chrome + Edge Manifest V3 extension (load unpacked)
│   ├── manifest.json
│   ├── service_worker.js       # Context menu, native messaging, notifications, pending retries
│   ├── popup.html / popup.js   # Status, add URL, info, actions
│   ├── styles.css
│   ├── instructions.html/.js   # In-extension setup guide (shows the extension ID)
│   ├── lib/validation.js       # Shared URL validation and status mapping
│   └── icons/                  # 16/32/48/128 px PNG icons
│
├── native_host/                # Native Messaging host: com.mydownloadmanager.native
│   ├── host.py                 # Stdlib-only host (framing, validation, IPC forwarding)
│   ├── manifest.template.json
│   ├── requirements.txt        # Build-only: PyInstaller
│   ├── build_host.ps1          # Builds + verifies MyDownloadManagerNativeHost.exe
│   ├── install_native_host.ps1 # Per-user (HKCU) registration for Chrome/Edge
│   ├── uninstall_native_host.ps1
│   ├── verify_host.py          # End-to-end check of host.py or the built .exe (mock app)
│   └── README.md
│
├── tools/
│   └── generate_extension_icons.py
│
└── tests/
    ├── test_downloader.py              # Stage 2 regression test suite (27 tests)
    ├── test_segmented_downloader.py    # Stage 3 multi-connection test suite (33 tests)
    ├── test_persistence_resume.py      # Stage 4 persistence & resume test suite (47 tests)
    ├── test_stage5_advanced.py         # Stage 5 advanced download management test suite (97 tests)
    ├── test_stage6a_native_messaging.py# Stage 6A framing, host handling, validation (41 tests)
    ├── test_stage6a_ipc_integration.py # Stage 6A IPC security + QueueManager/UI integration (37 tests)
    ├── test_stage6a_extension.py       # Stage 6A MV3 manifest, extension behaviour, host exe (20 tests)
    ├── test_stage6a_hardening.py       # Stage 6A bounded IPC memory, durable idempotency, schema v3 (37 tests)
    └── js/extension_tests.mjs          # Node.js harness with a mocked chrome.* API
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

The automated test suite contains **339 automated Python tests** plus 21 Node.js extension tests. Stage 1–5 tests are socket-free via `httpx.MockTransport`; Stage 6A tests use loopback sockets and mock IPC services only (no public websites):

```powershell
python -m compileall -q .
python -m unittest discover -s tests -v
node --test tests/js/extension_tests.mjs
```

Stage 6A extension behaviour tests require Node.js 18+ on `PATH` (skipped otherwise). The built-executable test runs only after `native_host\build_host.ps1` has produced `native_host\dist\MyDownloadManagerNativeHost.exe` (skipped otherwise).

### Test Suite Breakdown (339 Python tests total):

- **Stage 6A (135 tests):** `test_stage6a_native_messaging.py` (41), `test_stage6a_ipc_integration.py` (37), `test_stage6a_extension.py` (20), `test_stage6a_hardening.py` (37) — see [Browser Integration › Automated tests](#automated-tests).


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

---

## Browser Integration (Stage 6A)

Stage 6A lets you send HTTP/HTTPS links from **Google Chrome** and **Microsoft Edge** to My Download Manager. One Manifest V3 extension works in both browsers.

> **Scope:** Stage 6A covers **manual capture only**: the right-click menu and the popup's URL box. The browser's own downloads are **not** intercepted. Automatic capture is planned for Stage 6B, with explicit user consent.

### Architecture

```text
Chrome / Edge extension  (browser_extension/, Manifest V3)
   │  chrome.runtime.sendNativeMessage("com.mydownloadmanager.native", {...})
   ▼
Chromium Native Messaging  (stdin/stdout, uint32 little-endian length + UTF-8 JSON, ≤ 1 MB)
   ▼
MyDownloadManagerNativeHost.exe  (native_host/host.py, Python standard library only)
   │  validates the request, reads %LOCALAPPDATA%\MyDownloadManager\browser_integration\endpoint.json
   ▼
Authenticated local IPC  (TCP 127.0.0.1:<random port>, per-launch random token, length-prefixed JSON)
   ▼
My Download Manager  (mdm/browser_integration/ipc_server.py — QTcpServer on the Qt main thread)
   │  re-validates, de-duplicates by request_id, dispatches via a queued Qt signal
   ▼
Existing QueueManager.add_download()  → existing SQLite persistence → existing download engines
```

Browser downloads take the same path as the **Add URL** dialog. They get the same filename detection and `sanitize_filename`, the default connection count, priority, category, speed limits, retry policy and SQLite persistence. They are saved to the same default folder (`%USERPROFILE%\Downloads`) and show up in the existing download table. No second engine or queue was added. The only schema change is one additive table for request idempotency (schema v3, see below).

### Prerequisites

* Windows 10/11, Python 3.13 with the project `.venv` (`pip install -r requirements.txt`)
* Google Chrome 116+ and/or Microsoft Edge 116+
* For building the host only: PyInstaller (installed automatically by `build_host.ps1` from `native_host\requirements.txt`)

### 1. Build the native host

From the project root in PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File .\native_host\build_host.ps1
```

This produces `native_host\dist\MyDownloadManagerNativeHost.exe` (about 7.5 MB, no PySide6). It then verifies the executable automatically. The check frames a request, parses it, forwards it over authenticated IPC to a mock application, confirms the framed response and a clean exit, and confirms that duplicate and invalid requests are handled. To re-run only the verification:

```powershell
.venv\Scripts\python native_host\verify_host.py native_host\dist\MyDownloadManagerNativeHost.exe
```

### 2. Chrome setup

1. Open `chrome://extensions`.
2. Turn on **Developer mode** (top right).
3. Click **Load unpacked** and select the `browser_extension` folder.
4. Copy the extension **ID** (32 letters, e.g. `abcdefghijklmnopabcdefghijklmnop`).
5. Register the native host:
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\native_host\install_native_host.ps1 -ChromeExtensionId <CHROME_ID>
   ```
6. Close **every** Chrome window and start Chrome again.

### 3. Edge setup

1. Open `edge://extensions`.
2. Turn on **Developer mode** (left panel).
3. Click **Load unpacked** and select the same `browser_extension` folder.
4. Copy the extension **ID** (it differs from the Chrome ID).
5. Register the native host:
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\native_host\install_native_host.ps1 -EdgeExtensionId <EDGE_ID>
   ```
6. Close **every** Edge window and start Edge again.

You can register both browsers in one command: `-ChromeExtensionId <CHROME_ID> -EdgeExtensionId <EDGE_ID>`. Running the installer again keeps the IDs that are already registered.

### Native host registration details

`install_native_host.ps1` installs per user and needs no administrator rights. It:

* validates that the host `.exe` exists and that each extension ID matches `^[a-p]{32}$` (no ID is hard-coded)
* copies the host to `%LOCALAPPDATA%\MyDownloadManager\NativeHost\`
* writes `com.mydownloadmanager.native.json` there, from `manifest.template.json`
* sets the **default value** of each selected key to that manifest path:
  * `HKCU\Software\Google\Chrome\NativeMessagingHosts\com.mydownloadmanager.native`
  * `HKCU\Software\Microsoft\Edge\NativeMessagingHosts\com.mydownloadmanager.native`
* prints a result table and exits non-zero with a clear message on any error

Optional parameters:

| Parameter | Purpose |
|---|---|
| `-HostPath <exe>` | Use a host executable other than `native_host\dist\MyDownloadManagerNativeHost.exe` |
| `-InstallDir <dir>` | Install somewhere other than `%LOCALAPPDATA%\MyDownloadManager\NativeHost` |
| `-AppPath <exe> -AppArguments <args>` | Lets the popup's **Open My Download Manager** button start the app when it is not running. Example: `-AppPath "$PWD\.venv\Scripts\pythonw.exe" -AppArguments "$PWD\app.py"`. The arguments are fixed at install time; the browser can never supply them. |

When the app is already running, **Open My Download Manager** always brings its window to the front, with or without `-AppPath`.

### 4. Use it

1. Start My Download Manager (`python app.py`). The top bar shows **● Browser integration ready**.
2. Right-click a normal HTTP/HTTPS download link and choose **Download with My Download Manager**.
3. A browser notification confirms the result. The download appears in the application's table and starts according to the existing queue rules. The top bar briefly shows *Added from Google Chrome / Microsoft Edge: &lt;file&gt;*.
4. Or open the toolbar popup, paste a URL into **Add download** and click **Add Download**.

The popup shows:
* **connection status:** *Connected*, *Application not running*, *Native host unavailable* or *Connection error*, with a **Retry** button
* the **Add download** box
* **application information:** name, browser, connection, extension and app version
* **Open My Download Manager** and **Integration instructions** (an in-extension page that shows this extension's ID and the exact install command)

If a request can't be delivered (for example, the app is closed), the popup keeps it under **Not delivered yet** with **Retry** and **Dismiss**, so nothing is silently discarded. A retry reuses the original `request_id`, so it can never add the download twice.

### Uninstallation

```powershell
# Remove Chrome and Edge registrations and the installed host files
powershell -ExecutionPolicy Bypass -File .\native_host\uninstall_native_host.ps1

# Only one browser / keep the files
powershell -ExecutionPolicy Bypass -File .\native_host\uninstall_native_host.ps1 -Browser Chrome
powershell -ExecutionPolicy Bypass -File .\native_host\uninstall_native_host.ps1 -KeepFiles
```

The uninstaller removes a registry key only if it points to a `com.mydownloadmanager.native` manifest. It deletes only the host `.exe`, the manifest and `host_config.json`. It never touches downloads, the download database, settings or logs. To remove the extension itself, click **Remove** on `chrome://extensions` / `edge://extensions`.

### Windows testing guide

Run each case in Chrome and then in Edge.

| # | Scenario | Steps | Expected result |
|---|---|---|---|
| 1 | Normal link | App running → right-click an HTTP/HTTPS file link → *Download with My Download Manager* | "Download added" notification; item appears in the table and downloads |
| 2 | Browser opened before app | Start browser first → popup shows *Application not running* → start app → click **Retry** | Status becomes *Connected* |
| 3 | App opened before browser | Start app, then browser → open popup | *Connected* immediately |
| 4 | App closed during request | Close the app, then send a link | Notification: *My Download Manager is not running…*; request listed under **Not delivered yet**; start app → **Retry** adds it once |
| 5 | Native host unavailable | Run `uninstall_native_host.ps1`, restart browser, open popup | *Native host unavailable* with the install instruction; re-install to recover |
| 6 | Invalid URL | Popup: enter `javascript:alert(1)`, `file:///C:/x`, `ftp://x`, `https://user:pass@host/x` | Clear inline error; nothing reaches the app |
| 7 | Duplicate request | Send a link, close the app before the reply arrives, start it again, **Retry** | Exactly one item in the table (de-duplicated by `request_id`) |
| 8 | Large URL with query | Send a long signed URL (several KB of `?a=1&b=%20…`) | Accepted; URL is stored byte-for-byte; filename comes from the path |
| 9 | Shutdown during communication | Send a link and close the app at the same moment | The extension reports *closed the connection* or *not running*; the app exits normally; no orphan endpoint file remains |
| 10 | Open app from popup | With the app minimised, click **Open My Download Manager** | The window comes to the front |

### Troubleshooting

| Symptom | Fix |
|---|---|
| Popup: *Native host unavailable – not installed* | Run `install_native_host.ps1` with this browser's extension ID, then fully restart the browser. |
| Popup: *…this extension ID is not allowed* | The extension was reloaded from another folder (new ID). Re-run the installer with the new ID from `chrome://extensions` / `edge://extensions`. |
| Popup: *Application not running* | Start My Download Manager. Check the top bar for *Browser integration ready*. |
| Top bar: *Browser integration unavailable* | Another running instance already owns the integration. Close the other instance. |
| *Connection error / host stopped unexpectedly* | Check that `%LOCALAPPDATA%\MyDownloadManager\NativeHost\MyDownloadManagerNativeHost.exe` exists. Rebuild and re-install if needed. |
| Download fails with 401/403 | The site needs your browser login. Cookies are deliberately not shared (see Known limitations). |
| Logs | Host: `%LOCALAPPDATA%\MyDownloadManager\logs\native_host.log` · App: `%LOCALAPPDATA%\MyDownloadManager\logs\browser_integration.log` |
| Registry check | `Get-Item "HKCU:\Software\Google\Chrome\NativeMessagingHosts\com.mydownloadmanager.native"` (or `...\Microsoft\Edge\...`) |

### Security model

* **Minimal extension permissions:** `contextMenus`, `nativeMessaging`, `storage`, plus `notifications` for the required result notification (the badge is the fallback if it is unavailable). The extension has no host permissions, no content scripts, no `tabs`, `cookies`, `downloads` or `webRequest`, no remote code and no `eval`.
* **Strict input validation at every hop** (extension → host → application, the same rules re-checked each time):
  * only `http:` and `https:` are accepted; `javascript:`, `data:`, `blob:`, `file:`, `chrome:`, `edge:` and every other scheme are rejected
  * URLs with embedded `user:password@` are rejected
  * URLs are capped at 32 KB; control characters and whitespace are rejected
  * filenames must be bare names (no `\`, `/`, `:` or `..`) and are then passed through the existing `sanitize_filename`
  * referrer and page title are informational, type-checked and length-limited; unknown fields are rejected
* **The host executes nothing it receives.** It never uses `eval`/`exec` and never uses browser input as a path or command. *Open My Download Manager* only launches the executable set at install time, without a shell.
* **Native Messaging framing:** 4-byte little-endian length plus UTF-8 JSON, a 1 MB cap in both directions, and partial reads handled. An oversized frame is rejected without reading the body. Responses are flushed after every message, and nothing else is ever written to stdout (logs go to a rotating file).
* **Local IPC:**
  * binds to `127.0.0.1` only, on a random port, never `0.0.0.0`; non-loopback peers are dropped
  * each app launch generates a `secrets.token_urlsafe(32)` token, published in `%LOCALAPPDATA%\MyDownloadManager\browser_integration\endpoint.json`; that folder is readable only by the current user (plus SYSTEM and Administrators)
  * every request is authenticated with `hmac.compare_digest`; the protocol has a version and a strict envelope schema
  * the timestamp window is ±5 min; requests are limited to 1 MB, one request per connection, 10 s per connection and 16 concurrent connections
  * **bounded memory per connection:**
    * each socket's Qt read buffer is capped at 64 KB (`QTcpSocket.setReadBufferSize`); when it is full, Qt stops reading from the OS and TCP flow control pushes back on the sender
    * data is *pulled* only in the amounts the current frame still needs (`BoundedFrameReader`), and the declared length is checked as soon as the 4-byte header arrives, so an oversized or zero-length frame is rejected **before any body byte is read**
    * worst case is `4 B + 1 MB + 64 KB` per connection, about 17 MB across 16 connections
    * fragmented frames (down to one byte per packet) and valid frames of exactly 1 MB still work; incomplete headers or bodies are closed by the 10 s timeout
  * **durable at-most-once processing:** a `request_id` is accepted at most once, even across application restarts, using the SQLite `browser_requests` table (see [Database schema v3](#database-schema-v3-browser-request-idempotency)). An in-memory cache of recent successful responses (15 min), keyed by `request_id` plus a payload digest, answers exact repeats without touching the database.
  * only the `ping`, `add_download` and `show_window` actions exist
* **Thread safety:** the IPC server is event-driven on the Qt main thread, and the handler asserts it is running there. `QueueManager.add_download()` is reached only through a queued signal. No socket threads are created and the GUI is never blocked.
* **Lifecycle:** the endpoint file is written atomically on startup. A stale file is detected and replaced; one owned by another live instance is never hijacked. On close, the app stops accepting connections, aborts open sockets and deletes the endpoint file before the existing download shutdown runs. If that shutdown is vetoed, integration resumes.
* **Logging:** the IPC authentication token is never logged. URLs are logged as `scheme://host/…` only. This applies to the extension (which does not log at all), the native host, the browser-integration service and the download engine.

#### What is forwarded: cookies and saved logins vs. signed URLs

There are two kinds of "token", and they are handled differently:

| | Read by the extension? | Sent to the app? | Stored? |
|---|---|---|---|
| Browser cookies, saved passwords, session storage, `Authorization` headers | **No.** The extension has no permission to read them. | **No** | **No** |
| Query parameters **inside the link URL**, such as a signed CDN or cloud-storage link (`?X-Amz-Signature=…&token=…`) | Only as part of the URL that was right-clicked or pasted | **Yes, unchanged.** The download can't work without them, so they are never stripped. | See below |

Treat a signed URL like a short-lived password: anyone holding it can usually download the file until it expires. My Download Manager therefore:

* forwards the URL byte-for-byte (query string and percent-encoding preserved) and never logs it in full
* stores the full URL **only** in the existing `downloads` table, because the download engine needs it to start, resume and retry, exactly as for a URL typed into the **Add URL** dialog
* keeps only a SHA-256 fingerprint of the URL in the `browser_requests` idempotency table, never the URL itself
* keeps undelivered requests (full URL included) in the extension's private `chrome.storage.local`, so they can be retried from the popup. At most 20 are kept, nothing is synced to the cloud, and each one is deleted on successful delivery or when you click **Dismiss**.

Links whose access depends on your **browser session** (cookies) rather than on a signed URL are not supported in Stage 6A; see Known limitations.

### Database schema v3: browser request idempotency

Stage 6A raises `CURRENT_SCHEMA_VERSION` from 2 to **3**. The migration runs automatically the first time the new version opens an existing database. It is **purely additive**: existing `downloads`, `segments` and `settings` rows are not modified. A Stage 5 database (`user_version = 2`) gains one table:

```sql
CREATE TABLE browser_requests (
    request_id  TEXT PRIMARY KEY,  -- extension-generated UUID; reused on retry
    download_id TEXT,              -- id of the download it created
    browser     TEXT NOT NULL,     -- 'chrome' | 'edge'
    url_sha256  TEXT NOT NULL,     -- fingerprint only, never the URL
    status      TEXT NOT NULL,     -- 'accepted'
    created_at  TEXT NOT NULL      -- ISO-8601, used by the retention policy
);
CREATE INDEX idx_browser_requests_created ON browser_requests(created_at);
```

* **Atomic:** `QueueManager.add_download(..., browser_request=...)` passes the record to `DatabaseManager.add_download`, which inserts the download row **and** the idempotency row in **one SQLite transaction**. A download can never exist without its record, or the reverse. If the transaction fails, both are rolled back, nothing is added to the in-memory queue, the browser gets `storage_error`, and a retry with the same `request_id` can succeed.
* **Concurrency:** the `request_id` primary key is the final arbiter. If two identical requests race, one commits; the other gets `IntegrityError`, its download row is rolled back, and it is answered with the winner's download ID (`status: "duplicate"`).
* **Duplicates:** the same `request_id` with the same URL returns the original download ID, even after a restart. The same `request_id` with a **different** URL is rejected with `request_id_conflict` and is never retried by the extension.
* **Fail-safe:** if the record can't be read (database error), the request is refused with `storage_error` rather than risking a duplicate.
* **Retention:** on each start, records older than **30 days** are deleted and at most the **10,000** newest are kept (`BROWSER_REQUEST_RETENTION_DAYS`, `BROWSER_REQUEST_MAX_ROWS` in `mdm/database.py`). Download rows are never deleted by this policy. There is no foreign key, so removing a download from the list does not erase its idempotency record, and a late retry still can't add it again.

### Known limitations

* **No automatic capture:** Stage 6A doesn't intercept the browser's own downloads. That is Stage 6B.
* **No browser session:** cookies and auth are not forwarded, so links that need a logged-in session (and some signed or one-time links) may fail with 401/403 or download an HTML login page. This is by design.
* **Metadata isn't persisted:** referrer and page title are kept on the in-memory queue item (shown in **Details**) and are not saved to SQLite. Only the source browser is stored durably, in `browser_requests`. The referrer isn't sent as an HTTP `Referer` header.
* **Idempotency window:** duplicate detection lasts for the 30-day retention window. A request ID retried after its record has been purged would be treated as new; in practice the extension keeps at most 20 pending requests, and only until they are delivered or dismissed.
* **Page title needs extra permission:** it comes from the clicked tab only when the browser exposes it without the `tabs` permission, which is usually *not* the case. It is therefore often empty.
* **Fixed save folder:** browser downloads go to the default folder (`%USERPROFILE%\Downloads`) with default queue settings. There is no per-request folder prompt.
* **Unpacked extension IDs change:** loading from a different folder changes the ID, so re-run the installer with the new ID.
* **One owning instance:** only one running instance owns the integration endpoint.
* **Open app needs configuration:** *Open My Download Manager* can only **start** the app if `-AppPath` was given at install time. Otherwise it only focuses a running instance.
* **Stale endpoint delay:** after a crash, the first request can take about 2 s to report *Application not running*, because Windows retries refused localhost connects.

### Automated tests

| File | Coverage |
|---|---|
| `test_stage6a_native_messaging.py` (41) | Valid framing and little-endian prefix; one-byte partial reads; multiple messages per process; zero, truncated and oversized frames (body never read); invalid JSON (recoverable); unsupported actions; rejected schemes; embedded credentials; malformed metadata and path-like filenames; app not running; stale or corrupt endpoint; IPC timeout; wrong token; open-app safety; no `eval`/`exec`/`print`; validator and protocol units |
| `test_stage6a_ipc_integration.py` (37) | Valid, missing and wrong token; constant-time compare; schema, version, stale-timestamp and action rejection; malformed and oversized frames; fragmented requests; sequential and concurrent duplicate `request_id`s; failed requests not cached; handler on main thread; socket timeout; connection limit; localhost-only binding; endpoint contents and token rotation; token never logged; shutdown removes endpoint and closes port; shutdown during an in-flight request; stale-endpoint replacement; no hijacking of a live instance; **real QueueManager**: valid URL queued and persisted, invalid rejected, app-side re-validation, existing filename sanitisation, queue settings preserved, no duplicates, metadata preserved, UI-thread safety, queue rejection, ping/show window, app unavailable; **MainWindow**: item appears in table, status indicator, close cleans up, vetoed close resumes integration |
| `test_stage6a_extension.py` (20) | MV3 validity, minimal permissions, referenced files and icon sizes, CSP-safe pages, no `eval`/remote code, link-only context menu, no download interception, host manifest template, HKCU-only scripts with ID validation; Node-driven behaviour tests (`tests/js/extension_tests.mjs`): popup URL validation, context-menu creation and click, host failure handling, acknowledgements, retry with the same `request_id`, Chrome and Edge detection, sender checks, `request_id_conflict` not retried, signed URL forwarded intact; host end-to-end from source and from the built `.exe`. The same Node cases run standalone with `node --test tests/js/extension_tests.mjs` (21 tests); a failed assertion exits non-zero. |
| `test_stage6a_hardening.py` (37) | **Bounded IPC memory:** `BoundedFrameReader` (1-byte fragments, header-time oversize rejection, zero length, never over-reads, exact 1 MB); socket read buffer capped at 64 KB; valid 1 MB message; oversized header rejected promptly without a body; oversized header in fragments; 3 MB stream after a 1 MB header never buffered; incomplete large body and incomplete header time out; zero-length frame; 10 simultaneous oversized connections; 4 concurrent 3 MB streams stay within the per-connection bound; valid 1-byte-fragmented message; trailing bytes ignored. **Durable idempotency:** first request recorded (fingerprint only); signed URL kept intact in the queue but never stored in `browser_requests` or logged; duplicate returns the original ID through SQLite; duplicate after restart; same ID with a different URL (also after restart); concurrent duplicates at the database, service and socket level; transaction failure rolls back the download and a retry succeeds; integrity failure rollback; lookup failure refuses the request; retention by age and row cap; retention on start; download history preserved; in-memory fallback. **Migration:** genuine Stage 5 (v2) database with history, segments and settings migrates to v3 unchanged; fresh database is v3. **Docs:** cookie vs. signed-URL distinction present. |
