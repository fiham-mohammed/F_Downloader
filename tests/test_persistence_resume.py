import os
import sys
import time
import shutil
import sqlite3
import tempfile
import threading
from pathlib import Path
import unittest
from unittest.mock import patch, MagicMock

import httpx
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mdm.models import DownloadModel, SegmentModel, DownloadStatus, can_transition, VALID_TRANSITIONS
from mdm.database import DatabaseManager, CURRENT_SCHEMA_VERSION
from mdm.downloader import HttpDownloader, DownloadError, DownloadCancelledError, DownloadPausedError
from mdm.segment_manager import SegmentedDownloader, Segment, calculate_segments
from mdm.queue_manager import QueueManager
from mdm.main_window import MainWindow
from mdm.utils import is_strong_etag


class MockByteStream(httpx.SyncByteStream):
    def __init__(self, data: bytes, chunk_size: int = 16384, delay: float = 0.0):
        self.data = data
        self.chunk_size = chunk_size
        self.delay = delay

    def __iter__(self):
        for i in range(0, len(self.data), self.chunk_size):
            if self.delay > 0:
                time.sleep(self.delay)
            yield self.data[i:i + self.chunk_size]


class TestStage4PersistenceAndResume(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt_app = QApplication.instance()
        if not cls.qt_app:
            cls.qt_app = QApplication(sys.argv)

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="mdm_stage4_test_")
        self.db_path = Path(self.temp_dir) / "test_downloads.db"
        self.db = DatabaseManager(self.db_path)

    def tearDown(self):
        if hasattr(self, "db") and self.db:
            self.db.close()
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    # 1. SQLite schema initialization
    def test_01_schema_initialization(self):
        conn = self.db.get_connection()
        cursor = conn.cursor()

        # Check WAL mode
        journal_mode = cursor.execute("PRAGMA journal_mode;").fetchone()[0]
        self.assertEqual(journal_mode.lower(), "wal")

        # Check foreign keys
        fk = cursor.execute("PRAGMA foreign_keys;").fetchone()[0]
        self.assertEqual(fk, 1)

        # Check tables exist
        tables = [r[0] for r in cursor.execute("SELECT name FROM sqlite_master WHERE type='table';").fetchall()]
        self.assertIn("downloads", tables)
        self.assertIn("segments", tables)
        self.assertIn("settings", tables)

    # 2. Schema migration user_version pragma
    def test_02_migration_version_tracking(self):
        conn = sqlite3.connect(str(self.db_path))
        version = conn.execute("PRAGMA user_version;").fetchone()[0]
        conn.close()
        self.assertEqual(version, CURRENT_SCHEMA_VERSION)

    # 3. State machine transitions
    def test_03_state_machine_valid_and_invalid_transitions(self):
        # Valid transitions
        self.assertTrue(can_transition(DownloadStatus.QUEUED, DownloadStatus.DOWNLOADING))
        self.assertTrue(can_transition(DownloadStatus.DOWNLOADING, DownloadStatus.PAUSED))
        self.assertTrue(can_transition(DownloadStatus.PAUSED, DownloadStatus.QUEUED))
        self.assertTrue(can_transition(DownloadStatus.DOWNLOADING, DownloadStatus.COMPLETED))
        self.assertTrue(can_transition(DownloadStatus.DOWNLOADING, DownloadStatus.FAILED))
        self.assertTrue(can_transition(DownloadStatus.DOWNLOADING, DownloadStatus.INTERRUPTED))
        self.assertTrue(can_transition(DownloadStatus.INTERRUPTED, DownloadStatus.QUEUED))

        # Invalid transitions
        self.assertFalse(can_transition(DownloadStatus.COMPLETED, DownloadStatus.DOWNLOADING))
        self.assertFalse(can_transition(DownloadStatus.COMPLETED, DownloadStatus.PAUSED))
        self.assertFalse(can_transition(DownloadStatus.QUEUED, DownloadStatus.COMPLETED))

    # 4. Adding download persists to DB
    def test_04_add_download_persistence(self):
        dl = DownloadModel(
            id="dl_001",
            url="http://example.com/file.iso",
            destination_folder=self.temp_dir,
            filename="file.iso",
            explicit_filename="file.iso",
            total_size=1000000,
            downloaded_size=0,
            status=DownloadStatus.QUEUED,
            connections=8,
            queue_order=0,
        )
        self.db.add_download(dl)

        fetched = self.db.get_download("dl_001")
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.id, "dl_001")
        self.assertEqual(fetched.url, "http://example.com/file.iso")
        self.assertEqual(fetched.status, DownloadStatus.QUEUED)
        self.assertEqual(fetched.connections, 8)

    # 5. Status updates in DB
    def test_05_update_status_persistence(self):
        dl = DownloadModel(id="dl_002", url="http://example.com/a.zip", destination_folder=self.temp_dir)
        self.db.add_download(dl)

        self.db.update_status("dl_002", DownloadStatus.DOWNLOADING)
        self.assertEqual(self.db.get_download("dl_002").status, DownloadStatus.DOWNLOADING)

        self.db.update_status("dl_002", DownloadStatus.PAUSED)
        self.assertEqual(self.db.get_download("dl_002").status, DownloadStatus.PAUSED)

        self.db.update_status("dl_002", DownloadStatus.DOWNLOADING)
        self.db.update_status("dl_002", DownloadStatus.COMPLETED, final_path="/path/to/a.zip")
        fetched = self.db.get_download("dl_002")
        self.assertEqual(fetched.status, DownloadStatus.COMPLETED)
        self.assertEqual(Path(fetched.final_path), Path("/path/to/a.zip"))

    # 6. Progress updates in DB
    def test_06_update_progress_persistence(self):
        dl = DownloadModel(id="dl_003", url="http://example.com/b.zip", destination_folder=self.temp_dir)
        self.db.add_download(dl)

        self.db.update_progress("dl_003", downloaded_size=500000, total_size=1000000, speed=102400.0, eta=5.0)
        fetched = self.db.get_download("dl_003")
        self.assertEqual(fetched.downloaded_size, 500000)
        self.assertEqual(fetched.total_size, 1000000)

    # 7. Validator updates in DB
    def test_07_update_validators_persistence(self):
        dl = DownloadModel(id="dl_004", url="http://example.com/c.zip", destination_folder=self.temp_dir)
        self.db.add_download(dl)

        self.db.update_validators("dl_004", etag='"strong-etag-123"', last_modified="Wed, 21 Oct 2025 07:28:00 GMT", temp_path="/tmp/c.part")
        fetched = self.db.get_download("dl_004")
        self.assertEqual(fetched.etag, '"strong-etag-123"')
        self.assertEqual(fetched.last_modified, "Wed, 21 Oct 2025 07:28:00 GMT")
        self.assertEqual(fetched.temp_path, "/tmp/c.part")

    # 8. Saving and retrieving segments
    def test_08_save_and_retrieve_segments(self):
        dl = DownloadModel(id="dl_005", url="http://example.com/d.zip", destination_folder=self.temp_dir)
        self.db.add_download(dl)

        segments = [
            SegmentModel("dl_005", 0, 0, 499999, 500000, "Completed", "/tmp/seg_0.part"),
            SegmentModel("dl_005", 1, 500000, 999999, 250000, "Downloading", "/tmp/seg_1.part"),
        ]
        self.db.save_segments("dl_005", segments)

        fetched_segs = self.db.get_segments("dl_005")
        self.assertEqual(len(fetched_segs), 2)
        self.assertEqual(fetched_segs[0].segment_index, 0)
        self.assertEqual(fetched_segs[0].status, "Completed")
        self.assertEqual(fetched_segs[1].downloaded_bytes, 250000)

    # 9. Segment cascade delete
    def test_09_segments_cascade_delete(self):
        dl = DownloadModel(id="dl_006", url="http://example.com/e.zip", destination_folder=self.temp_dir)
        self.db.add_download(dl)
        self.db.save_segments("dl_006", [SegmentModel("dl_006", 0, 0, 999, 1000, "Completed")])

        self.assertEqual(len(self.db.get_segments("dl_006")), 1)
        self.db.delete_download("dl_006")
        self.assertEqual(len(self.db.get_segments("dl_006")), 0)

    # 10. Application restart recovery of Interrupted downloads
    def test_10_restart_recovers_downloading_to_interrupted(self):
        # Simulate download left in Downloading status before crash/shutdown
        dl1 = DownloadModel(id="crash_1", url="http://test/1.bin", destination_folder=self.temp_dir, status=DownloadStatus.DOWNLOADING)
        dl2 = DownloadModel(id="queued_1", url="http://test/2.bin", destination_folder=self.temp_dir, status=DownloadStatus.QUEUED)
        dl3 = DownloadModel(id="done_1", url="http://test/3.bin", destination_folder=self.temp_dir, status=DownloadStatus.COMPLETED)
        self.db.add_download(dl1)
        self.db.add_download(dl2)
        self.db.add_download(dl3)

        # Run recovery
        recovered_ids = self.db.recover_interrupted_downloads()
        self.assertEqual(len(recovered_ids), 1)
        self.assertEqual(recovered_ids[0], "crash_1")

        self.assertEqual(self.db.get_download("crash_1").status, DownloadStatus.INTERRUPTED)
        self.assertEqual(self.db.get_download("queued_1").status, DownloadStatus.QUEUED)
        self.assertEqual(self.db.get_download("done_1").status, DownloadStatus.COMPLETED)

    # 11. Other statuses retain their state across recovery
    def test_11_statuses_retained_on_restart(self):
        self.db.add_download(DownloadModel(id="p1", url="http://test/p", destination_folder=self.temp_dir, status=DownloadStatus.PAUSED))
        self.db.add_download(DownloadModel(id="f1", url="http://test/f", destination_folder=self.temp_dir, status=DownloadStatus.FAILED))
        self.db.add_download(DownloadModel(id="c1", url="http://test/c", destination_folder=self.temp_dir, status=DownloadStatus.CANCELLED))

        self.db.recover_interrupted_downloads()

        self.assertEqual(self.db.get_download("p1").status, DownloadStatus.PAUSED)
        self.assertEqual(self.db.get_download("f1").status, DownloadStatus.FAILED)
        self.assertEqual(self.db.get_download("c1").status, DownloadStatus.CANCELLED)

    # 12. Startup restores queue order
    def test_12_startup_restores_queue_order(self):
        self.db.add_download(DownloadModel(id="q1", url="http://test/1", destination_folder=self.temp_dir, queue_order=0, status=DownloadStatus.QUEUED))
        self.db.add_download(DownloadModel(id="q2", url="http://test/2", destination_folder=self.temp_dir, queue_order=1, status=DownloadStatus.QUEUED))

        qm = QueueManager(db=self.db)
        self.assertEqual(len(qm.items), 2)
        self.assertEqual(qm.items[0]["id"], "q1")
        self.assertEqual(qm.items[1]["id"], "q2")

    # 13. Pause single-connection download preserves partial .part file
    def test_13_pause_single_connection_preserves_part_file(self):
        file_content = b"P" * 100000

        def slow_stream_handler(request: httpx.Request):
            return httpx.Response(200, headers={"Content-Length": str(len(file_content))}, stream=MockByteStream(file_content, chunk_size=1024, delay=0.01))

        transport = httpx.MockTransport(slow_stream_handler)
        is_paused = False

        downloader = HttpDownloader(
            url="http://test.com/file.bin",
            destination_folder=self.temp_dir,
            chunk_size=1024,
            is_paused=lambda: is_paused,
            transport=transport,
        )

        def pause_after_start():
            part_file = Path(self.temp_dir) / "file.bin.part"
            for _ in range(100):
                if part_file.exists() and part_file.stat().st_size > 0:
                    break
                time.sleep(0.01)
            nonlocal is_paused
            is_paused = True

        t = threading.Thread(target=pause_after_start)
        t.start()

        with self.assertRaises(DownloadPausedError):
            downloader.download()
        t.join()

        # Partial file must exist on disk!
        part_file = Path(self.temp_dir) / "file.bin.part"
        self.assertTrue(part_file.exists())
        self.assertGreater(part_file.stat().st_size, 0)
        self.assertLess(part_file.stat().st_size, len(file_content))

    # 14. Resume single-connection download with HTTP 206
    def test_14_resume_single_connection_http_206(self):
        total_data = b"0123456789" * 10000  # 100,000 bytes
        part_path = Path(self.temp_dir) / "resume206.bin.part"

        # Write first 40,000 bytes to .part file
        initial_bytes = total_data[:40000]
        with open(part_path, "wb") as f:
            f.write(initial_bytes)

        range_headers_received = []

        def resume_handler(request: httpx.Request):
            range_hdr = request.headers.get("Range")
            range_headers_received.append(range_hdr)

            if range_hdr == "bytes=40000-":
                remaining = total_data[40000:]
                return httpx.Response(
                    206,
                    headers={
                        "Content-Range": f"bytes 40000-{len(total_data)-1}/{len(total_data)}",
                        "Content-Length": str(len(remaining)),
                        "ETag": '"resume-etag-1"',
                    },
                    stream=MockByteStream(remaining),
                )
            return httpx.Response(200, headers={"Content-Length": str(len(total_data))}, stream=MockByteStream(total_data))

        transport = httpx.MockTransport(resume_handler)
        downloader = HttpDownloader(
            url="http://test.com/resume206.bin",
            destination_folder=self.temp_dir,
            etag='"resume-etag-1"',
            resumed_part_path=part_path,
            transport=transport,
        )

        final_path, total_downloaded = downloader.download()
        self.assertIn("bytes=40000-", range_headers_received)
        self.assertTrue(final_path.exists())
        self.assertEqual(final_path.stat().st_size, len(total_data))
        with open(final_path, "rb") as f:
            self.assertEqual(f.read(), total_data)

    # 15. Single-connection resume server returns 200 (ignored Range header): restarts from zero cleanly
    def test_15_resume_single_connection_server_returns_200(self):
        total_data = b"ABCDEFGHIJ" * 5000  # 50,000 bytes
        part_path = Path(self.temp_dir) / "ignored_range.bin.part"

        with open(part_path, "wb") as f:
            f.write(b"CORRUPTED_OLD_DATA_1234567890")

        def handler_ignores_range(request: httpx.Request):
            # Server completely ignores Range and sends 200 with full content
            return httpx.Response(
                200,
                headers={"Content-Length": str(len(total_data))},
                stream=MockByteStream(total_data),
            )

        transport = httpx.MockTransport(handler_ignores_range)
        downloader = HttpDownloader(
            url="http://test.com/ignored_range.bin",
            destination_folder=self.temp_dir,
            resumed_part_path=part_path,
            transport=transport,
        )

        final_path, total_downloaded = downloader.download()
        self.assertTrue(final_path.exists())
        self.assertEqual(final_path.stat().st_size, len(total_data))
        with open(final_path, "rb") as f:
            self.assertEqual(f.read(), total_data)

    # 16. Single-connection resume 412 Precondition Failed: restarts from zero
    def test_16_resume_single_connection_412_precondition_failed(self):
        new_data = b"NEW_DATA_AFTER_MUTATION" * 2000
        part_path = Path(self.temp_dir) / "file412.bin.part"
        with open(part_path, "wb") as f:
            f.write(b"OLD_PARTIAL_DATA")

        req_count = 0

        def handler_412(request: httpx.Request):
            nonlocal req_count
            req_count += 1
            if "If-Range" in request.headers or "Range" in request.headers:
                if req_count == 1:
                    # Remote file changed, 412 returned
                    return httpx.Response(412, content=b"Precondition Failed")
            return httpx.Response(200, headers={"Content-Length": str(len(new_data))}, stream=MockByteStream(new_data))

        transport = httpx.MockTransport(handler_412)
        downloader = HttpDownloader(
            url="http://test.com/file412.bin",
            destination_folder=self.temp_dir,
            etag='"old-etag"',
            resumed_part_path=part_path,
            transport=transport,
        )

        final_path, total = downloader.download()
        self.assertEqual(final_path.stat().st_size, len(new_data))

    # 17. Single-connection resume 416 Range Not Satisfiable: handled cleanly
    def test_17_resume_single_connection_416_range_not_satisfiable(self):
        data = b"CORRECT_DATA" * 1000
        part_path = Path(self.temp_dir) / "file416.bin.part"
        # Simulate partial file larger than remote file
        with open(part_path, "wb") as f:
            f.write(b"OVERSHOT_DATA" * 2000)

        req_count = 0
        def handler_416(request: httpx.Request):
            nonlocal req_count
            req_count += 1
            if req_count == 1:
                return httpx.Response(416, headers={"Content-Range": f"bytes */{len(data)}"}, content=b"Range Not Satisfiable")
            return httpx.Response(200, headers={"Content-Length": str(len(data))}, stream=MockByteStream(data))

        transport = httpx.MockTransport(handler_416)
        downloader = HttpDownloader(
            url="http://test.com/file416.bin",
            destination_folder=self.temp_dir,
            resumed_part_path=part_path,
            transport=transport,
        )

        final_path, total = downloader.download()
        self.assertEqual(final_path.stat().st_size, len(data))

    # 18. Single-connection resume ETag mismatch restarts fresh
    def test_18_resume_single_connection_etag_mismatch(self):
        fresh_data = b"FRESH_SERVER_DATA" * 1000
        part_path = Path(self.temp_dir) / "etag_mismatch.bin.part"
        with open(part_path, "wb") as f:
            f.write(b"STALE_OLD_DATA")

        def handler(request: httpx.Request):
            # Remote server returns different ETag and full content
            return httpx.Response(
                200,
                headers={"ETag": '"brand-new-etag"', "Content-Length": str(len(fresh_data))},
                stream=MockByteStream(fresh_data),
            )

        transport = httpx.MockTransport(handler)
        downloader = HttpDownloader(
            url="http://test.com/etag_mismatch.bin",
            destination_folder=self.temp_dir,
            etag='"stale-etag"',
            resumed_part_path=part_path,
            transport=transport,
        )

        final_path, total = downloader.download()
        self.assertEqual(final_path.stat().st_size, len(fresh_data))
        with open(final_path, "rb") as f:
            self.assertEqual(f.read(), fresh_data)

    # 19. Single-connection resume Last-Modified mismatch restarts fresh
    def test_19_resume_single_connection_last_modified_mismatch(self):
        fresh_data = b"MUTATED_SINCE_LAST_WEEK" * 1000
        part_path = Path(self.temp_dir) / "mod_mismatch.bin.part"
        with open(part_path, "wb") as f:
            f.write(b"OLD_CONTENT")

        def handler(request: httpx.Request):
            return httpx.Response(
                200,
                headers={"Last-Modified": "Fri, 23 Oct 2026 12:00:00 GMT", "Content-Length": str(len(fresh_data))},
                stream=MockByteStream(fresh_data),
            )

        transport = httpx.MockTransport(handler)
        downloader = HttpDownloader(
            url="http://test.com/mod_mismatch.bin",
            destination_folder=self.temp_dir,
            last_modified="Mon, 19 Oct 2026 10:00:00 GMT",
            resumed_part_path=part_path,
            transport=transport,
        )

        final_path, total = downloader.download()
        self.assertEqual(final_path.stat().st_size, len(fresh_data))

    # 20. Weak ETag handling (RFC 7232/9110)
    def test_20_weak_etag_handling(self):
        self.assertFalse(is_strong_etag('W/"weak123"'))
        self.assertFalse(is_strong_etag('w/"weak456"'))
        self.assertTrue(is_strong_etag('"strong789"'))

    # 21. Pause multi-connection download preserves .parts directory
    def test_21_pause_multiconnection_preserves_parts_dir(self):
        total_size = 600000
        full_content = b"M" * total_size
        parts_dir = Path(self.temp_dir) / ".test_parts.parts"
        parts_dir.mkdir(parents=True, exist_ok=True)

        is_paused = False

        def multi_handler(request: httpx.Request):
            range_hdr = request.headers.get("Range", "")
            if range_hdr == "bytes=0-0":
                return httpx.Response(
                    206,
                    headers={"Content-Range": f"bytes 0-0/{total_size}", "Content-Length": "1", "ETag": '"multi-etag"'},
                    content=full_content[:1],
                )
            elif range_hdr.startswith("bytes="):
                s, e = range_hdr[6:].split("-")
                start, end = int(s), int(e)
                chunk = full_content[start:end+1]
                return httpx.Response(
                    206,
                    headers={"Content-Range": f"bytes {start}-{end}/{total_size}", "Content-Length": str(len(chunk))},
                    stream=MockByteStream(chunk, chunk_size=1024, delay=0.01),
                )
            return httpx.Response(200, headers={"Content-Length": str(total_size)}, stream=MockByteStream(full_content))

        def on_prog(downloaded, total, speed, eta):
            nonlocal is_paused
            if downloaded >= 4096:
                is_paused = True

        transport = httpx.MockTransport(multi_handler)
        downloader = SegmentedDownloader(
            url="http://test.com/multi.iso",
            destination_folder=self.temp_dir,
            connections=2,
            min_segment_size=50000,
            chunk_size=1024,
            is_paused=lambda: is_paused,
            on_progress=on_prog,
            transport=transport,
        )

        with self.assertRaises(DownloadPausedError):
            downloader.download()

        # Downloader parts_dir must still exist!
        self.assertIsNotNone(downloader.parts_dir)
        self.assertTrue(downloader.parts_dir.exists())

    # 22. Resume multi-connection download skips completed segments
    def test_22_resume_multiconnection_skips_completed_segments(self):
        total_size = 100000
        full_data = b"0123456789" * 10000
        parts_dir = Path(self.temp_dir) / ".resumed.parts"
        parts_dir.mkdir(parents=True, exist_ok=True)

        # 2 segments: 0..49999 and 50000..99999
        seg0_file = parts_dir / "segment_00.part"
        seg1_file = parts_dir / "segment_01.part"

        # Segment 0 is already completely downloaded on disk!
        with open(seg0_file, "wb") as f:
            f.write(full_data[:50000])

        requested_ranges = []

        def resume_handler(request: httpx.Request):
            range_hdr = request.headers.get("Range", "")
            requested_ranges.append(range_hdr)

            if range_hdr == "bytes=0-0":
                return httpx.Response(
                    206,
                    headers={"Content-Range": f"bytes 0-0/{total_size}", "Content-Length": "1", "ETag": '"etag-multi-1"'},
                    content=full_data[:1],
                )
            elif range_hdr.startswith("bytes="):
                s, e = range_hdr[6:].split("-")
                start, end = int(s), int(e)
                chunk = full_data[start:end+1]
                return httpx.Response(
                    206,
                    headers={"Content-Range": f"bytes {start}-{end}/{total_size}", "Content-Length": str(len(chunk))},
                    stream=MockByteStream(chunk),
                )
            return httpx.Response(200, content=full_data)

        transport = httpx.MockTransport(resume_handler)

        resumed_segments = [
            SegmentModel("dl_res", 0, 0, 49999, 50000, "Completed", str(seg0_file)),
            SegmentModel("dl_res", 1, 50000, 99999, 0, "Queued", str(seg1_file)),
        ]

        downloader = SegmentedDownloader(
            url="http://test.com/file_res.bin",
            destination_folder=self.temp_dir,
            connections=2,
            min_segment_size=10000,
            etag='"etag-multi-1"',
            resumed_parts_dir=parts_dir,
            resumed_segments=resumed_segments,
            transport=transport,
        )

        final_path, total = downloader.download()

        # Segment 0 range (0-49999) must NOT have been requested from server!
        self.assertNotIn("bytes=0-49999", requested_ranges)
        # Segment 1 range (50000-99999) MUST have been requested
        self.assertIn("bytes=50000-99999", requested_ranges)
        self.assertEqual(final_path.stat().st_size, total_size)
        with open(final_path, "rb") as f:
            self.assertEqual(f.read(), full_data)

    # 23. Resume multi-connection download resumes partial segment from offset
    def test_23_resume_multiconnection_partial_segment_offset(self):
        total_size = 100000
        full_data = b"ABCDEFGHIJ" * 10000
        parts_dir = Path(self.temp_dir) / ".partial_seg.parts"
        parts_dir.mkdir(parents=True, exist_ok=True)

        seg0_file = parts_dir / "segment_00.part"
        seg1_file = parts_dir / "segment_01.part"

        # Segment 0 has 20,000 bytes out of 50,000 downloaded
        with open(seg0_file, "wb") as f:
            f.write(full_data[:20000])

        requested_ranges = []

        def handler(request: httpx.Request):
            range_hdr = request.headers.get("Range", "")
            requested_ranges.append(range_hdr)

            if range_hdr == "bytes=0-0":
                return httpx.Response(
                    206,
                    headers={"Content-Range": f"bytes 0-0/{total_size}", "Content-Length": "1", "ETag": '"etag-part-1"'},
                    content=full_data[:1],
                )
            elif range_hdr.startswith("bytes="):
                s, e = range_hdr[6:].split("-")
                start, end = int(s), int(e)
                chunk = full_data[start:end+1]
                return httpx.Response(
                    206,
                    headers={"Content-Range": f"bytes {start}-{end}/{total_size}", "Content-Length": str(len(chunk))},
                    stream=MockByteStream(chunk),
                )
            return httpx.Response(200, content=full_data)

        transport = httpx.MockTransport(handler)
        resumed_segments = [
            SegmentModel("dl_p", 0, 0, 49999, 20000, "Queued", str(seg0_file)),
            SegmentModel("dl_p", 1, 50000, 99999, 0, "Queued", str(seg1_file)),
        ]

        downloader = SegmentedDownloader(
            url="http://test.com/file_part.bin",
            destination_folder=self.temp_dir,
            connections=2,
            min_segment_size=10000,
            etag='"etag-part-1"',
            resumed_parts_dir=parts_dir,
            resumed_segments=resumed_segments,
            transport=transport,
        )

        final_path, total = downloader.download()

        # Segment 0 must be requested starting at byte 20000: bytes=20000-49999
        self.assertIn("bytes=20000-49999", requested_ranges)
        self.assertEqual(final_path.stat().st_size, total_size)
        with open(final_path, "rb") as f:
            self.assertEqual(f.read(), full_data)

    # 24. Multi-connection resume validates Content-Range strictly
    def test_24_resume_multiconnection_strict_content_range(self):
        total_size = 600000
        full_data = b"X" * total_size

        def handler(request: httpx.Request):
            range_hdr = request.headers.get("Range", "")
            if not range_hdr:
                return httpx.Response(200, headers={"Content-Length": str(total_size)}, content=full_data)
            if range_hdr == "bytes=0-0":
                return httpx.Response(
                    206,
                    headers={"Content-Range": f"bytes 0-0/{total_size}", "Content-Length": "1"},
                    content=b"X",
                )
            # Valid 206 for segments
            s, e = range_hdr[6:].split("-")
            start, end = int(s), int(e)
            return httpx.Response(
                206,
                headers={"Content-Range": f"bytes {start}-{end}/{total_size}", "Content-Length": str(end - start + 1)},
                content=full_data[start:end+1],
            )

        transport = httpx.MockTransport(handler)
        downloader = SegmentedDownloader(
            url="http://test.com/strict.bin",
            destination_folder=self.temp_dir,
            connections=2,
            min_segment_size=50000,
            transport=transport,
        )
        final_path, total = downloader.download()
        self.assertEqual(final_path.stat().st_size, total_size)

    # 25. Multi-connection resume server returns 200 for range request falls back safely
    def test_25_resume_multiconnection_server_200_fallback(self):
        total_size = 60000
        full_data = b"Y" * total_size

        def handler(request: httpx.Request):
            range_hdr = request.headers.get("Range", "")
            if range_hdr == "bytes=0-0":
                # Probe accepts 206
                return httpx.Response(206, headers={"Content-Range": f"bytes 0-0/{total_size}"}, content=b"Y")
            # But segment request gets 200 (ignored range)
            return httpx.Response(200, headers={"Content-Length": str(total_size)}, content=full_data)

        transport = httpx.MockTransport(handler)
        downloader = SegmentedDownloader(
            url="http://test.com/fallback.bin",
            destination_folder=self.temp_dir,
            connections=2,
            min_segment_size=20000,
            transport=transport,
        )
        final_path, total = downloader.download()
        self.assertEqual(final_path.stat().st_size, total_size)

    # 26. Multi-connection resume ETag mismatch restarts safely
    def test_26_resume_multiconnection_etag_mismatch(self):
        new_data = b"Z" * 600000
        parts_dir = Path(self.temp_dir) / ".stale_multi.parts"
        parts_dir.mkdir(parents=True, exist_ok=True)
        seg0 = parts_dir / "segment_00.part"
        with open(seg0, "wb") as f:
            f.write(b"OLD_STALE_DATA" * 1000)

        def handler(request: httpx.Request):
            range_hdr = request.headers.get("Range", "")
            if not range_hdr:
                return httpx.Response(200, headers={"Content-Length": str(len(new_data))}, content=new_data)
            if range_hdr == "bytes=0-0":
                # Returns brand new ETag
                return httpx.Response(
                    206,
                    headers={"Content-Range": f"bytes 0-0/{len(new_data)}", "ETag": '"brand-new-etag"'},
                    content=new_data[:1],
                )
            s, e = range_hdr[6:].split("-")
            start, end = int(s), int(e)
            return httpx.Response(
                206,
                headers={"Content-Range": f"bytes {start}-{end}/{len(new_data)}", "Content-Length": str(end - start + 1), "ETag": '"brand-new-etag"'},
                content=new_data[start:end+1],
            )

        transport = httpx.MockTransport(handler)
        resumed_segments = [SegmentModel("m_etag", 0, 0, 299999, 14000, "Queued", str(seg0))]

        downloader = SegmentedDownloader(
            url="http://test.com/etag_changed.bin",
            destination_folder=self.temp_dir,
            connections=2,
            min_segment_size=50000,
            etag='"original-stale-etag"',
            resumed_parts_dir=parts_dir,
            resumed_segments=resumed_segments,
            transport=transport,
        )

        final_path, total = downloader.download()
        self.assertEqual(final_path.stat().st_size, len(new_data))

    # 27. Multi-connection resume Last-Modified mismatch restarts safely
    def test_27_resume_multiconnection_last_modified_mismatch(self):
        new_data = b"W" * 600000

        def handler(request: httpx.Request):
            range_hdr = request.headers.get("Range", "")
            if not range_hdr:
                return httpx.Response(200, headers={"Content-Length": str(len(new_data))}, content=new_data)
            if range_hdr == "bytes=0-0":
                return httpx.Response(
                    206,
                    headers={"Content-Range": f"bytes 0-0/{len(new_data)}", "Last-Modified": "Fri, 23 Oct 2026 12:00:00 GMT"},
                    content=new_data[:1],
                )
            s, e = range_hdr[6:].split("-")
            start, end = int(s), int(e)
            return httpx.Response(
                206,
                headers={"Content-Range": f"bytes {start}-{end}/{len(new_data)}", "Content-Length": str(end - start + 1)},
                content=new_data[start:end+1],
            )

        transport = httpx.MockTransport(handler)
        downloader = SegmentedDownloader(
            url="http://test.com/lm_changed.bin",
            destination_folder=self.temp_dir,
            connections=2,
            min_segment_size=50000,
            last_modified="Mon, 19 Oct 2026 10:00:00 GMT",
            transport=transport,
        )

        final_path, total = downloader.download()
        self.assertEqual(final_path.stat().st_size, len(new_data))

    # 28. Multi-connection resume corrupted/oversized segment file deletes and re-downloads
    def test_28_resume_multiconnection_oversized_segment_reset(self):
        total_size = 600000
        full_data = b"K" * total_size
        parts_dir = Path(self.temp_dir) / ".oversized.parts"
        parts_dir.mkdir(parents=True, exist_ok=True)
        seg0 = parts_dir / "segment_00.part"
        # Write corrupted file exceeding segment expected size (300,000)
        with open(seg0, "wb") as f:
            f.write(b"CORRUPT" * 50000)  # 350,000 bytes > 300,000

        def handler(request: httpx.Request):
            range_hdr = request.headers.get("Range", "")
            if not range_hdr:
                return httpx.Response(200, headers={"Content-Length": str(total_size)}, content=full_data)
            if range_hdr == "bytes=0-0":
                return httpx.Response(206, headers={"Content-Range": f"bytes 0-0/{total_size}"}, content=full_data[:1])
            s, e = range_hdr[6:].split("-")
            start, end = int(s), int(e)
            return httpx.Response(
                206,
                headers={"Content-Range": f"bytes {start}-{end}/{total_size}", "Content-Length": str(end - start + 1)},
                content=full_data[start:end+1],
            )

        transport = httpx.MockTransport(handler)
        resumed_segments = [
            SegmentModel("dl_ov", 0, 0, 299999, 350000, "Queued", str(seg0)),
            SegmentModel("dl_ov", 1, 300000, 599999, 0, "Queued"),
        ]

        downloader = SegmentedDownloader(
            url="http://test.com/oversized.bin",
            destination_folder=self.temp_dir,
            connections=2,
            min_segment_size=50000,
            resumed_parts_dir=parts_dir,
            resumed_segments=resumed_segments,
            transport=transport,
        )

        final_path, total = downloader.download()
        self.assertEqual(final_path.stat().st_size, total_size)

    # 29. Interrupted merge recovery
    def test_29_interrupted_merge_recovery(self):
        total_size = 60000
        data = b"M" * total_size
        parts_dir = Path(self.temp_dir) / ".merge_rec.parts"
        parts_dir.mkdir(parents=True, exist_ok=True)

        # Both segments are 100% complete on disk
        seg0 = parts_dir / "segment_00.part"
        seg1 = parts_dir / "segment_01.part"
        with open(seg0, "wb") as f:
            f.write(data[:30000])
        with open(seg1, "wb") as f:
            f.write(data[30000:])

        # Simulate interrupted partial merge file
        merged_part = Path(self.temp_dir) / "merge_rec.bin.part"
        with open(merged_part, "wb") as f:
            f.write(data[:15000])

        def handler(request: httpx.Request):
            if request.headers.get("Range") == "bytes=0-0":
                return httpx.Response(206, headers={"Content-Range": f"bytes 0-0/{total_size}"}, content=data[:1])
            return httpx.Response(200, content=data)

        transport = httpx.MockTransport(handler)
        resumed_segments = [
            SegmentModel("dl_m", 0, 0, 29999, 30000, "Completed", str(seg0)),
            SegmentModel("dl_m", 1, 30000, 59999, 30000, "Completed", str(seg1)),
        ]

        downloader = SegmentedDownloader(
            url="http://test.com/merge_rec.bin",
            destination_folder=self.temp_dir,
            connections=2,
            resumed_parts_dir=parts_dir,
            resumed_segments=resumed_segments,
            transport=transport,
        )

        final_path, total = downloader.download()
        self.assertEqual(final_path.stat().st_size, total_size)
        with open(final_path, "rb") as f:
            self.assertEqual(f.read(), data)

    # 30. History removal deletes DB record without deleting file on disk
    def test_30_history_removal_preserves_file_on_disk(self):
        file_path = Path(self.temp_dir) / "downloaded_file.txt"
        file_path.write_text("Hello, World!")

        dl = DownloadModel(
            id="hist_1",
            url="http://test/file.txt",
            destination_folder=self.temp_dir,
            status=DownloadStatus.COMPLETED,
            final_path=str(file_path),
        )
        self.db.add_download(dl)

        qm = QueueManager(db=self.db)
        self.assertEqual(len(qm.items), 1)

        # Remove from history without delete_file flag
        qm.remove_item("hist_1", delete_file=False)

        # Verify DB record is gone
        self.assertIsNone(self.db.get_download("hist_1"))
        self.assertEqual(len(qm.items), 0)
        # Verify file STILL exists on disk!
        self.assertTrue(file_path.exists())

    # 31. Explicit file deletion removes both DB record and file on disk
    def test_31_explicit_file_deletion_removes_file_on_disk(self):
        file_path = Path(self.temp_dir) / "to_delete.txt"
        file_path.write_text("Delete me")

        dl = DownloadModel(
            id="hist_2",
            url="http://test/to_delete.txt",
            destination_folder=self.temp_dir,
            filename="to_delete.txt",
            status=DownloadStatus.COMPLETED,
            final_path=str(file_path),
        )
        self.db.add_download(dl)

        qm = QueueManager(db=self.db)
        qm.remove_item("hist_2", delete_file=True)

        self.assertIsNone(self.db.get_download("hist_2"))
        self.assertFalse(file_path.exists())

    # 32. Database concurrency & thread safety
    def test_32_database_thread_safety(self):
        errors = []

        def worker_task(thread_id: int):
            try:
                for i in range(25):
                    dl_id = f"t_{thread_id}_{i}"
                    dl = DownloadModel(id=dl_id, url=f"http://test/{dl_id}", destination_folder=self.temp_dir)
                    self.db.add_download(dl)
                    self.db.update_status(dl_id, DownloadStatus.DOWNLOADING)
                    self.db.update_progress(dl_id, downloaded_size=i * 100, total_size=2500)
                    self.db.update_status(dl_id, DownloadStatus.COMPLETED)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker_task, args=(tid,)) for tid in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Concurrent DB access raised errors: {errors}")
        all_dls = self.db.get_all_downloads()
        self.assertEqual(len(all_dls), 100)

    # 33. QueueManager pause_active() pauses active download
    def test_33_queue_manager_pause_active(self):
        data = b"P" * 100000

        def slow_handler(request: httpx.Request):
            return httpx.Response(200, headers={"Content-Length": str(len(data))}, stream=MockByteStream(data, delay=0.01))

        transport = httpx.MockTransport(slow_handler)
        qm = QueueManager(db=self.db)

        paused_ids = []
        qm.download_paused.connect(lambda item_id: paused_ids.append(item_id))

        item = qm.add_download("http://test/pause_active.bin", self.temp_dir, auto_start=True, transport=transport)

        # Wait until actively downloading
        start_wait = time.time()
        while item["status"] != "Downloading" and time.time() - start_wait < 3.0:
            self.qt_app.processEvents()
            time.sleep(0.01)

        self.assertEqual(item["status"], "Downloading")

        # Pause active
        qm.pause_active()

        # Wait for paused signal
        start_wait = time.time()
        while not paused_ids and time.time() - start_wait < 3.0:
            self.qt_app.processEvents()
            time.sleep(0.02)

        self.assertIn(item["id"], paused_ids)
        self.assertEqual(item["status"], DownloadStatus.PAUSED)
        self.assertEqual(self.db.get_download(item["id"]).status, DownloadStatus.PAUSED)

    # 34. QueueManager resume_download() resumes paused download
    def test_34_queue_manager_resume_download(self):
        data = b"R" * 20000
        transport = httpx.MockTransport(lambda req: httpx.Response(200, headers={"Content-Length": str(len(data))}, content=data))

        qm = QueueManager(db=self.db)
        item = qm.add_download("http://test/resume_me.bin", self.temp_dir, connections=1, auto_start=False, transport=transport)

        qm.pause_download(item["id"])
        self.assertEqual(item["status"], DownloadStatus.PAUSED)

        completed_ids = []
        qm.download_completed.connect(lambda i_id, p, s: completed_ids.append(i_id))

        # Resume download
        qm.resume_download(item["id"])

        start_wait = time.time()
        while not completed_ids and time.time() - start_wait < 5.0:
            self.qt_app.processEvents()
            time.sleep(0.02)

        self.assertIn(item["id"], completed_ids)
        self.assertEqual(item["status"], DownloadStatus.COMPLETED)
        self.assertEqual(self.db.get_download(item["id"]).status, DownloadStatus.COMPLETED)

    # 35. UI integration: MainWindow loads history on startup and displays in table
    def test_35_main_window_loads_history_on_startup(self):
        # Insert historical items into DB
        self.db.add_download(DownloadModel(
            id="hist_ui_1",
            url="http://test/history1.zip",
            destination_folder=self.temp_dir,
            filename="history1.zip",
            total_size=1048576,
            downloaded_size=1048576,
            status=DownloadStatus.COMPLETED,
            final_path=str(Path(self.temp_dir) / "history1.zip"),
        ))
        self.db.add_download(DownloadModel(
            id="hist_ui_2",
            url="http://test/history2.iso",
            destination_folder=self.temp_dir,
            filename="history2.iso",
            total_size=5000000,
            downloaded_size=2500000,
            status=DownloadStatus.PAUSED,
        ))

        win = MainWindow(db=self.db)
        self.assertEqual(win.table.rowCount(), 2)

        # Check filenames and statuses rendered in table
        fnames = [win.table.item(r, 0).text() for r in range(2)]
        statuses = [win.table.item(r, 1).text() for r in range(2)]

        self.assertIn("history1.zip", fnames)
        self.assertIn("history2.iso", fnames)
        self.assertIn("Completed", statuses)
        self.assertIn("Paused", statuses)

        self.assertEqual(win.metric_total.value_label.text(), "2")
        self.assertEqual(win.metric_completed.value_label.text(), "1")
        win.close()

    # 36. End-to-End Integration: Single-connection pause -> app restart -> resume -> byte-for-byte match
    def test_36_e2e_single_connection_restart(self):
        data = b"0123456789ABCDEF" * 30000  # 480,000 bytes
        total_len = len(data)

        def handler(request: httpx.Request):
            range_h = request.headers.get("Range")
            if range_h and range_h.startswith("bytes="):
                m = range_h.replace("bytes=", "").split("-")
                start = int(m[0])
                end = int(m[1]) if m[1] else total_len - 1
                body = data[start:end + 1]
                return httpx.Response(
                    206,
                    headers={
                        "Content-Range": f"bytes {start}-{end}/{total_len}",
                        "Content-Length": str(len(body)),
                        "ETag": '"single-v1"',
                    },
                    stream=MockByteStream(body, chunk_size=8192, delay=0.01),
                )
            return httpx.Response(
                200,
                headers={"Content-Length": str(total_len), "ETag": '"single-v1"'},
                stream=MockByteStream(data, chunk_size=8192, delay=0.01),
            )

        transport = httpx.MockTransport(handler)

        # Session 1: Start download, allow partial download, then pause & shutdown
        qm1 = QueueManager(db=self.db)
        item1 = qm1.add_download(
            "http://test/single_e2e.bin",
            self.temp_dir,
            connections=1,
            auto_start=True,
            transport=transport,
        )

        start_wait = time.time()
        while item1["downloaded_bytes"] < 50000 and time.time() - start_wait < 5.0:
            self.qt_app.processEvents()
            time.sleep(0.02)

        self.assertGreater(item1["downloaded_bytes"], 0)
        qm1.pause_active()

        start_wait = time.time()
        while item1["status"] != DownloadStatus.PAUSED and time.time() - start_wait < 3.0:
            self.qt_app.processEvents()
            time.sleep(0.02)

        self.assertEqual(item1["status"], DownloadStatus.PAUSED)
        part_path = Path(self.temp_dir) / "single_e2e.bin.part"
        self.assertTrue(part_path.exists())
        partial_size = part_path.stat().st_size
        self.assertGreater(partial_size, 0)
        self.assertLess(partial_size, total_len)

        # Shutdown Session 1
        qm1.shutdown()

        # Session 2: Fresh QueueManager connecting to same SQLite database
        qm2 = QueueManager(db=self.db)
        self.assertEqual(len(qm2.items), 1)
        item2 = qm2.items[0]
        self.assertEqual(item2["id"], item1["id"])
        self.assertEqual(item2["downloaded_bytes"], partial_size)

        # Assign transport to loaded item and resume
        item2["transport"] = transport
        completed_ids = []
        qm2.download_completed.connect(lambda i_id, p, s: completed_ids.append(i_id))

        qm2.resume_download(item2["id"])

        start_wait = time.time()
        while not completed_ids and time.time() - start_wait < 10.0:
            self.qt_app.processEvents()
            time.sleep(0.02)

        self.assertIn(item2["id"], completed_ids)
        self.assertEqual(item2["status"], DownloadStatus.COMPLETED)

        final_file = Path(self.temp_dir) / "single_e2e.bin"
        self.assertTrue(final_file.exists())
        self.assertEqual(final_file.stat().st_size, total_len)
        self.assertEqual(final_file.read_bytes(), data, "Resumed single-connection download must match byte-for-byte!")
        qm2.shutdown()

    # 37. End-to-End Integration: Multi-connection pause -> app restart -> reuse .parts dir -> resume -> byte-for-byte match
    def test_37_e2e_multi_connection_restart(self):
        data = b"0123456789ABCDEF" * 150000  # 2,400,000 bytes
        total_len = len(data)

        def handler(request: httpx.Request):
            range_h = request.headers.get("Range")
            if range_h == "bytes=0-0":
                return httpx.Response(
                    206,
                    headers={
                        "Content-Range": f"bytes 0-0/{total_len}",
                        "Content-Length": "1",
                        "Accept-Ranges": "bytes",
                        "ETag": '"multi-etag-v1"',
                    },
                    content=data[:1],
                )
            elif range_h and range_h.startswith("bytes="):
                m = range_h.replace("bytes=", "").split("-")
                start = int(m[0])
                end = int(m[1]) if m[1] else total_len - 1
                body = data[start:end + 1]
                return httpx.Response(
                    206,
                    headers={
                        "Content-Range": f"bytes {start}-{end}/{total_len}",
                        "Content-Length": str(len(body)),
                        "ETag": '"multi-etag-v1"',
                    },
                    stream=MockByteStream(body, chunk_size=16384, delay=0.01),
                )
            return httpx.Response(200, headers={"Content-Length": str(total_len), "ETag": '"multi-etag-v1"'}, content=data)

        transport = httpx.MockTransport(handler)

        # Session 1: Start 4-connection download, let it progress, then pause
        qm1 = QueueManager(db=self.db)
        item1 = qm1.add_download(
            "http://test/multi_e2e.bin",
            self.temp_dir,
            connections=4,
            min_segment_size=500000,
            auto_start=True,
            transport=transport,
        )

        start_wait = time.time()
        while item1["downloaded_bytes"] < 100000 and time.time() - start_wait < 5.0:
            self.qt_app.processEvents()
            time.sleep(0.02)

        self.assertGreater(item1["downloaded_bytes"], 0)
        qm1.pause_active()

        start_wait = time.time()
        while item1["status"] != DownloadStatus.PAUSED and time.time() - start_wait < 3.0:
            self.qt_app.processEvents()
            time.sleep(0.02)

        self.assertEqual(item1["status"], DownloadStatus.PAUSED)

        # Verify .parts directory was stored in DB and exists on disk
        db_dl1 = self.db.get_download(item1["id"])
        self.assertIsNotNone(db_dl1.temp_path)
        parts_dir = Path(db_dl1.temp_path)
        self.assertTrue(parts_dir.exists())
        self.assertTrue(parts_dir.is_dir())

        # Verify segment records exist with temp_path
        db_segs1 = self.db.get_segments(item1["id"])
        self.assertEqual(len(db_segs1), 4)
        for s in db_segs1:
            self.assertTrue(s.temp_path)

        qm1.shutdown()

        # Session 2: Restart application with fresh QueueManager
        qm2 = QueueManager(db=self.db)
        item2 = qm2.items[0]
        self.assertEqual(item2["temp_path"], str(parts_dir))
        self.assertEqual(len(item2["segments"]), 4)

        # Resume download in Session 2
        item2["transport"] = transport
        completed_ids = []
        qm2.download_completed.connect(lambda i_id, p, s: completed_ids.append(i_id))

        qm2.resume_download(item2["id"])

        start_wait = time.time()
        while not completed_ids and time.time() - start_wait < 15.0:
            self.qt_app.processEvents()
            time.sleep(0.02)

        self.assertIn(item2["id"], completed_ids)
        self.assertEqual(item2["status"], DownloadStatus.COMPLETED)

        final_file = Path(self.temp_dir) / "multi_e2e.bin"
        self.assertTrue(final_file.exists())
        self.assertEqual(final_file.stat().st_size, total_len)
        self.assertEqual(final_file.read_bytes(), data, "Resumed multi-connection download must match byte-for-byte!")

        # Verify DB cleaned up temp_path and segments on completion
        db_dl_final = self.db.get_download(item2["id"])
        self.assertIsNone(db_dl_final.temp_path)
        self.assertEqual(len(self.db.get_segments(item2["id"])), 0)
        qm2.shutdown()

    # 38. End-to-End Integration: Application close during download -> Interrupted status -> Restart MainWindow -> Resume
    def test_38_e2e_application_shutdown_and_restart(self):
        data = b"SHUTDOWN_TEST" * 25000
        total_len = len(data)

        def handler(request: httpx.Request):
            range_h = request.headers.get("Range")
            if range_h and range_h.startswith("bytes="):
                m = range_h.replace("bytes=", "").split("-")
                start = int(m[0])
                end = int(m[1]) if m[1] else total_len - 1
                body = data[start:end + 1]
                return httpx.Response(
                    206,
                    headers={"Content-Range": f"bytes {start}-{end}/{total_len}", "Content-Length": str(len(body))},
                    stream=MockByteStream(body, delay=0.01),
                )
            return httpx.Response(200, headers={"Content-Length": str(total_len)}, stream=MockByteStream(data, delay=0.01))

        transport = httpx.MockTransport(handler)

        # MainWindow 1: Start download, close during download
        win1 = MainWindow(db=self.db)
        item1 = win1.queue_mgr.add_download(
            "http://test/shutdown_resume.bin",
            self.temp_dir,
            connections=1,
            auto_start=True,
            transport=transport,
        )

        start_wait = time.time()
        while item1["downloaded_bytes"] < 20000 and time.time() - start_wait < 5.0:
            self.qt_app.processEvents()
            time.sleep(0.02)

        # Trigger application shutdown via MainWindow.close()
        win1.close()

        # Check DB status is Interrupted and partial file remains
        db_dl = self.db.get_download(item1["id"])
        self.assertEqual(db_dl.status, DownloadStatus.INTERRUPTED)
        part_file = Path(self.temp_dir) / "shutdown_resume.bin.part"
        self.assertTrue(part_file.exists())

        # MainWindow 2: Start fresh MainWindow with same DB
        win2 = MainWindow(db=self.db)
        self.assertEqual(win2.table.rowCount(), 1)
        self.assertEqual(win2.table.item(0, 1).text(), "Interrupted")

        # Resume download
        win2.queue_mgr.items[0]["transport"] = transport
        completed_ids = []
        win2.queue_mgr.download_completed.connect(lambda i_id, p, s: completed_ids.append(i_id))

        win2.queue_mgr.resume_download(item1["id"])

        start_wait = time.time()
        while not completed_ids and time.time() - start_wait < 10.0:
            self.qt_app.processEvents()
            time.sleep(0.02)

        self.assertIn(item1["id"], completed_ids)
        final_file = Path(self.temp_dir) / "shutdown_resume.bin"
        self.assertTrue(final_file.exists())
        self.assertEqual(final_file.read_bytes(), data)
        win2.close()

    # 39. End-to-End Integration: Validator persistence & mismatch safe recovery (fresh GET from byte 0)
    def test_39_e2e_validator_persistence_and_mismatch_recovery(self):
        v1_data = b"VERSION_1_DATA" * 15000
        v2_data = b"VERSION_2_MODIFIED_DATA" * 15000
        current_version = [1]

        def handler(request: httpx.Request):
            range_h = request.headers.get("Range")
            if current_version[0] == 1:
                return httpx.Response(
                    200,
                    headers={"Content-Length": str(len(v1_data)), "ETag": '"etag-v1"'},
                    stream=MockByteStream(v1_data, delay=0.01),
                )
            else:
                # Version 2 on server
                if range_h and range_h.startswith("bytes="):
                    # Server sends 206 with mismatched ETag
                    return httpx.Response(
                        206,
                        headers={
                            "Content-Range": f"bytes 5000-{len(v2_data) - 1}/{len(v2_data)}",
                            "Content-Length": str(len(v2_data) - 5000),
                            "ETag": '"etag-v2"',
                        },
                        content=v2_data[5000:],
                    )
                return httpx.Response(
                    200,
                    headers={"Content-Length": str(len(v2_data)), "ETag": '"etag-v2"'},
                    content=v2_data,
                )

        transport = httpx.MockTransport(handler)

        qm = QueueManager(db=self.db)
        item = qm.add_download(
            "http://test/validator_test.bin",
            self.temp_dir,
            connections=1,
            auto_start=True,
            transport=transport,
        )

        start_wait = time.time()
        while item["downloaded_bytes"] < 10000 and time.time() - start_wait < 5.0:
            self.qt_app.processEvents()
            time.sleep(0.02)

        qm.pause_active()

        start_wait = time.time()
        while item["status"] != DownloadStatus.PAUSED and time.time() - start_wait < 3.0:
            self.qt_app.processEvents()
            time.sleep(0.02)

        self.assertEqual(self.db.get_download(item["id"]).etag, '"etag-v1"')

        # Server changes file behind the scenes!
        current_version[0] = 2

        completed_ids = []
        qm.download_completed.connect(lambda i_id, p, s: completed_ids.append(i_id))

        # Resume: client detects ETag changed, safely restarts from byte 0, and writes v2_data
        qm.resume_download(item["id"])

        start_wait = time.time()
        while not completed_ids and time.time() - start_wait < 10.0:
            self.qt_app.processEvents()
            time.sleep(0.02)

        self.assertIn(item["id"], completed_ids)
        final_file = Path(self.temp_dir) / "validator_test.bin"
        self.assertEqual(final_file.read_bytes(), v2_data, "When validator mismatches, must restart from byte 0 and download fresh file!")
        qm.shutdown()

    # 40. Single-connection strict 206 Content-Range validation failure restarts cleanly from byte 0
    def test_40_single_connection_strict_206_validation_failure(self):
        good_data = b"STRICT_RANGE_TEST" * 2000
        total_len = len(good_data)
        req_count = [0]

        def handler(request: httpx.Request):
            req_count[0] += 1
            range_h = request.headers.get("Range")
            if range_h:
                # Return malformed Content-Range (start byte does not match requested range)
                return httpx.Response(
                    206,
                    headers={"Content-Range": f"bytes 0-999/{total_len}", "Content-Length": "1000"},
                    content=good_data[:1000],
                )
            # Fresh GET without Range returns full content
            return httpx.Response(200, headers={"Content-Length": str(total_len)}, content=good_data)

        # Create a partial file with 500 bytes
        part_file = Path(self.temp_dir) / "strict_range.bin.part"
        part_file.write_bytes(good_data[:500])

        downloader = HttpDownloader(
            "http://test/strict_range.bin",
            self.temp_dir,
            allow_resume=True,
            resumed_part_path=part_file,
            transport=httpx.MockTransport(handler),
        )

        final_path, size = downloader.download()
        self.assertEqual(size, total_len)
        self.assertEqual(final_path.read_bytes(), good_data, "Invalid 206 Content-Range must discard stale partial data and download cleanly from byte 0!")

    # 41. Persistence callback receives all expected arguments (downloaded_bytes, speed, eta)
    def test_41_db_persist_callback_receives_expected_arguments(self):
        data = b"PERSIST_CB_TEST" * 2000
        total_len = len(data)
        callback_calls = []

        def persist_cb(downloaded_bytes, speed, eta):
            callback_calls.append((downloaded_bytes, speed, eta))

        def handler(request: httpx.Request):
            return httpx.Response(200, headers={"Content-Length": str(total_len)}, content=data)

        downloader = HttpDownloader(
            "http://test/persist_cb.bin",
            self.temp_dir,
            on_db_persist=persist_cb,
            chunk_size=1024,
            transport=httpx.MockTransport(handler),
        )

        final_path, size = downloader.download()
        self.assertEqual(size, total_len)
        self.assertTrue(final_path.exists())

        # Directly invoke callback with 3 arguments as HttpDownloader does
        persist_cb(5000, 10240.0, 2.5)
        self.assertGreater(len(callback_calls), 0)
        last_call = callback_calls[-1]
        self.assertEqual(last_call[0], 5000)
        self.assertEqual(last_call[1], 10240.0)
        self.assertEqual(last_call[2], 2.5)

    # 42. QueueManager persistence callback handles arguments, throttling, and exception safety
    def test_42_db_progress_persistence_throttled_and_safe(self):
        db_file = Path(self.temp_dir) / "persist_test.db"
        db = DatabaseManager(db_path=db_file)
        qm = QueueManager(db=db)

        item = qm.add_download(
            url="http://example.com/test_persist.bin",
            folder=self.temp_dir,
            explicit_filename="test_persist.bin",
            auto_start=False,
        )
        item_id = item["id"]

        cb = qm._make_db_persist_callback(item_id)

        # Call with 3 arguments: downloaded_bytes, speed, eta
        cb(4096, 2048.0, 15.0, force=True)

        row = qm.db.get_download(item_id)
        self.assertIsNotNone(row)
        self.assertEqual(row.downloaded_size, 4096)
        self.assertEqual(row.speed, 2048.0)
        self.assertEqual(row.eta, 15.0)

        # Test throttling: immediate call without force should be skipped
        cb(8192, 4096.0, 10.0, force=False)
        row_throttled = qm.db.get_download(item_id)
        self.assertEqual(row_throttled.downloaded_size, 4096, "Immediate call within 1s should be throttled!")

        # Test call with 0 arguments (backwards compatibility)
        item["downloaded_bytes"] = 12000
        cb(force=True)
        row_zero_arg = qm.db.get_download(item_id)
        self.assertEqual(row_zero_arg.downloaded_size, 12000)

        # Test exception safety: even if db raises, callback must not throw
        with unittest.mock.patch.object(qm.db, "update_progress", side_effect=RuntimeError("DB Lock error")):
            cb(16000, 1000.0, 5.0, force=True)

        qm.shutdown()

    # 43. Single-connection invalid Content-Range end (end != total - 1) restarts from byte 0
    def test_43_single_connection_invalid_content_range_end_restarts(self):
        full_content = b"VALID_END_TEST_PAYLOAD" * 500
        total_len = len(full_content)
        existing_size = 400
        requests_made = []

        def handler(request: httpx.Request):
            requests_made.append(request.headers.get("Range"))
            if request.headers.get("Range"):
                # Bad Content-Range end: reports end = 600 instead of total_len - 1
                return httpx.Response(
                    206,
                    headers={
                        "Content-Range": f"bytes {existing_size}-600/{total_len}",
                        "Content-Length": "201",
                    },
                    content=full_content[existing_size:601],
                )
            # Fresh GET
            return httpx.Response(200, headers={"Content-Length": str(total_len)}, content=full_content)

        part_file = Path(self.temp_dir) / "bad_end.bin.part"
        part_file.write_bytes(full_content[:existing_size])

        downloader = HttpDownloader(
            "http://test/bad_end.bin",
            self.temp_dir,
            allow_resume=True,
            resumed_part_path=part_file,
            transport=httpx.MockTransport(handler),
        )

        final_path, size = downloader.download()
        self.assertEqual(size, total_len)
        self.assertEqual(final_path.read_bytes(), full_content)
        # Should have attempted Range resume first, then made fresh GET without Range
        self.assertEqual(len(requests_made), 2)
        self.assertEqual(requests_made[0], f"bytes={existing_size}-")
        self.assertIsNone(requests_made[1])

    # 44. Single-connection incorrect total file size restarts from byte 0
    def test_44_single_connection_incorrect_total_size_restarts(self):
        full_content = b"INCORRECT_TOTAL_TEST" * 500
        total_len = len(full_content)
        existing_size = 300
        requests_made = []

        def handler(request: httpx.Request):
            requests_made.append(request.headers.get("Range"))
            if request.headers.get("Range"):
                # Mismatched total size: reports total_len + 5000
                bad_total = total_len + 5000
                return httpx.Response(
                    206,
                    headers={
                        "Content-Range": f"bytes {existing_size}-{bad_total - 1}/{bad_total}",
                        "Content-Length": str(bad_total - existing_size),
                    },
                    content=full_content[existing_size:],
                )
            return httpx.Response(200, headers={"Content-Length": str(total_len)}, content=full_content)

        part_file = Path(self.temp_dir) / "bad_total.bin.part"
        part_file.write_bytes(full_content[:existing_size])

        downloader = HttpDownloader(
            "http://test/bad_total.bin",
            self.temp_dir,
            allow_resume=True,
            resumed_part_path=part_file,
            transport=httpx.MockTransport(handler),
        )
        downloader.total_bytes = total_len  # Expected total known in advance

        final_path, size = downloader.download()
        self.assertEqual(size, total_len)
        self.assertEqual(final_path.read_bytes(), full_content)
        self.assertEqual(len(requests_made), 2)
        self.assertIsNone(requests_made[1])

    # 45. Single-connection incorrect remaining Content-Length restarts from byte 0
    def test_45_single_connection_incorrect_content_length_restarts(self):
        full_content = b"CONTENT_LENGTH_MISMATCH_TEST" * 500
        total_len = len(full_content)
        existing_size = 250
        requests_made = []

        def handler(request: httpx.Request):
            requests_made.append(request.headers.get("Range"))
            if request.headers.get("Range"):
                # Content-Length is wrong (e.g. 99999 instead of total_len - existing_size)
                return httpx.Response(
                    206,
                    headers={
                        "Content-Range": f"bytes {existing_size}-{total_len - 1}/{total_len}",
                        "Content-Length": "99999",
                    },
                    content=full_content[existing_size:],
                )
            return httpx.Response(200, headers={"Content-Length": str(total_len)}, content=full_content)

        part_file = Path(self.temp_dir) / "bad_cl.bin.part"
        part_file.write_bytes(full_content[:existing_size])

        downloader = HttpDownloader(
            "http://test/bad_cl.bin",
            self.temp_dir,
            allow_resume=True,
            resumed_part_path=part_file,
            transport=httpx.MockTransport(handler),
        )

        final_path, size = downloader.download()
        self.assertEqual(size, total_len)
        self.assertEqual(final_path.read_bytes(), full_content)
        self.assertEqual(len(requests_made), 2)
        self.assertIsNone(requests_made[1])

    # 46. Single-connection successful valid resume appends and completes byte-for-byte
    def test_46_single_connection_successful_valid_resume(self):
        full_content = b"VALID_RESUME_SUCCESS_PAYLOAD_" * 600
        total_len = len(full_content)
        existing_size = 800
        expected_remaining = total_len - existing_size
        requests_made = []

        def handler(request: httpx.Request):
            requests_made.append(request.headers.get("Range"))
            if request.headers.get("Range") == f"bytes={existing_size}-":
                return httpx.Response(
                    206,
                    headers={
                        "Content-Range": f"bytes {existing_size}-{total_len - 1}/{total_len}",
                        "Content-Length": str(expected_remaining),
                        "ETag": '"resume-etag-123"',
                    },
                    content=full_content[existing_size:],
                )
            return httpx.Response(200, headers={"Content-Length": str(total_len)}, content=full_content)

        part_file = Path(self.temp_dir) / "valid_resume.bin.part"
        part_file.write_bytes(full_content[:existing_size])

        downloader = HttpDownloader(
            "http://test/valid_resume.bin",
            self.temp_dir,
            etag='"resume-etag-123"',
            allow_resume=True,
            resumed_part_path=part_file,
            transport=httpx.MockTransport(handler),
        )

        final_path, size = downloader.download()
        self.assertEqual(size, total_len)
        self.assertEqual(final_path.read_bytes(), full_content)
        # Should have made exactly 1 request because resume succeeded on first try
        self.assertEqual(len(requests_made), 1)
        self.assertEqual(requests_made[0], f"bytes={existing_size}-")

    # 47. Single-connection malformed Content-Range where ending byte < starting byte and Content-Length is absent
    def test_47_single_connection_content_range_end_less_than_start_no_content_length(self):
        full_content = b"END_LESS_THAN_START_PAYLOAD" * 300
        total_len = len(full_content)
        existing_size = 500
        requests_made = []

        def handler(request: httpx.Request):
            requests_made.append(request.headers.get("Range"))
            if request.headers.get("Range"):
                # Malformed Content-Range: ending byte (400) is smaller than starting byte (500)
                # and Content-Length header is completely absent!
                return httpx.Response(
                    206,
                    headers={
                        "Content-Range": f"bytes {existing_size}-400/{total_len}",
                    },
                    content=full_content[existing_size:],
                )
            # Fresh GET without Range returns full content with 200 OK
            return httpx.Response(200, headers={"Content-Length": str(total_len)}, content=full_content)

        part_file = Path(self.temp_dir) / "end_lt_start.bin.part"
        part_file.write_bytes(full_content[:existing_size])

        downloader = HttpDownloader(
            "http://test/end_lt_start.bin",
            self.temp_dir,
            allow_resume=True,
            resumed_part_path=part_file,
            transport=httpx.MockTransport(handler),
        )

        final_path, size = downloader.download()
        self.assertEqual(size, total_len)
        self.assertEqual(final_path.read_bytes(), full_content)
        # Should have attempted Range resume first, detected cr_end < cr_start, discarded partial data, and made fresh GET
        self.assertEqual(len(requests_made), 2)
        self.assertEqual(requests_made[0], f"bytes={existing_size}-")
        self.assertIsNone(requests_made[1])


if __name__ == "__main__":
    unittest.main()
