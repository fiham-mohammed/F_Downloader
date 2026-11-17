import os
import sys
import time
import errno
import shutil
import tempfile
import threading
from pathlib import Path
import unittest
from unittest.mock import patch, mock_open
import httpx

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mdm.utils import (
    sanitize_filename,
    get_unique_filepath,
    safe_finalize_file,
    parse_content_disposition,
    extract_filename_from_url,
    format_bytes,
    format_speed,
    format_eta,
)
from mdm.downloader import (
    HttpDownloader,
    DownloadError,
    DownloadCancelledError,
)
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtWidgets import QApplication
from mdm.queue_manager import QueueManager


class MockStream(httpx.SyncByteStream):
    """Custom sync byte stream for simulating chunked streaming with optional delay."""
    def __init__(self, chunks, delay=0.0):
        self.chunks = chunks
        self.delay = delay

    def __iter__(self):
        for chunk in self.chunks:
            if self.delay > 0:
                time.sleep(self.delay)
            yield chunk


class TestDownloaderEngine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Ensure QApplication exists for Qt signals, widgets and threading tests
        cls.qt_app = QApplication.instance()
        if not cls.qt_app:
            cls.qt_app = QApplication(sys.argv)

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="mdm_test_")
        self.retry_counts = {}

    def tearDown(self):
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def create_mock_transport(self, custom_handler=None):
        """Creates an in-memory MockTransport for deterministic, socket-free testing."""
        def default_handler(request: httpx.Request):
            if custom_handler:
                res = custom_handler(request)
                if res is not None:
                    return res

            path = request.url.path

            if path == "/small.txt":
                content = b"Hello from My Download Manager Stage 2 engine!"
                return httpx.Response(
                    200,
                    headers={"Content-Type": "text/plain; charset=utf-8", "Content-Length": str(len(content))},
                    content=content,
                )

            elif path == "/large.bin":
                chunk = b"X" * 65536
                chunks = [chunk for _ in range(8)]
                total_size = len(chunk) * 8
                return httpx.Response(
                    200,
                    headers={"Content-Type": "application/octet-stream", "Content-Length": str(total_size)},
                    stream=MockStream(chunks),
                )

            elif path == "/disposition":
                content = b"Content-Disposition test payload."
                return httpx.Response(
                    200,
                    headers={
                        "Content-Type": "application/octet-stream",
                        "Content-Length": str(len(content)),
                        "Content-Disposition": 'attachment; filename="custom_server_name.pdf"',
                    },
                    content=content,
                )

            elif path == "/chunked-unknown":
                # Omits Content-Length
                chunks = [f"Chunk {i}\n".encode("utf-8") for i in range(5)]
                return httpx.Response(
                    200,
                    headers={"Content-Type": "application/octet-stream"},
                    stream=MockStream(chunks),
                )

            elif path == "/slow.bin":
                # Slow stream to allow deterministic cancellation
                chunks = [b"S" * 1024 for _ in range(50)]
                return httpx.Response(
                    200,
                    headers={"Content-Type": "application/octet-stream", "Content-Length": str(1024 * 50)},
                    stream=MockStream(chunks, delay=0.06),
                )

            elif path == "/flaky-503":
                count = self.retry_counts.get("503", 0) + 1
                self.retry_counts["503"] = count
                if count < 3:
                    return httpx.Response(503, headers={"Content-Type": "text/plain"}, content=b"Temporarily Unavailable")
                else:
                    content = b"Success after retries!"
                    return httpx.Response(
                        200,
                        headers={"Content-Type": "text/plain", "Content-Length": str(len(content))},
                        content=content,
                    )

            elif path == "/not-found":
                return httpx.Response(404, headers={"Content-Type": "text/plain"}, content=b"Not Found")

            elif path == "/redirect-to-small":
                return httpx.Response(
                    302,
                    headers={"Location": "http://mock.server/small.txt"},
                )

            return httpx.Response(404, content=b"Not Found")

        return httpx.MockTransport(default_handler)

    # 1. Test downloading a small file
    def test_01_small_file_download(self):
        transport = self.create_mock_transport()
        downloader = HttpDownloader("http://mock.server/small.txt", self.temp_dir, transport=transport)
        final_path, total_bytes = downloader.download()

        self.assertTrue(final_path.exists())
        self.assertEqual(final_path.name, "small.txt")
        self.assertEqual(total_bytes, len(b"Hello from My Download Manager Stage 2 engine!"))
        with open(final_path, "rb") as f:
            self.assertEqual(f.read(), b"Hello from My Download Manager Stage 2 engine!")

    # 2. Test downloading a larger file
    def test_02_large_file_download(self):
        transport = self.create_mock_transport()
        downloader = HttpDownloader("http://mock.server/large.bin", self.temp_dir, transport=transport)
        final_path, total_bytes = downloader.download()

        expected_size = 65536 * 8  # 512 KB
        self.assertTrue(final_path.exists())
        self.assertEqual(final_path.stat().st_size, expected_size)
        self.assertEqual(total_bytes, expected_size)

    # 3. Test HTTP redirect handling
    def test_03_http_redirect(self):
        transport = self.create_mock_transport()
        downloader = HttpDownloader("http://mock.server/redirect-to-small", self.temp_dir, transport=transport)
        final_path, total_bytes = downloader.download()

        self.assertTrue(final_path.exists())
        with open(final_path, "rb") as f:
            self.assertEqual(f.read(), b"Hello from My Download Manager Stage 2 engine!")

    # 4. Test Content-Disposition header filename extraction
    def test_04_content_disposition_filename(self):
        transport = self.create_mock_transport()
        downloader = HttpDownloader("http://mock.server/disposition", self.temp_dir, transport=transport)
        final_path, _ = downloader.download()

        self.assertTrue(final_path.exists())
        self.assertEqual(final_path.name, "custom_server_name.pdf")

    # 5. Test unknown file size (streamed without Content-Length)
    def test_05_unknown_file_size(self):
        transport = self.create_mock_transport()
        downloader = HttpDownloader(
            "http://mock.server/chunked-unknown",
            self.temp_dir,
            explicit_filename="streamed.txt",
            transport=transport,
        )
        final_path, total_bytes = downloader.download()

        self.assertTrue(final_path.exists())
        self.assertGreater(total_bytes, 0)
        with open(final_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        self.assertEqual(len(lines), 5)
        self.assertEqual(lines[0], "Chunk 0\n")

    # 6. Test duplicate filenames (safe alternative allocation)
    def test_06_duplicate_filenames(self):
        transport = self.create_mock_transport()
        url = "http://mock.server/small.txt"

        d1 = HttpDownloader(url, self.temp_dir, transport=transport)
        path1, _ = d1.download()

        d2 = HttpDownloader(url, self.temp_dir, transport=transport)
        path2, _ = d2.download()

        d3 = HttpDownloader(url, self.temp_dir, transport=transport)
        path3, _ = d3.download()

        self.assertTrue(path1.exists())
        self.assertTrue(path2.exists())
        self.assertTrue(path3.exists())
        self.assertEqual(path1.name, "small.txt")
        self.assertEqual(path2.name, "small (1).txt")
        self.assertEqual(path3.name, "small (2).txt")

    # 7. Test cancelling an active download
    def test_07_cancel_active_download(self):
        transport = self.create_mock_transport()
        url = "http://mock.server/slow.bin"
        is_cancelled = False

        def cancel_check():
            return is_cancelled

        downloader = HttpDownloader(url, self.temp_dir, is_cancelled=cancel_check, transport=transport)

        def trigger_cancel():
            time.sleep(0.08)
            nonlocal is_cancelled
            is_cancelled = True
            downloader.cancel()

        cancel_thread = threading.Thread(target=trigger_cancel)
        cancel_thread.start()

        with self.assertRaises(DownloadCancelledError):
            downloader.download()

        cancel_thread.join()

        # Confirm .part file is deleted on cancellation
        part_files = list(Path(self.temp_dir).glob("*.part"))
        self.assertEqual(len(part_files), 0, "Temporary .part file must be cleaned up on cancellation.")

    # 8. Test invalid URL scheme
    def test_08_invalid_url_scheme(self):
        downloader = HttpDownloader("ftp://example.com/file.iso", self.temp_dir)
        with self.assertRaises(DownloadError) as ctx:
            downloader.download()
        self.assertIn("Invalid URL scheme", str(ctx.exception))

    # 9. Test unavailable server (connection failure)
    def test_09_unavailable_server(self):
        def failing_handler(request: httpx.Request):
            raise httpx.ConnectError("Connection refused")

        transport = httpx.MockTransport(failing_handler)
        downloader = HttpDownloader(
            "http://mock.server/unavailable",
            self.temp_dir,
            max_retries=2,
            transport=transport,
        )
        with self.assertRaises(DownloadError):
            downloader.download()

    # 10. Test permanent 404 error (must fail immediately without retry loop)
    def test_10_permanent_404_error(self):
        transport = self.create_mock_transport()
        url = "http://mock.server/not-found"
        downloader = HttpDownloader(url, self.temp_dir, max_retries=3, transport=transport)
        with self.assertRaises(DownloadError) as ctx:
            downloader.download()
        self.assertIn("HTTP 404", str(ctx.exception))

    # 11. Test automatic retry on transient 503 error
    def test_11_automatic_retry_503(self):
        transport = self.create_mock_transport()
        url = "http://mock.server/flaky-503"
        retry_events = []

        def on_retry(attempt, max_attempts, msg):
            retry_events.append((attempt, max_attempts))

        downloader = HttpDownloader(url, self.temp_dir, max_retries=3, on_retry=on_retry, transport=transport)
        final_path, _ = downloader.download()

        self.assertTrue(final_path.exists())
        with open(final_path, "rb") as f:
            self.assertEqual(f.read(), b"Success after retries!")
        self.assertEqual(len(retry_events), 2, "Should have retried twice before succeeding on attempt 3.")

    # 12. Test Windows filename sanitization
    def test_12_windows_filename_sanitization(self):
        self.assertEqual(sanitize_filename("CON.txt"), "_CON.txt")
        self.assertEqual(sanitize_filename("AUX.pdf"), "_AUX.pdf")
        self.assertEqual(sanitize_filename("NUL.zip"), "_NUL.zip")
        self.assertEqual(sanitize_filename("COM1.bin"), "_COM1.bin")
        self.assertEqual(sanitize_filename("test:file*name?.zip"), "test_file_name_.zip")
        self.assertEqual(sanitize_filename("trailing.dots..."), "trailing.dots")
        self.assertEqual(sanitize_filename("   "), "download")

    # 13. Test sequential queue execution with QueueManager
    def test_13_queue_manager_sequential_downloads(self):
        transport = self.create_mock_transport()
        mgr = QueueManager()

        item1 = mgr.add_download("http://mock.server/small.txt", self.temp_dir, auto_start=False, transport=transport)
        item2 = mgr.add_download("http://mock.server/small.txt", self.temp_dir, auto_start=False, transport=transport)

        completed_ids = []
        mgr.download_completed.connect(lambda item_id, path, size: completed_ids.append(item_id))

        # Start queue
        mgr.start_all()

        # Run Qt event loop until both items complete or timeout
        start_wait = time.time()
        while len(completed_ids) < 2 and time.time() - start_wait < 10.0:
            self.qt_app.processEvents()
            time.sleep(0.05)

        self.assertEqual(len(completed_ids), 2)
        self.assertEqual(item1["status"], "Completed")
        self.assertEqual(item2["status"], "Completed")
        self.assertNotEqual(item1["final_path"], item2["final_path"], "Duplicate items should have distinct file paths.")
        self.assertTrue(Path(item1["final_path"]).exists())
        self.assertTrue(Path(item2["final_path"]).exists())

    # 14. Test failed downloads are never marked Completed
    def test_14_failed_download_not_marked_completed(self):
        def failing_handler(request: httpx.Request):
            return httpx.Response(404, content=b"Not Found")

        transport = httpx.MockTransport(failing_handler)
        mgr = QueueManager()

        failed_ids = []
        mgr.download_failed.connect(lambda item_id, err: failed_ids.append(item_id))

        item = mgr.add_download("http://mock.server/missing.zip", self.temp_dir, auto_start=True, transport=transport)

        start_wait = time.time()
        while not failed_ids and time.time() - start_wait < 5.0:
            self.qt_app.processEvents()
            time.sleep(0.05)

        self.assertIn(item["id"], failed_ids)
        self.assertEqual(item["status"], "Failed")
        self.assertNotEqual(item["status"], "Completed")
        self.assertIn("HTTP 404", item["error"])
        # Confirm no .part or final file exists
        self.assertEqual(len(list(Path(self.temp_dir).glob("*"))), 0)

    # 15. Test UI integration with MainWindow
    def test_15_main_window_integration(self):
        from mdm.main_window import MainWindow
        win = MainWindow()

        transport = self.create_mock_transport()
        item = win.queue_mgr.add_download("http://mock.server/small.txt", self.temp_dir, auto_start=False, transport=transport)

        win.refresh_table()
        self.assertEqual(win.table.rowCount(), 1)
        self.assertEqual(win.table.item(0, 0).text(), "small.txt")
        self.assertEqual(win.table.item(0, 1).text(), "Queued")

        # Start download
        win.queue_mgr.start_all()

        completed = []
        win.queue_mgr.download_completed.connect(lambda i_id, p, s: completed.append(i_id))

        start_wait = time.time()
        while not completed and time.time() - start_wait < 5.0:
            self.qt_app.processEvents()
            time.sleep(0.05)

        self.assertEqual(len(completed), 1)
        self.assertEqual(win.metric_completed.value_label.text(), "1")
        self.assertEqual(win.metric_active.value_label.text(), "0")
        self.assertEqual(win.metric_queued.value_label.text(), "0")
        win.close()

    # 16. Regression Test: Closing the application during a slow download
    def test_16_shutdown_during_slow_download(self):
        from mdm.main_window import MainWindow
        win = MainWindow()
        transport = self.create_mock_transport()

        item = win.queue_mgr.add_download("http://mock.server/slow.bin", self.temp_dir, auto_start=True, transport=transport)

        # Wait until download is actively in progress
        start_wait = time.time()
        while item["status"] != "Downloading" and time.time() - start_wait < 3.0:
            self.qt_app.processEvents()
            time.sleep(0.02)

        self.assertEqual(item["status"], "Downloading")
        self.assertTrue(win.queue_mgr.is_busy())

        # Trigger graceful shutdown / close
        win.close()

        # Confirm thread has cleanly exited and not destroyed while running
        self.assertFalse(win.queue_mgr.is_busy())
        self.assertEqual(item["status"], "Cancelled")

        # Confirm incomplete .part files are cleaned up
        part_files = list(Path(self.temp_dir).glob("*.part"))
        self.assertEqual(len(part_files), 0, "No .part files should remain after graceful shutdown.")

    # 17. Regression Test: Disk permission error (must fail immediately without retrying)
    def test_17_disk_permission_error_no_retry(self):
        retry_events = []
        def on_retry(att, max_att, msg):
            retry_events.append((att, max_att))

        transport = self.create_mock_transport()
        downloader = HttpDownloader(
            "http://mock.server/small.txt",
            self.temp_dir,
            transport=transport,
            on_retry=on_retry,
        )

        with patch("builtins.open", side_effect=PermissionError("Access is denied")):
            with self.assertRaises(DownloadError) as ctx:
                downloader.download()

        self.assertIn("Disk error", str(ctx.exception))
        self.assertEqual(len(retry_events), 0, "Disk permission errors must not be retried!")

    # 18. Regression Test: Disk write failure / disk full (must fail immediately without retrying)
    def test_18_disk_full_error_no_retry(self):
        retry_events = []
        def on_retry(att, max_att, msg):
            retry_events.append((att, max_att))

        transport = self.create_mock_transport()
        downloader = HttpDownloader(
            "http://mock.server/large.bin",
            self.temp_dir,
            transport=transport,
            on_retry=on_retry,
        )

        m_open = mock_open()
        m_open.return_value.write.side_effect = OSError(errno.ENOSPC, "No space left on device")
        with patch("builtins.open", m_open):
            with self.assertRaises(DownloadError) as ctx:
                downloader.download()

        self.assertIn("Disk error", str(ctx.exception))
        self.assertEqual(len(retry_events), 0, "Disk full errors must not be retried!")

    # 19. Regression Test: Redirected filename detection and priority
    def test_19_redirected_filename_detection(self):
        def redirect_handler(request: httpx.Request):
            if request.url.path == "/download-latest":
                return httpx.Response(302, headers={"Location": "http://mock.server/releases/release-v2.0.4.zip"})
            elif request.url.path == "/releases/release-v2.0.4.zip":
                return httpx.Response(200, headers={"Content-Length": "12"}, content=b"release-data")
            elif request.url.path == "/redirect-with-cd":
                return httpx.Response(302, headers={"Location": "http://mock.server/actual/path.bin"})
            elif request.url.path == "/actual/path.bin":
                return httpx.Response(
                    200,
                    headers={
                        "Content-Length": "7",
                        "Content-Disposition": 'attachment; filename="cd_priority_file.zip"',
                    },
                    content=b"cd-data",
                )
            return None

        transport = self.create_mock_transport(redirect_handler)

        # Priority 3 vs 4: Redirected URL takes priority over initial URL filename
        d1 = HttpDownloader("http://mock.server/download-latest", self.temp_dir, transport=transport)
        path1, _ = d1.download()
        self.assertEqual(path1.name, "release-v2.0.4.zip")

        # Priority 2: Explicit user override takes priority over redirected URL filename
        d2 = HttpDownloader(
            "http://mock.server/download-latest",
            self.temp_dir,
            explicit_filename="my_override_name.zip",
            transport=transport,
        )
        path2, _ = d2.download()
        self.assertEqual(path2.name, "my_override_name.zip")

        # Priority 1: Content-Disposition from final response takes top priority
        d3 = HttpDownloader("http://mock.server/redirect-with-cd", self.temp_dir, transport=transport)
        path3, _ = d3.download()
        self.assertEqual(path3.name, "cd_priority_file.zip")

    # 20. Regression Test: Dashboard statistics during retries
    def test_20_dashboard_statistics_during_retries(self):
        transport = self.create_mock_transport()
        mgr = QueueManager()

        snapshots = []
        mgr.metrics_changed.connect(lambda t, a, c, q: snapshots.append((t, a, c, q)))

        item = mgr.add_download("http://mock.server/flaky-503", self.temp_dir, auto_start=True, transport=transport)

        # Wait until download finishes 3rd attempt
        start = time.time()
        while item["status"] != "Completed" and time.time() - start < 10.0:
            self.qt_app.processEvents()
            time.sleep(0.04)

        self.assertEqual(item["status"], "Completed")

        # Verify that during retrying, active count was 1 and queued count was 0
        retrying_snapshots = [s for s in snapshots if s[1] == 1 and s[3] == 0]
        self.assertGreater(len(retrying_snapshots), 0, "Retrying items must be counted as active and not queued!")

        # Final metrics
        last = snapshots[-1]
        self.assertEqual(last[0], 1)  # Total: 1
        self.assertEqual(last[1], 0)  # Active: 0
        self.assertEqual(last[2], 1)  # Completed: 1
        self.assertEqual(last[3], 0)  # Queued: 0

    # 21. Regression Test: Destination filename collision safe finalisation (no overwrite)
    def test_21_filename_collision_safe_finalisation(self):
        # Create an existing target file
        existing_file = Path(self.temp_dir) / "document.pdf"
        existing_file.write_text("existing original content")

        part_file = Path(self.temp_dir) / "document.pdf.part"
        part_file.write_text("new downloaded content")

        final_path = safe_finalize_file(part_file, self.temp_dir, "document.pdf")

        # Must not overwrite existing_file
        self.assertEqual(final_path.name, "document (1).pdf")
        self.assertEqual(existing_file.read_text(), "existing original content")
        self.assertEqual(final_path.read_text(), "new downloaded content")

    # 22. Regression Test Scenario A: QueueManager shutdown timeout preserves references
    def test_22_queue_manager_shutdown_timeout(self):
        """
        Scenario A: When the worker thread outlasts the shutdown timeout:
        - QueueManager.shutdown() returns False.
        - active_thread and active_worker references are preserved.
        - The running thread is not destroyed.
        - Once the worker terminates, a subsequent shutdown succeeds.
        """
        # Slow transport: stream chunks with delay
        def slow_handler(request: httpx.Request):
            chunks = [b"Z" * 1024 for _ in range(100)]
            return httpx.Response(
                200,
                headers={"Content-Type": "application/octet-stream",
                         "Content-Length": str(1024 * 100)},
                stream=MockStream(chunks, delay=0.1),
            )

        transport = httpx.MockTransport(slow_handler)
        mgr = QueueManager()

        item = mgr.add_download(
            "http://mock.server/very-slow.bin",
            self.temp_dir, auto_start=True, transport=transport,
        )

        # Wait until download is actively in progress
        start_wait = time.time()
        while item["status"] != "Downloading" and time.time() - start_wait < 3.0:
            self.qt_app.processEvents()
            time.sleep(0.01)

        self.assertEqual(item["status"], "Downloading")
        self.assertTrue(mgr.is_busy())

        # Simulate a worker that takes longer than the shutdown timeout to stop
        real_cancel = mgr.active_worker.cancel
        def slow_cancel():
            def delayed():
                time.sleep(0.35)
                real_cancel()
            t = threading.Thread(target=delayed, daemon=True)
            t.start()

        mgr.active_worker.cancel = slow_cancel

        # Attempt shutdown with a short timeout (100ms).
        # Since cancel is delayed by 350ms, the worker is still running at 100ms.
        result = mgr.shutdown(timeout_ms=100)
        self.assertFalse(result, "Shutdown must return False when thread hasn't stopped within timeout")

        # References must be preserved
        self.assertIsNotNone(mgr.active_thread)
        self.assertIsNotNone(mgr.active_worker)
        self.assertTrue(mgr.active_thread.isRunning(), "Running QThread must not be destroyed while active")

        # Wait for delayed stop to finish and the worker thread to terminate
        start_wait = time.time()
        while mgr.active_thread and mgr.active_thread.isRunning() and time.time() - start_wait < 5.0:
            self.qt_app.processEvents()
            time.sleep(0.05)

        # Subsequent shutdown succeeds cleanly once worker terminates
        result2 = mgr.shutdown(timeout_ms=2000)
        self.assertTrue(result2, "Shutdown must succeed after worker terminates")
        self.assertIsNone(mgr.active_thread)
        self.assertIsNone(mgr.active_worker)
        self.assertFalse(mgr.is_busy())

    # 23. Regression Test Scenario B: MainWindow close-event rejection on failed shutdown
    def test_23_main_window_close_event_rejection(self):
        """
        Scenario B: When QueueManager.shutdown() fails (returns False):
        - MainWindow.closeEvent() rejects the close event.
        - The main window is re-enabled.
        - The shutdown warning message dialog is displayed.
        - A subsequent successful shutdown allows closure.
        """
        from mdm.main_window import MainWindow
        from PySide6.QtGui import QCloseEvent

        win = MainWindow()

        # 1. Simulate failed shutdown when busy
        with patch.object(win.queue_mgr, "is_busy", return_value=True), \
             patch.object(win.queue_mgr, "shutdown", return_value=False), \
             patch("PySide6.QtWidgets.QMessageBox.warning") as mock_warn:

            event1 = QCloseEvent()
            win.closeEvent(event1)

            # Close event must be rejected
            self.assertFalse(event1.isAccepted(), "closeEvent must reject close when shutdown returns False")
            # Main window must be re-enabled
            self.assertTrue(win.isEnabled(), "Main window must be re-enabled after failed shutdown")
            # Shutdown warning dialog must be displayed
            mock_warn.assert_called_once()
            self.assertIn("Shutdown Timeout", mock_warn.call_args[0][1])

        # 2. Verify that a subsequent successful shutdown allows closure
        with patch.object(win.queue_mgr, "is_busy", return_value=True), \
             patch.object(win.queue_mgr, "shutdown", return_value=True):

            event2 = QCloseEvent()
            win.closeEvent(event2)

            # Close event must be accepted
            self.assertTrue(event2.isAccepted(), "closeEvent must accept close when shutdown succeeds")

    # 24. Regression Test: Windows reserved names with multiple extensions
    def test_24_reserved_names_with_multiple_extensions(self):
        """
        Filenames like CON.foo.bar must be sanitised because Windows treats
        the part before the FIRST dot as the device name.
        """
        # Reserved names with multiple extensions — must be prefixed
        self.assertTrue(sanitize_filename("CON.foo.bar").startswith("_"))
        self.assertTrue(sanitize_filename("AUX.backup.tar").startswith("_"))
        self.assertTrue(sanitize_filename("NUL.backup.zip").startswith("_"))
        self.assertTrue(sanitize_filename("COM1.application.exe").startswith("_"))
        self.assertTrue(sanitize_filename("LPT1.document.pdf").startswith("_"))

        # Reserved names with single extension — still prefixed
        self.assertTrue(sanitize_filename("CON.txt").startswith("_"))
        self.assertTrue(sanitize_filename("AUX.pdf").startswith("_"))

        # Ordinary filenames that BEGIN with a reserved prefix but are NOT
        # reserved themselves — must NOT be prefixed
        self.assertFalse(sanitize_filename("CONFIGURE.txt").startswith("_"))
        self.assertFalse(sanitize_filename("console.log").startswith("_"))
        self.assertFalse(sanitize_filename("auxiliary.pdf").startswith("_"))
        self.assertFalse(sanitize_filename("connected.txt").startswith("_"))
        self.assertFalse(sanitize_filename("NullPointerException.java").startswith("_"))

    # 25. Regression Test: Collision during finalization (between check and rename)
    def test_25_collision_during_finalization(self):
        """
        Simulate a race condition where another process creates the target file
        between our existence check and the os.rename() call.
        """
        part_file = Path(self.temp_dir) / "report.pdf.part"
        part_file.write_text("downloaded report content")

        call_count = {"n": 0}
        original_rename = os.rename

        def racing_rename(src, dst):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # First attempt: create the collision file just before renaming
                Path(dst).write_text("colliding file from another process")
                # Now the rename should fail because dst exists (on Windows)
                # or we simulate it explicitly
                raise FileExistsError(f"[WinError 183] Cannot create a file when that file already exists: '{dst}'")
            # Second attempt (with alternative name): succeed
            return original_rename(src, dst)

        with patch("os.rename", side_effect=racing_rename):
            final_path = safe_finalize_file(part_file, self.temp_dir, "report.pdf")

        # The colliding file must NOT be overwritten
        collision_file = Path(self.temp_dir) / "report.pdf"
        self.assertTrue(collision_file.exists())
        self.assertEqual(collision_file.read_text(), "colliding file from another process")

        # The downloaded file must be saved with an alternative name
        self.assertEqual(final_path.name, "report (1).pdf")
        self.assertEqual(final_path.read_text(), "downloaded report content")

    # 26. Regression Test: Format utilities edge cases
    def test_26_format_utilities(self):
        self.assertEqual(format_bytes(0), "0 B")
        self.assertEqual(format_bytes(None), "Unknown")
        self.assertEqual(format_bytes(-5), "Unknown")
        self.assertIn("KB", format_bytes(2048))
        self.assertIn("MB", format_bytes(2 * 1024 * 1024))

        self.assertEqual(format_speed(0), "—")
        self.assertEqual(format_speed(None), "—")
        self.assertIn("KB/s", format_speed(500 * 1024))

        self.assertEqual(format_eta(None), "—")
        self.assertEqual(format_eta(-1), "—")
        self.assertEqual(format_eta(90), "01:30")
        self.assertEqual(format_eta(3661), "01:01:01")


if __name__ == "__main__":
    unittest.main()
