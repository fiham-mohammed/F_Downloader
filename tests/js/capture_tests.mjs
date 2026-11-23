// Stage 6B automatic-capture tests (node:test). Imported by extension_tests.mjs
// so `node --test tests/js/extension_tests.mjs` runs everything.
//
// The browser download manager, the native host/desktop app and the clock are
// all simulated: no browser, network or real timers are involved.

import assert from "node:assert/strict";
import { test } from "node:test";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const ext = path.resolve(here, "..", "..", "browser_extension");
const { createCaptureController, STORAGE_KEYS } = await import(pathToFileURL(path.join(ext, "lib", "capture.js")).href);
const rules = await import(pathToFileURL(path.join(ext, "lib", "capture_rules.js")).href);

const EXT_ID = "abcdefghijklmnopabcdefghijklmnop";

// ------------------------------------------------------------------ fakes
function makeArea(initial = {}) {
  const data = JSON.parse(JSON.stringify(initial));
  return {
    data,
    async get(key) { return { [key]: data[key] === undefined ? undefined : JSON.parse(JSON.stringify(data[key])) }; },
    async set(obj) { for (const [k, v] of Object.entries(obj)) data[k] = JSON.parse(JSON.stringify(v)); },
  };
}

function makeBrowser(items = []) {
  const store = new Map();
  const log = [];
  let nextId = 5000;
  const add = (it) => store.set(it.id, {
    state: "in_progress", paused: false, danger: "safe", incognito: false, filename: "", mime: "",
    totalBytes: 0, referrer: "", ...it, finalUrl: it.finalUrl || it.url,
  });
  items.forEach(add);
  const api = {
    failPause: false, failCancel: false, cancelNoEffect: false, failDownload: false, completeOnCancel: false,
    async pause(id) {
      log.push(["pause", id]);
      const it = store.get(id);
      if (api.failPause || !it || it.state !== "in_progress") throw new Error("Download must be in progress");
      it.paused = true;
    },
    async resume(id) { log.push(["resume", id]); const it = store.get(id); if (it) it.paused = false; },
    async cancel(id) {
      log.push(["cancel", id]);
      if (api.failCancel) throw new Error("cancel failed");
      const it = store.get(id);
      if (api.completeOnCancel) { it.state = "complete"; return; }
      if (!api.cancelNoEffect) { it.state = "interrupted"; it.error = "USER_CANCELED"; it.paused = false; }
    },
    async search({ id }) { const it = store.get(id); return it ? [{ ...it }] : []; },
    async download(opts) {
      log.push(["download", opts.url]);
      if (api.failDownload) throw new Error("download failed");
      const id = nextId++;
      add({ id, url: opts.url, byExtensionId: EXT_ID });
      return id;
    },
  };
  return { api, store, log, add };
}

/** Scripted desktop application behind the native host. */
function makeApp(opts = {}) {
  const app = {
    mode: "automatic", extensions: ["zip", "exe", "pdf", "iso", "tar.gz"], excluded: [],
    probe: "ok",            // "ok" | "auth_required" | ...
    decision: "approved",   // ask mode: "approved" | "declined" | "pending"
    pollsBeforeDecision: 2,
    commit: "ok",           // "ok" | "transient" | "definitive"
    prepareError: null,     // e.g. {status, error_code}
    statusError: null,
    configError: null,
    calls: [],
    captures: new Map(),
    ...opts,
  };
  app.handler = async (msg) => {
    app.calls.push(JSON.parse(JSON.stringify(msg)));
    switch (msg.action) {
      case "get_capture_config":
        if (app.configError) return { ok: false, ...app.configError };
        return { ok: true, data: { mode: app.mode, extensions: app.extensions, excluded_sites: app.excluded, pending: 0, fallbacks: [] } };
      case "prepare_capture": {
        if (app.prepareError) return { ok: false, ...app.prepareError };
        const existing = app.captures.get(msg.request_id);
        if (existing) return { ok: true, status: "duplicate", data: { state: existing.state } };
        const state = app.probe !== "ok" ? "declined" : app.mode === "automatic" ? "approved" : "awaiting_user";
        app.captures.set(msg.request_id, { state, reason: app.probe === "ok" ? null : app.probe, polls: 0, url: msg.url });
        return { ok: true, status: "prepared", data: { state: "probing" } };
      }
      case "capture_status": {
        if (app.statusError) return { ok: false, ...app.statusError };
        const c = app.captures.get(msg.request_id);
        if (!c) return { ok: false, error_code: "capture_not_found" };
        if (c.state === "awaiting_user") {
          c.polls += 1;
          if (c.polls >= app.pollsBeforeDecision && app.decision !== "pending") {
            c.state = app.decision;
            c.reason = app.decision === "declined" ? "user_declined" : null;
          }
        }
        return { ok: true, status: c.state, data: { state: c.state, reason: c.reason } };
      }
      case "commit_capture": {
        if (app.commit === "transient") return { ok: false, error_code: "app_not_running" };
        if (app.commit === "definitive") return { ok: false, error_code: "capture_not_approved" };
        const c = app.captures.get(msg.request_id);
        if (c) c.state = "committed";
        return { ok: true, status: "committed", data: { state: "committed", download_id: "dl000001", filename: "file.zip" } };
      }
      case "abort_capture": {
        const c = app.captures.get(msg.request_id);
        if (c && c.state !== "committed") c.state = "aborted";
        return { ok: true, data: { state: c ? c.state : "not_found" } };
      }
      case "set_capture_mode":
        app.mode = msg.mode;
        return { ok: true, data: { mode: msg.mode, extensions: app.extensions, excluded_sites: app.excluded } };
      case "claim_fallback":
        return { ok: true, data: { url: "https://files.example.com/needs-login.zip?sig=abc" } };
      default:
        return { ok: false, error_code: "unsupported_action" };
    }
  };
  return app;
}

function setup({ app: appOpts = {}, items = [], browser = "chrome", hasPermissions = async () => true,
  local = {}, session = {}, timing = {} } = {}) {
  let clock = 1_000_000;
  const b = makeBrowser(items);
  const app = makeApp(appOpts);
  const notes = [];
  const storageLocal = makeArea(local);
  const storageSession = makeArea(session);
  let ridCounter = 0;
  const deps = {
    downloads: b.api, storageLocal, storageSession,
    sendNative: (m) => app.handler(m),
    notify: (title, message, isError) => notes.push({ title, message, isError }),
    browser, extensionId: EXT_ID, extensionVersion: "0.7.0", hasPermissions,
    newRequestId: () => `req-${browser}-${String(++ridCounter).padStart(6, "0")}`,
    now: () => clock,
    sleep: async (ms) => { clock += ms; await Promise.resolve(); },
    timing,
  };
  const ctl = createCaptureController(deps);
  return { ctl, b, app, notes, storageLocal, storageSession, deps, advance: (ms) => { clock += ms; } };
}

function observeGet(ctl, url, headers = []) {
  ctl.recordResponse({ url, method: "GET", statusCode: 200, responseHeaders: headers });
}

const ZIP = "https://downloads.example.com/releases/file.zip";

function zipItem(over = {}) {
  return { id: 1, url: ZIP, filename: "C:\\Users\\me\\Downloads\\file.zip", referrer: "https://example.com/page", totalBytes: 1234, ...over };
}

const actions = (app) => app.calls.map((c) => c.action);

// ------------------------------------------------------------------ rules
test("capture_rules_file_extension_filtering", () => {
  assert.equal(rules.extensionMatches("Setup.EXE", ["exe"]), "exe");
  assert.equal(rules.extensionMatches("C:\\dl\\report.pdf", ["pdf"]), "pdf");
  assert.equal(rules.extensionMatches("photo.jpg", ["zip", "pdf"]), null);
  assert.equal(rules.extensionMatches("README", ["txt"]), null, "no extension");
  assert.equal(rules.extensionMatches(".zip", ["zip"]), null, "no stem");
  assert.equal(rules.extensionMatches("archive.tar.gz", ["gz", "tar.gz"]), "tar.gz", "longest match wins");
  assert.equal(rules.extensionMatches("archive.tar.gz", ["gz"]), "gz", "multiple extensions");
  assert.equal(rules.normalizeExtension(" .ZIP "), "zip");
  assert.equal(rules.normalizeExtension("*.Tar.GZ"), "tar.gz");
  for (const bad of ["", "z ip", "zip!", "a.b.c", "x".repeat(11), "../exe", 5]) assert.equal(rules.normalizeExtension(bad), null);
});

test("capture_rules_url_path_not_query", () => {
  assert.equal(rules.filenameFromUrl("https://e.com/dl/My%20File.zip?name=evil.exe&sig=abc"), "My File.zip");
  assert.equal(rules.filenameFromUrl("https://e.com/download?file=a.zip"), "download");
  assert.equal(rules.filenameFromUrl("https://e.com/"), "");
  assert.equal(rules.parseContentDispositionFilename("attachment; filename=\"Q3 report.pdf\""), "Q3 report.pdf");
  assert.equal(rules.parseContentDispositionFilename("attachment; filename*=UTF-8''r%C3%A9sum%C3%A9.pdf"), "résumé.pdf");
  assert.equal(rules.parseContentDispositionFilename("attachment; filename=\"..\\\\..\\\\evil.exe\""), "evil.exe");
});

test("website_exclusions_and_subdomain_matching", () => {
  const sites = ["example.com", "youtube.com", "xn--bcher-kva.example"];
  assert.equal(rules.hostIsExcluded("example.com", sites), true);
  assert.equal(rules.hostIsExcluded("sub.example.com", sites), true);
  assert.equal(rules.hostIsExcluded("a.b.example.com", sites), true);
  assert.equal(rules.hostIsExcluded("notexample.com", sites), false);
  assert.equal(rules.hostIsExcluded("example.com.evil.net", sites), false);
  assert.equal(rules.hostIsExcluded("EXAMPLE.COM.", sites), true);
  assert.equal(rules.hostIsExcluded(rules.hostOf("https://bücher.example/x"), sites), true, "IDN via punycode");
  assert.equal(rules.hostOf("blob:https://e.com/uuid"), null);
});

// ------------------------------------------------------------- the modes
test("automatic_capture_disabled", async () => {
  const s = setup({ app: { mode: "disabled" }, items: [zipItem()] });
  observeGet(s.ctl, ZIP);
  const r = await s.ctl.handleCreated(s.b.store.get(1));
  assert.equal(r.reason, "disabled");
  assert.deepEqual(s.b.log, [], "download never touched");
  assert.deepEqual(actions(s.app), ["get_capture_config"]);
});

test("automatic_capture_enabled", async () => {
  const s = setup({ items: [zipItem()] });
  observeGet(s.ctl, ZIP);
  const r = await s.ctl.handleCreated(s.b.store.get(1));
  assert.equal(r.outcome, "committed");
  assert.deepEqual(s.b.log.map((x) => x[0]), ["pause", "cancel"]);
  assert.deepEqual(actions(s.app), ["get_capture_config", "prepare_capture", "capture_status", "commit_capture"]);
  const prep = s.app.calls[1];
  assert.equal(prep.method, "GET");
  assert.equal(prep.danger, "safe");
  assert.equal(prep.filename, "file.zip", "basename only, never a local path");
  assert.equal(prep.browser_download_id, 1);
  assert.equal(s.b.store.get(1).state, "interrupted");
  assert.deepEqual(await s.ctl.listCaptures(), [], "record removed after commit");
  assert.equal(s.notes.at(-1).title, "Sent to My Download Manager");
});

test("cancel_happens_only_after_desktop_approval", async () => {
  const s = setup({ items: [zipItem()] });
  observeGet(s.ctl, ZIP);
  const order = [];
  const orig = s.b.api.cancel;
  s.b.api.cancel = async (id) => { order.push(`cancel after ${actions(s.app).at(-1)}`); return orig(id); };
  await s.ctl.handleCreated(s.b.store.get(1));
  assert.deepEqual(order, ["cancel after capture_status"]);
  const commitIdx = actions(s.app).indexOf("commit_capture");
  assert.ok(commitIdx > actions(s.app).indexOf("capture_status"));
});

test("ask_mode_user_acceptance", async () => {
  const s = setup({ app: { mode: "ask", pollsBeforeDecision: 3 }, items: [zipItem()] });
  observeGet(s.ctl, ZIP);
  const r = await s.ctl.handleCreated(s.b.store.get(1));
  assert.equal(r.outcome, "committed");
  assert.equal(actions(s.app).filter((a) => a === "capture_status").length, 3);
  assert.ok(s.b.log.some((x) => x[0] === "cancel"));
});

test("ask_mode_user_rejection_resumes_browser_download", async () => {
  const s = setup({ app: { mode: "ask", decision: "declined" }, items: [zipItem()] });
  observeGet(s.ctl, ZIP);
  const r = await s.ctl.handleCreated(s.b.store.get(1));
  assert.equal(r.outcome, "resumed");
  assert.equal(r.reason, "user_declined");
  assert.equal(s.b.store.get(1).paused, false, "browser download resumed");
  assert.equal(s.b.store.get(1).state, "in_progress");
  assert.ok(!s.b.log.some((x) => x[0] === "cancel"), "never cancelled");
  assert.ok(!actions(s.app).includes("commit_capture"));
  assert.equal(s.notes.length, 0, "no nagging after the user's own choice");
});

test("ask_mode_timeout_resumes", async () => {
  const s = setup({ app: { mode: "ask", decision: "pending" }, items: [zipItem()], timing: { decisionTimeoutMs: 10_000 } });
  observeGet(s.ctl, ZIP);
  const r = await s.ctl.handleCreated(s.b.store.get(1));
  assert.equal(r.reason, "timeout");
  assert.equal(s.b.store.get(1).paused, false);
  assert.ok(actions(s.app).includes("abort_capture"));
});

// ---------------------------------------------------------- eligibility
async function expectSkipped(itemOver, reason, opts = {}) {
  const s = setup({ items: [zipItem(itemOver)], ...opts });
  if (!opts.noObservation) observeGet(s.ctl, (itemOver && itemOver.finalUrl) || (itemOver && itemOver.url) || ZIP);
  const r = await s.ctl.handleCreated(s.b.store.get(1));
  assert.equal(r.reason, reason, JSON.stringify(r));
  assert.ok(!s.b.log.some((x) => x[0] === "cancel"));
  assert.ok(!actions(s.app).includes("prepare_capture"));
  return s;
}

test("file_extension_filtering_skips_without_pausing", async () => {
  const s = await expectSkipped({ url: "https://e.com/photo.jpg", filename: "C:\\dl\\photo.jpg" }, "extension_not_selected");
  assert.deepEqual(s.b.log, [], "not even paused");
});

test("custom_extensions_are_captured", async () => {
  const url = "https://e.com/src/archive.tar.gz";
  const s = setup({ app: { extensions: ["tar.gz"] }, items: [zipItem({ url, filename: "D:\\x\\archive.tar.gz" })] });
  observeGet(s.ctl, url);
  assert.equal((await s.ctl.handleCreated(s.b.store.get(1))).outcome, "committed");
});

test("website_exclusion_leaves_download", async () => {
  const s = await expectSkipped({ url: "https://cdn.sub.example.com/file.zip" }, "site_excluded",
    { app: { excluded: ["example.com"] } });
  assert.deepEqual(s.b.log, []);
  await expectSkipped({ referrer: "https://www.youtube.com/watch" }, "site_excluded", { app: { excluded: ["youtube.com"] } });
  const ok = setup({ app: { excluded: ["example.com"] }, items: [zipItem({ url: "https://notexample.com/file.zip", referrer: "" })] });
  observeGet(ok.ctl, "https://notexample.com/file.zip");
  assert.equal((await ok.ctl.handleCreated(ok.b.store.get(1))).outcome, "committed", "notexample.com is not excluded");
});

test("http_and_https_eligibility", async () => {
  for (const url of ["http://plain.example.net/file.zip", "https://secure.example.net/file.zip"]) {
    const s = setup({ items: [zipItem({ url, referrer: "" })] });
    observeGet(s.ctl, url);
    assert.equal((await s.ctl.handleCreated(s.b.store.get(1))).outcome, "committed", url);
  }
});

test("invalid_url_rejection", async () => {
  for (const url of ["ftp://e.com/file.zip", "https://user:pw@e.com/file.zip", "chrome://downloads/file.zip",
    "edge://settings/file.zip", "file:///C:/file.zip", "javascript:alert(1)", "about:blank"]) {
    const s = await expectSkipped({ url, finalUrl: url }, "unsupported_url", { noObservation: true });
    assert.deepEqual(s.b.log, [], url);
  }
});

test("blob_url_exclusion", async () => {
  await expectSkipped({ url: "blob:https://e.com/1b2c", finalUrl: "blob:https://e.com/1b2c" }, "unsupported_url", { noObservation: true });
});

test("data_url_exclusion", async () => {
  await expectSkipped({ url: "data:application/zip;base64,UEsDBA==", finalUrl: "data:application/zip;base64,UEsDBA==" },
    "unsupported_url", { noObservation: true });
});

test("incognito_exclusion", async () => {
  const s = await expectSkipped({ incognito: true }, "incognito");
  assert.deepEqual(s.b.log, []);
});

test("dangerous_download_exclusion", async () => {
  await expectSkipped({ danger: "file" }, "dangerous");
  await expectSkipped({ danger: "uncommon" }, "dangerous");
});

test("dangerous_flag_raised_while_paused_resumes", async () => {
  const s = setup({ items: [zipItem({ filename: "" })] });
  observeGet(s.ctl, ZIP);
  const origPause = s.b.api.pause;
  s.b.api.pause = async (id) => { await origPause(id); s.b.store.get(1).danger = "url"; s.b.store.get(1).filename = "C:\\f\\file.zip"; };
  const r = await s.ctl.handleCreated(s.b.store.get(1));
  assert.equal(r.reason, "dangerous");
  assert.equal(s.b.store.get(1).paused, false);
});

test("own_extension_download_exclusion", async () => {
  const s = await expectSkipped({ byExtensionId: EXT_ID }, "own_download");
  assert.deepEqual(s.b.log, []);
  await expectSkipped({ byExtensionId: "zyxwvutsrqponmlkzyxwvutsrqponmlk" }, "other_extension");
});

test("unsupported_request_methods_are_left_alone", async () => {
  const s = setup({ items: [zipItem()] });
  s.ctl.recordResponse({ url: ZIP, method: "POST", statusCode: 200, responseHeaders: [] });
  const r = await s.ctl.handleCreated(s.b.store.get(1));
  assert.equal(r.reason, "unsupported_method");
  assert.deepEqual(s.b.log, [], "POST downloads are never paused or replayed");
});

test("unknown_method_resumes", async () => {
  const s = setup({ items: [zipItem()] });
  const r = await s.ctl.handleCreated(s.b.store.get(1));  // no webRequest observation at all
  assert.equal(r.reason, "method_unknown");
  assert.equal(s.b.store.get(1).paused, false, "resumed after the brief metadata wait");
  assert.ok(!actions(s.app).includes("prepare_capture"));
});

test("redirect_hops_must_all_be_get", async () => {
  const final = "https://cdn.example.com/file.zip?X-Amz-Signature=abc%2Fdef";
  const s = setup({ items: [zipItem({ url: "https://example.com/get/42", finalUrl: final, filename: "" })] });
  s.ctl.recordResponse({ url: "https://example.com/get/42", method: "GET", statusCode: 302, responseHeaders: [] });
  observeGet(s.ctl, final, [{ name: "Content-Disposition", value: "attachment; filename=\"file.zip\"" }]);
  const r = await s.ctl.handleCreated(s.b.store.get(1));
  assert.equal(r.outcome, "committed");
  const prep = s.app.calls.find((c) => c.action === "prepare_capture");
  assert.equal(prep.filename, "file.zip", "Content-Disposition filename used");
  assert.equal(prep.url, "https://example.com/get/42", "original URL; the desktop follows the redirect itself");
});

test("signed_url_query_preserved", async () => {
  const url = "https://cdn.example.com/f/file.zip?X-Amz-Signature=a%2Fb%3D&X-Amz-Credential=AKIA%2F2026&token=t0k";
  const s = setup({ items: [zipItem({ url })] });
  observeGet(s.ctl, url);
  await s.ctl.handleCreated(s.b.store.get(1));
  assert.equal(s.app.calls.find((c) => c.action === "prepare_capture").url, url);
  assert.equal(s.app.calls.find((c) => c.action === "commit_capture").url, url);
});

// --------------------------------------------------------- duplicates
test("duplicate_event_prevention", async () => {
  const s = setup({ items: [zipItem()] });
  observeGet(s.ctl, ZIP);
  const item = s.b.store.get(1);
  const [a, b] = await Promise.all([s.ctl.handleCreated(item), s.ctl.handleCreated(item)]);
  assert.deepEqual([a.outcome, b.reason].sort(), ["committed", "duplicate_event"].sort());
  assert.equal(actions(s.app).filter((x) => x === "prepare_capture").length, 1);
  const again = await s.ctl.handleCreated(item);
  assert.equal(again.reason, "duplicate_event", "already handled earlier in this session");
  assert.equal(actions(s.app).filter((x) => x === "commit_capture").length, 1);
});

test("duplicate_request_ids", async () => {
  const s = setup({ items: [zipItem()], app: { prepareError: { error_code: "request_id_conflict" } } });
  observeGet(s.ctl, ZIP);
  const r = await s.ctl.handleCreated(s.b.store.get(1));
  assert.equal(r.reason, "request_id_conflict");
  assert.equal(s.b.store.get(1).paused, false);
  assert.ok(!s.b.log.some((x) => x[0] === "cancel"));

  const d = setup({ items: [zipItem()], app: { prepareError: { error_code: "duplicate_capture" } } });
  observeGet(d.ctl, ZIP);
  const r2 = await d.ctl.handleCreated(d.b.store.get(1));
  assert.equal(r2.reason, "duplicate_capture");
  assert.ok(!d.b.log.some((x) => x[0] === "resume"), "another capture owns it: leave it paused for that one");
});

// ------------------------------------------------------- restart / recovery
test("service_worker_restart_recovers_awaiting_capture", async () => {
  // A previous worker prepared the capture and was then stopped by the browser.
  const rec = { request_id: "req-prev-000001", download_id: 1, url: ZIP, filename: "file.zip", mime: null,
    total_bytes: null, referrer: null, mode: "ask", phase: "awaiting", created_at: 1_000_000, cancelled_at: null, attempts: 0 };
  const s = setup({
    app: { mode: "ask", pollsBeforeDecision: 1 },
    items: [zipItem({ paused: true })],
    local: { [STORAGE_KEYS.captures]: { [rec.request_id]: rec } },
  });
  s.app.captures.set(rec.request_id, { state: "awaiting_user", reason: null, polls: 0 });
  const [out] = await s.ctl.recover();
  assert.equal(out.outcome, "committed");
  assert.equal(s.app.calls.filter((c) => c.action === "commit_capture").length, 1);
  assert.deepEqual(await s.ctl.listCaptures(), []);
});

test("service_worker_restart_resumes_mid_evaluation", async () => {
  const s = setup({
    items: [zipItem({ paused: true })],
    session: { [STORAGE_KEYS.handled]: { 1: { phase: "evaluating", paused: true, at: 1 } } },
  });
  await s.ctl.recover();
  assert.equal(s.b.store.get(1).paused, false, "a download paused by a dead worker is resumed");
  assert.deepEqual(actions(s.app), []);
});

test("service_worker_restart_after_cancel_commits", async () => {
  const rec = { request_id: "req-prev-000002", download_id: 1, url: ZIP, filename: "file.zip", phase: "cancelling",
    created_at: 1_000_000, cancelled_at: null, attempts: 0 };
  const s = setup({ items: [zipItem({ state: "interrupted", error: "USER_CANCELED" })],
    local: { [STORAGE_KEYS.captures]: { [rec.request_id]: rec } } });
  const [out] = await s.ctl.recover();
  assert.equal(out.outcome, "committed", "the cancelled download is not lost");
});

test("recover_is_idempotent", async () => {
  const rec = { request_id: "req-prev-000003", download_id: 1, url: ZIP, filename: "file.zip", phase: "committing",
    created_at: 1_000_000, cancelled_at: 1_000_000, attempts: 0 };
  const s = setup({ items: [zipItem({ state: "interrupted" })], local: { [STORAGE_KEYS.captures]: { [rec.request_id]: rec } } });
  await Promise.all([s.ctl.recover(), s.ctl.recover()]);
  assert.equal(actions(s.app).filter((a) => a === "commit_capture").length, 1, "no duplicate commit");
});

// ------------------------------------------------------ failure handling
test("browser_permission_denied", async () => {
  const s = setup({ hasPermissions: async () => false, items: [zipItem()] });
  await s.ctl.getConfig({ refresh: true });
  assert.equal(s.app.calls[0].capture_permissions, false, "the desktop is told permissions are missing");
});

test("native_host_unavailable", async () => {
  const s = setup({ items: [zipItem()], app: { configError: { status: "native_host_unavailable", error_code: "native_host_not_found" } } });
  observeGet(s.ctl, ZIP);
  const r = await s.ctl.handleCreated(s.b.store.get(1));
  assert.equal(r.reason, "app_unavailable");
  assert.deepEqual(s.b.log, [], "nothing is paused when the desktop cannot be reached");

  const p = setup({ items: [zipItem()], app: { prepareError: { status: "native_host_unavailable", error_code: "native_host_exited" } } });
  observeGet(p.ctl, ZIP);
  const r2 = await p.ctl.handleCreated(p.b.store.get(1));
  assert.equal(r2.reason, "native_host_unavailable");
  assert.equal(p.b.store.get(1).paused, false);
  assert.match(p.notes.at(-1).message, /native messaging host/);
});

test("application_unavailable_resumes", async () => {
  const s = setup({ items: [zipItem()], app: { prepareError: { error_code: "app_not_running" } } });
  observeGet(s.ctl, ZIP);
  const r = await s.ctl.handleCreated(s.b.store.get(1));
  assert.equal(r.reason, "app_unavailable");
  assert.equal(s.b.store.get(1).paused, false);
  assert.equal(s.b.store.get(1).state, "in_progress");
});

test("ipc_timeout", async () => {
  const s = setup({ items: [zipItem()], app: { prepareError: { error_code: "timeout" } } });
  observeGet(s.ctl, ZIP);
  assert.equal((await s.ctl.handleCreated(s.b.store.get(1))).reason, "timeout");
  assert.equal(s.b.store.get(1).paused, false);

  const t = setup({ app: { mode: "ask", statusError: { error_code: "timeout" } }, items: [zipItem()] });
  observeGet(t.ctl, ZIP);
  const r = await t.ctl.handleCreated(t.b.store.get(1));
  assert.equal(r.reason, "timeout");
  assert.equal(actions(t.app).filter((a) => a === "capture_status").length, 3);
  assert.equal(t.b.store.get(1).paused, false);
  assert.ok(!t.b.log.some((x) => x[0] === "cancel"));
});

test("auth_required_probe_keeps_browser_download", async () => {
  const s = setup({ items: [zipItem()], app: { probe: "auth_required" } });
  observeGet(s.ctl, ZIP);
  const r = await s.ctl.handleCreated(s.b.store.get(1));
  assert.equal(r.reason, "auth_required");
  assert.equal(s.b.store.get(1).paused, false);
  assert.ok(!s.b.log.some((x) => x[0] === "cancel"));
  assert.match(s.notes.at(-1).message, /browser session/);
});

test("browser_cancellation_failure", async () => {
  const s = setup({ items: [zipItem()] });
  observeGet(s.ctl, ZIP);
  s.b.api.failCancel = true;
  const r = await s.ctl.handleCreated(s.b.store.get(1));
  assert.equal(r.reason, "cancel_failed");
  assert.ok(actions(s.app).includes("abort_capture"));
  assert.ok(!actions(s.app).includes("commit_capture"), "no second download");
  assert.equal(s.b.store.get(1).paused, false, "browser download resumed");

  const u = setup({ items: [zipItem()] });
  observeGet(u.ctl, ZIP);
  u.b.api.cancelNoEffect = true;
  const r2 = await u.ctl.handleCreated(u.b.store.get(1));
  assert.equal(r2.reason, "cancel_unconfirmed");
  assert.ok(!actions(u.app).includes("commit_capture"));
});

test("download_completed_before_cancel_is_not_duplicated", async () => {
  const s = setup({ items: [zipItem()] });
  observeGet(s.ctl, ZIP);
  s.b.api.completeOnCancel = true;
  const r = await s.ctl.handleCreated(s.b.store.get(1));
  assert.equal(r.reason, "download_completed");
  assert.ok(!actions(s.app).includes("commit_capture"));
});

test("user_resumed_in_browser_aborts_capture", async () => {
  const s = setup({ app: { mode: "ask", decision: "pending" }, items: [zipItem()] });
  observeGet(s.ctl, ZIP);
  const origSearch = s.b.api.search;
  let n = 0;
  s.b.api.search = async (q) => {
    n += 1;
    if (n === 6) s.b.store.get(1).paused = false;  // user clicks Resume in the browser
    return origSearch(q);
  };
  const r = await s.ctl.handleCreated(s.b.store.get(1));
  assert.equal(r.reason, "browser_resumed");
  assert.ok(actions(s.app).includes("abort_capture"));
  assert.ok(!s.b.log.some((x) => x[0] === "cancel"));
});

test("browser_resume_after_rejection", async () => {
  const s = setup({ app: { mode: "ask", decision: "declined" }, items: [zipItem()] });
  observeGet(s.ctl, ZIP);
  await s.ctl.handleCreated(s.b.store.get(1));
  assert.deepEqual(s.b.log.map((x) => x[0]), ["pause", "resume"]);
});

test("failed_capture_fallback_to_browser", async () => {
  const s = setup({ items: [zipItem()], app: { commit: "definitive" } });
  observeGet(s.ctl, ZIP);
  const r = await s.ctl.handleCreated(s.b.store.get(1));
  assert.equal(r.outcome, "browser_fallback");
  const dl = s.b.log.find((x) => x[0] === "download");
  assert.equal(dl[1], ZIP, "browser re-downloads with its own session");
  const fallbackItem = s.b.store.get(r.download_id);
  const again = await s.ctl.handleCreated(fallbackItem);
  assert.equal(again.outcome === "skipped" || again.outcome === "ignored", true, "fallback download is never re-captured");
});

test("commit_transient_failure_then_give_up_falls_back", async () => {
  const s = setup({ items: [zipItem()], app: { commit: "transient" }, timing: { commitRetryDelays: [1000] } });
  observeGet(s.ctl, ZIP);
  const r = await s.ctl.handleCreated(s.b.store.get(1));
  assert.equal(r.outcome, "commit_pending", "kept for later, not lost");
  assert.equal((await s.ctl.listCaptures())[0].phase, "committing");
  s.app.commit = "ok";
  const [out] = await s.ctl.recover();
  assert.equal(out.outcome, "committed", "retried after the app came back");

  const g = setup({ items: [zipItem()], app: { commit: "transient" }, timing: { commitRetryDelays: [1000], commitGiveUpMs: 500 } });
  observeGet(g.ctl, ZIP);
  assert.equal((await g.ctl.handleCreated(g.b.store.get(1))).outcome, "browser_fallback");
});

test("fallback_download_failure_is_kept_for_retry", async () => {
  const s = setup({ items: [zipItem()], app: { commit: "definitive" } });
  observeGet(s.ctl, ZIP);
  s.b.api.failDownload = true;
  const r = await s.ctl.handleCreated(s.b.store.get(1));
  assert.equal(r.outcome, "fallback_failed");
  const [pending] = await s.ctl.listCaptures();
  assert.equal(pending.phase, "fallback_failed");
  assert.equal(pending.url, undefined, "the popup never receives the URL");
  s.b.api.failDownload = false;
  assert.equal((await s.ctl.retryFallback(pending.request_id)).ok, true);
  assert.deepEqual(await s.ctl.listCaptures(), []);
});

test("claim_auth_fallback_downloads_in_browser", async () => {
  const s = setup();
  const r = await s.ctl.claimFallback("req-fallback-0001");
  assert.equal(r.ok, true);
  assert.equal(s.b.log[0][0], "download");
  assert.match(s.b.log[0][1], /needs-login\.zip\?sig=abc$/);
});

// ----------------------------------------------------- Chrome and Edge
test("chrome_capture_behaviour", async () => {
  const s = setup({ browser: "chrome", items: [zipItem()] });
  observeGet(s.ctl, ZIP);
  await s.ctl.handleCreated(s.b.store.get(1));
  assert.ok(s.app.calls.filter((c) => c.action !== "get_capture_config").every((c) => c.browser === "chrome"));
});

test("edge_capture_behaviour", async () => {
  const s = setup({ browser: "edge", items: [zipItem()] });
  observeGet(s.ctl, ZIP);
  const r = await s.ctl.handleCreated(s.b.store.get(1));
  assert.equal(r.outcome, "committed");
  assert.ok(s.app.calls.filter((c) => c.action !== "get_capture_config").every((c) => c.browser === "edge"));
});

test("config_cache_and_unreachable_app", async () => {
  const s = setup({ items: [zipItem()] });
  await s.ctl.getConfig();
  await s.ctl.getConfig();
  assert.equal(actions(s.app).filter((a) => a === "get_capture_config").length, 1, "cached");
  s.advance(31_000);
  await s.ctl.getConfig();
  assert.equal(actions(s.app).filter((a) => a === "get_capture_config").length, 2, "refreshed when stale");
  s.app.configError = { error_code: "app_not_running" };
  const cfg = await s.ctl.getConfig({ refresh: true });
  assert.equal(cfg.mode, "disabled");
  assert.equal(cfg.unreachable, true);
});

// Shared with capture_pdf_image_tests.mjs (ES modules are evaluated once, so these tests are not re-run).
export { makeArea, makeBrowser, makeApp, setup, observeGet, actions, EXT_ID, ZIP, zipItem };
