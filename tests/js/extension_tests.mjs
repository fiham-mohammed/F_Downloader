// Stage 6A extension behaviour tests (node:test). The `chrome` API is mocked;
// no browser, native host or network access is needed.
//
// Run directly:   node --test tests/js/extension_tests.mjs
// Also executed by tests/test_stage6a_extension.py, which parses the TAP
// output and asserts every case individually. Any failed assertion makes the
// process exit non-zero.

import assert from "node:assert/strict";
import { test as nodeTest } from "node:test";
import { readFileSync } from "node:fs";
import { fileURLToPath, pathToFileURL } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const extDir = path.resolve(here, "..", "..", "browser_extension");
const manifest = JSON.parse(readFileSync(path.join(extDir, "manifest.json"), "utf8"));

// Timers created by the service worker must not keep Node alive.
const realSetTimeout = globalThis.setTimeout;
globalThis.setTimeout = (fn, ms) => { const t = realSetTimeout(fn, ms); t.unref?.(); return t; };

function listener() {
  const fns = [];
  return { addListener: (fn) => fns.push(fn), fns };
}

const calls = { menus: [], notifications: [], badges: [], native: [] };
const store = {};
let nativeImpl = async () => ({ ok: true });

globalThis.chrome = {
  runtime: {
    id: "abcdefghijklmnopabcdefghijklmnop",
    lastError: undefined,
    onInstalled: listener(),
    onStartup: listener(),
    onMessage: listener(),
    getManifest: () => manifest,
    sendNativeMessage: (host, msg) => {
      calls.native.push({ host, msg });
      return nativeImpl(host, msg);
    },
  },
  contextMenus: {
    removeAll: (cb) => { calls.menus.length = 0; cb && cb(); },
    create: (opts, cb) => { calls.menus.push(opts); cb && cb(); },
    onClicked: listener(),
  },
  notifications: { create: (_id, opts, cb) => { calls.notifications.push(opts); cb && cb(); } },
  action: { setBadgeText: (o) => calls.badges.push(o), setBadgeBackgroundColor: () => {} },
  storage: {
    local: {
      get: async (key) => ({ [key]: store[key] }),
      set: async (obj) => { Object.assign(store, obj); },
    },
  },
};

const v = await import(pathToFileURL(path.join(extDir, "lib", "validation.js")).href);
const sw = await import(pathToFileURL(path.join(extDir, "service_worker.js")).href);

function reset() {
  calls.native.length = 0;
  calls.notifications.length = 0;
  calls.badges.length = 0;
  for (const k of Object.keys(store)) delete store[k];
}

// Top-level node:test cases run sequentially in declaration order, so the
// shared chrome mock is reset before each one.
async function test(name, fn) {
  await nodeTest(name, async () => {
    reset();
    await fn();
  });
}

// ------------------------------------------------------------ validation
await test("popup_url_validation_accepts_http_https", () => {
  for (const u of [
    "http://example.com/file.zip",
    "https://example.com/a/b/My%20File.zip?x=1&y=%E2%9C%93#frag",
    "HTTPS://EXAMPLE.COM/x",
    "https://[::1]:8443/x.bin",
    "  https://example.com/padded.iso  ",
  ]) {
    const r = v.validateDownloadUrl(u);
    assert.equal(r.ok, true, u);
  }
  assert.equal(v.validateDownloadUrl("  https://example.com/padded.iso ").url, "https://example.com/padded.iso");
});

await test("popup_url_validation_rejects_bad_schemes", () => {
  for (const u of ["javascript:alert(1)", "data:text/html,hi", "blob:https://example.com/uuid",
    "file:///C:/Windows/win.ini", "chrome://settings", "edge://settings", "ftp://example.com/x",
    "chrome-extension://abc/x"]) {
    const r = v.validateDownloadUrl(u);
    assert.equal(r.ok, false, u);
    assert.equal(r.code, "unsupported_scheme", u);
  }
});

await test("popup_url_validation_rejects_credentials_and_invalid", () => {
  assert.equal(v.validateDownloadUrl("https://user:pass@example.com/x").code, "embedded_credentials");
  assert.equal(v.validateDownloadUrl("https://user@example.com/x").code, "embedded_credentials");
  for (const u of ["", "   ", "example.com/file.zip", "http:/example.com", "https://exa mple.com/x",
    "https://example.com/\u0000", 42, null]) {
    assert.equal(v.validateDownloadUrl(u).ok, false, String(u));
  }
});

await test("large_url_with_query_parameters", () => {
  const big = "https://cdn.example.com/f.bin?" + Array.from({ length: 800 }, (_, i) => `k${i}=v%20${i}`).join("&");
  assert.ok(big.length > 8000);
  const r = v.validateDownloadUrl(big);
  assert.equal(r.ok, true);
  assert.equal(r.url, big);
  assert.equal(v.validateDownloadUrl("https://e.com/?" + "a".repeat(40000)).ok, false);
});

await test("referrer_and_title_cleaning", () => {
  assert.equal(v.cleanReferrer("file:///C:/x.html"), null);
  assert.equal(v.cleanReferrer("https://u:p@example.com/"), null);
  assert.equal(v.cleanReferrer("https://example.com/page"), "https://example.com/page");
  assert.equal(v.cleanPageTitle("  A\u0007B  "), "A B");
  assert.equal(v.cleanPageTitle("x".repeat(900)).length, 512);
});

// ------------------------------------------------------- browser detection
await test("edge_compatibility_detection", () => {
  assert.equal(v.detectBrowser({ userAgentData: { brands: [{ brand: "Microsoft Edge", version: "131" }] } }), "edge");
  assert.equal(v.detectBrowser({ userAgent: "Mozilla/5.0 (Windows NT 10.0) Chrome/131.0 Safari/537.36 Edg/131.0" }), "edge");
  assert.equal(v.browserLabel("edge"), "Microsoft Edge");
});

await test("chrome_compatibility_detection", () => {
  assert.equal(v.detectBrowser({ userAgentData: { brands: [{ brand: "Google Chrome", version: "131" }] } }), "chrome");
  assert.equal(v.detectBrowser({ userAgent: "Mozilla/5.0 (Windows NT 10.0) Chrome/131.0 Safari/537.36" }), "chrome");
  assert.equal(v.detectBrowser(undefined), "chrome");
  assert.equal(v.browserLabel("chrome"), "Google Chrome");
});

// ---------------------------------------------------------- context menu
await test("context_menu_creation", () => {
  assert.equal(chrome.runtime.onInstalled.fns.length, 1);
  chrome.runtime.onInstalled.fns[0]({ reason: "install" });
  assert.equal(calls.menus.length, 1);
  const m = calls.menus[0];
  assert.equal(m.title, "Download with My Download Manager");
  assert.deepEqual(m.contexts, ["link"]);
  assert.deepEqual(m.targetUrlPatterns, ["http://*/*", "https://*/*"]);
  // Re-running setup (browser restart) never duplicates the menu.
  chrome.runtime.onStartup.fns[0]();
  assert.equal(calls.menus.length, 1);
});

await test("context_menu_click_sends_request", async () => {
  nativeImpl = async (_h, msg) => ({ ok: true, status: "accepted", request_id: msg.request_id,
    download: { id: "abc12345", filename: "file.zip" } });
  const handler = chrome.contextMenus.onClicked.fns[0];
  await handler({ menuItemId: sw.MENU_ID, linkUrl: "https://example.com/file.zip?a=1&b=%20",
    pageUrl: "https://example.com/downloads" }, { title: "Downloads page" });
  await new Promise((r) => realSetTimeout(r, 20));
  assert.equal(calls.native.length, 1);
  const { host, msg } = calls.native[0];
  assert.equal(host, "com.mydownloadmanager.native");
  assert.equal(msg.action, "add_download");
  assert.equal(msg.url, "https://example.com/file.zip?a=1&b=%20");
  assert.equal(msg.referrer, "https://example.com/downloads");
  assert.equal(msg.page_title, "Downloads page");
  assert.equal(msg.filename, null);
  assert.ok(["chrome", "edge"].includes(msg.browser));
  assert.match(msg.request_id, /^[A-Za-z0-9_-]{8,128}$/);
  assert.deepEqual(Object.keys(msg).sort(),
    ["action", "browser", "filename", "page_title", "referrer", "request_id", "url"]);
});

await test("context_menu_ignores_other_menu_ids", async () => {
  await chrome.contextMenus.onClicked.fns[0]({ menuItemId: "other", linkUrl: "https://e.com/x" }, {});
  assert.equal(calls.native.length, 0);
});

// ----------------------------------------------------------- acknowledgement
await test("successful_request_acknowledgement", async () => {
  nativeImpl = async (_h, msg) => ({ ok: true, status: "accepted", request_id: msg.request_id,
    download: { id: "abc12345", filename: "report.pdf" } });
  const res = await sw.submitDownload({ url: "https://example.com/report.pdf" });
  assert.equal(res.ok, true);
  assert.equal(res.connection_status, "connected");
  assert.equal(calls.notifications.length, 1);
  assert.equal(calls.notifications[0].title, "Download added");
  assert.match(calls.notifications[0].message, /report\.pdf/);
  assert.equal((store.pendingRequests || []).length, 0);
});

await test("invalid_url_never_reaches_native_host", async () => {
  const res = await sw.submitDownload({ url: "javascript:alert(1)" });
  assert.equal(res.ok, false);
  assert.equal(res.error_code, "unsupported_scheme");
  assert.equal(calls.native.length, 0);
  assert.equal(calls.notifications[0].title, "Link not supported");
});

// ------------------------------------------------------ failure handling
await test("native_host_connection_failure_handling", async () => {
  nativeImpl = async () => { throw new Error("Specified native messaging host not found."); };
  const res = await sw.submitDownload({ url: "https://example.com/a.zip" });
  assert.equal(res.ok, false);
  assert.equal(res.status, "native_host_unavailable");
  assert.equal(res.connection_status, "native_host_unavailable");
  assert.equal(store.pendingRequests.length, 1, "request kept for retry, not discarded");
  assert.match(calls.notifications[0].message, /install_native_host\.ps1/);

  nativeImpl = async () => { throw new Error("Access to the specified native messaging host is forbidden."); };
  assert.equal((await sw.sendNative({ action: "ping" })).error_code, "native_host_forbidden");
  nativeImpl = async () => { throw new Error("Native host has exited."); };
  assert.equal((await sw.sendNative({ action: "ping" })).status, "connection_error");
  nativeImpl = async () => undefined;
  assert.equal((await sw.sendNative({ action: "ping" })).error_code, "invalid_response");
});

await test("app_not_running_is_reported_and_retry_reuses_request_id", async () => {
  nativeImpl = async (_h, msg) => ({ ok: false, status: "error", error_code: "app_not_running",
    request_id: msg.request_id, message: "My Download Manager is not running. Start the application and try again." });
  const res = await sw.submitDownload({ url: "https://example.com/b.zip" });
  assert.equal(res.connection_status, "app_not_running");
  assert.equal(store.pendingRequests.length, 1);
  const rid = store.pendingRequests[0].message.request_id;
  assert.match(calls.notifications[0].message, /not running/);

  nativeImpl = async (_h, msg) => ({ ok: true, status: "accepted", request_id: msg.request_id,
    download: { id: "x1234567", filename: "b.zip" } });
  const retried = await sw.handlePopupMessage({ type: "retryPending", requestId: rid });
  assert.equal(retried.ok, true);
  assert.equal(calls.native.at(-1).msg.request_id, rid, "retry must reuse the same request_id");
  assert.equal(store.pendingRequests.length, 0);
});

await test("validation_errors_are_not_queued_for_retry", async () => {
  nativeImpl = async () => ({ ok: false, error_code: "embedded_credentials", message: "nope" });
  await sw.submitDownload({ url: "https://example.com/c.zip" });
  assert.equal((store.pendingRequests || []).length, 0);
});

await test("duplicate_acknowledgement", async () => {
  nativeImpl = async (_h, msg) => ({ ok: true, status: "duplicate", request_id: msg.request_id,
    download: { id: "abc12345", filename: "d.zip" } });
  const res = await sw.submitDownload({ url: "https://example.com/d.zip" });
  assert.equal(res.ok, true);
  assert.equal(calls.notifications[0].title, "Already sent to My Download Manager");
});

await test("popup_status_mapping", async () => {
  nativeImpl = async () => ({ ok: true, status: "connected", app_version: "0.6.0", host_version: "0.6.0",
    open_app_supported: true });
  let st = await sw.handlePopupMessage({ type: "getStatus" });
  assert.equal(st.status, "connected");
  assert.equal(st.app_version, "0.6.0");
  assert.equal(st.extension_version, manifest.version);
  nativeImpl = async () => ({ ok: false, error_code: "app_not_running" });
  assert.equal((await sw.handlePopupMessage({ type: "getStatus" })).status, "app_not_running");
  nativeImpl = async () => { throw new Error("Specified native messaging host not found."); };
  assert.equal((await sw.handlePopupMessage({ type: "getStatus" })).status, "native_host_unavailable");
  nativeImpl = async () => ({ ok: false, error_code: "timeout" });
  assert.equal((await sw.handlePopupMessage({ type: "getStatus" })).status, "connection_error");
});

await test("popup_messages_from_other_senders_ignored", () => {
  const onMessage = chrome.runtime.onMessage.fns[0];
  let responded = false;
  const keepOpen = onMessage({ type: "addUrl", url: "https://e.com/x" }, { id: "someone-else" }, () => { responded = true; });
  assert.equal(keepOpen, false);
  assert.equal(responded, false);
  assert.equal(calls.native.length, 0);
});

await test("unsupported_popup_message", async () => {
  const r = await sw.handlePopupMessage({ type: "runCommand", cmd: "calc.exe" });
  assert.equal(r.ok, false);
  assert.equal(calls.native.length, 0);
});

await test("request_id_conflict_is_not_retried", async () => {
  nativeImpl = async () => ({ ok: false, error_code: "request_id_conflict",
    message: "This request ID was already used for a different link." });
  const res = await sw.submitDownload({ url: "https://example.com/e.zip" });
  assert.equal(res.ok, false);
  assert.equal((store.pendingRequests || []).length, 0);
  assert.equal(v.isRetryable({ ok: false, error_code: "storage_error" }), true);
});

await test("signed_url_query_forwarded_intact", async () => {
  // A signed download link carries its access token in the query string; it
  // must reach the native host byte-for-byte or the download would fail.
  const signed = "https://cdn.example.com/f.zip?X-Amz-Signature=abc%2Fdef&X-Amz-Credential=AKIA%2F20261121&token=t0k3n";
  nativeImpl = async (_h, msg) => ({ ok: true, status: "accepted", request_id: msg.request_id,
    download: { id: "sig00001", filename: "f.zip" } });
  await sw.submitDownload({ url: signed });
  assert.equal(calls.native[0].msg.url, signed);
});
