"""
Stage 6B repair - PDF and image automatic capture.

* Image extension defaults and the v1 -> v2 configuration upgrade.
* Settings panel: Images group and the "Last capture decision" diagnostics.
* report_capture_decision validation (privacy) and bounded storage.
* Content-Disposition filename parsing used by the desktop probe.
* End-to-end PDF and image captures against a LOCAL HTTP server through the
  real native host -> IPC -> CaptureManager -> QueueManager -> download engine.
"""
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "native_host"))
sys.path.insert(0, str(ROOT / "tests"))

from mdm.browser_integration import capture_rules as rules  # noqa: E402
from mdm.browser_integration import ipc_protocol as proto  # noqa: E402
from mdm.browser_integration.capture import CaptureManager  # noqa: E402
from mdm.browser_integration.request_validator import ValidationError, validate_capture_decision  # noqa: E402
from mdm.browser_integration.service import BrowserIntegrationService  # noqa: E402
from mdm.browser_integration.settings_widget import BrowserCaptureSettingsPanel, describe_decision  # noqa: E402
from mdm.database import DatabaseManager  # noqa: E402
from mdm.models import DownloadStatus  # noqa: E402
from mdm.queue_manager import QueueManager  # noqa: E402
from mdm.utils import parse_content_disposition  # noqa: E402
import host  # noqa: E402
import test_stage6b_integration as base_it  # noqa: E402  (module import: its TestCase is not re-collected here)
from test_stage6b_integration import _Handler, _QuietServer, pump_until  # noqa: E402
from verify_host import MockAppServer  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

IMAGES = ["jpg", "jpeg", "png", "gif", "webp", "bmp", "tif", "tiff", "ico"]


def decision(**over):
    d = {"at": 1_700_000_000_000, "download_id": 12, "browser": "chrome", "resource_type": "main_frame",
         "method": "GET", "method_source": "memory", "from_cache": False, "filename": "report.pdf",
         "extension": "pdf", "mime": "application/pdf", "state": "interrupted", "paused": False,
         "by_extension": False, "initiator": "browser", "mode": "automatic", "decision": "committed", "reason": "committed"}
    d.update(over)
    return d


# ===========================================================================
# Defaults and configuration upgrade
# ===========================================================================
class ImageDefaultsTests(unittest.TestCase):

    def test_images_group_and_default_list(self):
        self.assertEqual(rules.DEFAULT_EXTENSION_GROUPS["Images"], IMAGES)
        self.assertEqual(rules.IMAGE_EXTENSIONS, IMAGES)
        for e in IMAGES:
            self.assertIn(e, rules.DEFAULT_EXTENSIONS)
        self.assertEqual(len(rules.DEFAULT_EXTENSIONS), len(set(rules.DEFAULT_EXTENSIONS)), "no duplicates")
        self.assertEqual(rules.CONFIG_VERSION, 2)

    def test_fresh_configuration_enables_images(self):
        cfg = rules.default_config()
        self.assertTrue(all(cfg["extensions"][e] for e in IMAGES))
        public = rules.public_config(dict(cfg, enabled=True))
        for e in IMAGES:
            self.assertIn(e, public["extensions"], "synchronised to the browser")
        self.assertEqual(rules.public_config(cfg)["mode"], "disabled", "capture itself stays off by default")

    def test_python_image_matching(self):
        for name, ext in (("photo.JPG", "jpg"), ("C:\\dl\\scan.jpeg", "jpeg"), ("a.png", "png"), ("b.gif", "gif"),
                          ("c.webp", "webp"), ("d.bmp", "bmp"), ("e.TIF", "tif"), ("f.tiff", "tiff"), ("favicon.ico", "ico"),
                          ("Report.PDF", "pdf")):
            self.assertEqual(rules.extension_matches(name, set(rules.DEFAULT_EXTENSIONS)), ext, name)
        for name in ("photo.heic", "vector.svg", "png", ".png"):
            self.assertIsNone(rules.extension_matches(name, set(rules.DEFAULT_EXTENSIONS)), name)
        self.assertEqual(rules.filename_from_url("https://e.com/i/My%20Photo.PNG?name=x.exe&sig=a"), "My Photo.PNG")

    def test_existing_v1_configuration_keeps_user_preferences(self):
        v1_exts = {e: True for e in rules.DEFAULT_EXTENSIONS if e not in IMAGES}
        v1_exts.update(zip=False, txt=False, pdf=False)
        v1 = {"version": 1, "enabled": True, "mode": "automatic", "extensions": v1_exts,
              "custom_extensions": ["epub", "webp"], "excluded_sites": ["youtube.com"]}
        with tempfile.TemporaryDirectory() as tmp:
            db = DatabaseManager(Path(tmp) / "s.db")
            db.set_setting(rules.CONFIG_SETTING_KEY, json.dumps(v1))
            cfg = rules.CaptureSettingsStore(db).get()
            self.assertEqual(rules.effective_mode(cfg), "automatic", "mode preserved")
            self.assertFalse(cfg["extensions"]["zip"])
            self.assertFalse(cfg["extensions"]["txt"])
            self.assertFalse(cfg["extensions"]["pdf"], "a saved 'off' is never overwritten")
            self.assertTrue(cfg["extensions"]["exe"])
            for e in IMAGES:
                self.assertTrue(cfg["extensions"][e], f"new default {e} introduced")
            self.assertEqual(cfg["custom_extensions"], ["epub"], "custom 'webp' became the built-in one")
            self.assertEqual(cfg["excluded_sites"], ["youtube.com"])
            self.assertEqual(cfg["version"], 2)
            db.close()

    def test_saved_image_choices_survive_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = DatabaseManager(Path(tmp) / "s.db")
            store = rules.CaptureSettingsStore(db)
            cfg = store.get()
            cfg["extensions"]["png"] = False
            store.save(dict(cfg, custom_extensions=["png", "heic"]))
            db.close()
            db2 = DatabaseManager(Path(tmp) / "s.db")
            again = rules.CaptureSettingsStore(db2).get()
            self.assertFalse(again["extensions"]["png"], "explicit choice wins over a stale custom entry")
            self.assertEqual(again["custom_extensions"], ["heic"])
            self.assertTrue(again["extensions"]["jpg"])
            db2.close()


class ContentDispositionTests(unittest.TestCase):

    def test_pdf_and_image_filenames(self):
        cases = {
            'attachment; filename="Q3 report.pdf"': "Q3 report.pdf",
            "attachment; filename=Annual report.pdf; size=10": "Annual report.pdf",
            "inline;filename=photo.PNG": "photo.PNG",
            "attachment; filename*=UTF-8''r%C3%A9sum%C3%A9.pdf; filename=\"resume.pdf\"": "résumé.pdf",
            'attachment; filename="a\\"b.jpg"': 'a"b.jpg',
            "attachment": None,
        }
        for header, expected in cases.items():
            self.assertEqual(parse_content_disposition(header), expected, header)


# ===========================================================================
# Diagnostics
# ===========================================================================
class DecisionValidationTests(unittest.TestCase):

    def test_valid_decision_is_normalised(self):
        out = validate_capture_decision({"action": "report_capture_decision", "browser": "edge", "decision": decision()})
        self.assertEqual(out["browser"], "edge")
        self.assertEqual(out["decision"]["filename"], "report.pdf")
        self.assertEqual(out["decision"]["method"], "GET")

    def test_no_url_can_be_stored(self):
        with self.assertRaises(ValidationError) as ctx:
            validate_capture_decision({"browser": "chrome", "decision": decision(url="https://e.com/a.pdf?sig=1")})
        self.assertEqual(ctx.exception.code, "invalid_field", "there is no URL field")
        with self.assertRaises(ValidationError):
            validate_capture_decision({"browser": "chrome", "cookie": "sid=1", "decision": decision()})
        out = validate_capture_decision({"browser": "chrome", "decision": decision(
            filename="https://e.com/a.pdf?token=SECRET", mime="text/html; x", method="get", reason="Bad Reason!",
            resource_type="<script>", extension="p.d.f", download_id=-4, paused="yes")})["decision"]
        self.assertIsNone(out["filename"])
        self.assertNotIn("SECRET", json.dumps(out))
        for key in ("mime", "method", "reason", "resource_type", "extension", "download_id", "paused"):
            self.assertIsNone(out[key], key)
        with self.assertRaises(ValidationError):
            validate_capture_decision({"browser": "chrome", "decision": "committed"})
        with self.assertRaises(ValidationError):
            validate_capture_decision({"browser": "firefox", "decision": decision()})

    def test_paths_in_filenames_are_dropped(self):
        out = validate_capture_decision({"decision": decision(filename="C:\\Users\\me\\Downloads\\x.pdf")})["decision"]
        self.assertIsNone(out["filename"])
        long = validate_capture_decision({"decision": decision(filename="a" * 500 + ".png")})["decision"]
        self.assertEqual(len(long["filename"]), 120)

    def test_host_forwards_only_validated_decisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            endpoint = Path(tmp) / "endpoint.json"
            with MockAppServer() as app_srv:
                proto.write_endpoint_atomic(endpoint, app_srv.port, app_srv.token)
                bad = host.handle_message({"action": "report_capture_decision", "browser": "chrome",
                                           "decision": decision(url="https://e.com/x.pdf")}, endpoint, config={})
                self.assertEqual(bad["error_code"], "invalid_field")
                self.assertEqual(app_srv.received, [], "rejected before IPC")
                host.handle_message({"action": "report_capture_decision", "browser": "chrome",
                                     "decision": decision()}, endpoint, config={})
            env = app_srv.received[0]
            self.assertEqual(env["action"], "report_capture_decision")
            self.assertEqual(env["payload"]["decision"]["decision"], "committed")
            self.assertIn("report_capture_decision", proto.IPC_ACTIONS)


class DecisionStorageAndPanelTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.db = DatabaseManager(tmp / "downloads.db")
        self.queue = QueueManager(db=self.db)
        self.capture = CaptureManager(self.queue, folder_provider=lambda: tmp / "dl")
        self.capture.start()
        self.panel = BrowserCaptureSettingsPanel(self.capture, registration_reader=lambda: {})

    def tearDown(self):
        self.panel.deleteLater()
        self.capture.stop()
        self.queue.shutdown()
        self.capture.deleteLater()
        self.queue.deleteLater()
        QCoreApplication.processEvents()
        self.db.close()
        self._tmp.cleanup()

    def test_record_decision_is_bounded_and_shown(self):
        self.assertIn("No capture decision", self.panel.last_decision.text())
        status, data, _ = self.capture.record_decision({"browser": "chrome", "decision": decision(
            decision="resumed", reason="method_unknown", filename="manual.pdf", method=None, download_id=7)})
        self.assertEqual((status, data), ("ok", {"recorded": True}))
        text = self.panel.last_decision.text()
        for part in ("manual.pdf", "Left in the browser", "method unknown", "Google Chrome", "download #7"):
            self.assertIn(part, text)
        for i in range(70):
            self.capture.record_decision({"browser": "edge", "decision": decision(download_id=i, filename=f"p{i}.png")})
        self.assertEqual(len(self.capture.decisions), CaptureManager.MAX_DECISIONS)
        self.assertEqual(self.capture.latest_decision()["download_id"], 69)
        self.assertEqual(self.panel.decision_list.count(), 10)
        self.assertIn("p69.png — Sent to My Download Manager", self.panel.last_decision.text())
        self.assertIn("Microsoft Edge", self.panel.last_decision.text())

    def test_decision_from_viewer_mentions_initiator(self):
        line = describe_decision(decision(by_extension=True, initiator="browser_pdf_viewer", decision="committed"))
        self.assertIn("started by browser pdf viewer", line)
        self.assertNotIn("://", line)

    def test_image_checkboxes_in_settings(self):
        for e in IMAGES:
            self.assertIn(e, self.panel.ext_checkboxes)
            self.assertTrue(self.panel.ext_checkboxes[e].isChecked(), f"{e} enabled by default")
        self.panel.ext_checkboxes["gif"].setChecked(False)
        self.assertFalse(self.capture.get_config()["extensions"]["gif"])
        self.assertNotIn("gif", self.capture.config_payload()["extensions"], "synchronised to the browser config")
        self.panel.custom_input.setText(".WEBP")
        self.panel.add_custom_extension()
        self.assertTrue(self.capture.get_config()["extensions"]["webp"])
        self.assertEqual(self.capture.get_config()["custom_extensions"], [])


# ===========================================================================
# End-to-end against a local HTTP server
# ===========================================================================
JPG = b"\xff\xd8\xff\xe0" + os.urandom(30_000)
PNG = b"\x89PNG\r\n\x1a\n" + os.urandom(25_000)
GIF = b"GIF89a" + os.urandom(9_000)
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + os.urandom(12_000)
BMP = b"BM" + os.urandom(15_000)
PDF = b"%PDF-1.7\n" + os.urandom(40_000)
MEDIA = {
    "/img/photo.jpg": (JPG, "image/jpeg"), "/img/scan.jpeg": (JPG, "image/jpeg"), "/img/logo.png": (PNG, "image/png"),
    "/img/anim.gif": (GIF, "image/gif"), "/img/pic.webp": (WEBP, "image/webp"), "/img/old.bmp": (BMP, "image/bmp"),
    "/img/photo.heic": (JPG, "image/heic"),
    "/docs/report.pdf": (PDF, "application/pdf"), "/docs/REPORT.PDF": (PDF, "application/pdf"),
}


class _MediaHandler(_Handler):

    def do_GET(self):  # noqa: N802
        parts = urlsplit(self.path)
        q = parse_qs(parts.query)
        path = parts.path
        if path in MEDIA:
            self.server.state["requests"].append((self.command, self.path, self.headers.get("Range"),
                                                  self.headers.get("Cookie"), self.headers.get("User-Agent") or ""))
            data, ctype = MEDIA[path]
            return self._serve(data, ctype)
        if path == "/img/r/7":
            return self._send(302, b"", {"Location": "/img/logo.png"})
        if path == "/media":
            return self._serve(PNG, "image/png", {"Content-Disposition": 'inline; filename="holiday photo.png"'})
        if path == "/signed/report.pdf":
            if q.get("X-Amz-Signature") != ["abc/def=="] or q.get("token") != ["t0p53cr3t"]:
                return self._send(403, b"bad signature")
            return self._serve(PDF, "application/pdf")
        if path == "/get/42":
            return self._send(302, b"", {"Location": "/docs/report.pdf?sig=keep"})
        if path == "/export":
            return self._serve(PDF, "application/pdf",
                               {"Content-Disposition": "attachment; filename*=UTF-8''Rapport%20%C3%A9t%C3%A9.pdf"})
        if path == "/private/payslip.pdf":
            if not self.headers.get("Cookie"):
                return self._send(401, b"login required", {"WWW-Authenticate": "Basic"})
            return self._serve(PDF, "application/pdf")
        if path == "/sso/statement.pdf":
            return self._send(200, b"<html><body>Sign in</body></html>", {"Content-Type": "text/html"})
        return super().do_GET()


class PdfImageCaptureIntegrationTests(unittest.TestCase):
    """Same browser emulation as test_stage6b_integration (shared helpers)."""

    native = base_it.CaptureIntegrationTests.native
    prepare = base_it.CaptureIntegrationTests.prepare
    wait_decision = base_it.CaptureIntegrationTests.wait_decision
    capture = base_it.CaptureIntegrationTests.capture
    wait_download = base_it.CaptureIntegrationTests.wait_download
    assert_file = base_it.CaptureIntegrationTests.assert_file
    stop_app = base_it.CaptureIntegrationTests.stop_app

    @classmethod
    def setUpClass(cls):
        cls.httpd = _QuietServer(("127.0.0.1", 0), _MediaHandler)
        cls.httpd.state = {"requests": [], "expiring_hits": 0}
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.server_thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.httpd.state["requests"].clear()
        self.endpoint = self.tmp / "endpoint.json"
        self.db = DatabaseManager(self.tmp / "downloads.db")
        self.queue = QueueManager(db=self.db)
        self.service = BrowserIntegrationService(self.queue, folder_provider=lambda: self.tmp / "dl",
                                                 endpoint_path=self.endpoint)
        cfg = self.service.capture.get_config()   # fresh defaults: images and PDFs enabled
        cfg.update(enabled=True, mode="automatic")
        self.service.capture.update_config(cfg)
        self.assertTrue(self.service.start())
        self._bdid = 500

    def tearDown(self):
        self.stop_app()
        self._tmp.cleanup()

    def captured_file(self, path, rid, data, name, **kw):
        d, c = self.capture(f"{self.base}{path}", rid, **kw)
        self.assertEqual(d["data"]["state"], "approved", d)
        item = self.wait_download(c["data"]["download_id"])
        self.assert_file(item, data, name)
        return d, c, item

    # --------------------------------------------------------------- images
    def test_image_formats_automatic_capture(self):
        for path, data, name, mime in (("/img/photo.jpg", JPG, "photo.jpg", "image/jpeg"),
                                       ("/img/scan.jpeg", JPG, "scan.jpeg", "image/jpeg"),
                                       ("/img/logo.png", PNG, "logo.png", "image/png"),
                                       ("/img/anim.gif", GIF, "anim.gif", "image/gif"),
                                       ("/img/pic.webp", WEBP, "pic.webp", "image/webp"),
                                       ("/img/old.bmp", BMP, "old.bmp", "image/bmp")):
            with self.subTest(name=name):
                rid = "it-img-" + name.replace(".", "-").ljust(8, "0")
                self.captured_file(path, rid, data, name, filename=name, mime=mime)
        self.assertTrue(all(r[3] is None for r in self.httpd.state["requests"]), "no cookies are ever sent")

    def test_unsupported_image_format_stays_in_browser(self):
        r = self.prepare(f"{self.base}/img/photo.heic", "it-heic-000001", filename="photo.heic", mime="image/heic")
        self.assertEqual((r["data"]["state"], r["data"]["reason"]), ("declined", "extension_not_selected"))
        self.assertEqual(self.httpd.state["requests"], [], "not even probed")
        self.assertEqual(self.queue.items, [])

    def test_image_get_only_and_unknown_method(self):
        for method in ("POST", None, "get"):
            r = self.native({"action": "prepare_capture", "request_id": f"it-imgm-{method or 'none'}00",
                             "url": f"{self.base}/img/logo.png", "filename": "logo.png", "browser": "chrome",
                             "browser_download_id": 9, "method": method, "danger": "safe", "incognito": False})
            self.assertEqual(r["error_code"], "unsupported_method", method)
        self.assertEqual(self.httpd.state["requests"], [], "never replayed")

    def test_image_redirect(self):
        d, c, item = self.captured_file("/img/r/7", "it-imgredir-01", PNG, "logo.png", filename="logo.png")
        self.assertEqual(item["url"], f"{self.base}/img/r/7")

    def test_image_content_disposition_filename(self):
        d, c, item = self.captured_file("/media", "it-imgcd-00001", PNG, "holiday photo.png")  # no browser filename
        self.assertEqual(d["data"]["filename"], "holiday photo.png")

    # ----------------------------------------------------------------- PDFs
    def test_direct_pdf_attachment(self):
        self.captured_file("/docs/report.pdf", "it-pdf-000001", PDF, "report.pdf", filename="report.pdf",
                           mime="application/pdf")

    def test_pdf_uppercase_extension(self):
        self.captured_file("/docs/REPORT.PDF", "it-pdfup-00001", PDF, "REPORT.PDF", filename="REPORT.PDF")

    def test_pdf_signed_query(self):
        path = "/signed/report.pdf?X-Amz-Signature=abc%2Fdef%3D%3D&token=t0p53cr3t&file=evil.exe"
        with self.assertLogs("mdm", level="INFO") as logs:
            d, c, item = self.captured_file(path, "it-pdfsig-0001", PDF, "report.pdf", filename="report.pdf")
        self.assertEqual(item["url"], f"{self.base}{path}", "signed URL forwarded byte-for-byte")
        text = "\n".join(logs.output)
        for secret in ("abc%2Fdef", "t0p53cr3t", "X-Amz-Signature", "evil.exe"):
            self.assertNotIn(secret, text)

    def test_pdf_redirect(self):
        d, c, item = self.captured_file("/get/42", "it-pdfredir-01", PDF, "report.pdf", filename="report.pdf")
        self.assertEqual(item["url"], f"{self.base}/get/42")

    def test_pdf_without_url_extension_uses_content_disposition(self):
        d, c, item = self.captured_file("/export", "it-pdfcd-00001", PDF, "Rapport été.pdf")
        self.assertEqual(d["data"]["filename"], "Rapport été.pdf")

    def test_pdf_viewer_download_button_request(self):
        # What the extension sends for the viewer's Download button: an ordinary
        # GET capture of the viewed URL with the browser's PDF filename.
        self.captured_file("/docs/report.pdf", "it-pdfview-001", PDF, "report.pdf", filename="report.pdf",
                           mime="application/pdf")

    def test_authentication_required_pdf_stays_in_browser(self):
        d, c = self.capture(f"{self.base}/private/payslip.pdf", "it-pdfauth-001", filename="payslip.pdf")
        self.assertEqual((d["data"]["state"], d["data"]["reason"]), ("declined", "auth_required"))
        self.assertIsNone(c)
        d, c = self.capture(f"{self.base}/sso/statement.pdf", "it-pdfsso-0001", filename="statement.pdf",
                            mime="application/pdf")
        self.assertEqual(d["data"]["reason"], "html_response", "sign-in page instead of the PDF")
        self.assertEqual(self.queue.items, [])

    def test_pdf_when_desktop_app_is_closed(self):
        self.service.stop()
        r = self.prepare(f"{self.base}/docs/report.pdf", "it-pdfdown-001", filename="report.pdf")
        self.assertEqual(r["error_code"], "app_not_running")
        self.assertEqual(self.httpd.state["requests"], [], "nothing fetched; the browser keeps its download")
        self.service.start()

    def test_duplicate_pdf_commit_creates_one_download(self):
        url = f"{self.base}/docs/report.pdf"
        d, c = self.capture(url, "it-pdfdup-0001", filename="report.pdf")
        again = self.native({"action": "commit_capture", "request_id": "it-pdfdup-0001", "url": url, "browser": "chrome"})
        self.assertEqual(again["status"], "duplicate")
        self.wait_download(c["data"]["download_id"])
        self.assertEqual(len(self.db.get_all_downloads()), 1)

    # ----------------------------------------------------------- diagnostics
    def test_decision_report_reaches_settings_through_native_host(self):
        r = self.native({"action": "report_capture_decision", "browser": "edge",
                         "decision": decision(browser="edge", filename="manual.pdf", decision="resumed",
                                              reason="method_unknown", method=None)})
        self.assertTrue(r["ok"], r)
        pump_until(lambda: self.service.capture.latest_decision() is not None)
        latest = self.service.capture.latest_decision()
        self.assertEqual((latest["filename"], latest["reason"], latest["browser"]), ("manual.pdf", "method_unknown", "edge"))


if __name__ == "__main__":
    unittest.main()
