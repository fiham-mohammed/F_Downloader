# My Download Manager — Stage 3: Multi-Connection Download Engine

A modern Windows desktop application built with Python 3.13, PySide6, and HTTPX.

Stage 3 introduces an advanced **multi-connection download engine** inspired by Internet Download Manager (IDM), accelerating downloads using simultaneous HTTP Range requests with automatic single-connection fallback, independent per-segment retries, exact byte-ordered merging, and real-time segment monitoring.

The application has passed all **59 automated regression tests** with 100% success.

---

## What Works in Stage 3

### 1. Advanced Multi-Connection Downloading (HTTP Range)
- **Configurable Connections:** Select from 1, 2, 4, 8, or 16 simultaneous HTTP connections per download (default: 8).
- **Server Capability Detection:** Before segmenting, sends a probe Range request (`bytes=0-0`) to verify:
  - Server returns `HTTP 206 Partial Content`.
  - Validates `Content-Range` header and total file size.
  - Detects `ETag` and `Last-Modified` validators for consistency.
- **Graceful Single-Connection Fallback:** Automatically and seamlessly switches to the reliable single-connection engine when:
  - The server does not support Range requests or returns `HTTP 200 OK`.
  - The total file size is unknown or chunked.
  - The file size is below the minimum threshold (2 MB).
  - The server returns invalid range responses or changes content validators.
- **Exact Segment Boundary Calculation:** Divides files into exact, contiguous byte ranges with zero gaps and zero overlaps, handling uneven file sizes cleanly.
- **Low Memory Streaming:** Each segment streams directly to disk in chunks (64 KB). Entire files are never loaded into RAM.
- **Independent Segment Retries:** Temporary network issues or transient server errors (HTTP 500, 502, 503, 504) are retried independently up to 3 times per segment without corrupting or re-downloading completed segments.
- **Atomic Segment Merging:** Verifies all segment file sizes before sequentially merging them into a temporary file, validates the final merged file size, and applies atomic no-overwrite naming (`safe_finalize_file`).
- **Clean File Lifecycle:** Incomplete segment files and working directories are automatically purged upon cancellation, permanent failure, or application closure.

### 2. Real-Time UI & Segment Details Dialog
- **Segment Details Dialog:** Double-click any active/downloading item or choose **📊 View Segments…** from the right-click menu to open the real-time Segment Details Dialog:
  - Live per-segment progress bars and percentage completed.
  - Exact byte ranges per segment (e.g. `0 B – 25.00 MB`).
  - Downloaded vs. expected byte count per segment.
  - Real-time status for each segment (`Queued`, `Downloading`, `Completed`, `Retrying`).
  - Active connections counter (e.g., `8 / 8 max`).
- **Live Progress & Throttling:** Overall progress is computed strictly from actual downloaded bytes. Signal emissions are throttled to ~10 Hz to keep the Qt GUI buttery smooth without freezing.
- **Settings Page Connection Configuration:** Configure the global default connection count (1, 2, 4, 8, or 16) directly in the Settings tab.

### 3. All Stage 2 Core Features Preserved
- **Sequential Queue Management:** Sequential download processing with auto-start of next queued item.
- **Graceful Application Shutdown:** Active downloads are safely cancelled, worker threads exit cleanly, and references are preserved if a worker outlasts the timeout.
- **Windows Filename Sanitization:** Reserved device names (including compound names like `CON.foo.bar`) and illegal characters (`< > : " / \ | ? *`) are safely handled.
- **Smart Filename Detection Priority:**
  1. `Content-Disposition` filename from the final HTTP response.
  2. Explicit user-provided filename override (if supplied).
  3. Filename extracted from the final redirected response URL.
  4. Fallback filename.

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
│   ├── main_window.py          # PySide6 dark UI, segment dialog & settings page
│   ├── downloader.py           # Single-connection streaming downloader & retry engine
│   ├── download_worker.py      # QThread QObject worker with throttled signals
│   ├── segment_manager.py      # Stage 3: Multi-connection engine & Range coordinator
│   ├── queue_manager.py        # Sequential queue controller & metrics coordinator
│   ├── settings.py             # App settings (default connections, thresholds)
│   ├── utils.py                # Filename sanitization, formatting & path resolution
│   ├── browser_host.py         # Browser host integration (future stage placeholder)
│   ├── database.py             # SQLite persistence (future stage placeholder)
│   └── scheduler.py            # Download scheduler (future stage placeholder)
│
└── tests/
    ├── test_downloader.py              # Stage 2 regression test suite (26 tests)
    └── test_segmented_downloader.py    # Stage 3 multi-connection test suite (33 tests)
```

---

## How to Select Connection Counts

1. **For an Individual Download:**
   - Click **+ Add URL** in the top bar.
   - In the **Connections** dropdown, choose between **1, 2, 4, 8, or 16 connections** (default: 8).
   - Enter your URL and save folder, then click **Add to queue**.
2. **Global Default for Future Downloads:**
   - Navigate to the **Settings** tab in the sidebar.
   - Under **Multi-Connection Download Settings**, select your desired default (1, 2, 4, 8, or 16).
   - The preference is saved and applied automatically to new downloads.

---

## Run on Windows PowerShell

### 1. Activate Virtual Environment
Open PowerShell in the project directory and activate the existing virtual environment:

```powershell
.venv\Scripts\Activate.ps1
```

### 2. Install Requirements (if needed)
```powershell
python -m pip install -r requirements.txt
```

### 3. Run the Application
```powershell
python app.py
```

---

## Running the Automated Test Suite

The test suite contains **59 automated tests** executed self-contained and socket-free via `httpx.MockTransport`:

```powershell
python -m unittest discover -s tests -v
```

### Coverage Breakdown:
- **`tests/test_downloader.py` (26 tests):** Small/large file downloads, HTTP redirects, Content-Disposition priority, user explicit overrides, cancellation, graceful shutdown, shutdown timeout reference preservation (QueueManager), close-event rejection and retry dialog (MainWindow), disk permission and disk-full errors, permanent 404s, transient 503 retries, dashboard retry metrics, Windows filename sanitization (`CON.foo.bar`), collision-safe finalisation under race conditions, format utilities, and Qt UI integration.
- **`tests/test_segmented_downloader.py` (33 tests):**
  1. Server supporting HTTP Range requests.
  2. Server rejecting HTTP Range requests (fallback).
  3. Server returning HTTP 200 instead of HTTP 206 (fallback).
  4. Two-connection downloading.
  5. Four-connection downloading.
  6. Eight-connection downloading.
  7. Sixteen-connection configuration.
  8. Correct segment boundary calculations.
  9. Uneven file-size segmentation.
  10. Segment merging.
  11. Incorrect Content-Range responses.
  12. Incomplete segment responses.
  13. Segment network failure and retry.
  14. Cancellation during parallel downloading (verified active workers, prompt cleanup).
  15. Unknown file-size fallback.
  16. Small-file fallback (< 2 MB).
  17. ETag or Last-Modified validation.
  18. Duplicate destination filename protection.
  19. Final file size verification.
  20. Real QThread GUI responsiveness and signal delivery during parallel downloading.
  21. Cleanup of temporary segment files and directories.
  22. Single-connection fallback after Range rejection.
  23. Concurrency test: 2 connections (tracking simultaneous requests).
  24. Concurrency test: 4 connections (verified parallel overlap without exceeding limit).
  25. Concurrency test: 8 connections (verified parallel overlap without exceeding limit).
  26. Concurrency test: 16 connections (verified parallel overlap without exceeding limit).
  27. RFC 7232/9110 validator helper: Strong vs. Weak ETag discrimination.
  28. Validator consistency: Weak ETags omitted from If-Range in favor of Last-Modified.
  29. Server validator mutation mid-download triggers clean single-connection fallback.
  30. Strict Content-Range probe validation failure triggers graceful fallback.
  31. Segment Content-Length header mismatch fails safely and purges temporary files.
  32. Monotonic wire-streamed speed calculation during segment retry cycles.
  33. Immediate sibling stream abort and thread unblocking on fatal segment errors.

---

## Known Scope Boundaries (Stage 3)

The following capabilities are deliberately reserved for subsequent development stages:
- **Pause & Resume Across Restarts:** Persisting partial segment state to disk for resuming after app restarts will be introduced with database persistence in Stage 4.
- **SQLite Database Persistence:** History and queue persistence will be implemented in Stage 4.
- **Bandwidth Scheduling:** Speed limits and scheduling rules will be introduced in Stage 6.
- **Browser Extension Integration:** Native messaging host for Chrome/Edge integration will be implemented in Stage 7.
