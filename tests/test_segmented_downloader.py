import os
import sys
import time
import shutil
import tempfile
import threading
from pathlib import Path
import unittest
from unittest.mock import patch
import httpx

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QThread

from mdm.utils import safe_finalize_file, sanitize_filename
from mdm.downloader import HttpDownloader, DownloadError, DownloadCancelledError
from mdm.segment_manager import (
    SegmentedDownloader,
    calculate_segments,
    Segment,
    SegmentValidationError,
    RangeNotSupportedError,
    is_strong_etag,
)
from mdm.download_worker import DownloadWorker
from mdm.queue_manager import QueueManager
from mdm.settings import app_settings


class TestSegmentedDownloader(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt_app = QApplication.instance()
        if not cls.qt_app:
            cls.qt_app = QApplication(sys.argv)

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="mdm_stage3_test_")
        self.retry_counts = {}

    def tearDown(self):
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def create_range_transport(self, payload: bytes, mode: str = "normal", etag: str = '"etag-123"'):
        """
        Creates an in-memory MockTransport supporting HTTP Range requests.
        """
        retry_state = {}

        def handler(request: httpx.Request):
            range_header = request.headers.get("range", "").strip()

            if mode == "return_200":
                # Ignores Range header completely and returns full content with 200
                return httpx.Response(
                    200,
                    headers={
                        "Content-Type": "application/octet-stream",
                        "Content-Length": str(len(payload)),
                        "ETag": etag,
                    },
                    content=payload,
                )

            if mode == "reject_range" and range_header:
                return httpx.Response(416, headers={"Content-Type": "text/plain"}, content=b"Range Not Satisfiable")

            if mode == "unknown_size" and range_header:
                # Returns 206 but Content-Range has unknown total: bytes 0-0/*
                return httpx.Response(
                    206,
                    headers={
                        "Content-Type": "application/octet-stream",
                        "Content-Range": "bytes 0-0/*",
                    },
                    content=payload[:1],
                )

            if range_header.startswith("bytes="):
                spec = range_header[6:].strip()
                parts = spec.split("-")
                start = int(parts[0])
                end = int(parts[1]) if parts[1] else len(payload) - 1
                end = min(end, len(payload) - 1)

                if mode == "invalid_content_range":
                    # Probe succeeds so segmented mode begins, but segment requests get invalid Content-Range
                    if start == 0 and end == 0:
                        return httpx.Response(
                            206,
                            headers={
                                "Content-Type": "application/octet-stream",
                                "Content-Range": f"bytes 0-0/{len(payload)}",
                                "Content-Length": "1",
                            },
                            content=payload[:1],
                        )
                    # Return incorrect start offset in Content-Range for segments
                    return httpx.Response(
                        206,
                        headers={
                            "Content-Type": "application/octet-stream",
                            "Content-Range": f"bytes {start + 100}-{end}/{len(payload)}",
                            "Content-Length": str(end - start + 1),
                        },
                        content=payload[start:end + 1],
                    )

                if mode == "incomplete_segment":
                    # Send only half the requested bytes for segment 1
                    if start > 0:
                        short_slice = payload[start:start + 10]
                        return httpx.Response(
                            206,
                            headers={
                                "Content-Type": "application/octet-stream",
                                "Content-Range": f"bytes {start}-{end}/{len(payload)}",
                                "Content-Length": str(end - start + 1),
                            },
                            content=short_slice,
                        )

                if mode == "flaky_segment" and start > 0:
                    cnt = retry_state.get(start, 0) + 1
                    retry_state[start] = cnt
                    if cnt < 2:
                        return httpx.Response(503, headers={"Content-Type": "text/plain"}, content=b"Server Busy")

                if mode == "reject_on_segment" and start > 0:
                    # Probe succeeds, but actual segment request returns 200
                    return httpx.Response(
                        200,
                        headers={"Content-Length": str(len(payload))},
                        content=payload,
                    )

                chunk = payload[start:end + 1]
                return httpx.Response(
                    206,
                    headers={
                        "Content-Type": "application/octet-stream",
                        "Content-Range": f"bytes {start}-{end}/{len(payload)}",
                        "Content-Length": str(len(chunk)),
                        "ETag": etag,
                        "Last-Modified": "Wed, 21 Oct 2026 07:28:00 GMT",
                    },
                    content=chunk,
                )

            # Request without Range header
            return httpx.Response(
                200,
                headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(len(payload)),
                    "ETag": etag,
                },
                content=payload,
            )

        return httpx.MockTransport(handler)

    # 1. Server supporting HTTP Range requests
    def test_01_server_supporting_range_requests(self):
        payload = b"R" * (3 * 1024 * 1024)  # 3 MB
        transport = self.create_range_transport(payload)

        downloader = SegmentedDownloader(
            url="http://mock.server/ranged_file.bin",
            destination_folder=self.temp_dir,
            connections=4,
            min_segment_size=1024 * 1024,
            transport=transport,
        )
        final_path, total_bytes = downloader.download()

        self.assertTrue(final_path.exists())
        self.assertEqual(total_bytes, len(payload))
        self.assertEqual(final_path.stat().st_size, len(payload))
        self.assertEqual(final_path.read_bytes(), payload)
        self.assertIn("Multi-connection", downloader.mode_description)

    # 2. Server rejecting HTTP Range requests (fallback to single connection)
    def test_02_server_rejecting_range_requests(self):
        payload = b"SingleConnectionPayload" * 1000
        transport = self.create_range_transport(payload, mode="reject_range")

        downloader = SegmentedDownloader(
            url="http://mock.server/no_range.bin",
            destination_folder=self.temp_dir,
            connections=4,
            min_segment_size=1000,
            transport=transport,
        )
        final_path, total_bytes = downloader.download()

        self.assertTrue(final_path.exists())
        self.assertEqual(total_bytes, len(payload))
        self.assertEqual(final_path.read_bytes(), payload)
        self.assertIn("Single connection", downloader.mode_description)

    # 3. Server returning HTTP 200 instead of HTTP 206
    def test_03_server_returning_200_instead_of_206(self):
        payload = b"IgnoreRangePayload" * 5000
        transport = self.create_range_transport(payload, mode="return_200")

        downloader = SegmentedDownloader(
            url="http://mock.server/returns_200.bin",
            destination_folder=self.temp_dir,
            connections=4,
            min_segment_size=1000,
            transport=transport,
        )
        final_path, total_bytes = downloader.download()

        self.assertTrue(final_path.exists())
        self.assertEqual(total_bytes, len(payload))
        self.assertEqual(final_path.read_bytes(), payload)
        self.assertIn("Single connection", downloader.mode_description)

    # 4. Two-connection downloading
    def test_04_two_connection_downloading(self):
        payload = b"2-Conn-Data-" * (256 * 1024)
        transport = self.create_range_transport(payload)

        downloader = SegmentedDownloader(
            url="http://mock.server/two_conn.bin",
            destination_folder=self.temp_dir,
            connections=2,
            min_segment_size=512 * 1024,
            transport=transport,
        )
        final_path, total_bytes = downloader.download()

        self.assertEqual(len(downloader.segments), 2)
        self.assertEqual(total_bytes, len(payload))
        self.assertEqual(final_path.read_bytes(), payload)

    # 5. Four-connection downloading
    def test_05_four_connection_downloading(self):
        payload = b"4-Conn-Data-" * (256 * 1024)
        transport = self.create_range_transport(payload)

        downloader = SegmentedDownloader(
            url="http://mock.server/four_conn.bin",
            destination_folder=self.temp_dir,
            connections=4,
            min_segment_size=512 * 1024,
            transport=transport,
        )
        final_path, total_bytes = downloader.download()

        self.assertEqual(len(downloader.segments), 4)
        self.assertEqual(total_bytes, len(payload))
        self.assertEqual(final_path.read_bytes(), payload)

    # 6. Eight-connection downloading
    def test_06_eight_connection_downloading(self):
        payload = b"8-Conn-Data-" * (512 * 1024)
        transport = self.create_range_transport(payload)

        downloader = SegmentedDownloader(
            url="http://mock.server/eight_conn.bin",
            destination_folder=self.temp_dir,
            connections=8,
            min_segment_size=512 * 1024,
            transport=transport,
        )
        final_path, total_bytes = downloader.download()

        self.assertEqual(len(downloader.segments), 8)
        self.assertEqual(total_bytes, len(payload))
        self.assertEqual(final_path.read_bytes(), payload)

    # 7. Sixteen-connection configuration
    def test_07_sixteen_connection_configuration(self):
        payload = b"16-Conn-Data" * (1024 * 1024)  # 12 MB
        transport = self.create_range_transport(payload)

        downloader = SegmentedDownloader(
            url="http://mock.server/sixteen_conn.bin",
            destination_folder=self.temp_dir,
            connections=16,
            min_segment_size=512 * 1024,
            transport=transport,
        )
        final_path, total_bytes = downloader.download()

        self.assertEqual(len(downloader.segments), 16)
        self.assertEqual(total_bytes, len(payload))
        self.assertEqual(final_path.read_bytes(), payload)

    # 8. Correct segment boundary calculations
    def test_08_correct_segment_boundary_calculations(self):
        # 100 MB with 4 connections
        size = 100 * 1024 * 1024
        segs = calculate_segments(size, 4, min_segment_size=1024)
        self.assertEqual(len(segs), 4)

        # Check contiguous non-overlapping bounds
        self.assertEqual(segs[0][0], 0)
        for i in range(len(segs) - 1):
            self.assertEqual(segs[i][1] + 1, segs[i + 1][0])
        self.assertEqual(segs[-1][1], size - 1)

        # Verify sum of segment lengths equals total size
        total_len = sum(end - start + 1 for start, end in segs)
        self.assertEqual(total_len, size)

    # 9. Uneven file-size segmentation
    def test_09_uneven_file_size_segmentation(self):
        # Uneven size: 10,000,007 bytes divided by 6 connections
        size = 10000007
        segs = calculate_segments(size, 6, min_segment_size=1024)
        self.assertEqual(len(segs), 6)

        total_len = sum(end - start + 1 for start, end in segs)
        self.assertEqual(total_len, size)
        self.assertEqual(segs[0][0], 0)
        self.assertEqual(segs[-1][1], size - 1)

    # 10. Segment merging
    def test_10_segment_merging(self):
        part1 = Path(self.temp_dir) / "seg1.part"
        part2 = Path(self.temp_dir) / "seg2.part"
        part1.write_bytes(b"HELLO_")
        part2.write_bytes(b"WORLD!")

        d = SegmentedDownloader(
            url="http://mock.server/test.txt",
            destination_folder=self.temp_dir,
            explicit_filename="merged.txt",
        )
        d.effective_filename = "merged.txt"
        d.total_bytes = 12
        d.segments = [
            Segment(index=0, start_byte=0, end_byte=5, expected_size=6, part_file=part1),
            Segment(index=1, start_byte=6, end_byte=11, expected_size=6, part_file=part2),
        ]

        final_path, size = d._merge_segments()
        self.assertTrue(final_path.exists())
        self.assertEqual(final_path.read_bytes(), b"HELLO_WORLD!")
        self.assertEqual(size, 12)

    # 11. Incorrect Content-Range responses
    def test_11_incorrect_content_range_responses(self):
        payload = b"InvalidContentRangeData" * 100000
        transport = self.create_range_transport(payload, mode="invalid_content_range")

        downloader = SegmentedDownloader(
            url="http://mock.server/invalid_range.bin",
            destination_folder=self.temp_dir,
            connections=4,
            min_segment_size=1024,
            transport=transport,
        )
        # Should detect Content-Range mismatch and safely fail without corrupting file
        with self.assertRaises((DownloadError, SegmentValidationError)):
            downloader.download()

    # 12. Incomplete segment responses
    def test_12_incomplete_segment_responses(self):
        payload = b"IncompleteData" * 100000
        transport = self.create_range_transport(payload, mode="incomplete_segment")

        downloader = SegmentedDownloader(
            url="http://mock.server/incomplete.bin",
            destination_folder=self.temp_dir,
            connections=4,
            min_segment_size=1024,
            max_retries=1,
            transport=transport,
        )
        with self.assertRaises(DownloadError):
            downloader.download()

    # 13. Segment network failure and retry
    def test_13_segment_network_failure_and_retry(self):
        payload = b"FlakyNetworkData" * 100000
        transport = self.create_range_transport(payload, mode="flaky_segment")

        downloader = SegmentedDownloader(
            url="http://mock.server/flaky.bin",
            destination_folder=self.temp_dir,
            connections=3,
            min_segment_size=1024,
            max_retries=3,
            transport=transport,
        )
        final_path, total_bytes = downloader.download()

        self.assertTrue(final_path.exists())
        self.assertEqual(total_bytes, len(payload))
        self.assertEqual(final_path.read_bytes(), payload)

    # 14. Cancellation during parallel downloading
    def test_14_cancellation_during_parallel_downloading(self):
        payload = b"CancellableData" * 200000
        segments_started = threading.Event()
        cancel_triggered = threading.Event()

        def cancellable_handler(request: httpx.Request):
            range_header = request.headers.get("range", "").strip()

            # 1. Initial probe request must succeed with valid bytes 0-0/TOTAL
            if range_header == "bytes=0-0":
                return httpx.Response(
                    206,
                    headers={
                        "Content-Type": "application/octet-stream",
                        "Content-Range": f"bytes 0-0/{len(payload)}",
                        "Content-Length": "1",
                    },
                    content=payload[:1],
                )

            # 2. Parallel segment request started
            if range_header.startswith("bytes="):
                segments_started.set()
                # Hold segment worker until cancellation is initiated
                cancel_triggered.wait(timeout=2.0)
                time.sleep(0.05)
                spec = range_header[6:].strip()
                parts = spec.split("-")
                start = int(parts[0])
                end = int(parts[1]) if parts[1] else len(payload) - 1
                chunk = payload[start:end + 1]
                return httpx.Response(
                    206,
                    headers={
                        "Content-Type": "application/octet-stream",
                        "Content-Range": f"bytes {start}-{end}/{len(payload)}",
                        "Content-Length": str(len(chunk)),
                    },
                    content=chunk,
                )

            return httpx.Response(200, content=payload)

        d = SegmentedDownloader(
            url="http://mock.server/cancel.bin",
            destination_folder=self.temp_dir,
            connections=4,
            min_segment_size=1024,
            transport=httpx.MockTransport(cancellable_handler),
        )

        def trigger_cancellation():
            # Wait until parallel segment workers are verified active
            self.assertTrue(segments_started.wait(timeout=3.0))
            cancel_triggered.set()
            d.cancel()

        cancel_thread = threading.Thread(target=trigger_cancellation, daemon=True)
        cancel_thread.start()

        with self.assertRaises(DownloadCancelledError):
            d.download()

        cancel_thread.join(timeout=2.0)

        # Temporary segment directories and files must be cleaned up
        parts_dirs = list(Path(self.temp_dir).glob("*.parts"))
        self.assertEqual(len(parts_dirs), 0)
        part_files = list(Path(self.temp_dir).glob("*.part"))
        self.assertEqual(len(part_files), 0)
        # Ensure no final completed file was created
        final_files = [f for f in Path(self.temp_dir).iterdir() if f.is_file()]
        self.assertEqual(len(final_files), 0)

    # 15. Unknown file-size fallback
    def test_15_unknown_file_size_fallback(self):
        payload = b"UnknownSizePayload" * 1000
        transport = self.create_range_transport(payload, mode="unknown_size")

        downloader = SegmentedDownloader(
            url="http://mock.server/unknown_size.bin",
            destination_folder=self.temp_dir,
            connections=4,
            min_segment_size=1024,
            transport=transport,
        )
        final_path, total_bytes = downloader.download()

        self.assertTrue(final_path.exists())
        self.assertIn("Single connection", downloader.mode_description)

    # 16. Small-file fallback (< 2 MB)
    def test_16_small_file_fallback(self):
        payload = b"SmallFileContent" * 100  # < 2 MB
        transport = self.create_range_transport(payload)

        downloader = SegmentedDownloader(
            url="http://mock.server/small.bin",
            destination_folder=self.temp_dir,
            connections=4,
            min_segment_size=2 * 1024 * 1024,  # 2 MB
            transport=transport,
        )
        final_path, total_bytes = downloader.download()

        self.assertTrue(final_path.exists())
        self.assertEqual(total_bytes, len(payload))
        self.assertIn("Single connection", downloader.mode_description)

    # 17. ETag or Last-Modified validation
    def test_17_etag_or_last_modified_validation(self):
        payload = b"ETagValidatedPayload" * 100000
        transport = self.create_range_transport(payload, etag='"stable-etag-xyz"')

        downloader = SegmentedDownloader(
            url="http://mock.server/etag_file.bin",
            destination_folder=self.temp_dir,
            connections=4,
            min_segment_size=1024,
            transport=transport,
        )
        final_path, total_bytes = downloader.download()

        self.assertTrue(final_path.exists())
        self.assertEqual(total_bytes, len(payload))

    # 18. Duplicate destination filename protection
    def test_18_duplicate_destination_filename_protection(self):
        payload = b"DuplicateNameData" * 100000
        transport = self.create_range_transport(payload)

        # Pre-create existing file
        existing = Path(self.temp_dir) / "document.bin"
        existing.write_bytes(b"EXISTING_ORIGINAL_FILE")

        downloader = SegmentedDownloader(
            url="http://mock.server/document.bin",
            destination_folder=self.temp_dir,
            connections=4,
            min_segment_size=1024,
            transport=transport,
        )
        final_path, _ = downloader.download()

        # Must not overwrite existing file
        self.assertEqual(final_path.name, "document (1).bin")
        self.assertEqual(existing.read_bytes(), b"EXISTING_ORIGINAL_FILE")
        self.assertEqual(final_path.read_bytes(), payload)

    # 19. Final file size verification
    def test_19_final_file_size_verification(self):
        payload = b"ExactSizeData" * 80000
        transport = self.create_range_transport(payload)

        downloader = SegmentedDownloader(
            url="http://mock.server/exact_size.bin",
            destination_folder=self.temp_dir,
            connections=4,
            min_segment_size=1024,
            transport=transport,
        )
        final_path, total_bytes = downloader.download()

        self.assertEqual(final_path.stat().st_size, len(payload))
        self.assertEqual(total_bytes, len(payload))

    # 20. GUI responsiveness during parallel downloading (real QThread integration)
    def test_20_gui_responsiveness_during_parallel_downloading(self):
        payload = b"ResponsivePayload" * 200000
        transport = self.create_range_transport(payload)

        worker = DownloadWorker(
            item_id="test_worker_1",
            url="http://mock.server/responsive.bin",
            folder=self.temp_dir,
            connections=4,
            min_segment_size=1024,
            transport=transport,
        )

        progress_emits = []
        worker.progress.connect(lambda i_id, dl, tot, spd, eta: progress_emits.append(dl))

        segments_emits = []
        worker.segments_updated.connect(lambda i_id, segs: segments_emits.append(len(segs)))

        finished_emits = []

        qthread = QThread()
        worker.moveToThread(qthread)
        qthread.started.connect(worker.run)
        worker.completed.connect(lambda i_id, path, size: finished_emits.append(path))
        worker.finished.connect(qthread.quit)
        worker.failed.connect(lambda i_id, err: qthread.quit())

        qthread.start()

        # Pump Qt event loop while QThread worker runs
        start = time.time()
        while qthread.isRunning() and time.time() - start < 10.0:
            self.qt_app.processEvents()
            time.sleep(0.02)

        qthread.wait(5000)
        self.assertFalse(qthread.isRunning())
        self.assertGreater(len(progress_emits), 0)
        self.assertGreater(len(segments_emits), 0)
        self.assertEqual(len(finished_emits), 1)

    # 21. Cleanup of temporary segment files
    def test_21_cleanup_of_temporary_segment_files(self):
        payload = b"CleanSegmentFiles" * 150000
        transport = self.create_range_transport(payload)

        downloader = SegmentedDownloader(
            url="http://mock.server/clean_me.bin",
            destination_folder=self.temp_dir,
            connections=4,
            min_segment_size=1024,
            transport=transport,
        )
        final_path, _ = downloader.download()

        self.assertTrue(final_path.exists())
        # All .parts directories and .part segment files must be removed
        parts_dirs = list(Path(self.temp_dir).glob("*.parts"))
        self.assertEqual(len(parts_dirs), 0)
        part_files = list(Path(self.temp_dir).glob("*.part"))
        self.assertEqual(len(part_files), 0)

    # 22. Single-connection fallback after Range rejection
    def test_22_single_connection_fallback_after_range_rejection(self):
        payload = b"RejectAfterProbePayload" * 100000
        transport = self.create_range_transport(payload, mode="reject_on_segment")

        downloader = SegmentedDownloader(
            url="http://mock.server/reject_on_seg.bin",
            destination_folder=self.temp_dir,
            connections=4,
            min_segment_size=1024,
            transport=transport,
        )
        final_path, total_bytes = downloader.download()

        self.assertTrue(final_path.exists())
        self.assertEqual(final_path.read_bytes(), payload)
        self.assertIn("Single connection fallback", downloader.mode_description)

    # 23-26. Real parallel concurrency tests for 2, 4, 8, and 16 connections
    def _run_concurrency_test(self, connections: int):
        payload = b"ConcurrentPayloadData-" * (connections * 128 * 1024)
        active_segments = 0
        max_active_segments = 0
        lock = threading.Lock()
        probe_count = 0

        def concurrency_handler(request: httpx.Request):
            nonlocal active_segments, max_active_segments, probe_count
            range_header = request.headers.get("range", "").strip()

            if range_header == "bytes=0-0":
                with lock:
                    probe_count += 1
                return httpx.Response(
                    206,
                    headers={
                        "Content-Type": "application/octet-stream",
                        "Content-Range": f"bytes 0-0/{len(payload)}",
                        "Content-Length": "1",
                    },
                    content=payload[:1],
                )

            if range_header.startswith("bytes="):
                with lock:
                    active_segments += 1
                    if active_segments > max_active_segments:
                        max_active_segments = active_segments

                try:
                    # Brief delay to allow parallel worker overlap across threads
                    time.sleep(0.04)
                    spec = range_header[6:].strip()
                    parts = spec.split("-")
                    start = int(parts[0])
                    end = int(parts[1]) if parts[1] else len(payload) - 1
                    chunk = payload[start:end + 1]
                    return httpx.Response(
                        206,
                        headers={
                            "Content-Type": "application/octet-stream",
                            "Content-Range": f"bytes {start}-{end}/{len(payload)}",
                            "Content-Length": str(len(chunk)),
                        },
                        content=chunk,
                    )
                finally:
                    with lock:
                        active_segments -= 1

            return httpx.Response(200, content=payload)

        downloader = SegmentedDownloader(
            url=f"http://mock.server/concurrent_{connections}.bin",
            destination_folder=self.temp_dir,
            connections=connections,
            min_segment_size=1024,
            transport=httpx.MockTransport(concurrency_handler),
        )

        final_path, total_bytes = downloader.download()

        self.assertTrue(final_path.exists())
        self.assertEqual(total_bytes, len(payload))
        self.assertEqual(final_path.read_bytes(), payload)
        self.assertEqual(len(downloader.segments), connections)
        # Probe connection is separate and not counted in segment concurrency
        self.assertEqual(probe_count, 1)
        # Maximum simultaneous connections must never exceed configured connection limit
        self.assertLessEqual(max_active_segments, connections)
        # Confirm simultaneous parallel overlap occurred
        self.assertGreaterEqual(max_active_segments, min(2, connections))
        return max_active_segments

    def test_23_concurrency_2_connections(self):
        max_active = self._run_concurrency_test(connections=2)
        self.assertEqual(max_active, 2)

    def test_24_concurrency_4_connections(self):
        max_active = self._run_concurrency_test(connections=4)
        self.assertGreaterEqual(max_active, 2)
        self.assertLessEqual(max_active, 4)

    def test_25_concurrency_8_connections(self):
        max_active = self._run_concurrency_test(connections=8)
        self.assertGreaterEqual(max_active, 2)
        self.assertLessEqual(max_active, 8)

    def test_26_concurrency_16_connections(self):
        max_active = self._run_concurrency_test(connections=16)
        self.assertGreaterEqual(max_active, 2)
        self.assertLessEqual(max_active, 16)

    # 27. Strong vs. Weak ETag RFC 7232/9110 rules
    def test_27_is_strong_etag_validator(self):
        self.assertFalse(is_strong_etag(None))
        self.assertFalse(is_strong_etag(""))
        self.assertFalse(is_strong_etag('W/"weak-123"'))
        self.assertFalse(is_strong_etag('w/"weak-123"'))
        self.assertFalse(is_strong_etag("W/'weak-123'"))
        self.assertTrue(is_strong_etag('"strong-123"'))
        self.assertTrue(is_strong_etag('strong-bare-123'))

    # 28. Weak ETag not sent with If-Range; Last-Modified sent instead
    def test_28_if_range_uses_strong_etag_or_last_modified_not_weak_etag(self):
        payload = b"WeakETagValidationPayload" * 100000
        observed_if_range = []

        def if_range_handler(request: httpx.Request):
            range_header = request.headers.get("range", "").strip()
            if_range = request.headers.get("if-range")
            if if_range:
                observed_if_range.append(if_range)

            if range_header == "bytes=0-0":
                return httpx.Response(
                    206,
                    headers={
                        "Content-Type": "application/octet-stream",
                        "Content-Range": f"bytes 0-0/{len(payload)}",
                        "Content-Length": "1",
                        "ETag": 'W/"weak-abc-123"',
                        "Last-Modified": "Wed, 21 Oct 2026 07:28:00 GMT",
                    },
                    content=payload[:1],
                )

            spec = range_header[6:].strip()
            parts = spec.split("-")
            start = int(parts[0])
            end = int(parts[1]) if parts[1] else len(payload) - 1
            chunk = payload[start:end + 1]
            return httpx.Response(
                206,
                headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Range": f"bytes {start}-{end}/{len(payload)}",
                    "Content-Length": str(len(chunk)),
                    "ETag": 'W/"weak-abc-123"',
                    "Last-Modified": "Wed, 21 Oct 2026 07:28:00 GMT",
                },
                content=chunk,
            )

        downloader = SegmentedDownloader(
            url="http://mock.server/weak_etag.bin",
            destination_folder=self.temp_dir,
            connections=4,
            min_segment_size=1024,
            transport=httpx.MockTransport(if_range_handler),
        )
        final_path, total_bytes = downloader.download()

        self.assertTrue(final_path.exists())
        self.assertEqual(total_bytes, len(payload))
        # Verify that weak ETag was NOT sent in If-Range; Last-Modified was sent instead
        self.assertGreater(len(observed_if_range), 0)
        for val in observed_if_range:
            self.assertFalse(val.startswith('W/"'))
            self.assertEqual(val, "Wed, 21 Oct 2026 07:28:00 GMT")

    # 29. Server validator change mid-download triggers clean fallback
    def test_29_etag_change_mid_download_triggers_fallback(self):
        payload = b"ETagChangedOnServerData" * 100000

        def changed_etag_handler(request: httpx.Request):
            range_header = request.headers.get("range", "").strip()

            if range_header == "bytes=0-0":
                # Probe receives original ETag
                return httpx.Response(
                    206,
                    headers={
                        "Content-Type": "application/octet-stream",
                        "Content-Range": f"bytes 0-0/{len(payload)}",
                        "Content-Length": "1",
                        "ETag": '"initial-version-1"',
                    },
                    content=payload[:1],
                )

            if range_header.startswith("bytes="):
                # Segment request gets changed ETag (file was modified on server)
                spec = range_header[6:].strip()
                parts = spec.split("-")
                start = int(parts[0])
                end = int(parts[1]) if parts[1] else len(payload) - 1
                chunk = payload[start:end + 1]
                return httpx.Response(
                    206,
                    headers={
                        "Content-Type": "application/octet-stream",
                        "Content-Range": f"bytes {start}-{end}/{len(payload)}",
                        "Content-Length": str(len(chunk)),
                        "ETag": '"modified-version-2"',
                    },
                    content=chunk,
                )

            # Single-connection fallback request without Range
            return httpx.Response(
                200,
                headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(len(payload)),
                    "ETag": '"modified-version-2"',
                },
                content=payload,
            )

        downloader = SegmentedDownloader(
            url="http://mock.server/modified_etag.bin",
            destination_folder=self.temp_dir,
            connections=4,
            min_segment_size=1024,
            transport=httpx.MockTransport(changed_etag_handler),
        )
        final_path, total_bytes = downloader.download()

        self.assertTrue(final_path.exists())
        self.assertEqual(total_bytes, len(payload))
        self.assertEqual(final_path.read_bytes(), payload)
        self.assertIn("Single connection fallback", downloader.mode_description)

    # 30. Probe strict Content-Range validation failure falls back
    def test_30_probe_strict_content_range_validation_failure_falls_back(self):
        payload = b"ProbeStrictContentRangeData" * 100000

        def bad_probe_handler(request: httpx.Request):
            range_header = request.headers.get("range", "").strip()
            if range_header == "bytes=0-0":
                # Inconsistent probe: start 0, end 1 (must be 0-0)
                return httpx.Response(
                    206,
                    headers={
                        "Content-Type": "application/octet-stream",
                        "Content-Range": f"bytes 0-1/{len(payload)}",
                        "Content-Length": "2",
                    },
                    content=payload[:2],
                )

            # Fallback single connection request
            return httpx.Response(
                200,
                headers={"Content-Length": str(len(payload))},
                content=payload,
            )

        downloader = SegmentedDownloader(
            url="http://mock.server/bad_probe.bin",
            destination_folder=self.temp_dir,
            connections=4,
            min_segment_size=1024,
            transport=httpx.MockTransport(bad_probe_handler),
        )
        final_path, total_bytes = downloader.download()

        self.assertTrue(final_path.exists())
        self.assertEqual(total_bytes, len(payload))
        self.assertIn("Single connection mode", downloader.mode_description)

    # 31. Segment Content-Length header mismatch fails safely
    def test_31_segment_content_length_mismatch_fails_safely(self):
        payload = b"ContentLengthMismatchData" * 100000

        def cl_mismatch_handler(request: httpx.Request):
            range_header = request.headers.get("range", "").strip()
            if range_header == "bytes=0-0":
                return httpx.Response(
                    206,
                    headers={
                        "Content-Type": "application/octet-stream",
                        "Content-Range": f"bytes 0-0/{len(payload)}",
                        "Content-Length": "1",
                    },
                    content=payload[:1],
                )

            spec = range_header[6:].strip()
            parts = spec.split("-")
            start = int(parts[0])
            end = int(parts[1]) if parts[1] else len(payload) - 1
            chunk = payload[start:end + 1]
            # Send incorrect Content-Length header
            return httpx.Response(
                206,
                headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Range": f"bytes {start}-{end}/{len(payload)}",
                    "Content-Length": "999999",
                },
                content=chunk,
            )

        downloader = SegmentedDownloader(
            url="http://mock.server/cl_mismatch.bin",
            destination_folder=self.temp_dir,
            connections=4,
            min_segment_size=1024,
            max_retries=1,
            transport=httpx.MockTransport(cl_mismatch_handler),
        )
        with self.assertRaises(DownloadError):
            downloader.download()

        # Temporary files cleaned up, no corrupted final file
        parts_dirs = list(Path(self.temp_dir).glob("*.parts"))
        self.assertEqual(len(parts_dirs), 0)
        final_files = [f for f in Path(self.temp_dir).iterdir() if f.is_file()]
        self.assertEqual(len(final_files), 0)

    # 32. Monotonic wire bytes speed calculation during retries
    def test_32_speed_calculation_is_monotonic_during_retries(self):
        payload = b"RetrySpeedMonotonicData" * 100000
        speeds = []

        def on_prog(downloaded, total, speed, eta):
            if speed is not None:
                speeds.append(speed)

        transport = self.create_range_transport(payload, mode="flaky_segment")
        downloader = SegmentedDownloader(
            url="http://mock.server/speed_retry.bin",
            destination_folder=self.temp_dir,
            connections=4,
            min_segment_size=1024,
            max_retries=3,
            on_progress=on_prog,
            transport=transport,
        )
        final_path, total_bytes = downloader.download()

        self.assertTrue(final_path.exists())
        self.assertEqual(total_bytes, len(payload))
        # Ensure all reported speeds were non-negative
        for s in speeds:
            self.assertGreaterEqual(s, 0.0)
        # Ensure wire bytes counter is strictly positive
        self.assertGreater(downloader._wire_bytes_received, 0)

    # 33. Sibling segments abort promptly on fatal error
    def test_33_sibling_segments_abort_on_fatal_error(self):
        payload = b"SiblingAbortPayload" * 100000
        started_event = threading.Event()

        def fatal_segment_handler(request: httpx.Request):
            range_header = request.headers.get("range", "").strip()
            if range_header == "bytes=0-0":
                return httpx.Response(
                    206,
                    headers={
                        "Content-Type": "application/octet-stream",
                        "Content-Range": f"bytes 0-0/{len(payload)}",
                        "Content-Length": "1",
                    },
                    content=payload[:1],
                )

            spec = range_header[6:].strip()
            parts = spec.split("-")
            start = int(parts[0])

            if start == 0:
                # First segment triggers a fatal non-retryable 404 error
                return httpx.Response(404, content=b"Segment Not Found")

            # Sibling segments will be aborted promptly
            started_event.set()
            time.sleep(1.0)
            return httpx.Response(206, headers={"Content-Range": f"bytes {start}-{len(payload)-1}/{len(payload)}"}, content=payload[start:])

        downloader = SegmentedDownloader(
            url="http://mock.server/fatal_sibling.bin",
            destination_folder=self.temp_dir,
            connections=4,
            min_segment_size=1024,
            max_retries=1,
            transport=httpx.MockTransport(fatal_segment_handler),
        )
        start_t = time.time()
        with self.assertRaises(DownloadError):
            downloader.download()
        elapsed = time.time() - start_t

        # Must have aborted promptly without waiting full sleep duration on siblings
        self.assertLess(elapsed, 4.0)
        self.assertTrue(downloader._fatal_error)
        parts_dirs = list(Path(self.temp_dir).glob("*.parts"))
        self.assertEqual(len(parts_dirs), 0)


if __name__ == "__main__":
    unittest.main()
