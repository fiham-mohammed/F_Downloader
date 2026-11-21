"""
Stage 5 Automated Test Suite for My Download Manager.
Tests for:
1. Global and individual bandwidth rate limiter (token bucket).
2. Advanced download scheduler (QTimer, start/stop, overdue, timezones).
3. Advanced queue management (concurrency 1-5, priority ordering, FIFO, queue controls).
4. Automatic retry scheduling (transient vs permanent, backoff, Retry-After, persistence, cancel/manual).
5. Category classification (extensions, manual override, persistence).
6. SQLite database v1 -> v2 migrations and persistent settings.
"""

import os
import sys
import time
import shutil
import tempfile
import sqlite3
import threading
import ssl
import email.utils
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock
import re

import unittest
import httpx

# Prevent PySide6 Windows access violation on interpreter module teardown (0xC0000005)
_orig_exit = sys.exit
def _clean_exit(code=0):
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    val = code if (isinstance(code, int) and not isinstance(code, bool)) else (1 if code else 0)
    os._exit(val)
sys.exit = _clean_exit
from PySide6.QtWidgets import QApplication, QDialog
from PySide6.QtCore import QCoreApplication

from mdm.models import DownloadModel, SegmentModel, DownloadStatus, DownloadPriority
from mdm.database import DatabaseManager, CURRENT_SCHEMA_VERSION
from mdm.bandwidth_controller import BandwidthController, TokenBucket
from mdm.scheduler import DownloadScheduler, parse_iso_datetime, normalize_to_aware_datetime
from mdm.queue_manager import QueueManager, is_retryable_failure, parse_retry_after
from mdm.categories import get_category_for_filename, ALL_CATEGORIES
from mdm.downloader import (
    HttpDownloader,
    DownloadError,
    DownloadCancelledError,
    DownloadPausedError,
    INTERNAL_RETRY_AFTER_MAX_SECONDS,
)
from mdm.segment_manager import SegmentedDownloader
from mdm.main_window import MainWindow, DownloadDetailsDialog
from mdm.settings import (
    get_max_simultaneous_downloads, set_max_simultaneous_downloads,
    get_global_speed_limit, set_global_speed_limit,
    is_global_speed_limit_enabled, set_global_speed_limit_enabled,
    get_default_priority, set_default_priority,
    is_auto_retry_enabled, set_auto_retry_enabled,
    get_max_retry_attempts, set_max_retry_attempts,
    get_default_individual_speed_limit, set_default_individual_speed_limit,
    app_settings, Settings,
)


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


class TestStage5AdvancedManagement(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt_app = QApplication.instance()
        if not cls.qt_app:
            cls.qt_app = QApplication(sys.argv)

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "qt_app") and cls.qt_app:
            cls.qt_app.processEvents()
            cls.qt_app.sendPostedEvents()
        cls.qt_app = None

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="mdm_stage5_test_")
        self.db_path = Path(self.temp_dir) / "test_stage5.db"
        self.db = DatabaseManager(self.db_path)
        self.qms = []

    def make_qm(self, **kwargs):
        if "db" not in kwargs:
            kwargs["db"] = self.db
        qm = QueueManager(**kwargs)
        self.qms.append(qm)
        return qm

    def tearDown(self):
        if hasattr(self, "qms"):
            for qm in self.qms:
                try:
                    qm.shutdown(timeout_ms=500)
                except Exception:
                    pass
            self.qms.clear()
        if hasattr(self, "qt_app") and self.qt_app:
            self.qt_app.processEvents()
        if hasattr(self, "db") and self.db:
            self.db.close()
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)
        if hasattr(self, "qt_app") and self.qt_app:
            self.qt_app.processEvents()

    # =========================================================================
    # 1. BANDWIDTH CONTROLLER & SPEED LIMITER TESTS (10 tests)
    # =========================================================================

    def test_01_global_speed_limiter_accuracy(self):
        """Global rate limiter limits consumption within expected tolerance."""
        target_bps = 51200
        ctrl = BandwidthController(global_limit=target_bps, global_enabled=True)
        # Drain initial burst capacity
        ctrl.global_bucket.tokens = 0.0

        start = time.time()
        for _ in range(4):
            ctrl.acquire(10240)
        elapsed = time.time() - start

        self.assertGreaterEqual(elapsed, 0.70, "Limiter allowed tokens too quickly")

    def test_02_individual_speed_limiter_accuracy(self):
        """Per-download speed limit applies when global limit is higher."""
        ctrl = BandwidthController(global_limit=500000, global_enabled=True)
        ctrl.set_individual_limit("dl_1", 20480)
        ctrl.item_buckets["dl_1"].tokens = 0.0

        start = time.time()
        for _ in range(4):
            ctrl.acquire(5120, item_id="dl_1")
        elapsed = time.time() - start

        self.assertGreaterEqual(elapsed, 0.70, "Individual limiter did not throttle")

    def test_03_combined_global_and_individual_limits(self):
        """When both global and individual limits are active, the more restrictive limit wins."""
        ctrl = BandwidthController(global_limit=20480, global_enabled=True)
        ctrl.set_individual_limit("dl_1", 100000)
        ctrl.global_bucket.tokens = 0.0

        start = time.time()
        for _ in range(4):
            ctrl.acquire(5120, item_id="dl_1")
        elapsed = time.time() - start

        self.assertGreaterEqual(elapsed, 0.70, "Global limit should have constrained individual download")

    def test_04_multi_segment_bandwidth_sharing(self):
        """Multiple threads (simulating segments) collectively respect the download limit."""
        ctrl = BandwidthController(global_limit=0, global_enabled=False)
        ctrl.set_individual_limit("seg_dl", 40960)  # 40 KB/s collective
        ctrl.item_buckets["seg_dl"].tokens = 0.0

        bytes_acquired = [0, 0]

        def segment_worker(idx):
            for _ in range(4):
                ctrl.acquire(5120, item_id="seg_dl")
                bytes_acquired[idx] += 5120

        start = time.time()
        t1 = threading.Thread(target=segment_worker, args=(0,))
        t2 = threading.Thread(target=segment_worker, args=(1,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        elapsed = time.time() - start

        total_acquired = sum(bytes_acquired)
        self.assertEqual(total_acquired, 40960)
        self.assertGreaterEqual(elapsed, 0.70, "Parallel segments exceeded collective download rate")

    def test_05_multi_download_bandwidth_sharing(self):
        """Multiple active downloads collectively share the global bandwidth pool."""
        ctrl = BandwidthController(global_limit=40960, global_enabled=True)
        ctrl.global_bucket.tokens = 0.0

        bytes_acquired = [0, 0]

        def download_worker(idx, item_id):
            for _ in range(4):
                ctrl.acquire(5120, item_id=item_id)
                bytes_acquired[idx] += 5120

        start = time.time()
        t1 = threading.Thread(target=download_worker, args=(0, "dl_a"))
        t2 = threading.Thread(target=download_worker, args=(1, "dl_b"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        elapsed = time.time() - start

        self.assertEqual(sum(bytes_acquired), 40960)
        self.assertGreaterEqual(elapsed, 0.70, "Multiple downloads exceeded global bandwidth limit")

    def test_06_dynamic_speed_limit_adjustment(self):
        """Updating limit dynamically adjusts token consumption rate immediately."""
        ctrl = BandwidthController(global_limit=100000, global_enabled=True)
        self.assertEqual(ctrl.global_limit, 100000)

        # Dynamic update
        ctrl.set_global_limit(20000)
        self.assertEqual(ctrl.global_limit, 20000)

        # Enable/disable toggle
        ctrl.set_global_enabled(False)
        self.assertFalse(ctrl.global_enabled)

    def test_07_unlimited_speed_mode(self):
        """When speed limiting is disabled, acquire returns with near-zero latency."""
        ctrl = BandwidthController(global_limit=0, global_enabled=False)

        start = time.time()
        for _ in range(50):
            ctrl.acquire(65536)
        elapsed = time.time() - start

        self.assertLess(elapsed, 0.2, "Unlimited mode introduced unnecessary delay")

    def test_08_speed_limiter_responsive_to_cancellation_during_wait(self):
        """Cancellation during limiter wait raises DownloadCancelledError immediately."""
        ctrl = BandwidthController(global_limit=1000, global_enabled=True)
        ctrl.global_bucket.tokens = 0.0

        with self.assertRaises(DownloadCancelledError):
            ctrl.acquire(50000, is_cancelled=lambda: True)

    def test_09_speed_limiter_responsive_to_pause_during_wait(self):
        """Pause during limiter wait raises DownloadPausedError immediately."""
        ctrl = BandwidthController(global_limit=1000, global_enabled=True)
        ctrl.global_bucket.tokens = 0.0

        with self.assertRaises(DownloadPausedError):
            ctrl.acquire(50000, is_paused=lambda: True)

    def test_10_speed_limiter_gui_responsiveness(self):
        """Limiter slice wait adapts chunk size for responsiveness."""
        ctrl = BandwidthController(global_limit=10000, global_enabled=True)
        chunk = ctrl.get_effective_chunk_size("dl_gui", 65536)
        self.assertLessEqual(chunk, 10000)
        self.assertGreaterEqual(chunk, 1024)

    # =========================================================================
    # 2. DOWNLOAD SCHEDULER TESTS (8 tests)
    # =========================================================================

    def test_11_scheduled_download_starts_at_specified_future_time(self):
        """Queued download with future schedule is skipped until the time arrives."""
        qm = self.make_qm()
        qm.is_auto_queue_running = True

        future_iso = (datetime.now().astimezone() + timedelta(hours=2)).isoformat()
        item = qm.add_download(
            "http://test.com/future.bin",
            self.temp_dir,
            auto_start=False,
            scheduled_start_at=future_iso,
            schedule_enabled=True,
        )

        qm.start_next()
        self.assertEqual(len(qm.active_workers), 0, "Future scheduled download should not start immediately")
        self.assertEqual(item["status"], DownloadStatus.QUEUED)

    def test_12_scheduled_download_starts_immediately_if_due(self):
        """Queued download with due or overdue schedule is selected to start."""
        qm = self.make_qm()
        past_iso = (datetime.now().astimezone() - timedelta(minutes=5)).isoformat()

        transport = httpx.MockTransport(lambda req: httpx.Response(200, headers={"Content-Length": "11"}, stream=MockByteStream(b"due content", delay=0.05)))
        item = qm.add_download(
            "http://test.com/due.bin",
            self.temp_dir,
            auto_start=False,
            scheduled_start_at=past_iso,
            schedule_enabled=True,
            transport=transport,
        )

        qm.start_next()
        self.assertTrue(item["id"] in qm.active_workers or item["status"] in (DownloadStatus.DOWNLOADING, DownloadStatus.COMPLETED))
        qm.shutdown(timeout_ms=1000)

    def test_13_overdue_scheduled_download_starts_on_app_restart(self):
        """Overdue scheduled downloads are recognized on database load and restart."""
        past_iso = (datetime.now().astimezone() - timedelta(hours=1)).isoformat()
        dl = DownloadModel(
            id="overdue_1",
            url="http://test/overdue.bin",
            destination_folder=self.temp_dir,
            status=DownloadStatus.QUEUED,
            scheduled_start_at=past_iso,
            schedule_enabled=1,
        )
        self.db.add_download(dl)

        qm = self.make_qm()
        item = qm.get_item("overdue_1")
        self.assertIsNotNone(item)
        self.assertTrue(item["schedule_enabled"])
        self.assertEqual(item["scheduled_start_at"], past_iso)

    def test_14_scheduled_stop_pauses_active_download_safely(self):
        """Active download reaching scheduled_stop_at is automatically paused."""
        data = b"X" * 100000
        transport = httpx.MockTransport(lambda req: httpx.Response(200, headers={"Content-Length": str(len(data))}, stream=MockByteStream(data, delay=0.01)))

        past_stop = (datetime.now().astimezone() - timedelta(seconds=1)).isoformat()
        qm = self.make_qm()
        item = qm.add_download(
            "http://test/stop_me.bin",
            self.temp_dir,
            auto_start=True,
            scheduled_stop_at=past_stop,
            schedule_enabled=True,
            transport=transport,
        )

        start_wait = time.time()
        while item["status"] != DownloadStatus.DOWNLOADING and time.time() - start_wait < 3.0:
            self.qt_app.processEvents()
            time.sleep(0.02)

        # Trigger scheduler check
        now = datetime.now().astimezone()
        qm.scheduler.check_schedules(now)

        start_wait = time.time()
        while item["status"] != DownloadStatus.PAUSED and time.time() - start_wait < 3.0:
            self.qt_app.processEvents()
            time.sleep(0.02)

        self.assertEqual(item["status"], DownloadStatus.PAUSED)
        qm.shutdown(timeout_ms=1000)

    def test_15_schedule_persists_across_restart(self):
        """Schedule metadata persists accurately into SQLite schema version 2."""
        start_time = "2026-11-20T10:00:00"
        stop_time = "2026-11-20T12:00:00"

        qm1 = self.make_qm()
        item = qm1.add_download(
            "http://test/sched_persist.bin",
            self.temp_dir,
            auto_start=False,
            scheduled_start_at=start_time,
            scheduled_stop_at=stop_time,
            schedule_enabled=True,
        )
        item_id = item["id"]

        # Simulate restart
        qm2 = self.make_qm()
        restored = qm2.get_item(item_id)
        self.assertIsNotNone(restored)
        self.assertTrue(restored["schedule_enabled"])
        self.assertEqual(restored["scheduled_start_at"], start_time)
        self.assertEqual(restored["scheduled_stop_at"], stop_time)

    def test_16_invalid_schedule_handling(self):
        """Malformed or unparseable timestamps do not crash the scheduler."""
        dt = parse_iso_datetime("not-a-valid-date")
        self.assertIsNone(dt)

        dt_none = parse_iso_datetime(None)
        self.assertIsNone(dt_none)

        dt_valid = parse_iso_datetime("2026-11-19T08:00:00+00:00")
        self.assertIsNotNone(dt_valid)

    def test_17_no_duplicate_triggers(self):
        """Scheduler does not attempt to restart an already active download."""
        qm = self.make_qm()
        past_iso = (datetime.now().astimezone() - timedelta(minutes=10)).isoformat()

        data = b"A" * 50000
        transport = httpx.MockTransport(lambda req: httpx.Response(200, headers={"Content-Length": str(len(data))}, stream=MockByteStream(data, delay=0.01)))

        item = qm.add_download(
            "http://test/no_dup.bin",
            self.temp_dir,
            auto_start=True,
            scheduled_start_at=past_iso,
            schedule_enabled=True,
            transport=transport,
        )

        start_wait = time.time()
        while item["status"] != DownloadStatus.DOWNLOADING and time.time() - start_wait < 3.0:
            self.qt_app.processEvents()
            time.sleep(0.02)

        # Run scheduler multiple times
        qm.scheduler.check_schedules(datetime.now().astimezone())
        qm.scheduler.check_schedules(datetime.now().astimezone())

        self.assertEqual(len(qm.active_workers), 1, "Duplicate workers must never be spawned")
        qm.shutdown(timeout_ms=1000)

    def test_18_scheduled_downloads_respect_queue_limits(self):
        """When scheduled downloads trigger, they respect max_simultaneous_downloads."""
        qm = self.make_qm()
        qm.max_simultaneous_downloads = 1

        past_iso = (datetime.now().astimezone() - timedelta(minutes=5)).isoformat()
        data = b"M" * 50000
        transport = httpx.MockTransport(lambda req: httpx.Response(200, headers={"Content-Length": str(len(data))}, stream=MockByteStream(data, delay=0.01)))

        item1 = qm.add_download("http://test/limit1.bin", self.temp_dir, auto_start=False, scheduled_start_at=past_iso, schedule_enabled=True, transport=transport)
        item2 = qm.add_download("http://test/limit2.bin", self.temp_dir, auto_start=False, scheduled_start_at=past_iso, schedule_enabled=True, transport=transport)

        qm.scheduler.check_schedules(datetime.now().astimezone())
        self.assertEqual(len(qm.active_workers), 1, "Scheduled downloads must respect simultaneous limit")
        qm.shutdown(timeout_ms=1000)

    # =========================================================================
    # 3. ADVANCED QUEUE MANAGEMENT TESTS (11 tests)
    # =========================================================================

    def test_19_queue_respects_max_simultaneous_1(self):
        """With limit 1, exactly one download runs at a time."""
        qm = self.make_qm()
        qm.max_simultaneous_downloads = 1

        data = b"Q" * 50000
        transport = httpx.MockTransport(lambda req: httpx.Response(200, headers={"Content-Length": str(len(data))}, stream=MockByteStream(data, delay=0.01)))

        i1 = qm.add_download("http://test/one1.bin", self.temp_dir, auto_start=False, transport=transport)
        i2 = qm.add_download("http://test/one2.bin", self.temp_dir, auto_start=False, transport=transport)

        qm.start_all()
        self.assertEqual(len(qm.active_workers), 1)
        qm.shutdown(timeout_ms=1000)

    def test_20_queue_respects_max_simultaneous_2(self):
        """With limit 2 (default), up to two distinct downloads run concurrently."""
        qm = self.make_qm()
        qm.max_simultaneous_downloads = 2

        data = b"Q" * 50000
        transport = httpx.MockTransport(lambda req: httpx.Response(200, headers={"Content-Length": str(len(data))}, stream=MockByteStream(data, delay=0.01)))

        i1 = qm.add_download("http://test/two1.bin", self.temp_dir, auto_start=False, transport=transport)
        i2 = qm.add_download("http://test/two2.bin", self.temp_dir, auto_start=False, transport=transport)
        i3 = qm.add_download("http://test/two3.bin", self.temp_dir, auto_start=False, transport=transport)

        qm.start_all()
        self.assertEqual(len(qm.active_workers), 2)
        qm.shutdown(timeout_ms=1000)

    def test_21_queue_respects_max_simultaneous_4(self):
        """With limit 4, four distinct downloads run concurrently."""
        qm = self.make_qm()
        qm.max_simultaneous_downloads = 4

        data = b"Q" * 50000
        transport = httpx.MockTransport(lambda req: httpx.Response(200, headers={"Content-Length": str(len(data))}, stream=MockByteStream(data, delay=0.01)))

        for idx in range(5):
            qm.add_download(f"http://test/four_{idx}.bin", self.temp_dir, auto_start=False, transport=transport)

        qm.start_all()
        self.assertEqual(len(qm.active_workers), 4)
        qm.shutdown(timeout_ms=1000)

    def test_22_priority_ordering_high_before_normal_before_low(self):
        """High priority downloads start before Normal, and Normal before Low."""
        qm = self.make_qm()
        qm.max_simultaneous_downloads = 1

        data = b"P" * 1000
        transport = httpx.MockTransport(lambda req: httpx.Response(200, headers={"Content-Length": str(len(data))}, content=data))

        # Add Low first, then Normal, then High
        low = qm.add_download("http://test/low.bin", self.temp_dir, priority=DownloadPriority.LOW, auto_start=False, transport=transport)
        norm = qm.add_download("http://test/norm.bin", self.temp_dir, priority=DownloadPriority.NORMAL, auto_start=False, transport=transport)
        high = qm.add_download("http://test/high.bin", self.temp_dir, priority=DownloadPriority.HIGH, auto_start=False, transport=transport)

        completed_order = []
        qm.download_completed.connect(lambda i_id, p, s: completed_order.append(i_id))

        qm.start_all()

        start_wait = time.time()
        while len(completed_order) < 3 and time.time() - start_wait < 5.0:
            self.qt_app.processEvents()
            time.sleep(0.02)

        self.assertEqual(completed_order, [high["id"], norm["id"], low["id"]], "Downloads must execute in High -> Normal -> Low order")

    def test_23_fifo_ordering_within_same_priority(self):
        """Downloads with equal priority are dispatched FIFO based on queue_order."""
        qm = self.make_qm()
        qm.max_simultaneous_downloads = 1

        data = b"F" * 1000
        transport = httpx.MockTransport(lambda req: httpx.Response(200, headers={"Content-Length": str(len(data))}, content=data))

        first = qm.add_download("http://test/fifo1.bin", self.temp_dir, priority=DownloadPriority.NORMAL, auto_start=False, transport=transport)
        second = qm.add_download("http://test/fifo2.bin", self.temp_dir, priority=DownloadPriority.NORMAL, auto_start=False, transport=transport)

        completed = []
        qm.download_completed.connect(lambda i_id, p, s: completed.append(i_id))

        qm.start_all()

        start_wait = time.time()
        while len(completed) < 2 and time.time() - start_wait < 5.0:
            self.qt_app.processEvents()
            time.sleep(0.02)

        self.assertEqual(completed, [first["id"], second["id"]])

    def test_24_dynamic_priority_change_reorders_queue(self):
        """Changing an item's priority re-prioritizes it for the next dispatch."""
        qm = self.make_qm()
        qm.max_simultaneous_downloads = 1

        i1 = qm.add_download("http://test/dyn1.bin", self.temp_dir, priority=DownloadPriority.LOW, auto_start=False)
        i2 = qm.add_download("http://test/dyn2.bin", self.temp_dir, priority=DownloadPriority.LOW, auto_start=False)

        # Elevate second item to High
        qm.set_priority(i2["id"], DownloadPriority.HIGH)
        self.assertEqual(i2["priority"], DownloadPriority.HIGH)

        # DB must also reflect priority
        if qm.db:
            db_item = qm.db.get_download(i2["id"])
            self.assertEqual(db_item.priority, DownloadPriority.HIGH)

    def test_25_manual_queue_reordering_move_up_and_down(self):
        """move_up and move_down reorder queue items and persist updated queue_order."""
        qm = self.make_qm()
        i1 = qm.add_download("http://test/item1.bin", self.temp_dir, auto_start=False)
        i2 = qm.add_download("http://test/item2.bin", self.temp_dir, auto_start=False)

        self.assertEqual(qm.items[0]["id"], i1["id"])
        self.assertEqual(qm.items[1]["id"], i2["id"])

        # Move item2 up
        qm.move_up(i2["id"])
        self.assertEqual(qm.items[0]["id"], i2["id"])
        self.assertEqual(qm.items[1]["id"], i1["id"])

        # Move item2 down
        qm.move_down(i2["id"])
        self.assertEqual(qm.items[0]["id"], i1["id"])
        self.assertEqual(qm.items[1]["id"], i2["id"])

    def test_26_queue_order_persists_across_restart(self):
        """Manual queue order persists across application restart."""
        qm1 = self.make_qm()
        i1 = qm1.add_download("http://test/pers1.bin", self.temp_dir, auto_start=False)
        i2 = qm1.add_download("http://test/pers2.bin", self.temp_dir, auto_start=False)

        qm1.move_up(i2["id"])

        # Restart
        qm2 = self.make_qm()
        self.assertEqual(qm2.items[0]["id"], i2["id"])
        self.assertEqual(qm2.items[1]["id"], i1["id"])

    def test_27_start_all_and_pause_all(self):
        """start_all starts eligible queued downloads; pause_all pauses active and queued."""
        qm = self.make_qm()
        qm.max_simultaneous_downloads = 2

        data = b"Z" * 100000
        transport = httpx.MockTransport(lambda req: httpx.Response(200, headers={"Content-Length": str(len(data))}, stream=MockByteStream(data, delay=0.01)))

        i1 = qm.add_download("http://test/ctrl1.bin", self.temp_dir, auto_start=False, transport=transport)
        i2 = qm.add_download("http://test/ctrl2.bin", self.temp_dir, auto_start=False, transport=transport)
        i3 = qm.add_download("http://test/ctrl3.bin", self.temp_dir, auto_start=False, transport=transport)

        qm.start_all()
        self.assertEqual(len(qm.active_workers), 2)

        qm.pause_all()
        self.assertFalse(qm.is_auto_queue_running)
        self.assertEqual(i3["status"], DownloadStatus.PAUSED)
        qm.shutdown(timeout_ms=1000)

    def test_28_resume_all_and_stop_queue(self):
        """resume_all sets paused/failed to Queued and begins; stop_queue halts queue processing."""
        qm = self.make_qm()
        qm.max_simultaneous_downloads = 1

        data = b"S" * 20000
        transport = httpx.MockTransport(lambda req: httpx.Response(200, headers={"Content-Length": str(len(data))}, content=data))

        i1 = qm.add_download("http://test/res1.bin", self.temp_dir, auto_start=False, transport=transport)
        i1["status"] = DownloadStatus.PAUSED

        qm.resume_all()
        self.assertTrue(qm.is_auto_queue_running)

        qm.stop_queue()
        self.assertFalse(qm.is_auto_queue_running)
        qm.shutdown(timeout_ms=1000)

    def test_29_prevent_duplicate_worker_instances(self):
        """Starting an already active download does not spawn duplicate workers."""
        qm = self.make_qm()
        data = b"D" * 50000
        transport = httpx.MockTransport(lambda req: httpx.Response(200, headers={"Content-Length": str(len(data))}, stream=MockByteStream(data, delay=0.01)))

        item = qm.add_download("http://test/dup_work.bin", self.temp_dir, auto_start=True, transport=transport)

        start_wait = time.time()
        while item["status"] != DownloadStatus.DOWNLOADING and time.time() - start_wait < 3.0:
            self.qt_app.processEvents()
            time.sleep(0.02)

        # Call start_item again on active item
        qm.start_item(item["id"])
        self.assertEqual(len(qm.active_workers), 1)
        qm.shutdown(timeout_ms=1000)

    # =========================================================================
    # 4. AUTOMATIC RETRY SCHEDULING TESTS (10 tests)
    # =========================================================================

    def test_30_retryable_transient_network_failure_retries(self):
        """Transient network error qualifies for auto-retry and schedules next_retry_at."""
        self.assertTrue(is_retryable_failure("Connection reset by peer"))
        self.assertTrue(is_retryable_failure("HTTP 503 Service Unavailable"))
        self.assertTrue(is_retryable_failure("Timeout while reading response"))

    def test_31_retryable_http_503_retries(self):
        """HTTP 503 error triggers retry scheduling with exponential backoff."""
        qm = self.make_qm()
        qm.automatic_retry_enabled = True

        transport = httpx.MockTransport(lambda req: httpx.Response(503, content=b"Server Unavailable"))
        item = qm.add_download("http://test/503_err.bin", self.temp_dir, auto_start=True, transport=transport)

        start_wait = time.time()
        while item["retry_count"] == 0 and time.time() - start_wait < 5.0:
            self.qt_app.processEvents()
            time.sleep(0.02)

        self.assertEqual(item["retry_count"], 1)
        self.assertIsNotNone(item["next_retry_at"])
        self.assertEqual(item["status"], DownloadStatus.QUEUED)
        qm.shutdown(timeout_ms=1000)

    def test_32_retryable_http_429_respects_retry_after_header(self):
        """HTTP 429 error with Retry-After: 12 schedules retry ~12s in future."""
        qm = self.make_qm()
        qm.automatic_retry_enabled = True

        # Simulate worker failed with Retry-After header message
        item = qm.add_download("http://test/429.bin", self.temp_dir, auto_start=False)
        qm._on_worker_failed(item["id"], "HTTP 429 Too Many Requests: Retry-After: 12")

        self.assertEqual(item["retry_count"], 1)
        self.assertIsNotNone(item["next_retry_at"])
        dt = parse_iso_datetime(item["next_retry_at"])
        now = datetime.now().astimezone()
        diff = (dt - now).total_seconds()
        self.assertGreaterEqual(diff, 10.0)
        self.assertLessEqual(diff, 14.0)

    def test_33_non_retryable_http_404_fails_immediately(self):
        """HTTP 404 permanent failure is never automatically retried."""
        self.assertFalse(is_retryable_failure("HTTP 404 Not Found"))
        qm = self.make_qm()
        qm.automatic_retry_enabled = True

        transport = httpx.MockTransport(lambda req: httpx.Response(404, content=b"Not Found"))
        item = qm.add_download("http://test/404.bin", self.temp_dir, auto_start=True, transport=transport)

        failed_reported = []
        qm.download_failed.connect(lambda i_id, err: failed_reported.append(i_id))

        start_wait = time.time()
        while not failed_reported and time.time() - start_wait < 5.0:
            self.qt_app.processEvents()
            time.sleep(0.02)

        self.assertEqual(item["status"], DownloadStatus.FAILED)
        self.assertIsNone(item["next_retry_at"])
        self.assertEqual(item["retry_count"], 0)
        qm.shutdown(timeout_ms=1000)

    def test_34_non_retryable_disk_full_fails_immediately(self):
        """Disk full / filesystem permission errors are permanent and never retried."""
        self.assertFalse(is_retryable_failure("Disk error: [Errno 28] No space left on device"))
        self.assertFalse(is_retryable_failure("Permission denied: cannot create destination folder"))

    def test_35_retry_count_reaches_maximum_then_fails(self):
        """When retries exhaust max_retry_attempts, download permanently fails."""
        qm = self.make_qm()
        qm.automatic_retry_enabled = True

        item = qm.add_download("http://test/exhaust.bin", self.temp_dir, auto_start=False, max_retry_attempts=2)
        # Attempt 1
        qm._on_worker_failed(item["id"], "HTTP 503 Server Unavailable")
        self.assertEqual(item["retry_count"], 1)
        self.assertEqual(item["status"], DownloadStatus.QUEUED)

        # Attempt 2
        qm._on_worker_failed(item["id"], "HTTP 503 Server Unavailable")
        self.assertEqual(item["retry_count"], 2)
        self.assertEqual(item["status"], DownloadStatus.QUEUED)

        # Attempt 3 (exceeds max 2 retries)
        qm._on_worker_failed(item["id"], "HTTP 503 Server Unavailable")
        self.assertEqual(item["status"], DownloadStatus.FAILED)
        self.assertIsNone(item["next_retry_at"])

    def test_36_exponential_backoff_timing_5s_15s_45s(self):
        """Exponential backoff calculates 5s, 15s, 45s for successive attempts."""
        qm = self.make_qm()
        item = qm.add_download("http://test/backoff.bin", self.temp_dir, auto_start=False, max_retry_attempts=4)

        # Attempt 1 -> 5s (5 * 3^0)
        qm._on_worker_failed(item["id"], "HTTP 500 Server Error")
        dt1 = parse_iso_datetime(item["next_retry_at"])
        diff1 = (dt1 - datetime.now().astimezone()).total_seconds()
        self.assertGreaterEqual(diff1, 4.0)
        self.assertLessEqual(diff1, 6.0)

        # Attempt 2 -> 15s (5 * 3^1)
        qm._on_worker_failed(item["id"], "HTTP 500 Server Error")
        dt2 = parse_iso_datetime(item["next_retry_at"])
        diff2 = (dt2 - datetime.now().astimezone()).total_seconds()
        self.assertGreaterEqual(diff2, 13.0)
        self.assertLessEqual(diff2, 17.0)

        # Attempt 3 -> 45s (5 * 3^2)
        qm._on_worker_failed(item["id"], "HTTP 500 Server Error")
        dt3 = parse_iso_datetime(item["next_retry_at"])
        diff3 = (dt3 - datetime.now().astimezone()).total_seconds()
        self.assertGreaterEqual(diff3, 40.0)
        self.assertLessEqual(diff3, 47.0)

    def test_37_retry_state_persists_across_restart(self):
        """Retry count and next_retry_at timestamp persist in database across app restarts."""
        qm1 = self.make_qm()
        item = qm1.add_download("http://test/retry_persist.bin", self.temp_dir, auto_start=False)
        qm1._on_worker_failed(item["id"], "HTTP 502 Bad Gateway")

        retry_iso = item["next_retry_at"]
        item_id = item["id"]

        # Restart
        qm2 = self.make_qm()
        restored = qm2.get_item(item_id)
        self.assertIsNotNone(restored)
        self.assertEqual(restored["retry_count"], 1)
        self.assertEqual(restored["next_retry_at"], retry_iso)

    def test_38_manual_retry_resets_count_and_retries_immediately(self):
        """Manual retry action clears next_retry_at, resets retry_count, and queues immediately."""
        qm = self.make_qm()
        qm.is_auto_queue_running = False
        item = qm.add_download("http://test/manual.bin", self.temp_dir, auto_start=False)
        item["status"] = DownloadStatus.FAILED
        item["retry_count"] = 3
        item["next_retry_at"] = "2026-11-20T12:00:00"

        qm.manual_retry(item["id"])
        self.assertEqual(item["retry_count"], 0)
        self.assertIsNone(item["next_retry_at"])
        self.assertEqual(item["status"], DownloadStatus.QUEUED)

    def test_39_cancel_pending_retry(self):
        """User can cancel pending scheduled retry, leaving item failed."""
        qm = self.make_qm()
        item = qm.add_download("http://test/cancel_ret.bin", self.temp_dir, auto_start=False)
        qm._on_worker_failed(item["id"], "HTTP 503 Service Unavailable")
        self.assertIsNotNone(item["next_retry_at"])

        qm.cancel_pending_retry(item["id"])
        self.assertIsNone(item["next_retry_at"])
        self.assertEqual(item["status"], DownloadStatus.FAILED)

    # =========================================================================
    # 5. DOWNLOAD CATEGORIES TESTS (9 tests)
    # =========================================================================

    def test_40_categorize_videos_by_extension(self):
        self.assertEqual(get_category_for_filename("movie.mp4"), "Videos")
        self.assertEqual(get_category_for_filename("clip.mkv"), "Videos")
        self.assertEqual(get_category_for_filename("stream.webm"), "Videos")

    def test_41_categorize_audio_by_extension(self):
        self.assertEqual(get_category_for_filename("song.mp3"), "Audio")
        self.assertEqual(get_category_for_filename("track.flac"), "Audio")
        self.assertEqual(get_category_for_filename("podcast.wav"), "Audio")

    def test_42_categorize_documents_by_extension(self):
        self.assertEqual(get_category_for_filename("manual.pdf"), "Documents")
        self.assertEqual(get_category_for_filename("report.docx"), "Documents")
        self.assertEqual(get_category_for_filename("data.xlsx"), "Documents")

    def test_43_categorize_images_by_extension(self):
        self.assertEqual(get_category_for_filename("photo.jpg"), "Images")
        self.assertEqual(get_category_for_filename("graphic.png"), "Images")
        self.assertEqual(get_category_for_filename("vector.svg"), "Images")

    def test_44_categorize_archives_by_extension(self):
        self.assertEqual(get_category_for_filename("archive.zip"), "Archives")
        self.assertEqual(get_category_for_filename("backup.tar.gz"), "Archives")
        self.assertEqual(get_category_for_filename("package.7z"), "Archives")

    def test_45_categorize_software_by_extension(self):
        self.assertEqual(get_category_for_filename("installer.exe"), "Software")
        self.assertEqual(get_category_for_filename("setup.msi"), "Software")
        self.assertEqual(get_category_for_filename("app.iso"), "Software")

    def test_46_categorize_unknown_as_other(self):
        self.assertEqual(get_category_for_filename("random.xyz"), "Other")
        self.assertEqual(get_category_for_filename("noextension"), "Other")

    def test_47_manual_category_override(self):
        """Manual category assignment overrides auto-detection."""
        qm = self.make_qm()
        item = qm.add_download("http://test/file.mp4", self.temp_dir, category="Documents", auto_start=False)
        self.assertEqual(item["category"], "Documents")

        # Dynamic category change
        qm.set_category(item["id"], "Software")
        self.assertEqual(item["category"], "Software")

    def test_48_category_persists_across_restart(self):
        """Download category persists into database and restores on restart."""
        qm1 = self.make_qm()
        item = qm1.add_download("http://test/music.flac", self.temp_dir, auto_start=False)
        self.assertEqual(item["category"], "Audio")
        item_id = item["id"]

        qm2 = self.make_qm()
        restored = qm2.get_item(item_id)
        self.assertEqual(restored["category"], "Audio")

    # =========================================================================
    # 6. SQLITE MIGRATIONS & PERSISTENCE TESTS (5 tests)
    # =========================================================================

    def test_49_v1_to_v2_migration_adds_all_stage5_columns_and_indexes(self):
        """Migrating v1 schema adds all required Stage 5 columns and indexes."""
        migration_db_path = Path(self.temp_dir) / "migration_test.db"
        conn = sqlite3.connect(str(migration_db_path))

        # Build v1 manually
        conn.execute("PRAGMA user_version = 1;")
        conn.executescript("""
            CREATE TABLE downloads (
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
            CREATE TABLE segments (
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
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
        """)
        conn.commit()
        conn.close()

        # Open with DatabaseManager (triggers migration to v2)
        db_mgr = DatabaseManager(migration_db_path)
        c = db_mgr.get_connection()
        version = c.execute("PRAGMA user_version;").fetchone()[0]
        self.assertEqual(version, CURRENT_SCHEMA_VERSION)

        # Verify new columns exist
        col_names = {row["name"] for row in c.execute("PRAGMA table_info(downloads);").fetchall()}
        expected_new_cols = [
            "priority", "scheduled_start_at", "scheduled_stop_at",
            "schedule_enabled", "individual_speed_limit", "category",
            "retry_count", "max_retry_attempts", "next_retry_at", "retry_enabled"
        ]
        for col in expected_new_cols:
            self.assertIn(col, col_names, f"Column {col} missing in migrated v2 table")
        db_mgr.close()

    def test_50_v1_download_history_preserved_during_v2_migration(self):
        """All existing v1 download records remain intact after migration to v2."""
        v1_db_path = Path(self.temp_dir) / "v1_preserve.db"
        conn = sqlite3.connect(str(v1_db_path))
        conn.execute("PRAGMA user_version = 1;")
        conn.executescript("""
            CREATE TABLE downloads (
                id TEXT PRIMARY KEY, url TEXT, original_url TEXT, filename TEXT,
                destination_path TEXT, temp_path TEXT, status TEXT, download_mode TEXT,
                connection_count INTEGER, total_size INTEGER, downloaded_size INTEGER,
                speed REAL, eta REAL, created_at TEXT, started_at TEXT, paused_at TEXT,
                completed_at TEXT, failed_at TEXT, error_message TEXT, etag TEXT,
                last_modified TEXT, supports_range INTEGER, queue_order INTEGER
            );
            CREATE TABLE segments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                download_id TEXT NOT NULL,
                segment_index INTEGER NOT NULL,
                start_byte INTEGER NOT NULL,
                end_byte INTEGER NOT NULL,
                downloaded_bytes INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'Queued',
                temp_path TEXT,
                retry_count INTEGER NOT NULL DEFAULT 0
            );
            INSERT INTO downloads (id, url, filename, destination_path, status, created_at)
            VALUES ('v1_rec_001', 'http://example.com/v1.iso', 'v1.iso', '/downloads', 'Completed', '2026-11-18T00:00:00');
        """)
        conn.commit()
        conn.close()

        # Migrate
        db_mgr = DatabaseManager(v1_db_path)
        dls = db_mgr.get_all_downloads()
        self.assertEqual(len(dls), 1)
        self.assertEqual(dls[0].id, "v1_rec_001")
        self.assertEqual(dls[0].filename, "v1.iso")
        self.assertEqual(dls[0].priority, "Normal")  # Default backfilled
        self.assertEqual(dls[0].category, "Other")
        db_mgr.close()

    def test_51_interrupted_downloads_recover_with_stage5_fields(self):
        """Recovering interrupted downloads preserves priority, category, and speed limits."""
        dl = DownloadModel(
            id="rec_s5",
            url="http://test/recover.bin",
            destination_folder=self.temp_dir,
            status=DownloadStatus.DOWNLOADING,
            priority=DownloadPriority.HIGH,
            category="Software",
            individual_speed_limit=51200,
        )
        self.db.add_download(dl)

        # Recovery on restart
        self.db.recover_interrupted_downloads()
        recovered = self.db.get_download("rec_s5")
        self.assertEqual(recovered.status, DownloadStatus.INTERRUPTED)
        self.assertEqual(recovered.priority, DownloadPriority.HIGH)
        self.assertEqual(recovered.category, "Software")
        self.assertEqual(recovered.individual_speed_limit, 51200)

    def test_52_stage5_settings_persist_in_database(self):
        """Settings values persist in the settings table."""
        self.db.set_setting("global_speed_limit", "102400")
        self.db.set_setting("max_simultaneous", "3")

        self.assertEqual(self.db.get_setting("global_speed_limit"), "102400")
        self.assertEqual(self.db.get_setting("max_simultaneous"), "3")
        self.assertIsNone(self.db.get_setting("non_existent"))

    def test_53_queue_order_and_priority_persist_in_database(self):
        """Direct database helper methods update priority, category, schedule, and queue order."""
        dl = DownloadModel(id="hlp_1", url="http://test/help.bin", destination_folder=self.temp_dir)
        self.db.add_download(dl)

        self.db.update_priority("hlp_1", DownloadPriority.HIGH)
        self.db.update_category("hlp_1", "Videos")
        self.db.update_schedule("hlp_1", "2026-11-20T10:00:00", "2026-11-20T11:00:00", True)
        self.db.update_speed_limit("hlp_1", 20480)
        self.db.update_queue_order("hlp_1", 42)

        updated = self.db.get_download("hlp_1")
        self.assertEqual(updated.priority, DownloadPriority.HIGH)
        self.assertEqual(updated.category, "Videos")
        self.assertEqual(updated.scheduled_start_at, "2026-11-20T10:00:00")
        self.assertEqual(updated.scheduled_stop_at, "2026-11-20T11:00:00")
        self.assertEqual(updated.schedule_enabled, 1)
        self.assertEqual(updated.individual_speed_limit, 20480)
        self.assertEqual(updated.queue_order, 42)

    def test_54_automatic_retry_dispatch_when_due(self):
        """Scheduler checks due next_retry_at timestamps and dispatches queue processing."""
        qm = self.make_qm()
        past_retry = (datetime.now().astimezone() - timedelta(seconds=2)).isoformat()
        transport = httpx.MockTransport(lambda req: httpx.Response(200, headers={"Content-Length": "10"}, content=b"0123456789"))
        item = qm.add_download(
            "http://test/retry_due.bin",
            self.temp_dir,
            auto_start=False,
            transport=transport,
        )
        item["next_retry_at"] = past_retry
        if qm.db:
            qm.db.update_retry_info(item["id"], retry_count=1, next_retry_at=past_retry)

        # Trigger scheduler check
        qm.scheduler.check_schedules()

        start_wait = time.time()
        while item["status"] != DownloadStatus.COMPLETED and time.time() - start_wait < 3.0:
            self.qt_app.processEvents()
            time.sleep(0.02)

        self.assertEqual(item["status"], DownloadStatus.COMPLETED)
        self.assertIsNone(item.get("next_retry_at"))

    def test_55_retry_dispatch_after_app_restart(self):
        """Preserved next_retry_at timestamp from database is dispatched after restart."""
        past_retry = (datetime.now().astimezone() - timedelta(seconds=2)).isoformat()
        dl = DownloadModel(
            id="retry_rst_1",
            url="http://test/restart_retry.bin",
            destination_folder=self.temp_dir,
            filename="restart_retry.bin",
            status=DownloadStatus.QUEUED,
            retry_count=1,
            next_retry_at=past_retry,
            retry_enabled=1,
        )
        self.db.add_download(dl)

        transport = httpx.MockTransport(lambda req: httpx.Response(200, headers={"Content-Length": "5"}, content=b"hello"))
        qm = self.make_qm(db=self.db)
        loaded = qm.get_item("retry_rst_1")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.get("next_retry_at"), past_retry)
        loaded["transport"] = transport

        # Scheduler triggers dispatch
        qm.scheduler.check_schedules()

        start_wait = time.time()
        while loaded["status"] != DownloadStatus.COMPLETED and time.time() - start_wait < 3.0:
            self.qt_app.processEvents()
            time.sleep(0.02)

        self.assertEqual(loaded["status"], DownloadStatus.COMPLETED)

    def test_56_cancelled_item_never_retried(self):
        """Cancelled items clear next_retry_at and are never dispatched by scheduler."""
        qm = self.make_qm()
        past_retry = (datetime.now().astimezone() - timedelta(seconds=2)).isoformat()
        item = qm.add_download("http://test/cancel_retry.bin", self.temp_dir, auto_start=False)
        item["next_retry_at"] = past_retry
        qm.cancel_download(item["id"])

        self.assertEqual(item["status"], "Cancelled")
        self.assertIsNone(item["next_retry_at"])

        qm.scheduler.check_schedules()
        self.qt_app.processEvents()
        self.assertEqual(item["status"], "Cancelled")
        self.assertNotIn(item["id"], qm.active_workers)

    def test_57_strict_error_classification(self):
        """Strict positive classification: only transient errors retry, permanent & unknown fail permanently."""
        # Permanent errors: False
        self.assertFalse(is_retryable_failure("HTTP 400 Bad Request"))
        self.assertFalse(is_retryable_failure("HTTP 401 Unauthorized"))
        self.assertFalse(is_retryable_failure("HTTP 403 Forbidden"))
        self.assertFalse(is_retryable_failure("HTTP 404 Not Found"))
        self.assertFalse(is_retryable_failure("HTTP 405 Method Not Allowed"))
        self.assertFalse(is_retryable_failure("HTTP 410 Gone"))
        self.assertFalse(is_retryable_failure("HTTP 416 Range Not Satisfiable"))
        self.assertFalse(is_retryable_failure("Invalid Range: start out of bounds"))
        self.assertFalse(is_retryable_failure("Content-Range mismatch"))
        self.assertFalse(is_retryable_failure("Invalid URL scheme"))
        self.assertFalse(is_retryable_failure("Disk full: no space left"))
        self.assertFalse(is_retryable_failure("Permission denied: cannot write to folder"))
        self.assertFalse(is_retryable_failure(PermissionError("Permission denied")))
        self.assertFalse(is_retryable_failure(ssl.SSLError("SSL certificate verify failed")))
        self.assertFalse(is_retryable_failure(httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED]")))
        self.assertFalse(is_retryable_failure("Random completely unclassified error string"))
        self.assertFalse(is_retryable_failure(RuntimeError("Unknown internal failure")))

        # Transient errors: True
        self.assertTrue(is_retryable_failure("HTTP 408 Request Timeout"))
        self.assertTrue(is_retryable_failure("HTTP 429 Too Many Requests"))
        self.assertTrue(is_retryable_failure("HTTP 500 Internal Server Error"))
        self.assertTrue(is_retryable_failure("HTTP 502 Bad Gateway"))
        self.assertTrue(is_retryable_failure("HTTP 503 Service Unavailable"))
        self.assertTrue(is_retryable_failure("HTTP 504 Gateway Timeout"))
        self.assertTrue(is_retryable_failure("Network error: connection reset by peer"))
        self.assertTrue(is_retryable_failure("Read timed out after 10.0 seconds"))
        self.assertTrue(is_retryable_failure(httpx.TimeoutException("ReadTimeout")))
        self.assertTrue(is_retryable_failure(httpx.ConnectTimeout("ConnectTimeout")))
        self.assertTrue(is_retryable_failure(ConnectionResetError("Connection reset by peer")))

    def test_58_retry_after_delta_seconds_and_http_date(self):
        """parse_retry_after parses delta seconds and HTTP-date formats."""
        self.assertEqual(parse_retry_after("120"), 120.0)
        self.assertEqual(parse_retry_after("Retry-After: 45"), 45.0)
        self.assertEqual(parse_retry_after("HTTP 429 Retry-After: 30 seconds"), 30.0)

        # HTTP-date format
        future_dt = datetime.now(timezone.utc) + timedelta(seconds=60)
        http_date = future_dt.strftime("%a, %d %b %Y %H:%M:%S GMT")
        parsed = parse_retry_after(http_date)
        self.assertIsNotNone(parsed)
        self.assertTrue(55.0 <= parsed <= 65.0)

        # Malformed format
        self.assertIsNone(parse_retry_after(""))
        self.assertIsNone(parse_retry_after("not-a-valid-date"))

    def test_59_bandwidth_token_bucket_burst_and_dynamic_wake(self):
        """BandwidthController uses proportional burst capacity and condition variable dynamic wake."""
        bc = BandwidthController(global_limit=10000, global_enabled=True)
        bucket = bc.global_bucket
        # Proportional burst: ~20% of 10000 = 2000, max(512, 2000) = 2000
        self.assertEqual(bucket.capacity, 2000)

        # Dynamic wake-up test
        bc.set_global_limit(500)
        bucket.tokens = 0.0

        wake_time = None
        def waiter():
            nonlocal wake_time
            # Wait for 1000 bytes at 500 B/s would take 2.0s without limit change
            t0 = time.time()
            bc.acquire(1000, None)
            wake_time = time.time() - t0

        t = threading.Thread(target=waiter)
        t.start()
        time.sleep(0.05)
        # Dynamically bump limit to 100,000 B/s to wake condition immediately
        bc.set_global_limit(100000)
        t.join(timeout=1.0)
        self.assertFalse(t.is_alive())
        self.assertIsNotNone(wake_time)
        self.assertLess(wake_time, 0.5)

    def test_60_default_individual_speed_limit_applied_and_override(self):
        """Default individual limit applies when omitted, explicit 0 stays unlimited, explicit value respected."""
        set_default_individual_speed_limit(40960)  # 40 KB/s
        try:
            qm = self.make_qm()
            item1 = qm.add_download("http://test/f1.bin", self.temp_dir, auto_start=False)
            self.assertEqual(item1["individual_speed_limit"], 40960)

            item2 = qm.add_download("http://test/f2.bin", self.temp_dir, individual_speed_limit=0, auto_start=False)
            self.assertEqual(item2["individual_speed_limit"], 0)

            item3 = qm.add_download("http://test/f3.bin", self.temp_dir, individual_speed_limit=10240, auto_start=False)
            self.assertEqual(item3["individual_speed_limit"], 10240)
        finally:
            set_default_individual_speed_limit(0)

    def test_61_add_download_dialog_data_passing(self):
        """AddDownloadDialog properly passes priority and category to QueueManager."""
        win = MainWindow(db=self.db)
        fake_data = {
            "url": "http://test/dialog_data.bin",
            "folder": self.temp_dir,
            "explicit_filename": "dialog_data.bin",
            "connections": 4,
            "priority": DownloadPriority.HIGH,
            "category": "Videos",
        }
        with patch("mdm.main_window.AddDownloadDialog") as MockDialog:
            mock_inst = MagicMock()
            mock_inst.exec.return_value = QDialog.Accepted
            mock_inst.get_data.return_value = fake_data
            MockDialog.return_value = mock_inst

            win.open_add_dialog()

        item = win.queue_mgr.get_item(win.queue_mgr.items[-1]["id"])
        self.assertEqual(item["priority"], DownloadPriority.HIGH)
        self.assertEqual(item["category"], "Videos")
        win.queue_mgr.shutdown(timeout_ms=500)
        win.close()

    def test_62_settings_persistence_through_database(self):
        """Settings persist through DatabaseManager get_setting / set_setting."""
        app_settings.bind_database(self.db)
        set_max_simultaneous_downloads(4)
        set_default_priority("High")

        self.assertEqual(self.db.get_setting("max_simultaneous_downloads"), "4")
        self.assertEqual(self.db.get_setting("default_priority"), "High")

        # New settings instance bound to the same db recovers the values
        new_settings = Settings(db=self.db)
        self.assertEqual(new_settings.max_simultaneous_downloads, 4)
        self.assertEqual(new_settings.default_priority, "High")

    def test_63_category_recalculation_on_server_filename(self):
        """Category recalculates when server filename arrives, but preserves manual user assignment."""
        qm = self.make_qm()
        # Case A: Auto-detected (not manual)
        item1 = qm.add_download("http://test/resource_unknown", self.temp_dir, auto_start=False)
        self.assertEqual(item1["category"], "Other")
        self.assertFalse(item1.get("category_manual", False))

        # Server detects Content-Disposition filename
        qm._on_worker_filename_detected(item1["id"], "video_clip.mp4")
        self.assertEqual(item1["category"], "Videos")

        # Case B: User manual override
        qm.set_category(item1["id"], "Software")
        self.assertEqual(item1["category"], "Software")
        self.assertTrue(item1.get("category_manual", False))

        # Another filename update from server must NOT overwrite user choice
        qm._on_worker_metadata_detected(item1["id"], {"filename": "archive.zip"})
        self.assertEqual(item1["category"], "Software")

    def test_64_scheduler_filter_and_schedule_validation(self):
        """set_schedule validates stop > start and valid ISO datetime; refresh_table filters Scheduler."""
        qm = self.make_qm()
        item = qm.add_download("http://test/sched_val.bin", self.temp_dir, auto_start=False)

        # Invalid start datetime
        with self.assertRaises(ValueError):
            qm.set_schedule(item["id"], "invalid-time", "2026-11-20T12:00:00", True)

        # stop <= start
        with self.assertRaises(ValueError):
            qm.set_schedule(item["id"], "2026-11-20T12:00:00", "2026-11-20T11:00:00", True)

        # Valid schedule
        qm.set_schedule(item["id"], "2026-11-20T10:00:00", "2026-11-20T11:00:00", True)
        self.assertTrue(item["schedule_enabled"])

        # MainWindow filter test
        win = MainWindow(db=self.db)
        if hasattr(win, "queue_mgr") and win.queue_mgr:
            win.queue_mgr.shutdown(timeout_ms=500)
        win.close()
        win.queue_mgr = qm
        item_unscheduled = qm.add_download("http://test/unscheduled.bin", self.temp_dir, auto_start=False)

        win.current_filter = "Scheduler"
        win.refresh_table()

        visible_ids = list(win._item_row_map.keys())
        self.assertIn(item["id"], visible_ids)
        self.assertNotIn(item_unscheduled["id"], visible_ids)
        win.close()
        qm.shutdown(timeout_ms=500)

    def test_65_dynamic_queue_concurrency_increase(self):
        """Increasing max_simultaneous_downloads immediately begins next queued items without duplicate workers."""
        set_max_simultaneous_downloads(1)
        qm = self.make_qm()
        qm.max_simultaneous_downloads = 1

        data = b"Y" * 100000
        transport = httpx.MockTransport(lambda req: httpx.Response(200, headers={"Content-Length": str(len(data))}, stream=MockByteStream(data, delay=0.02)))

        i1 = qm.add_download("http://test/c1.bin", self.temp_dir, auto_start=True, transport=transport)
        i2 = qm.add_download("http://test/c2.bin", self.temp_dir, auto_start=True, transport=transport)
        i3 = qm.add_download("http://test/c3.bin", self.temp_dir, auto_start=True, transport=transport)

        start_wait = time.time()
        while i1["status"] != DownloadStatus.DOWNLOADING and time.time() - start_wait < 3.0:
            self.qt_app.processEvents()
            time.sleep(0.02)

        self.assertEqual(len(qm.active_workers), 1)
        self.assertEqual(i2["status"], DownloadStatus.QUEUED)
        self.assertEqual(i3["status"], DownloadStatus.QUEUED)

        # Increase concurrency from 1 to 3
        qm.max_simultaneous_downloads = 3

        start_wait = time.time()
        while len(qm.active_workers) < 3 and time.time() - start_wait < 3.0:
            self.qt_app.processEvents()
            time.sleep(0.02)

        self.assertEqual(len(qm.active_workers), 3)
        self.assertEqual(i2["status"], DownloadStatus.DOWNLOADING)
        self.assertEqual(i3["status"], DownloadStatus.DOWNLOADING)
        # Ensure no duplicates in active_workers
        self.assertEqual(len(set(qm.active_workers.keys())), 3)
        qm.shutdown(timeout_ms=1000)

    def test_66_scheduler_timezone_normalisation(self):
        """Scheduler timezone normalisation: naive local Windows time, aware UTC, and invalid timestamp handling."""
        qm = self.make_qm()
        now_local = datetime.now().astimezone()

        # 1. Naive future scheduled start (yyyy-MM-ddTHH:mm:ss)
        future_dt = (now_local + timedelta(hours=2)).replace(tzinfo=None)
        future_str = future_dt.strftime("%Y-%m-%dT%H:%M:%S")
        i_future = qm.add_download(
            "http://test/naive_future.bin",
            self.temp_dir,
            auto_start=True,
            scheduled_start_at=future_str,
            schedule_enabled=True,
        )
        self.assertEqual(i_future["status"], DownloadStatus.QUEUED)
        self.assertNotIn(i_future["id"], qm.active_workers)

        # 2. Naive overdue scheduled start (yyyy-MM-ddTHH:mm:ss)
        overdue_dt = (now_local - timedelta(minutes=5)).replace(tzinfo=None)
        overdue_str = overdue_dt.strftime("%Y-%m-%dT%H:%M:%S")
        data = b"T" * 50000
        transport = httpx.MockTransport(lambda req: httpx.Response(200, headers={"Content-Length": str(len(data))}, stream=MockByteStream(data, delay=0.01)))
        i_overdue = qm.add_download(
            "http://test/naive_overdue.bin",
            self.temp_dir,
            auto_start=True,
            scheduled_start_at=overdue_str,
            schedule_enabled=True,
            transport=transport,
        )
        start_wait = time.time()
        while i_overdue["status"] != DownloadStatus.DOWNLOADING and time.time() - start_wait < 3.0:
            self.qt_app.processEvents()
            time.sleep(0.02)
        self.assertEqual(i_overdue["status"], DownloadStatus.DOWNLOADING)

        # 3. Naive scheduled stop (yyyy-MM-ddTHH:mm:ss)
        stop_naive_dt = (now_local - timedelta(seconds=1)).replace(tzinfo=None)
        stop_naive_str = stop_naive_dt.strftime("%Y-%m-%dT%H:%M:%S")
        i_overdue["scheduled_stop_at"] = stop_naive_str
        qm.scheduler.check_schedules(now_local)
        start_wait = time.time()
        while i_overdue["status"] != DownloadStatus.PAUSED and time.time() - start_wait < 3.0:
            self.qt_app.processEvents()
            time.sleep(0.02)
        self.assertEqual(i_overdue["status"], DownloadStatus.PAUSED)

        # 4. Aware UTC scheduled start (Z or +00:00)
        utc_overdue = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        i_utc = qm.add_download(
            "http://test/aware_utc.bin",
            self.temp_dir,
            auto_start=True,
            scheduled_start_at=utc_overdue,
            schedule_enabled=True,
            transport=transport,
        )
        start_wait = time.time()
        while i_utc["status"] != DownloadStatus.DOWNLOADING and time.time() - start_wait < 3.0:
            self.qt_app.processEvents()
            time.sleep(0.02)
        self.assertEqual(i_utc["status"], DownloadStatus.DOWNLOADING)

        # 5. Aware local scheduled stop
        stop_aware = (now_local - timedelta(seconds=1)).isoformat()
        i_utc["scheduled_stop_at"] = stop_aware
        qm.scheduler.check_schedules(now_local)
        start_wait = time.time()
        while i_utc["status"] != DownloadStatus.PAUSED and time.time() - start_wait < 3.0:
            self.qt_app.processEvents()
            time.sleep(0.02)
        self.assertEqual(i_utc["status"], DownloadStatus.PAUSED)

        # 6. Invalid scheduled start timestamp must never silently start
        i_invalid = qm.add_download(
            "http://test/invalid_time.bin",
            self.temp_dir,
            auto_start=True,
            scheduled_start_at="invalid-date-not-an-iso-string",
            schedule_enabled=True,
            transport=transport,
        )
        self.assertEqual(i_invalid["status"], DownloadStatus.QUEUED)
        qm.scheduler.check_schedules(now_local)
        self.assertEqual(i_invalid["status"], DownloadStatus.QUEUED)
        self.assertNotIn(i_invalid["id"], qm.active_workers)

        qm.shutdown(timeout_ms=1000)

    def test_67_http_429_retry_after_propagation(self):
        """HTTP 429 Retry-After is propagated to QueueManager and worker exits immediately to free slot."""
        qm = self.make_qm()
        qm.max_simultaneous_downloads = 1

        def handler_429(request: httpx.Request):
            return httpx.Response(429, headers={"Retry-After": "120"}, text="Rate limit exceeded")

        transport = httpx.MockTransport(handler_429)
        item = qm.add_download(
            "http://test/rate_limit.bin",
            self.temp_dir,
            auto_start=True,
            transport=transport,
        )

        start_wait = time.time()
        while item["status"] != DownloadStatus.QUEUED and time.time() - start_wait < 3.0:
            self.qt_app.processEvents()
            time.sleep(0.02)

        start_wait = time.time()
        while len(qm.active_workers) > 0 and time.time() - start_wait < 3.0:
            self.qt_app.processEvents()
            time.sleep(0.02)

        # Verify worker terminated promptly and slot is free
        self.assertEqual(len(qm.active_workers), 0)
        self.assertEqual(item["retry_count"], 1)
        self.assertIsNotNone(item["next_retry_at"])
        # Verify next_retry_at is ~120 seconds in future
        now_dt = datetime.now().astimezone()
        retry_dt = normalize_to_aware_datetime(item["next_retry_at"])
        diff = (retry_dt - now_dt).total_seconds()
        self.assertTrue(110 <= diff <= 130, f"Expected ~120s delay, got {diff}s")

        # Verify database record
        db_item = qm.db.get_download(item["id"])
        self.assertEqual(db_item.retry_count, 1)
        self.assertEqual(db_item.next_retry_at, item["next_retry_at"])

        qm.shutdown(timeout_ms=500)

    def test_68_http_429_retry_after_http_date(self):
        """HTTP 429 with RFC 7231 HTTP-date header calculates correct delay and schedules next_retry_at."""
        qm = self.make_qm()

        target_time = datetime.now(timezone.utc) + timedelta(seconds=75)
        http_date = email.utils.format_datetime(target_time)

        def handler_date(request: httpx.Request):
            return httpx.Response(429, headers={"Retry-After": http_date}, text="Rate limited until date")

        transport = httpx.MockTransport(handler_date)
        item = qm.add_download(
            "http://test/date_limit.bin",
            self.temp_dir,
            auto_start=True,
            transport=transport,
        )

        start_wait = time.time()
        while item["status"] != DownloadStatus.QUEUED and time.time() - start_wait < 3.0:
            self.qt_app.processEvents()
            time.sleep(0.02)

        self.assertEqual(item["retry_count"], 1)
        now_dt = datetime.now().astimezone()
        retry_dt = normalize_to_aware_datetime(item["next_retry_at"])
        diff = (retry_dt - now_dt).total_seconds()
        self.assertTrue(65 <= diff <= 85, f"Expected ~75s delay, got {diff}s")
        qm.shutdown(timeout_ms=500)

    def test_69_http_408_transient_failure_and_retry(self):
        """HTTP 408 Request Timeout is classified as retryable and schedules automatic queue retry."""
        qm = self.make_qm()

        def handler_408(request: httpx.Request):
            return httpx.Response(408, text="Request Timeout")

        transport = httpx.MockTransport(handler_408)
        item = qm.add_download(
            "http://test/timeout_408.bin",
            self.temp_dir,
            auto_start=True,
            transport=transport,
        )

        start_wait = time.time()
        while item["status"] != DownloadStatus.QUEUED and time.time() - start_wait < 5.0:
            self.qt_app.processEvents()
            time.sleep(0.02)

        self.assertEqual(item["retry_count"], 1)
        self.assertIn("HTTP 408", item["error"])
        self.assertIsNotNone(item["next_retry_at"])
        qm.shutdown(timeout_ms=500)

    def test_70_segmented_download_retry_after_propagation(self):
        """Segmented downloader encountering HTTP 429 with Retry-After propagates delay to QueueManager."""
        qm = self.make_qm()
        file_data = b"Z" * 100000

        def segmented_429_handler(request: httpx.Request):
            rng = request.headers.get("Range")
            if rng == "bytes=0-0":
                return httpx.Response(206, headers={
                    "Content-Range": f"bytes 0-0/{len(file_data)}",
                    "Content-Length": "1",
                }, content=file_data[:1])
            # Segment request returns 429 with Retry-After: 90
            return httpx.Response(429, headers={"Retry-After": "90"}, text="Segment rate limited")

        transport = httpx.MockTransport(segmented_429_handler)
        item = qm.add_download(
            "http://test/seg_429.bin",
            self.temp_dir,
            connections=4,
            auto_start=True,
            transport=transport,
        )

        start_wait = time.time()
        while item["status"] != DownloadStatus.QUEUED and time.time() - start_wait < 5.0:
            self.qt_app.processEvents()
            time.sleep(0.02)

        self.assertEqual(item["retry_count"], 1)
        self.assertIsNotNone(item["next_retry_at"])
        now_dt = datetime.now().astimezone()
        retry_dt = normalize_to_aware_datetime(item["next_retry_at"])
        diff = (retry_dt - now_dt).total_seconds()
        self.assertTrue(80 <= diff <= 100, f"Expected ~90s delay, got {diff}s")
        qm.shutdown(timeout_ms=500)

    def test_71_http_503_exponential_backoff_and_malformed_retry_after(self):
        """HTTP 503 and malformed Retry-After header gracefully fall back to exponential backoff."""
        qm = self.make_qm()

        def handler_junk(request: httpx.Request):
            return httpx.Response(503, headers={"Retry-After": "not-a-number-or-date"}, text="Service Unavailable")

        transport = httpx.MockTransport(handler_junk)
        item = qm.add_download(
            "http://test/fallback_backoff.bin",
            self.temp_dir,
            auto_start=True,
            transport=transport,
        )

        start_wait = time.time()
        while item["status"] != DownloadStatus.QUEUED and time.time() - start_wait < 5.0:
            self.qt_app.processEvents()
            time.sleep(0.02)

        self.assertEqual(item["retry_count"], 1)
        # Attempt 1 backoff is 5s
        now_dt = datetime.now().astimezone()
        retry_dt = normalize_to_aware_datetime(item["next_retry_at"])
        diff = (retry_dt - now_dt).total_seconds()
        self.assertTrue(3 <= diff <= 8, f"Expected ~5s delay, got {diff}s")
        qm.shutdown(timeout_ms=500)

    def test_72_scheduled_stop_during_worker_retrying_state(self):
        """Active download in Retrying internal state is safely paused on scheduled stop without cancellation."""
        qm = self.make_qm()
        # Fail first 2 attempts, then would succeed
        attempt_count = 0

        def flaky_handler(request: httpx.Request):
            nonlocal attempt_count
            attempt_count += 1
            if attempt_count <= 2:
                raise httpx.ConnectError("Temporary glitch")
            return httpx.Response(200, content=b"Success")

        transport = httpx.MockTransport(flaky_handler)
        item = qm.add_download(
            "http://test/internal_flaky.bin",
            self.temp_dir,
            auto_start=True,
            transport=transport,
        )

        start_wait = time.time()
        # Wait until worker enters Retrying state
        while not str(item["status"]).startswith("Retrying") and item["status"] != DownloadStatus.DOWNLOADING and time.time() - start_wait < 3.0:
            self.qt_app.processEvents()
            time.sleep(0.02)

        # Trigger scheduled stop while worker is retrying internally
        now = datetime.now().astimezone()
        item["scheduled_stop_at"] = (now - timedelta(seconds=1)).isoformat()
        item["schedule_enabled"] = True
        qm.scheduler.check_schedules(now)

        start_wait = time.time()
        while item["status"] not in (DownloadStatus.PAUSED, DownloadStatus.INTERRUPTED) and time.time() - start_wait < 3.0:
            self.qt_app.processEvents()
            time.sleep(0.02)

        self.assertIn(item["status"], (DownloadStatus.PAUSED, DownloadStatus.INTERRUPTED))
        self.assertNotEqual(item["status"], DownloadStatus.CANCELLED)
        qm.shutdown(timeout_ms=1000)

    def test_73_token_reservation_lifecycle_and_dynamic_limit_changes(self):
        """Deterministic tests for token reservation lifecycle when limits are disabled, removed, changed, paused, or cancelled."""
        controller = BandwidthController()

        # 1. Disable global limit while waiting
        controller.set_global_limit(1000)
        controller.set_global_enabled(True)
        # Drain bucket
        controller.global_bucket.tokens = 0.0
        t_start = time.time()

        def wait_and_disable():
            time.sleep(0.05)
            controller.set_global_enabled(False)

        threading.Thread(target=wait_and_disable, daemon=True).start()
        controller.acquire(500)
        elapsed = time.time() - t_start
        self.assertLess(elapsed, 0.4, "Waiting thread must wake promptly when global limit is disabled")

        # 2. Remove individual limit while waiting
        controller.set_individual_limit("item1", 1000)
        controller.item_buckets["item1"].tokens = 0.0
        t_start = time.time()

        def wait_and_remove():
            time.sleep(0.05)
            controller.remove_individual_limit("item1")

        threading.Thread(target=wait_and_remove, daemon=True).start()
        controller.acquire(500, item_id="item1")
        elapsed = time.time() - t_start
        self.assertLess(elapsed, 0.4, "Waiting thread must wake promptly when individual limit is removed")

        # 3. Change individual limit to unlimited (0)
        controller.set_individual_limit("item2", 1000)
        controller.item_buckets["item2"].tokens = 0.0
        t_start = time.time()

        def wait_and_zero():
            time.sleep(0.05)
            controller.set_individual_limit("item2", 0)

        threading.Thread(target=wait_and_zero, daemon=True).start()
        controller.acquire(500, item_id="item2")
        elapsed = time.time() - t_start
        self.assertLess(elapsed, 0.4, "Waiting thread must wake promptly when individual limit is set to 0")

        # 4. Re-enable global limit after disabling
        controller.set_global_limit(10000)
        controller.set_global_enabled(True)
        self.assertTrue(controller.is_global_enabled())

        # 5. Cancel while waiting: refunds tokens, no stale token debt
        controller.set_global_limit(500)
        controller.global_bucket.tokens = 0.0
        cancelled = False

        def wait_and_cancel():
            time.sleep(0.05)
            nonlocal cancelled
            cancelled = True
            with controller._cond:
                controller._cond.notify_all()

        threading.Thread(target=wait_and_cancel, daemon=True).start()
        with self.assertRaises(DownloadCancelledError):
            controller.acquire(1000, is_cancelled=lambda: cancelled)
        self.assertGreaterEqual(controller.global_bucket.tokens, -0.01, "Cancellation must not leave token debt")

        # 6. Pause while waiting: refunds tokens, no stale token debt
        controller.global_bucket.tokens = 0.0
        paused = False

        def wait_and_pause():
            time.sleep(0.05)
            nonlocal paused
            paused = True
            with controller._cond:
                controller._cond.notify_all()

        threading.Thread(target=wait_and_pause, daemon=True).start()
        with self.assertRaises(DownloadPausedError):
            controller.acquire(1000, is_paused=lambda: paused)
        self.assertGreaterEqual(controller.global_bucket.tokens, -0.01, "Pause must not leave token debt")

        # 7. Multiple segment workers share same individual limit
        controller.set_global_enabled(False)
        controller.set_individual_limit("shared_item", 100000)
        results = []

        def worker_drain():
            controller.acquire(5000, item_id="shared_item")
            results.append(True)

        threads = [threading.Thread(target=worker_drain) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=2.0)
        self.assertEqual(len(results), 3)

    def test_74_internal_retries_vs_queue_retries_distinction(self):
        """Layer A internal retries do not increment whole-download retry_count; cancel clears retry state."""
        qm = self.make_qm()
        # Fail once transiently internally, then succeed
        call_count = 0
        data = b"M" * 20000

        def internal_retry_handler(request: httpx.Request):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(502, text="Bad Gateway transient")
            return httpx.Response(200, headers={"Content-Length": str(len(data))}, content=data)

        transport = httpx.MockTransport(internal_retry_handler)
        item = qm.add_download(
            "http://test/internal_retry.bin",
            self.temp_dir,
            auto_start=True,
            transport=transport,
        )

        start_wait = time.time()
        while item["status"] != DownloadStatus.COMPLETED and time.time() - start_wait < 5.0:
            self.qt_app.processEvents()
            time.sleep(0.02)

        self.assertEqual(item["status"], DownloadStatus.COMPLETED)
        # Layer A internal retry succeeded, so queue-level retry_count must remain 0!
        self.assertEqual(item["retry_count"], 0)

        # Cancel pending retry permanently clears retry state and never retries
        item_fail = qm.add_download(
            "http://test/permanent_cancel.bin",
            self.temp_dir,
            auto_start=False,
        )
        item_fail["next_retry_at"] = (datetime.now().astimezone() + timedelta(minutes=5)).isoformat()
        item_fail["status"] = DownloadStatus.QUEUED
        item_fail["retry_count"] = 1

        qm.cancel_download(item_fail["id"])
        self.assertEqual(item_fail["status"], DownloadStatus.CANCELLED)
        self.assertIsNone(item_fail["next_retry_at"])

        # DownloadDetailsDialog displays accurate retry info
        details_dialog = DownloadDetailsDialog(item_fail, qm)
        self.assertIsNotNone(details_dialog)
        details_dialog.close()
        details_dialog.deleteLater()
        self.qt_app.processEvents()

        qm.shutdown(timeout_ms=500)

    # =========================================================================
    # 7. STAGE 5 FINAL PATCH REGRESSION TESTS (Tests 75-84)
    # =========================================================================

    def test_75_http_503_retry_after_propagation(self):
        """HTTP 503 with Retry-After: 120 promotes to queue-level retry without internal retries."""
        qm = self.make_qm()
        call_count = 0

        def handler_503(request: httpx.Request):
            nonlocal call_count
            call_count += 1
            return httpx.Response(503, headers={"Retry-After": "120"}, text="Service Unavailable")

        transport = httpx.MockTransport(handler_503)
        item = qm.add_download(
            "http://test/retry_503.bin",
            self.temp_dir,
            auto_start=True,
            transport=transport,
        )

        start_wait = time.time()
        while item["status"] != DownloadStatus.QUEUED and time.time() - start_wait < 3.0:
            self.qt_app.processEvents()
            time.sleep(0.02)

        start_wait = time.time()
        while len(qm.active_workers) > 0 and time.time() - start_wait < 3.0:
            self.qt_app.processEvents()
            time.sleep(0.02)

        # Worker slot released immediately without internal retry
        self.assertEqual(call_count, 1, "Internal retries must be stopped for long Retry-After delays")
        self.assertEqual(len(qm.active_workers), 0)
        self.assertEqual(item["retry_count"], 1)
        self.assertIn("HTTP 503", item["error"])
        self.assertIn("120", item["error"])

        # next_retry_at persisted with ~120s offset
        now_dt = datetime.now().astimezone()
        retry_dt = normalize_to_aware_datetime(item["next_retry_at"])
        diff = (retry_dt - now_dt).total_seconds()
        self.assertTrue(110 <= diff <= 130, f"Expected ~120s delay, got {diff}s")

        db_item = qm.db.get_download(item["id"])
        self.assertEqual(db_item.retry_count, 1)
        self.assertEqual(db_item.next_retry_at, item["next_retry_at"])
        qm.shutdown(timeout_ms=500)

    def test_76_http_500_retry_after_propagation(self):
        """HTTP 500 with Retry-After: 60 promotes to queue retry without internal delay."""
        qm = self.make_qm()
        call_count = 0

        def handler_500(request: httpx.Request):
            nonlocal call_count
            call_count += 1
            return httpx.Response(500, headers={"Retry-After": "60"}, text="Internal Server Error")

        transport = httpx.MockTransport(handler_500)
        item = qm.add_download(
            "http://test/retry_500.bin",
            self.temp_dir,
            auto_start=True,
            transport=transport,
        )

        start_wait = time.time()
        while item["status"] != DownloadStatus.QUEUED and time.time() - start_wait < 3.0:
            self.qt_app.processEvents()
            time.sleep(0.02)

        start_wait = time.time()
        while len(qm.active_workers) > 0 and time.time() - start_wait < 3.0:
            self.qt_app.processEvents()
            time.sleep(0.02)

        self.assertEqual(call_count, 1)
        self.assertEqual(len(qm.active_workers), 0)
        self.assertEqual(item["retry_count"], 1)
        self.assertIn("HTTP 500", item["error"])
        now_dt = datetime.now().astimezone()
        retry_dt = normalize_to_aware_datetime(item["next_retry_at"])
        diff = (retry_dt - now_dt).total_seconds()
        self.assertTrue(50 <= diff <= 70, f"Expected ~60s delay, got {diff}s")
        qm.shutdown(timeout_ms=500)

    def test_77_http_502_retry_after_http_date(self):
        """HTTP 502 with Retry-After RFC 7231 HTTP-date calculates delay and persists next_retry_at."""
        qm = self.make_qm()
        call_count = 0
        target_time = datetime.now(timezone.utc) + timedelta(seconds=85)
        http_date = email.utils.format_datetime(target_time)

        def handler_502(request: httpx.Request):
            nonlocal call_count
            call_count += 1
            return httpx.Response(502, headers={"Retry-After": http_date}, text="Bad Gateway")

        transport = httpx.MockTransport(handler_502)
        item = qm.add_download(
            "http://test/retry_502.bin",
            self.temp_dir,
            auto_start=True,
            transport=transport,
        )

        start_wait = time.time()
        while item["status"] != DownloadStatus.QUEUED and time.time() - start_wait < 3.0:
            self.qt_app.processEvents()
            time.sleep(0.02)

        start_wait = time.time()
        while len(qm.active_workers) > 0 and time.time() - start_wait < 3.0:
            self.qt_app.processEvents()
            time.sleep(0.02)

        self.assertEqual(call_count, 1)
        self.assertEqual(len(qm.active_workers), 0)
        self.assertEqual(item["retry_count"], 1)
        self.assertIn("HTTP 502", item["error"])
        now_dt = datetime.now().astimezone()
        retry_dt = normalize_to_aware_datetime(item["next_retry_at"])
        diff = (retry_dt - now_dt).total_seconds()
        self.assertTrue(75 <= diff <= 95, f"Expected ~85s delay, got {diff}s")
        qm.shutdown(timeout_ms=500)

    def test_78_http_504_and_408_retry_after_propagation(self):
        """HTTP 504 and HTTP 408 with Retry-After headers promote to queue-level retry."""
        qm = self.make_qm()

        # 1. HTTP 504 with Retry-After: 45
        def handler_504(request: httpx.Request):
            return httpx.Response(504, headers={"Retry-After": "45"}, text="Gateway Timeout")

        transport_504 = httpx.MockTransport(handler_504)
        i_504 = qm.add_download("http://test/retry_504.bin", self.temp_dir, auto_start=True, transport=transport_504)

        start_wait = time.time()
        while i_504["status"] != DownloadStatus.QUEUED and time.time() - start_wait < 3.0:
            self.qt_app.processEvents()
            time.sleep(0.02)

        start_wait = time.time()
        while len(qm.active_workers) > 0 and time.time() - start_wait < 3.0:
            self.qt_app.processEvents()
            time.sleep(0.02)

        self.assertEqual(i_504["retry_count"], 1)
        self.assertIn("HTTP 504", i_504["error"])
        now_dt = datetime.now().astimezone()
        retry_dt = normalize_to_aware_datetime(i_504["next_retry_at"])
        diff = (retry_dt - now_dt).total_seconds()
        self.assertTrue(35 <= diff <= 55, f"Expected ~45s delay, got {diff}s")

        # 2. HTTP 408 with Retry-After: 30
        def handler_408(request: httpx.Request):
            return httpx.Response(408, headers={"Retry-After": "30"}, text="Request Timeout")

        transport_408 = httpx.MockTransport(handler_408)
        i_408 = qm.add_download("http://test/retry_408_ra.bin", self.temp_dir, auto_start=True, transport=transport_408)

        start_wait = time.time()
        while i_408["status"] != DownloadStatus.QUEUED and time.time() - start_wait < 3.0:
            self.qt_app.processEvents()
            time.sleep(0.02)

        start_wait = time.time()
        while len(qm.active_workers) > 0 and time.time() - start_wait < 3.0:
            self.qt_app.processEvents()
            time.sleep(0.02)

        self.assertEqual(i_408["retry_count"], 1)
        self.assertIn("HTTP 408", i_408["error"])
        now_dt = datetime.now().astimezone()
        retry_dt = normalize_to_aware_datetime(i_408["next_retry_at"])
        diff = (retry_dt - now_dt).total_seconds()
        self.assertTrue(20 <= diff <= 40, f"Expected ~30s delay, got {diff}s")
        qm.shutdown(timeout_ms=500)

    def test_79_segmented_http_503_retry_after_propagation(self):
        """Segmented download encountering HTTP 503 with Retry-After on segment request promotes to queue retry."""
        qm = self.make_qm()
        file_data = b"W" * 100000

        def segmented_503_handler(request: httpx.Request):
            rng = request.headers.get("Range")
            if rng == "bytes=0-0":
                return httpx.Response(206, headers={
                    "Content-Range": f"bytes 0-0/{len(file_data)}",
                    "Content-Length": "1",
                }, content=file_data[:1])
            # Segment returns HTTP 503 with Retry-After: 90
            return httpx.Response(503, headers={"Retry-After": "90"}, text="Segment 503")

        transport = httpx.MockTransport(segmented_503_handler)
        item = qm.add_download(
            "http://test/seg_503.bin",
            self.temp_dir,
            connections=4,
            auto_start=True,
            transport=transport,
        )

        start_wait = time.time()
        while item["status"] != DownloadStatus.QUEUED and time.time() - start_wait < 5.0:
            self.qt_app.processEvents()
            time.sleep(0.02)

        start_wait = time.time()
        while len(qm.active_workers) > 0 and time.time() - start_wait < 3.0:
            self.qt_app.processEvents()
            time.sleep(0.02)

        self.assertEqual(item["retry_count"], 1)
        self.assertIn("HTTP 503", item["error"])
        now_dt = datetime.now().astimezone()
        retry_dt = normalize_to_aware_datetime(item["next_retry_at"])
        diff = (retry_dt - now_dt).total_seconds()
        self.assertTrue(80 <= diff <= 100, f"Expected ~90s delay, got {diff}s")
        qm.shutdown(timeout_ms=500)

    def test_80_retry_after_short_vs_missing_header_exponential_backoff(self):
        """Short Retry-After (<=3s) retries internally; missing Retry-After uses exponential backoff; no stale headers."""
        qm = self.make_qm()

        # 1. Short Retry-After (1.0s) retries internally and succeeds on attempt 2
        call_count_short = 0
        data = b"S" * 20000

        def short_ra_handler(request: httpx.Request):
            nonlocal call_count_short
            call_count_short += 1
            if call_count_short == 1:
                return httpx.Response(503, headers={"Retry-After": "1"}, text="Brief glitch")
            return httpx.Response(200, headers={"Content-Length": str(len(data))}, content=data)

        t_short = httpx.MockTransport(short_ra_handler)
        i_short = qm.add_download("http://test/short_ra.bin", self.temp_dir, connections=1, auto_start=True, transport=t_short)

        start_wait = time.time()
        while i_short["status"] != DownloadStatus.COMPLETED and time.time() - start_wait < 5.0:
            self.qt_app.processEvents()
            time.sleep(0.02)

        self.assertEqual(i_short["status"], DownloadStatus.COMPLETED)
        self.assertEqual(call_count_short, 2)
        # Internal retry succeeded: queue retry count remains 0!
        self.assertEqual(i_short["retry_count"], 0)

        # 2. Missing Retry-After header: internal retries exhaust max_retries, then QueueManager applies exponential backoff
        call_count_missing = 0

        # Stop scheduler so automatic queue dispatch doesn't start attempt #2 while asserting attempt #1 state
        qm.scheduler.stop()

        def missing_ra_handler(request: httpx.Request):
            nonlocal call_count_missing
            call_count_missing += 1
            return httpx.Response(500, text="Internal Server Error without Retry-After")

        t_missing = httpx.MockTransport(missing_ra_handler)
        i_missing = qm.add_download("http://test/missing_ra.bin", self.temp_dir, connections=1, auto_start=True, transport=t_missing)

        # Wait until worker begins processing
        start_wait = time.time()
        while i_missing["status"] == DownloadStatus.QUEUED and len(qm.active_workers) == 0 and time.time() - start_wait < 2.0:
            self.qt_app.processEvents()
            time.sleep(0.01)

        # Wait until download fails and returns to QUEUED via exponential backoff
        start_wait = time.time()
        while i_missing["status"] != DownloadStatus.QUEUED and time.time() - start_wait < 8.0:
            self.qt_app.processEvents()
            time.sleep(0.02)

        start_wait = time.time()
        while len(qm.active_workers) > 0 and time.time() - start_wait < 5.0:
            self.qt_app.processEvents()
            time.sleep(0.02)

        self.assertEqual(call_count_missing, 3, "Missing Retry-After retried internally up to max_retries (3 total attempts)")
        self.assertEqual(i_missing["retry_count"], 1)
        # Attempt 1 exponential backoff is 5s
        now_dt = datetime.now().astimezone()
        retry_dt = normalize_to_aware_datetime(i_missing["next_retry_at"])
        diff = (retry_dt - now_dt).total_seconds()
        self.assertTrue(3 <= diff <= 8, f"Expected ~5s exponential backoff, got {diff}s")
        qm.shutdown(timeout_ms=500)

    def test_81_global_generation_disable_and_immediate_reenable_re_reserves(self):
        """Disabling and immediately re-enabling global limiter increments generation and forces re-reservation."""
        controller = BandwidthController(global_limit=1000, global_enabled=True)
        # Drain bucket so acquire must block
        controller.global_bucket.tokens = 0.0
        self.assertEqual(controller.global_generation, 0)

        waiting_event = threading.Event()
        acquired_event = threading.Event()

        def worker_task():
            # Signal after a tiny delay when worker is entering acquire
            def notify_waiting():
                time.sleep(0.02)
                waiting_event.set()
            threading.Thread(target=notify_waiting, daemon=True).start()
            controller.acquire(500)
            acquired_event.set()

        t = threading.Thread(target=worker_task)
        t.start()

        # Wait until worker is inside acquire
        self.assertTrue(waiting_event.wait(timeout=1.0))
        time.sleep(0.03)

        # Disable global limit (increments generation) and immediately re-enable before worker finishes
        gen_before = controller.global_generation
        controller.set_global_enabled(False)
        gen_disabled = controller.global_generation
        self.assertGreater(gen_disabled, gen_before)
        controller.set_global_enabled(True)

        # Replenish bucket (+1000 tokens) so re-reservation (-500) can complete
        time.sleep(0.05)
        with controller._cond:
            controller.global_bucket.tokens += 1000.0
            controller._cond.notify_all()

        t.join(timeout=1.0)
        self.assertTrue(acquired_event.is_set(), "Worker must complete after re-reserving against active limiter")
        # Ensure no bandwidth bypass: tokens must reflect the reservation (starts at -500 + 1000 = 500 < 1000)
        self.assertLess(controller.global_bucket.tokens, 1000.0)

    def test_82_global_generation_rate_change_while_waiting(self):
        """Changing global limit rate dynamically updates wait duration and wakes waiting workers promptly."""
        controller = BandwidthController(global_limit=500, global_enabled=True)
        controller.global_bucket.tokens = 0.0

        waiting_event = threading.Event()
        finished_event = threading.Event()

        def worker_task():
            def notify_waiting():
                time.sleep(0.02)
                waiting_event.set()
            threading.Thread(target=notify_waiting, daemon=True).start()
            t0 = time.time()
            controller.acquire(1000)
            elapsed = time.time() - t0
            # If rate wasn't updated, 1000 bytes at 500 bps would take 2.0s
            if elapsed < 0.6:
                finished_event.set()

        t = threading.Thread(target=worker_task)
        t.start()

        self.assertTrue(waiting_event.wait(timeout=1.0))
        time.sleep(0.03)

        # Boost rate to 100,000 bps
        controller.set_global_limit(100000)

        t.join(timeout=1.5)
        self.assertTrue(finished_event.is_set(), "Worker must finish quickly after dynamic rate increase")

    def test_83_global_generation_remove_individual_keeps_global_active(self):
        """Removing individual limit while global limit remains active continues enforcing global rate limit."""
        controller = BandwidthController(global_limit=500, global_enabled=True)
        controller.set_individual_limit("item_g", 1000)
        controller.global_bucket.tokens = 0.0

        waiting_event = threading.Event()
        worker_done = False

        def worker_task():
            nonlocal worker_done
            def notify_waiting():
                time.sleep(0.02)
                waiting_event.set()
            threading.Thread(target=notify_waiting, daemon=True).start()
            controller.acquire(1000, item_id="item_g")
            worker_done = True

        t = threading.Thread(target=worker_task)
        t.start()

        self.assertTrue(waiting_event.wait(timeout=1.0))
        time.sleep(0.03)

        # Remove individual limit
        controller.remove_individual_limit("item_g")

        # Global limiter (500 bps) must STILL be active, so worker should not be done immediately
        self.assertFalse(worker_done, "Worker must NOT bypass global limit when individual limit is removed")

        # Replenish global tokens to allow worker to finish cleanly
        with controller._cond:
            controller.global_bucket.tokens = 1000.0
            controller._cond.notify_all()

        t.join(timeout=1.0)
        self.assertTrue(worker_done)

    def test_84_global_generation_cancel_pause_and_multi_worker_concurrency(self):
        """Cancellation and pause after generation change refund properly; multiple workers share global bucket."""
        controller = BandwidthController(global_limit=1000, global_enabled=True)

        # 1. Cancel after generation change
        controller.global_bucket.tokens = 0.0
        cancelled = False
        waiting_cancel = threading.Event()

        cancel_raised = False
        def cancel_worker():
            nonlocal cancel_raised
            def notify():
                time.sleep(0.02)
                waiting_cancel.set()
            threading.Thread(target=notify, daemon=True).start()
            try:
                controller.acquire(500, is_cancelled=lambda: cancelled)
            except DownloadCancelledError:
                cancel_raised = True

        t_cancel = threading.Thread(target=cancel_worker)
        t_cancel.start()
        waiting_cancel.wait(timeout=1.0)
        time.sleep(0.02)

        # Invalidate global reservations (increment generation)
        controller.invalidate_global_reservations()
        cancelled = True
        with controller._cond:
            controller._cond.notify_all()
        t_cancel.join(timeout=1.0)
        self.assertTrue(cancel_raised)

        # 2. Pause after generation change
        controller.global_bucket.tokens = 0.0
        paused = False
        waiting_pause = threading.Event()
        pause_raised = False

        def pause_worker():
            nonlocal pause_raised
            def notify():
                time.sleep(0.02)
                waiting_pause.set()
            threading.Thread(target=notify, daemon=True).start()
            try:
                controller.acquire(500, is_paused=lambda: paused)
            except DownloadPausedError:
                pause_raised = True

        t_pause = threading.Thread(target=pause_worker)
        t_pause.start()
        waiting_pause.wait(timeout=1.0)
        time.sleep(0.02)

        controller.invalidate_global_reservations()
        paused = True
        with controller._cond:
            controller._cond.notify_all()
        t_pause.join(timeout=1.0)
        self.assertTrue(pause_raised)

        # 3. Multiple concurrent workers sharing global bucket
        controller.set_global_limit(500000)
        controller.set_global_enabled(True)
        results = []

        def concurrent_worker():
            controller.acquire(2000)
            results.append(True)

        threads = [threading.Thread(target=concurrent_worker) for _ in range(5)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=2.0)

        self.assertEqual(len(results), 5, "All concurrent workers must acquire bandwidth from shared global pool")

    # =========================================================================
    # 8. STAGE 5 FINAL SEGMENT RETRY FIXES (Tests 85-90)
    # =========================================================================

    def test_85_short_retry_after_handling_single_and_segmented(self):
        """Short Retry-After values (0.5, 1, 2, 2.5, 3s), >3s (3.1s), missing, and invalid headers in single and segmented."""
        class MockClock:
            def __init__(self):
                self.curr = 1000.0
                self.sleeps = []
            def fake_time(self):
                return self.curr
            def fake_sleep(self, d):
                self.sleeps.append(d)
                self.curr += d

        # 1. Single-connection tests
        test_delays = [0.5, 1.0, 2.0, 2.5, 3.0]
        for delay in test_delays:
            calls = 0
            def handler(request: httpx.Request):
                nonlocal calls
                calls += 1
                if calls == 1:
                    return httpx.Response(503, headers={"Retry-After": str(delay)}, text="Glitch")
                return httpx.Response(200, headers={"Content-Length": "10"}, content=b"0123456789")

            transport = httpx.MockTransport(handler)
            downloader = HttpDownloader(f"http://test/short_single_{delay}.bin", self.temp_dir, transport=transport)
            clock = MockClock()
            with patch("mdm.downloader.time.time", side_effect=clock.fake_time), \
                 patch("mdm.downloader.time.sleep", side_effect=clock.fake_sleep):
                dl_path, total = downloader.download()
            self.assertEqual(calls, 2)
            self.assertTrue(delay <= sum(clock.sleeps) <= delay + 0.06, f"Expected ~{delay}s, got {sum(clock.sleeps)}s")

        # 1b. Single-connection 3.1s (> 3.0s threshold): promotes immediately to queue-level retry
        calls_31 = 0
        def handler_31(request: httpx.Request):
            nonlocal calls_31
            calls_31 += 1
            return httpx.Response(503, headers={"Retry-After": "3.1"}, text="Long delay")

        dl_31 = HttpDownloader("http://test/single_31.bin", self.temp_dir, transport=httpx.MockTransport(handler_31))
        with self.assertRaises(DownloadError) as cm:
            dl_31.download()
        self.assertEqual(calls_31, 1)
        self.assertEqual(cm.exception.status_code, 503)
        self.assertEqual(cm.exception.retry_after, "3.1")

        # 1c. Single-connection missing header: uses min(attempt * 1.0, 3.0) = 1.0s
        calls_miss = 0
        def handler_miss(request: httpx.Request):
            nonlocal calls_miss
            calls_miss += 1
            if calls_miss == 1:
                return httpx.Response(503, text="No retry after")
            return httpx.Response(200, headers={"Content-Length": "10"}, content=b"0123456789")

        dl_miss = HttpDownloader("http://test/single_miss.bin", self.temp_dir, transport=httpx.MockTransport(handler_miss))
        clock_miss = MockClock()
        with patch("mdm.downloader.time.time", side_effect=clock_miss.fake_time), \
             patch("mdm.downloader.time.sleep", side_effect=clock_miss.fake_sleep):
            dl_miss.download()
        self.assertEqual(calls_miss, 2)
        self.assertTrue(1.0 <= sum(clock_miss.sleeps) <= 1.06, f"Expected ~1.0s, got {sum(clock_miss.sleeps)}s")

        # 1d. Single-connection invalid header: uses min(attempt * 1.0, 3.0) = 1.0s
        calls_inv = 0
        def handler_inv(request: httpx.Request):
            nonlocal calls_inv
            calls_inv += 1
            if calls_inv == 1:
                return httpx.Response(503, headers={"Retry-After": "not-a-number"}, text="Invalid header")
            return httpx.Response(200, headers={"Content-Length": "10"}, content=b"0123456789")

        dl_inv = HttpDownloader("http://test/single_inv.bin", self.temp_dir, transport=httpx.MockTransport(handler_inv))
        clock_inv = MockClock()
        with patch("mdm.downloader.time.time", side_effect=clock_inv.fake_time), \
             patch("mdm.downloader.time.sleep", side_effect=clock_inv.fake_sleep):
            dl_inv.download()
        self.assertEqual(calls_inv, 2)
        self.assertTrue(1.0 <= sum(clock_inv.sleeps) <= 1.06, f"Expected ~1.0s, got {sum(clock_inv.sleeps)}s")

        # 2. Segmented download tests: identical behaviour
        file_data = b"M" * 10000000
        for delay in test_delays:
            seg_attempts = {}
            seg_lock = threading.Lock()
            def seg_handler(request: httpx.Request, d=delay):
                rng = request.headers.get("Range")
                if rng == "bytes=0-0":
                    return httpx.Response(206, headers={
                        "Content-Range": f"bytes 0-0/{len(file_data)}",
                        "Content-Length": "1",
                    }, content=file_data[:1])
                m = re.match(r"bytes=(\d+)-(\d+)", rng)
                s, e = int(m.group(1)), int(m.group(2))
                with seg_lock:
                    att = seg_attempts.get(s, 0) + 1
                    seg_attempts[s] = att
                if s == 0 and att == 1:
                    return httpx.Response(503, headers={"Retry-After": str(d)}, text="Transient seg glitch")
                return httpx.Response(206, headers={
                    "Content-Range": f"bytes {s}-{e}/{len(file_data)}",
                    "Content-Length": str(e - s + 1),
                }, content=file_data[s:e+1])

            seg_dl = SegmentedDownloader(f"http://test/seg_short_{delay}.bin", self.temp_dir, connections=2, transport=httpx.MockTransport(seg_handler))
            clock_seg = MockClock()
            with patch("mdm.segment_manager.time.time", side_effect=clock_seg.fake_time), \
                 patch("mdm.segment_manager.time.sleep", side_effect=clock_seg.fake_sleep):
                dl_p, tot = seg_dl.download()
            self.assertEqual(seg_attempts.get(0, 0), 2)
            self.assertTrue(delay <= sum(clock_seg.sleeps) <= delay + 0.06, f"Expected ~{delay}s, got {sum(clock_seg.sleeps)}s")

        # 2b. Segmented 3.1s (> 3.0s): promotes immediately
        def seg_handler_31(request: httpx.Request):
            rng = request.headers.get("Range")
            if rng == "bytes=0-0":
                return httpx.Response(206, headers={
                    "Content-Range": f"bytes 0-0/{len(file_data)}",
                    "Content-Length": "1",
                }, content=file_data[:1])
            return httpx.Response(503, headers={"Retry-After": "3.1"}, text="Long seg delay")

        seg_dl_31 = SegmentedDownloader("http://test/seg_31.bin", self.temp_dir, connections=2, transport=httpx.MockTransport(seg_handler_31))
        with self.assertRaises(DownloadError) as cm_seg:
            seg_dl_31.download()
        self.assertEqual(cm_seg.exception.status_code, 503)
        self.assertEqual(cm_seg.exception.retry_after, "3.1")

        # 2c. Segmented missing header: uses min(attempt * 1.0, 3.0) = 1.0s
        seg_miss_attempts = {}
        seg_miss_lock = threading.Lock()
        def seg_handler_miss(request: httpx.Request):
            rng = request.headers.get("Range")
            if rng == "bytes=0-0":
                return httpx.Response(206, headers={
                    "Content-Range": f"bytes 0-0/{len(file_data)}",
                    "Content-Length": "1",
                }, content=file_data[:1])
            m = re.match(r"bytes=(\d+)-(\d+)", rng)
            s, e = int(m.group(1)), int(m.group(2))
            with seg_miss_lock:
                att = seg_miss_attempts.get(s, 0) + 1
                seg_miss_attempts[s] = att
            if s == 0 and att == 1:
                return httpx.Response(503, text="No retry after")
            return httpx.Response(206, headers={
                "Content-Range": f"bytes {s}-{e}/{len(file_data)}",
                "Content-Length": str(e - s + 1),
            }, content=file_data[s:e+1])

        seg_dl_miss = SegmentedDownloader("http://test/seg_miss.bin", self.temp_dir, connections=2, transport=httpx.MockTransport(seg_handler_miss))
        clock_seg_miss = MockClock()
        with patch("mdm.segment_manager.time.time", side_effect=clock_seg_miss.fake_time), \
             patch("mdm.segment_manager.time.sleep", side_effect=clock_seg_miss.fake_sleep):
            seg_dl_miss.download()
        self.assertEqual(seg_miss_attempts.get(0, 0), 2)
        self.assertTrue(1.0 <= sum(clock_seg_miss.sleeps) <= 1.06, f"Expected ~1.0s, got {sum(clock_seg_miss.sleeps)}s")

        # 2d. Segmented invalid header: uses min(attempt * 1.0, 3.0) = 1.0s
        seg_inv_attempts = {}
        seg_inv_lock = threading.Lock()
        def seg_handler_inv(request: httpx.Request):
            rng = request.headers.get("Range")
            if rng == "bytes=0-0":
                return httpx.Response(206, headers={
                    "Content-Range": f"bytes 0-0/{len(file_data)}",
                    "Content-Length": "1",
                }, content=file_data[:1])
            m = re.match(r"bytes=(\d+)-(\d+)", rng)
            s, e = int(m.group(1)), int(m.group(2))
            with seg_inv_lock:
                att = seg_inv_attempts.get(s, 0) + 1
                seg_inv_attempts[s] = att
            if s == 0 and att == 1:
                return httpx.Response(503, headers={"Retry-After": "garbage-date"}, text="Invalid header")
            return httpx.Response(206, headers={
                "Content-Range": f"bytes {s}-{e}/{len(file_data)}",
                "Content-Length": str(e - s + 1),
            }, content=file_data[s:e+1])

        seg_dl_inv = SegmentedDownloader("http://test/seg_inv.bin", self.temp_dir, connections=2, transport=httpx.MockTransport(seg_handler_inv))
        clock_seg_inv = MockClock()
        with patch("mdm.segment_manager.time.time", side_effect=clock_seg_inv.fake_time), \
             patch("mdm.segment_manager.time.sleep", side_effect=clock_seg_inv.fake_sleep):
            seg_dl_inv.download()
        self.assertEqual(seg_inv_attempts.get(0, 0), 2)
        self.assertTrue(1.0 <= sum(clock_seg_inv.sleeps) <= 1.06, f"Expected ~1.0s, got {sum(clock_seg_inv.sleeps)}s")

    def test_86_concurrent_segments_network_timeout_does_not_erase_retry_after(self):
        """Segment B network timeout does not erase Segment A's HTTP 503 Retry-After metadata."""
        file_size = 20000000
        seg_0_failed = threading.Event()

        def handler(request: httpx.Request):
            rng = request.headers.get("Range")
            if rng == "bytes=0-0":
                return httpx.Response(206, headers={
                    "Content-Range": f"bytes 0-0/{file_size}",
                    "Content-Length": "1",
                }, content=b"X")

            if rng.startswith("bytes=0-"):
                # Segment 0 fails fatal with HTTP 503 Retry-After 120
                seg_0_failed.set()
                return httpx.Response(503, headers={"Retry-After": "120"}, text="Service Unavailable")

            # Segment 1 waits for Segment 0 to fail first, then raises network timeout
            seg_0_failed.wait(timeout=2.0)
            time.sleep(0.02)
            raise httpx.ConnectTimeout("Network timeout on segment 1")

        transport = httpx.MockTransport(handler)
        downloader = SegmentedDownloader(
            "http://test/concurrent_timeout.bin",
            self.temp_dir,
            connections=2,
            transport=transport,
        )

        with self.assertRaises(DownloadError) as cm:
            downloader.download()

        self.assertEqual(cm.exception.status_code, 503)
        self.assertEqual(cm.exception.retry_after, "120")
        self.assertEqual(downloader.last_retry_after, "120")

    def test_87_concurrent_segments_both_fatal_retry_after_preserves_selected_metadata(self):
        """Selected fatal exception retains its own response metadata without cross-segment overwriting."""
        file_size = 20000000

        # Part 1: Segment 0 fails first with RA: 120; Segment 1 fails second with RA: 30
        seg_0_done = threading.Event()
        def handler_1(request: httpx.Request):
            rng = request.headers.get("Range")
            if rng == "bytes=0-0":
                return httpx.Response(206, headers={"Content-Range": f"bytes 0-0/{file_size}", "Content-Length": "1"}, content=b"X")
            if rng.startswith("bytes=0-"):
                seg_0_done.set()
                return httpx.Response(503, headers={"Retry-After": "120"}, text="Fail 120")
            seg_0_done.wait(timeout=2.0)
            time.sleep(0.02)
            return httpx.Response(503, headers={"Retry-After": "30"}, text="Fail 30")

        dl_1 = SegmentedDownloader("http://test/fatal_1.bin", self.temp_dir, connections=2, transport=httpx.MockTransport(handler_1))
        with self.assertRaises(DownloadError) as cm1:
            dl_1.download()
        self.assertEqual(cm1.exception.status_code, 503)
        self.assertEqual(cm1.exception.retry_after, "120")
        self.assertEqual(dl_1.last_retry_after, "120")

        # Part 2: Segment 1 fails first with RA: 30; Segment 0 fails second with RA: 120
        seg_1_done = threading.Event()
        def handler_2(request: httpx.Request):
            rng = request.headers.get("Range")
            if rng == "bytes=0-0":
                return httpx.Response(206, headers={"Content-Range": f"bytes 0-0/{file_size}", "Content-Length": "1"}, content=b"X")
            if rng.startswith("bytes=0-"):
                seg_1_done.wait(timeout=2.0)
                time.sleep(0.02)
                return httpx.Response(503, headers={"Retry-After": "120"}, text="Fail 120")
            seg_1_done.set()
            return httpx.Response(503, headers={"Retry-After": "30"}, text="Fail 30")

        dl_2 = SegmentedDownloader("http://test/fatal_2.bin", self.temp_dir, connections=2, transport=httpx.MockTransport(handler_2))
        with self.assertRaises(DownloadError) as cm2:
            dl_2.download()
        self.assertEqual(cm2.exception.status_code, 503)
        self.assertEqual(cm2.exception.retry_after, "30")
        self.assertEqual(dl_2.last_retry_after, "30")

    def test_88_segment_attempt_clears_stale_retry_after_on_subsequent_failure(self):
        """Segment clears Retry-After metadata per attempt; subsequent failure does not reuse stale value."""
        file_size = 20000000
        call_count = 0

        def handler(request: httpx.Request):
            nonlocal call_count
            rng = request.headers.get("Range")
            if rng == "bytes=0-0":
                return httpx.Response(206, headers={"Content-Range": f"bytes 0-0/{file_size}", "Content-Length": "1"}, content=b"X")
            call_count += 1
            if call_count == 1:
                # Attempt 1: short retry-after (1.0s)
                return httpx.Response(503, headers={"Retry-After": "1.0"}, text="Temporary")
            # Attempt 2 & 3: network error without any HTTP response headers
            raise httpx.NetworkError("Connection reset by peer")

        dl = SegmentedDownloader("http://test/stale_check.bin", self.temp_dir, connections=1, max_retries=3, transport=httpx.MockTransport(handler))
        with patch("mdm.segment_manager.time.sleep"):
            with self.assertRaises(DownloadError) as cm:
                dl.download()
        self.assertIsNone(cm.exception.retry_after, "Fatal network error must not have stale Retry-After from attempt 1")
        self.assertIsNone(dl.last_retry_after, "Downloader last_retry_after must be None when final error has no Retry-After")

    def test_89_segmented_all_retryable_status_codes_preserve_metadata(self):
        """Segmented engine preserves status_code and retry_after for HTTP 429, 500, 502, 503, 504."""
        file_size = 20000000
        for code in [429, 500, 502, 503, 504]:
            def handler(request: httpx.Request, c=code):
                rng = request.headers.get("Range")
                if rng == "bytes=0-0":
                    return httpx.Response(206, headers={"Content-Range": f"bytes 0-0/{file_size}", "Content-Length": "1"}, content=b"X")
                return httpx.Response(c, headers={"Retry-After": "60"}, text=f"HTTP {c} Error")

            dl = SegmentedDownloader(f"http://test/status_{code}.bin", self.temp_dir, connections=2, transport=httpx.MockTransport(handler))
            with self.assertRaises(DownloadError) as cm:
                dl.download()
            self.assertEqual(cm.exception.status_code, code)
            self.assertEqual(cm.exception.retry_after, "60")
            self.assertEqual(dl.last_retry_after, "60")

    def test_90_queue_manager_persists_correct_retry_timestamp_from_concurrent_segments(self):
        """QueueManager receives fatal metadata from concurrent segments and persists correct next_retry_at."""
        file_size = 20000000
        seg_0_failed = threading.Event()

        def handler(request: httpx.Request):
            rng = request.headers.get("Range")
            if rng == "bytes=0-0":
                return httpx.Response(206, headers={"Content-Range": f"bytes 0-0/{file_size}", "Content-Length": "1"}, content=b"X")
            if rng.startswith("bytes=0-"):
                seg_0_failed.set()
                return httpx.Response(503, headers={"Retry-After": "90"}, text="Service Unavailable")
            seg_0_failed.wait(timeout=2.0)
            time.sleep(0.02)
            raise httpx.ConnectTimeout("Timeout on sibling")

        qm = self.make_qm()
        item = qm.add_download(
            "http://test/qm_concurrent_seg.bin",
            self.temp_dir,
            connections=2,
            auto_start=True,
            transport=httpx.MockTransport(handler),
        )

        start_wait = time.time()
        while item["status"] != DownloadStatus.QUEUED and time.time() - start_wait < 5.0:
            self.qt_app.processEvents()
            time.sleep(0.02)

        start_wait = time.time()
        while len(qm.active_workers) > 0 and time.time() - start_wait < 3.0:
            self.qt_app.processEvents()
            time.sleep(0.02)

        self.assertEqual(len(qm.active_workers), 0, "All sibling workers must terminate safely after fatal failure")
        self.assertEqual(item["retry_count"], 1)
        self.assertIn("HTTP 503", item["error"])
        self.assertIn("90", item["error"])

        now_dt = datetime.now().astimezone()
        retry_dt = normalize_to_aware_datetime(item["next_retry_at"])
        diff = (retry_dt - now_dt).total_seconds()
        self.assertTrue(80 <= diff <= 100, f"Expected ~90s delay, got {diff}s")

        db_item = qm.db.get_download(item["id"])
        self.assertEqual(db_item.retry_count, 1)
        self.assertEqual(db_item.next_retry_at, item["next_retry_at"])
        qm.shutdown(timeout_ms=500)

    # =========================================================================
    # 9. STAGE 5 FINAL BANDWIDTH RESERVATION ACCOUNTING FIX (Tests 91-97)
    # =========================================================================

    def test_91_positive_global_tokens_individual_waiting_disable_reenable_no_double_charge(self):
        """Positive global token balance + individual waiting: global disable/re-enable does not double charge."""
        clock_val = 1000.0
        with patch("mdm.bandwidth_controller.time.monotonic", side_effect=lambda: clock_val):
            controller = BandwidthController(global_limit=10000, global_enabled=True)
            controller.global_bucket.tokens = 2000.0
            controller.set_individual_limit("item_a", 500)
            controller.item_buckets["item_a"].tokens = 0.0

            waiting_event = threading.Event()
            worker_done = [False]

            def worker_task():
                def notify_waiting():
                    time.sleep(0.02)
                    waiting_event.set()
                threading.Thread(target=notify_waiting, daemon=True).start()
                controller.acquire(1000, item_id="item_a")
                worker_done[0] = True

            t = threading.Thread(target=worker_task)
            t.start()

            self.assertTrue(waiting_event.wait(timeout=1.0))
            time.sleep(0.03)

            # Worker reserved 1000 globally: tokens dropped from 2000 to 1000
            # Now disable global limiter and immediately re-enable before worker finishes
            controller.set_global_enabled(False)
            # Upon disabling, the 1000 reservation was refunded, restoring tokens to 2000.0
            self.assertEqual(controller.global_bucket.tokens, 2000.0)
            controller.set_global_enabled(True)
            time.sleep(0.02)
            # Worker re-reserves 1000 against re-enabled global limiter
            self.assertEqual(controller.global_bucket.tokens, 1000.0)

            # Allow worker to proceed by giving individual bucket tokens
            with controller._cond:
                controller.item_buckets["item_a"].tokens = 1000.0
                controller._cond.notify_all()

            t.join(timeout=1.0)
            self.assertTrue(worker_done[0])
            # Global bucket must reflect exactly ONE charge of 1000 bytes (2000 - 1000 = 1000), NOT 0 (double charge)
            self.assertEqual(controller.global_bucket.tokens, 1000.0)

    def test_92_negative_global_tokens_disable_reenable_no_phantom_refund(self):
        """Negative global token balance + disable/re-enable: debt is reset without phantom token inflation."""
        clock_val = 1000.0
        with patch("mdm.bandwidth_controller.time.monotonic", side_effect=lambda: clock_val):
            controller = BandwidthController(global_limit=1000, global_enabled=True)
            controller.global_bucket.tokens = 0.0

            waiting_event = threading.Event()
            worker_done = [False]

            def worker_task():
                def notify_waiting():
                    time.sleep(0.02)
                    waiting_event.set()
                threading.Thread(target=notify_waiting, daemon=True).start()
                controller.acquire(1000)
                worker_done[0] = True

            t = threading.Thread(target=worker_task)
            t.start()

            self.assertTrue(waiting_event.wait(timeout=1.0))
            time.sleep(0.03)

            # Worker reserved 1000 globally -> tokens became -1000
            # Now disable global limiter
            controller.set_global_enabled(False)
            # Debt must be reset to 0.0, and no phantom refund added (tokens must be exactly 0.0, not 1000.0)
            self.assertEqual(controller.global_bucket.tokens, 0.0)

            # Re-enable global limiter
            controller.set_global_enabled(True)
            time.sleep(0.03)
            # Worker re-reserves 1000 against active limiter, establishing -1000 debt
            self.assertEqual(controller.global_bucket.tokens, -1000.0)

            # Supply tokens so worker can complete
            with controller._cond:
                controller.global_bucket.tokens = 1000.0
                controller._cond.notify_all()

            t.join(timeout=1.0)
            self.assertTrue(worker_done[0])

    def test_93_multiple_workers_outstanding_reservations_during_limiter_changes(self):
        """Multiple concurrent workers with reservations are reconciled accurately when limiter changes."""
        clock_val = 1000.0
        with patch("mdm.bandwidth_controller.time.monotonic", side_effect=lambda: clock_val):
            controller = BandwidthController(global_limit=10000, global_enabled=True)
            controller.global_bucket.tokens = 1500.0

            # Depleted individual buckets to keep workers waiting
            controller.set_individual_limit("w1", 500)
            controller.item_buckets["w1"].tokens = 0.0
            controller.set_individual_limit("w2", 500)
            controller.item_buckets["w2"].tokens = 0.0

            w1_waiting = threading.Event()
            w2_waiting = threading.Event()
            results = []

            def worker(item_id, evt):
                def notify():
                    time.sleep(0.02)
                    evt.set()
                threading.Thread(target=notify, daemon=True).start()
                controller.acquire(1000, item_id=item_id)
                results.append(item_id)

            t1 = threading.Thread(target=worker, args=("w1", w1_waiting))
            t2 = threading.Thread(target=worker, args=("w2", w2_waiting))
            t1.start()
            t2.start()

            self.assertTrue(w1_waiting.wait(timeout=1.0))
            self.assertTrue(w2_waiting.wait(timeout=1.0))
            time.sleep(0.03)

            # Worker 1 took 1000 (tokens: 1500 -> 500), Worker 2 took 1000 (tokens: 500 -> -500).
            # Total reserved = 2000, debt = 500.
            # Disabling must forgive 500 debt and refund 1500 paid tokens back to bucket:
            controller.set_global_enabled(False)
            self.assertEqual(controller.global_bucket.tokens, 1500.0)

            # Re-enable: both workers re-reserve 1000 each (total 2000), bringing tokens to -500
            controller.set_global_enabled(True)
            time.sleep(0.03)
            self.assertEqual(controller.global_bucket.tokens, -500.0)

            # Supply tokens to complete both workers
            with controller._cond:
                controller.item_buckets["w1"].tokens = 1000.0
                controller.item_buckets["w2"].tokens = 1000.0
                controller.global_bucket.tokens = 1000.0
                controller._cond.notify_all()

            t1.join(timeout=1.0)
            t2.join(timeout=1.0)
            self.assertEqual(len(results), 2)

    def test_94_cancellation_after_reservation_invalidation_no_double_refund(self):
        """Cancelling a download after reservation invalidation does not double refund tokens."""
        clock_val = 1000.0
        with patch("mdm.bandwidth_controller.time.monotonic", side_effect=lambda: clock_val):
            controller = BandwidthController(global_limit=5000, global_enabled=True)
            controller.global_bucket.tokens = 1000.0
            controller.set_individual_limit("item_d", 500)
            controller.item_buckets["item_d"].tokens = 0.0

            cancelled = [False]
            waiting_event = threading.Event()
            cancel_raised = [False]

            def worker_task():
                def notify():
                    time.sleep(0.02)
                    waiting_event.set()
                threading.Thread(target=notify, daemon=True).start()
                try:
                    controller.acquire(1000, item_id="item_d", is_cancelled=lambda: cancelled[0])
                except DownloadCancelledError:
                    cancel_raised[0] = True

            t = threading.Thread(target=worker_task)
            t.start()

            self.assertTrue(waiting_event.wait(timeout=1.0))
            time.sleep(0.03)
            self.assertEqual(controller.global_bucket.tokens, 0.0)

            # Invalidate global reservations: reconciles and refunds 1000
            controller.invalidate_global_reservations()
            self.assertEqual(controller.global_bucket.tokens, 1000.0)

            # Now cancel the worker
            cancelled[0] = True
            with controller._cond:
                controller._cond.notify_all()

            t.join(timeout=1.0)
            self.assertTrue(cancel_raised[0])
            # Tokens must remain 1000.0, NOT 2000.0 (no double refund)
            self.assertEqual(controller.global_bucket.tokens, 1000.0)

    def test_95_pause_after_reservation_invalidation_no_double_refund(self):
        """Pausing a download after reservation invalidation does not double refund tokens."""
        clock_val = 1000.0
        with patch("mdm.bandwidth_controller.time.monotonic", side_effect=lambda: clock_val):
            controller = BandwidthController(global_limit=5000, global_enabled=True)
            controller.global_bucket.tokens = 1000.0
            controller.set_individual_limit("item_e", 500)
            controller.item_buckets["item_e"].tokens = 0.0

            paused = [False]
            waiting_event = threading.Event()
            pause_raised = [False]

            def worker_task():
                def notify():
                    time.sleep(0.02)
                    waiting_event.set()
                threading.Thread(target=notify, daemon=True).start()
                try:
                    controller.acquire(1000, item_id="item_e", is_paused=lambda: paused[0])
                except DownloadPausedError:
                    pause_raised[0] = True

            t = threading.Thread(target=worker_task)
            t.start()

            self.assertTrue(waiting_event.wait(timeout=1.0))
            time.sleep(0.03)
            self.assertEqual(controller.global_bucket.tokens, 0.0)

            # Invalidate global reservations: reconciles and refunds 1000
            controller.invalidate_global_reservations()
            self.assertEqual(controller.global_bucket.tokens, 1000.0)

            # Now pause the worker
            paused[0] = True
            with controller._cond:
                controller._cond.notify_all()

            t.join(timeout=1.0)
            self.assertTrue(pause_raised[0])
            # Tokens must remain 1000.0, NOT 2000.0 (no double refund)
            self.assertEqual(controller.global_bucket.tokens, 1000.0)

    def test_96_individual_limit_removal_keeps_global_reservation_valid(self):
        """Removing individual limit while global limit remains active preserves global reservation and enforcement."""
        clock_val = 1000.0
        with patch("mdm.bandwidth_controller.time.monotonic", side_effect=lambda: clock_val):
            controller = BandwidthController(global_limit=500, global_enabled=True)
            controller.global_bucket.tokens = 0.0
            controller.set_individual_limit("item_f", 1000)
            controller.item_buckets["item_f"].tokens = 0.0

            waiting_event = threading.Event()
            worker_done = [False]

            def worker_task():
                def notify():
                    time.sleep(0.02)
                    waiting_event.set()
                threading.Thread(target=notify, daemon=True).start()
                controller.acquire(1000, item_id="item_f")
                worker_done[0] = True

            t = threading.Thread(target=worker_task)
            t.start()

            self.assertTrue(waiting_event.wait(timeout=1.0))
            time.sleep(0.03)
            # Global reservation was made: tokens is -1000
            self.assertEqual(controller.global_bucket.tokens, -1000.0)

            # Remove individual limit
            controller.remove_individual_limit("item_f")
            time.sleep(0.02)
            # Worker must still be throttled by global limiter
            self.assertFalse(worker_done[0])
            self.assertEqual(controller.global_bucket.tokens, -1000.0)

            # Replenish global tokens to finish
            with controller._cond:
                controller.global_bucket.tokens = 1000.0
                controller._cond.notify_all()

            t.join(timeout=1.0)
            self.assertTrue(worker_done[0])

    def test_97_repeated_enable_disable_cycles_exact_accounting(self):
        """Repeated enable/disable cycles reconcile reservations exactly once per cycle without token leakage."""
        clock_val = 1000.0
        with patch("mdm.bandwidth_controller.time.monotonic", side_effect=lambda: clock_val):
            controller = BandwidthController(global_limit=10000, global_enabled=True)
            controller.global_bucket.tokens = 2000.0
            controller.set_individual_limit("item_g", 100)
            controller.item_buckets["item_g"].tokens = 0.0

            waiting_event = threading.Event()
            worker_done = [False]

            def worker_task():
                def notify():
                    time.sleep(0.02)
                    waiting_event.set()
                threading.Thread(target=notify, daemon=True).start()
                controller.acquire(1000, item_id="item_g")
                worker_done[0] = True

            t = threading.Thread(target=worker_task)
            t.start()

            self.assertTrue(waiting_event.wait(timeout=1.0))
            time.sleep(0.03)

            # Run 5 consecutive enable/disable cycles while worker is waiting on individual bucket
            for _ in range(5):
                controller.set_global_enabled(False)
                self.assertEqual(controller.global_bucket.tokens, 2000.0)
                controller.set_global_enabled(True)
                time.sleep(0.02)
                self.assertEqual(controller.global_bucket.tokens, 1000.0)

            # Supply individual tokens so worker finishes
            with controller._cond:
                controller.item_buckets["item_g"].tokens = 1000.0
                controller._cond.notify_all()

            t.join(timeout=1.0)
            self.assertTrue(worker_done[0])
            # Final token balance must be exactly 1000.0 (one single 1000 charge from 2000.0)
            self.assertEqual(controller.global_bucket.tokens, 1000.0)


if __name__ == "__main__":
    unittest.main()
