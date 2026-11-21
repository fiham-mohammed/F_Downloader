"""
Stage 6A - browser extension validation and native host executable checks.

* Static validation of the Manifest V3 extension (permissions, files, CSP-safe pages).
* Behavioural tests of the service worker / popup logic, executed with Node.js
  against a mocked ``chrome`` API (tests/js/extension_tests.mjs). Skipped if
  Node.js is not installed.
* End-to-end verification of the native host from source and, when built, of
  native_host/dist/MyDownloadManagerNativeHost.exe.
"""
import json
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXT = ROOT / "browser_extension"
NATIVE = ROOT / "native_host"
sys.path.insert(0, str(NATIVE))
sys.path.insert(0, str(ROOT))

import verify_host  # noqa: E402


class ManifestV3Tests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads((EXT / "manifest.json").read_text(encoding="utf-8"))

    def test_manifest_v3_validity(self):
        m = self.manifest
        self.assertEqual(m["manifest_version"], 3)
        self.assertEqual(m["name"], "My Download Manager")
        self.assertRegex(m["version"], r"^\d+(\.\d+){0,3}$")
        self.assertLessEqual(len(m["description"]), 132)
        self.assertEqual(m["background"], {"service_worker": "service_worker.js", "type": "module"})
        self.assertNotIn("browser_action", m, "Manifest V2 key")
        self.assertNotIn("page_action", m, "Manifest V2 key")
        self.assertNotIn("persistent", m.get("background", {}))
        self.assertEqual(m["action"]["default_popup"], "popup.html")

    def test_minimum_permissions(self):
        perms = set(self.manifest["permissions"])
        self.assertTrue({"contextMenus", "nativeMessaging", "storage"} <= perms)
        # "notifications" is the only addition, needed for the required browser notification.
        self.assertTrue(perms <= {"contextMenus", "nativeMessaging", "storage", "notifications"}, perms)
        for forbidden in ("host_permissions", "optional_permissions", "content_scripts",
                          "externally_connectable", "web_accessible_resources"):
            self.assertNotIn(forbidden, self.manifest)
        for dangerous in ("cookies", "tabs", "downloads", "webRequest", "<all_urls>", "history", "debugger"):
            self.assertNotIn(dangerous, perms)

    def test_referenced_files_exist(self):
        m = self.manifest
        paths = [m["background"]["service_worker"], m["action"]["default_popup"],
                 *m["icons"].values(), *m["action"]["default_icon"].values(),
                 "popup.js", "styles.css", "lib/validation.js", "instructions.html", "instructions.js"]
        for rel in paths:
            self.assertTrue((EXT / rel).is_file(), rel)
        for size, rel in m["icons"].items():
            data = (EXT / rel).read_bytes()
            self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n", rel)
            width = int.from_bytes(data[16:20], "big")
            self.assertEqual(width, int(size), rel)

    def test_pages_are_csp_compliant(self):
        for page in ("popup.html", "instructions.html"):
            html = (EXT / page).read_text(encoding="utf-8")
            for tag in re.findall(r"<script\b[^>]*>(.*?)</script>", html, re.S):
                self.assertEqual(tag.strip(), "", f"inline script in {page}")
            self.assertNotRegex(html, r"\son[a-z]+\s*=", f"inline event handler in {page}")
            self.assertNotRegex(html, r"(src|href)\s*=\s*\"(https?:)?//", f"remote resource in {page}")

    def test_no_eval_or_remote_code(self):
        for js in list(EXT.rglob("*.js")):
            src = js.read_text(encoding="utf-8")
            for bad in ("eval(", "new Function", "innerHTML", "document.write", "importScripts(\"http",
                        "chrome.cookies", "chrome.downloads", "chrome.extension."):
                self.assertNotIn(bad, src, f"{bad} in {js.name}")

    def test_context_menu_declared_for_links_only(self):
        src = (EXT / "service_worker.js").read_text(encoding="utf-8")
        self.assertIn('"Download with My Download Manager"', src)
        self.assertIn('contexts: ["link"]', src)
        self.assertIn('"com.mydownloadmanager.native"', (EXT / "lib" / "validation.js").read_text(encoding="utf-8"))

    def test_no_automatic_download_interception(self):
        """Stage 6A must not intercept browser downloads (that is Stage 6B)."""
        for js in EXT.rglob("*.js"):
            src = js.read_text(encoding="utf-8")
            self.assertNotIn("onDeterminingFilename", src)
            self.assertNotIn("downloads.onCreated", src)

    def test_native_host_manifest_template(self):
        t = json.loads((NATIVE / "manifest.template.json").read_text(encoding="utf-8"))
        self.assertEqual(t["name"], "com.mydownloadmanager.native")
        self.assertEqual(t["type"], "stdio")
        self.assertEqual(t["description"], "My Download Manager Native Messaging Host")
        self.assertTrue(all(o.startswith("chrome-extension://") and o.endswith("/") for o in t["allowed_origins"]))

    def test_install_scripts_target_hkcu_only(self):
        install = (NATIVE / "install_native_host.ps1").read_text(encoding="utf-8")
        uninstall = (NATIVE / "uninstall_native_host.ps1").read_text(encoding="utf-8")
        for src in (install, uninstall):
            self.assertIn(r"HKCU:\Software\Google\Chrome\NativeMessagingHosts\$HostName", src)
            self.assertIn(r"HKCU:\Software\Microsoft\Edge\NativeMessagingHosts\$HostName", src)
            self.assertNotIn("HKLM", src)
        self.assertIn("'^[a-p]{32}$'", install, "extension ID format validated")
        self.assertNotRegex(install, r"[a-p]{32}/\"", "no hard-coded extension ID")
        for protected in ("downloads.db", "USERPROFILE", "settings.json", "endpoint.json", "browser_integration"):
            self.assertNotIn(protected, uninstall)


@unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
class ExtensionBehaviourTests(unittest.TestCase):
    """Runs tests/js/extension_tests.mjs once and asserts each case individually."""

    results: dict = {}

    @classmethod
    def setUpClass(cls):
        proc = subprocess.run(
            [shutil.which("node"), "--test", "--test-reporter=tap", str(ROOT / "tests" / "js" / "extension_tests.mjs")],
            capture_output=True, text=True, encoding="utf-8", timeout=120, cwd=str(ROOT),
        )
        cls.returncode = proc.returncode
        cls.output = proc.stdout + proc.stderr
        for m in re.finditer(r"^\s*(not ok|ok) \d+ - (\S+)", proc.stdout, re.M):
            cls.results[m.group(2)] = "pass" if m.group(1) == "ok" else f"FAILED - see node output:\n{cls.output[-3000:]}"
        if not cls.results:
            raise RuntimeError(f"Node test harness produced no results:\n{cls.output[-3000:]}")

    def check(self, name):
        self.assertIn(name, self.results, "JS test case missing")
        self.assertEqual(self.results[name], "pass", self.results[name])

    def test_popup_url_validation(self):
        self.check("popup_url_validation_accepts_http_https")
        self.check("popup_url_validation_rejects_bad_schemes")
        self.check("popup_url_validation_rejects_credentials_and_invalid")
        self.check("large_url_with_query_parameters")
        self.check("referrer_and_title_cleaning")

    def test_context_menu_creation(self):
        self.check("context_menu_creation")
        self.check("context_menu_click_sends_request")
        self.check("context_menu_ignores_other_menu_ids")

    def test_native_host_connection_failure_handling(self):
        self.check("native_host_connection_failure_handling")
        self.check("app_not_running_is_reported_and_retry_reuses_request_id")
        self.check("validation_errors_are_not_queued_for_retry")

    def test_successful_request_acknowledgement(self):
        self.check("successful_request_acknowledgement")
        self.check("duplicate_acknowledgement")
        self.check("invalid_url_never_reaches_native_host")

    def test_chrome_compatibility(self):
        self.check("chrome_compatibility_detection")

    def test_edge_compatibility(self):
        self.check("edge_compatibility_detection")

    def test_popup_bridge(self):
        self.check("popup_status_mapping")
        self.check("popup_messages_from_other_senders_ignored")
        self.check("unsupported_popup_message")

    def test_all_js_cases_pass(self):
        failed = {k: v for k, v in self.results.items() if v != "pass"}
        self.assertEqual(failed, {})
        self.assertEqual(self.returncode, 0, self.output[-3000:])
        self.assertGreaterEqual(len(self.results), 21)

    def test_hardening_cases(self):
        self.check("request_id_conflict_is_not_retried")
        self.check("signed_url_query_forwarded_intact")


class NativeHostProcessTests(unittest.TestCase):

    def test_host_from_source_end_to_end(self):
        ok, report = verify_host.verify(None)
        self.assertTrue(ok, "\n".join(report))

    @unittest.skipUnless((NATIVE / "dist" / "MyDownloadManagerNativeHost.exe").is_file(),
                         "Native host executable not built (run native_host/build_host.ps1)")
    def test_built_executable_end_to_end(self):
        ok, report = verify_host.verify(str(NATIVE / "dist" / "MyDownloadManagerNativeHost.exe"))
        self.assertTrue(ok, "\n".join(report))


if __name__ == "__main__":
    unittest.main()
