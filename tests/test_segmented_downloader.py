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

from mdm.utils import safe_finalize_file, sanitize_filename
from mdm.downloader import HttpDownloader, DownloadError, DownloadCancelledError
from mdm.segment_manager import (
    SegmentedDownloader,
    calculate_segments,
    Segment,
    SegmentValidationError,
    RangeNotSupportedError,
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

        # Slow transport to give time to trigger cancel
        def slow_handler(req: httpx.Request):
            time.sleep(0.1)
            return httpx.Response(206, headers={"Content-Range": f"bytes 0-10/{len(payload)}"}, content=payload[:10])

        d = SegmentedDownloader(
            url="http://mock.server/cancel.bin",
            destination_folder=self.temp_dir,
            connections=4,
            min_segment_size=1024,
            transport=httpx.MockTransport(slow_handler),
        )

        def do_cancel():
            time.sleep(0.05)
            d.cancel()

        threading.Thread(target=do_cancel, daemon=True).start()

        with self.assertRaises(DownloadCancelledError):
            d.download()

        # Temporary segment directories and files must be cleaned up
        parts_dirs = list(Path(self.temp_dir).glob("*.parts"))
        self.assertEqual(len(parts_dirs), 0)

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

    # 20. GUI responsiveness during parallel downloading
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

        thread = threading.Thread(target=worker.run)
        thread.start()

        # Keep Qt event loop pumping events while worker runs
        start = time.time()
        while thread.is_alive() and time.time() - start < 10.0:
            self.qt_app.processEvents()
            time.sleep(0.02)

        thread.join(timeout=2.0)
        self.assertFalse(thread.is_alive())
        self.assertGreater(len(progress_emits), 0)

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


if __name__ == "__main__":
    unittest.main()
