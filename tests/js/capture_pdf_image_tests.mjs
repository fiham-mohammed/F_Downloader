// Stage 6B repair: PDF and image automatic capture (node:test).
// Imported by extension_tests.mjs, so `node --test tests/js/extension_tests.mjs`
// runs everything. Reuses the simulated browser/desktop harness of capture_tests.mjs.
//
// webRequest events are fed exactly as Chrome/Edge deliver them (details.type,
// details.method, details.fromCache, details.requestId, responseHeaders).

import assert from "node:assert/strict";
import { test } from "node:test";
import path from "node:path";
import { readFileSync } from "node:fs";
import { fileURLToPath, pathToFileURL } from "node:url";
import { setup, actions, EXT_ID, ZIP, zipItem } from "./capture_tests.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, "..", "..");
const ext = path.join(root, "browser_extension");
const { createCaptureController, STORAGE_KEYS } = await import(pathToFileURL(path.join(ext, "lib", "capture.js")).href);
const rules = await import(pathToFileURL(path.join(ext, "lib", "capture_rules.js")).href);

const VIEWER_ID = "mhjfbmdgcfjbbpaeojofohoefgiehjai";
const IMAGES = ["jpg", "jpeg", "png", "gif", "webp", "bmp", "tif", "tiff", "ico"];
const ALL = ["zip", "exe", "pdf", "iso", "tar.gz", ...IMAGES];
const MINUTE = 60_000;

let rid = 0;
function observe(ctl, url, { method = "GET", status = 200, type = "other", ctype = null, cd = null, fromCache = false } = {}) {
  const responseHeaders = [];
  if (ctype) responseHeaders.push({ name: "Content-Type", value: ctype });
  if (cd) responseHeaders.push({ name: "Content-Disposition", value: cd });
  const details = { url, statusCode: status, type, fromCache, requestId: String(++rid), responseHeaders };
  if (method !== null) details.method = method;  // null: the event carried no method
  ctl.recordResponse(details);
}

function item(id, url, over = {}) {
  return { id, url, filename: "", referrer: "https://example.com/page", totalBytes: 2048, ...over };
}

const prepOf = (s) => s.app.calls.find((c) => c.action === "prepare_capture");
const untouched = (s) => !s.b.log.some((x) => x[0] === "pause" || x[0] === "cancel");

// --------------------------------------------------------------- rules
test("image_extensions_in_rules_mirror_python_defaults", () => {
  assert.deepEqual([...rules.IMAGE_EXTENSIONS], IMAGES);
  const py = readFileSync(path.join(root, "mdm", "browser_integration", "capture_rules.py"), "utf8");
  const m = /IMAGE_EXTENSIONS: List\[str\] = \[([^\]]+)\]/.exec(py);
  assert.ok(m, "Python IMAGE_EXTENSIONS found");
  assert.deepEqual(m[1].split(",").map((x) => x.trim().replace(/"/g, "")), IMAGES, "JS and Python lists match");
  for (const e of IMAGES) assert.equal(rules.extensionMatches(`C:\\dl\\Photo.${e.toUpperCase()}`, IMAGES), e);
  assert.equal(rules.extensionMatches("photo.heic", ALL), null, "unsupported image formats never match");
  assert.equal(rules.extensionMatches("vector.svg", ALL), null);
});

test("observed_resource_types_include_image", () => {
  assert.deepEqual([...rules.OBSERVED_RESOURCE_TYPES], ["main_frame", "sub_frame", "other", "image"]);
});

test("filename_detection_pdf_and_image_names", () => {
  assert.equal(rules.parseContentDispositionFilename("attachment; filename=Annual report.pdf; size=3"), "Annual report.pdf",
    "unquoted value with spaces");
  assert.equal(rules.parseContentDispositionFilename("attachment; filename=\"rÃ©sumÃ©.pdf\""), "résumé.pdf",
    "raw UTF-8 bytes exposed as Latin-1 are repaired");
  assert.equal(rules.parseContentDispositionFilename("inline; filename*=\"UTF-8''na%C3%AFve%20photo.png\""), "naïve photo.png");
  assert.equal(rules.parseContentDispositionFilename("attachment; filename=\"a\\\"b.jpg\""), "a\"b.jpg", "escaped quote");
  assert.equal(rules.parseContentDispositionFilename("attachment; filename*=UTF-8''%E6%8A%A5%E5%91%8A.pdf; filename=\"x.pdf\""),
    "报告.pdf", "filename* preferred");
  assert.equal(rules.parseContentDispositionFilename("attachment"), null);
  assert.equal(rules.filenameFromUrl("https://e.com/a/my%20photo.JPEG?name=x.png&sig=abc"), "my photo.JPEG",
    "never from the query string");
  assert.equal(rules.filenameFromUrl("https://e.com/files/report.PDF?X-Amz-Signature=a%2Fb"), "report.PDF");
  assert.equal(rules.fileExtension("REPORT.PDF"), "pdf");
  assert.equal(rules.fileExtension("download"), "");
  assert.equal(rules.extensionForMime("image/jpeg; charset=binary"), "jpg");
  assert.equal(rules.extensionForMime("application/PDF"), "pdf");
  assert.equal(rules.extensionForMime("application/octet-stream"), null, "ambiguous types give nothing");
});

test("browser_pdf_viewer_allowlist_is_narrow", () => {
  assert.equal(rules.isBrowserPdfViewer(VIEWER_ID, "chrome"), true);
  assert.equal(rules.isBrowserPdfViewer(VIEWER_ID, "edge"), true);
  assert.equal(rules.isBrowserPdfViewer(VIEWER_ID, "firefox"), false);
  assert.equal(rules.isBrowserPdfViewer("zyxwvutsrqponmlkzyxwvutsrqponmlk", "chrome"), false);
  assert.equal(rules.isBrowserPdfViewer(EXT_ID, "chrome"), false);
  assert.equal(rules.isBrowserPdfViewer(undefined, "chrome"), false);
});

// --------------------------------------------------------------- images
for (const [fmt, mime] of [["jpg", "image/jpeg"], ["jpeg", "image/jpeg"], ["png", "image/png"], ["gif", "image/gif"],
  ["webp", "image/webp"], ["bmp", "image/bmp"]]) {
  test(`image_${fmt}_automatic_capture`, async () => {
    const url = `https://img.example.com/gallery/photo.${fmt}`;
    const s = setup({ app: { extensions: ALL }, items: [item(1, url, { filename: `C:\\dl\\photo.${fmt}`, mime })] });
    observe(s.ctl, url, { type: "image", ctype: mime });
    const r = await s.ctl.handleCreated(s.b.store.get(1));
    assert.equal(r.outcome, "committed", JSON.stringify(r));
    assert.equal(prepOf(s).filename, `photo.${fmt}`);
    assert.equal(prepOf(s).mime, mime);
    assert.equal(prepOf(s).method, "GET");
    assert.deepEqual(s.b.log.map((x) => x[0]), ["pause", "cancel"]);
  });
}

test("unsupported_image_format_falls_back_to_browser", async () => {
  const url = "https://img.example.com/photo.heic";
  const s = setup({ app: { extensions: ALL }, items: [item(1, url, { filename: "C:\\dl\\photo.heic", mime: "image/heic" })] });
  observe(s.ctl, url, { type: "image", ctype: "image/heic" });
  const r = await s.ctl.handleCreated(s.b.store.get(1));
  assert.equal(r.reason, "extension_not_selected");
  assert.deepEqual(s.b.log, [], "never paused");
  assert.ok(!actions(s.app).includes("prepare_capture"));
});

test("image_request_with_get_observed_as_image_type", async () => {
  const url = "https://img.example.com/a/cat.png";
  const s = setup({ app: { extensions: ALL }, items: [item(1, url, { filename: "C:\\dl\\cat.png", mime: "image/png" })] });
  observe(s.ctl, url, { type: "image", ctype: "image/png" });
  assert.equal((await s.ctl.handleCreated(s.b.store.get(1))).outcome, "committed");
  const [d] = await s.ctl.listDiagnostics();
  assert.equal(d.resource_type, "image");
  assert.equal(d.method, "GET");
});

test("image_request_with_unknown_method_is_never_captured", async () => {
  const url = "https://img.example.com/a/cat.png";
  const s = setup({ app: { extensions: ALL }, items: [item(1, url, { filename: "C:\\dl\\cat.png" })] });
  observe(s.ctl, url, { method: null, type: "image", ctype: "image/png" });
  const r = await s.ctl.handleCreated(s.b.store.get(1));
  assert.equal(r.reason, "method_unknown");
  assert.deepEqual(s.b.log, [], "not even paused");
  const t = setup({ app: { extensions: ALL }, items: [item(1, url, { filename: "C:\\dl\\cat.png" })] });
  observe(t.ctl, url, { method: "G E T", type: "image", ctype: "image/png" });
  assert.equal((await t.ctl.handleCreated(t.b.store.get(1))).reason, "method_unknown", "malformed method");
});

test("image_post_request_is_excluded", async () => {
  const url = "https://img.example.com/render.png";
  const s = setup({ app: { extensions: ALL }, items: [item(1, url, { filename: "C:\\dl\\render.png" })] });
  observe(s.ctl, url, { method: "POST", type: "other", ctype: "image/png" });
  assert.equal((await s.ctl.handleCreated(s.b.store.get(1))).reason, "unsupported_method");
  assert.deepEqual(s.b.log, []);
});

test("image_redirect", async () => {
  const start = "https://img.example.com/i/42";
  const final = "https://cdn.example.com/u/cat.webp?sig=a%2Fb&exp=99";
  const s = setup({ app: { extensions: ALL }, items: [item(1, start, { finalUrl: final, mime: "image/webp" })] });
  observe(s.ctl, start, { status: 302 });
  observe(s.ctl, final, { ctype: "image/webp" });
  const r = await s.ctl.handleCreated(s.b.store.get(1));
  assert.equal(r.outcome, "committed");
  assert.equal(prepOf(s).filename, "cat.webp", "final URL path, never its query");
  assert.equal(prepOf(s).url, start, "original URL; the desktop follows the redirect");
});

test("image_content_disposition_filename", async () => {
  const url = "https://e.com/media?id=7&name=evil.exe";
  const s = setup({ app: { extensions: ALL }, items: [item(1, url, { mime: "image/png" })] });
  observe(s.ctl, url, { ctype: "image/png", cd: "inline; filename=\"holiday photo.png\"" });
  assert.equal((await s.ctl.handleCreated(s.b.store.get(1))).outcome, "committed");
  assert.equal(prepOf(s).filename, "holiday photo.png");
});

test("save_image_as_served_from_cache_minutes_later", async () => {
  // "Save image as…": the image was displayed (type "image") long before the
  // download, which is then served from the HTTP cache.
  const url = "https://img.example.com/big/sunset.jpg?w=2048";
  const s = setup({ app: { extensions: ALL }, items: [item(1, url, { filename: "C:\\dl\\sunset.jpg", mime: "image/jpeg" })] });
  observe(s.ctl, url, { type: "image", ctype: "image/jpeg" });
  s.advance(10 * MINUTE);
  assert.equal((await s.ctl.handleCreated(s.b.store.get(1))).outcome, "committed");

  // The longer window applies only to displayed PDFs/images, not to other downloads.
  const z = setup({ items: [zipItem()] });
  observe(z.ctl, ZIP, { type: "other", ctype: "application/zip" });
  z.advance(10 * MINUTE);
  assert.equal((await z.ctl.handleCreated(z.b.store.get(1))).reason, "method_unknown");

  // ...and never beyond 30 minutes.
  const o = setup({ app: { extensions: ALL }, items: [item(1, url, { filename: "C:\\dl\\sunset.jpg", mime: "image/jpeg" })] });
  observe(o.ctl, url, { type: "image", ctype: "image/jpeg" });
  o.advance(31 * MINUTE);
  assert.equal((await o.ctl.handleCreated(o.b.store.get(1))).reason, "method_unknown");
});

test("cached_response_reported_only_by_on_response_started", async () => {
  const url = "https://img.example.com/c/logo.gif";
  const s = setup({ app: { extensions: ALL }, items: [item(1, url, { filename: "C:\\dl\\logo.gif", mime: "image/gif" })] });
  observe(s.ctl, url, { type: "other", ctype: "image/gif", fromCache: true });
  assert.equal((await s.ctl.handleCreated(s.b.store.get(1))).outcome, "committed");
  const [d] = await s.ctl.listDiagnostics();
  assert.equal(d.from_cache, true);
});

test("observations_survive_service_worker_restart_without_storing_urls", async () => {
  const url = "https://img.example.com/private/cat.png?X-Amz-Signature=SECRETSIG&token=t0k";
  const s = setup({ app: { extensions: ALL }, items: [item(1, url, { filename: "C:\\dl\\cat.png", mime: "image/png" })] });
  const before = createCaptureController({ ...s.deps });  // the worker that saw the image
  observe(before, url, { type: "image", ctype: "image/png", cd: "inline; filename=\"cat.png\"" });
  await before.flushObservations();
  s.advance(3 * MINUTE);  // worker stopped; a new one (s.ctl) handles the download
  const r = await s.ctl.handleCreated(s.b.store.get(1));
  assert.equal(r.outcome, "committed");
  const [d] = await s.ctl.listDiagnostics();
  assert.equal(d.method_source, "session");
  const dump = JSON.stringify(s.storageSession.data);
  for (const secret of ["img.example.com", "SECRETSIG", "t0k", "/private/", "https://"]) {
    assert.ok(!dump.includes(secret), `session storage never holds the URL (${secret})`);
  }
  assert.ok(Object.keys(s.storageSession.data[STORAGE_KEYS.observations]).every((k) => /^[0-9a-f]{64}$/.test(k)));
});

// ----------------------------------------------------------------- PDFs
test("pdf_direct_attachment_capture", async () => {
  const url = "https://example.com/report.pdf";
  const s = setup({ items: [item(1, url, { filename: "C:\\dl\\report.pdf", mime: "application/pdf" })] });
  observe(s.ctl, url, { type: "main_frame", ctype: "application/pdf", cd: "attachment; filename=\"report.pdf\"" });
  const r = await s.ctl.handleCreated(s.b.store.get(1));
  assert.equal(r.outcome, "committed");
  assert.deepEqual(actions(s.app), ["get_capture_config", "prepare_capture", "capture_status", "commit_capture"]);
  assert.equal(prepOf(s).filename, "report.pdf");
  assert.equal(s.b.store.get(1).state, "interrupted", "browser copy cancelled after approval");
});

test("pdf_uppercase_extension", async () => {
  const url = "https://example.com/docs/REPORT.PDF";
  const s = setup({ items: [item(1, url, { filename: "C:\\dl\\REPORT.PDF" })] });
  observe(s.ctl, url, { ctype: "application/pdf" });
  assert.equal((await s.ctl.handleCreated(s.b.store.get(1))).outcome, "committed");
  assert.equal(prepOf(s).filename, "REPORT.PDF");
});

test("pdf_signed_query_string", async () => {
  const url = "https://cdn.example.com/r/report.pdf?X-Amz-Signature=a%2Fb%3D&file=evil.exe&token=t0k";
  const s = setup({ items: [item(1, url, { mime: "application/pdf" })] });
  observe(s.ctl, url, { ctype: "application/pdf" });
  assert.equal((await s.ctl.handleCreated(s.b.store.get(1))).outcome, "committed");
  assert.equal(prepOf(s).filename, "report.pdf", "extension from the path, never the query");
  assert.equal(prepOf(s).url, url, "signed URL forwarded unmodified");
  assert.equal(s.app.calls.find((c) => c.action === "commit_capture").url, url);
  const dump = JSON.stringify(await s.ctl.listDiagnostics());
  assert.ok(!dump.includes("X-Amz") && !dump.includes("t0k") && !dump.includes("cdn.example.com"), "diagnostics hold no URL");
});

test("pdf_redirect", async () => {
  const start = "https://example.com/download/77";
  const final = "https://files.example.net/store/Q3.pdf?sig=xyz";
  const s = setup({ items: [item(1, start, { finalUrl: final, mime: "application/pdf" })] });
  observe(s.ctl, start, { status: 302 });
  observe(s.ctl, final, { ctype: "application/pdf" });
  assert.equal((await s.ctl.handleCreated(s.b.store.get(1))).outcome, "committed");
  assert.equal(prepOf(s).filename, "Q3.pdf");
  assert.equal(prepOf(s).url, start);
});

test("pdf_redirect_with_a_post_hop_is_excluded", async () => {
  const start = "https://example.com/export";
  const final = "https://files.example.net/out/Q3.pdf";
  const s = setup({ items: [item(1, start, { finalUrl: final })] });
  observe(s.ctl, start, { method: "POST", status: 303 });
  observe(s.ctl, final, { ctype: "application/pdf" });
  assert.equal((await s.ctl.handleCreated(s.b.store.get(1))).reason, "unsupported_method");
  assert.deepEqual(s.b.log, []);
});

test("pdf_without_url_extension_uses_content_disposition", async () => {
  const url = "https://example.com/documents/get?id=5";
  const s = setup({ items: [item(1, url)] });
  observe(s.ctl, url, { ctype: "application/pdf", cd: "attachment; filename*=UTF-8''Rapport%20annuel%20%C3%A9t%C3%A9.pdf" });
  assert.equal((await s.ctl.handleCreated(s.b.store.get(1))).outcome, "committed");
  assert.equal(prepOf(s).filename, "Rapport annuel été.pdf");
});

test("pdf_without_any_name_uses_mime_type", async () => {
  const url = "https://example.com/export?id=9";
  const s = setup({ items: [item(1, url, { mime: "application/pdf" })] });
  observe(s.ctl, url, { ctype: "application/pdf" });
  assert.equal((await s.ctl.handleCreated(s.b.store.get(1))).outcome, "committed");
  assert.equal(prepOf(s).filename, "export.pdf");
  const o = setup({ items: [item(1, url, { mime: "application/octet-stream" })] });
  observe(o.ctl, url, { ctype: "application/octet-stream" });
  assert.equal((await o.ctl.handleCreated(o.b.store.get(1))).reason, "extension_not_selected", "ambiguous MIME: no guess");
});

test("pdf_opened_in_browser_viewer_is_left_alone", async () => {
  // A PDF link displayed in the built-in viewer is a navigation: the browser
  // creates no download item, so nothing is paused, cancelled or sent.
  const url = "https://example.com/manual.pdf";
  const s = setup();
  observe(s.ctl, url, { type: "main_frame", ctype: "application/pdf" });
  assert.deepEqual(s.b.log, []);
  assert.deepEqual(s.app.calls, []);
});

test("pdf_viewer_download_button", async () => {
  // Viewed earlier (main_frame GET application/pdf), the Download button is
  // clicked minutes later and served from the cache (no new network headers).
  const url = "https://example.com/manual.pdf";
  for (const byExtensionId of [undefined, VIEWER_ID]) {
    const s = setup({ items: [item(1, url, { filename: "C:\\dl\\manual.pdf", mime: "application/pdf", byExtensionId })] });
    observe(s.ctl, url, { type: "main_frame", ctype: "application/pdf" });
    s.advance(6 * MINUTE);
    const r = await s.ctl.handleCreated(s.b.store.get(1));
    assert.equal(r.outcome, "committed", `byExtensionId=${byExtensionId}`);
    const [d] = await s.ctl.listDiagnostics();
    assert.equal(d.initiator, byExtensionId ? "browser_pdf_viewer" : "browser");
    assert.equal(d.by_extension, !!byExtensionId);
  }
  // The viewer allowance covers PDFs only.
  const z = setup({ items: [zipItem({ byExtensionId: VIEWER_ID })] });
  observe(z.ctl, ZIP);
  assert.equal((await z.ctl.handleCreated(z.b.store.get(1))).reason, "other_extension");
  const n = setup({ items: [item(1, "https://example.com/get?id=1", { byExtensionId: VIEWER_ID, mime: "application/zip" })] });
  observe(n.ctl, "https://example.com/get?id=1", { ctype: "application/zip", cd: "attachment; filename=\"x.zip\"" });
  assert.equal((await n.ctl.handleCreated(n.b.store.get(1))).reason, "other_extension");
});

test("pdf_viewer_download_with_mismatched_cached_type_is_not_trusted", async () => {
  const url = "https://example.com/doc";
  const s = setup({ items: [item(1, url, { filename: "C:\\dl\\doc.pdf", mime: "application/pdf" })] });
  observe(s.ctl, url, { type: "main_frame", ctype: "image/png" });
  s.advance(6 * MINUTE);
  assert.equal((await s.ctl.handleCreated(s.b.store.get(1))).reason, "method_unknown");
  assert.equal(s.b.store.get(1).paused, false);
});

test("pdf_download_initiated_by_another_extension", async () => {
  const url = "https://example.com/report.pdf";
  const s = setup({ items: [item(1, url, { filename: "C:\\dl\\report.pdf", byExtensionId: "zyxwvutsrqponmlkzyxwvutsrqponmlk" })] });
  observe(s.ctl, url, { ctype: "application/pdf" });
  assert.equal((await s.ctl.handleCreated(s.b.store.get(1))).reason, "other_extension");
  assert.deepEqual(s.b.log, [], "never paused");
  const [d] = await s.ctl.listDiagnostics();
  assert.equal(d.initiator, "other_extension");
});

test("pdf_unsupported_blob_url_stays_in_browser", async () => {
  for (const byExtensionId of [undefined, VIEWER_ID]) {
    const url = "blob:https://example.com/0b1c-22";
    const s = setup({ items: [item(1, url, { filename: "C:\\dl\\edited.pdf", mime: "application/pdf", byExtensionId })] });
    assert.equal((await s.ctl.handleCreated(s.b.store.get(1))).reason, "unsupported_url");
    assert.deepEqual(s.b.log, []);
  }
  const d = setup({ items: [item(1, "data:application/pdf;base64,JVBERi0=", { filename: "C:\\dl\\x.pdf" })] });
  assert.equal((await d.ctl.handleCreated(d.b.store.get(1))).reason, "unsupported_url");
});

test("pdf_authentication_required_resumes_in_browser", async () => {
  const url = "https://intranet.example.com/hr/payslip.pdf";
  const s = setup({ app: { probe: "auth_required" }, items: [item(1, url, { filename: "C:\\dl\\payslip.pdf" })] });
  observe(s.ctl, url, { ctype: "application/pdf" });
  const r = await s.ctl.handleCreated(s.b.store.get(1));
  assert.equal(r.reason, "auth_required");
  assert.equal(s.b.store.get(1).paused, false);
  assert.equal(s.b.store.get(1).state, "in_progress");
  assert.ok(!s.b.log.some((x) => x[0] === "cancel"));
});

test("pdf_fallback_when_desktop_app_is_closed", async () => {
  const url = "https://example.com/report.pdf";
  // No cached configuration: nothing is touched at all.
  const s = setup({ app: { configError: { error_code: "app_not_running" } }, items: [item(1, url, { filename: "C:\\dl\\report.pdf" })] });
  observe(s.ctl, url, { ctype: "application/pdf" });
  assert.equal((await s.ctl.handleCreated(s.b.store.get(1))).reason, "app_unavailable");
  assert.deepEqual(s.b.log, []);
  // A stale cached "automatic" configuration: paused at once, then resumed when the app cannot be reached.
  const stale = { mode: "automatic", extensions: ["pdf"], excluded_sites: [], fetched_at: 1 };
  const t = setup({ app: { configError: { error_code: "app_not_running" } }, local: { [STORAGE_KEYS.config]: stale },
    items: [item(1, url, { filename: "C:\\dl\\report.pdf" })] });
  observe(t.ctl, url, { ctype: "application/pdf" });
  const r = await t.ctl.handleCreated(t.b.store.get(1));
  assert.deepEqual([r.outcome, r.reason], ["resumed", "app_unavailable"]);
  assert.deepEqual(t.b.log.map((x) => x[0]), ["pause", "resume"]);
  assert.equal(t.b.store.get(1).state, "in_progress", "the browser download continues");
  assert.ok(!actions(t.app).includes("prepare_capture"));
});

test("pause_happens_before_config_refresh_when_cached_config_enables_capture", async () => {
  const url = "https://example.com/small.png";
  const stale = { mode: "automatic", extensions: ALL, excluded_sites: [], fetched_at: 1 };
  const s = setup({ app: { extensions: ALL }, local: { [STORAGE_KEYS.config]: stale },
    items: [item(1, url, { filename: "C:\\dl\\small.png" })] });
  observe(s.ctl, url, { type: "image", ctype: "image/png" });
  const order = [];
  const origPause = s.b.api.pause;
  s.b.api.pause = async (id) => { order.push(`pause after ${actions(s.app).length} native calls`); return origPause(id); };
  const r = await s.ctl.handleCreated(s.b.store.get(1));
  assert.equal(r.outcome, "committed");
  assert.deepEqual(order, ["pause after 0 native calls"]);
  assert.equal(actions(s.app)[0], "get_capture_config", "configuration still refreshed before preparing");
  // Disabled since the cached copy was taken: resumed, never prepared.
  const d = setup({ app: { mode: "disabled", extensions: ALL }, local: { [STORAGE_KEYS.config]: stale },
    items: [item(1, url, { filename: "C:\\dl\\small.png" })] });
  observe(d.ctl, url, { type: "image", ctype: "image/png" });
  assert.deepEqual(Object.values(await d.ctl.handleCreated(d.b.store.get(1))), ["resumed", "disabled"]);
  assert.equal(d.b.store.get(1).paused, false);
  assert.ok(!actions(d.app).includes("prepare_capture"));
});

test("small_file_completed_before_pause_is_left_alone", async () => {
  const url = "https://example.com/tiny.png";
  const s = setup({ app: { extensions: ALL }, items: [item(1, url, { filename: "C:\\dl\\tiny.png" })] });
  observe(s.ctl, url, { type: "image", ctype: "image/png" });
  s.b.api.pause = async () => { s.b.store.get(1).state = "complete"; throw new Error("Download must be in progress"); };
  const r = await s.ctl.handleCreated(s.b.store.get(1));
  assert.equal(r.reason, "completed_before_pause");
  assert.ok(!actions(s.app).includes("prepare_capture"), "no duplicate download");
});

// ---------------------------------------------------------- diagnostics
test("capture_diagnostics_are_complete_bounded_and_private", async () => {
  const reported = [];
  const url = "https://cdn.example.com/f/report.pdf?X-Amz-Signature=SECRETSIG&token=t0p53cr3t";
  const s = setup({ items: [item(1, url, { filename: "C:\\Users\\me\\Downloads\\report.pdf", mime: "application/pdf" })] });
  const ctl = createCaptureController({ ...s.deps, reportDiagnostic: (e) => { reported.push(e); } });
  observe(ctl, url, { type: "main_frame", ctype: "application/pdf" });
  await ctl.handleCreated(s.b.store.get(1));
  const [d] = await ctl.listDiagnostics();
  for (const key of ["download_id", "browser", "resource_type", "method", "filename", "extension", "mime", "state",
    "paused", "by_extension", "initiator", "decision", "reason"]) {
    assert.ok(key in d, `diagnostic has ${key}`);
  }
  assert.deepEqual([d.download_id, d.browser, d.resource_type, d.method, d.filename, d.extension, d.mime, d.decision],
    [1, "chrome", "main_frame", "GET", "report.pdf", "pdf", "application/pdf", "committed"]);
  assert.equal(d.state, "interrupted");
  assert.equal(reported.length, 1, "forwarded to the desktop app");
  const dump = JSON.stringify([s.storageSession.data, s.storageLocal.data[STORAGE_KEYS.config], reported]);
  for (const secret of ["SECRETSIG", "t0p53cr3t", "cdn.example.com", "X-Amz", "C:\\\\Users"]) {
    assert.ok(!dump.includes(secret), `no ${secret} in diagnostics`);
  }

  for (let i = 2; i < 40; i += 1) {
    s.b.add({ id: i, url: `https://e.com/f${i}.heic`, filename: `C:\\dl\\f${i}.heic` });
    await ctl.handleCreated(s.b.store.get(i));
  }
  const all = await ctl.listDiagnostics();
  assert.equal(all.length, 25, "bounded history");
  assert.equal(all[0].download_id, 39, "newest first");
  assert.equal(all[0].reason, "extension_not_selected");
});

test("diagnostics_not_reported_to_desktop_when_capture_is_off", async () => {
  const reported = [];
  const s = setup({ app: { mode: "disabled" }, items: [zipItem()] });
  const ctl = createCaptureController({ ...s.deps, reportDiagnostic: (e) => { reported.push(e); } });
  await ctl.handleCreated(s.b.store.get(1));
  assert.equal(reported.length, 0);
  assert.equal((await ctl.listDiagnostics())[0].reason, "disabled", "still visible in the popup");
  assert.deepEqual(actions(s.app), ["get_capture_config"]);
});

// ----------------------------------------------------------- regression
test("regression_zip_exe_and_modes_with_images_enabled", async () => {
  const z = setup({ app: { extensions: ALL }, items: [zipItem()] });
  observe(z.ctl, ZIP);
  assert.equal((await z.ctl.handleCreated(z.b.store.get(1))).outcome, "committed", "ZIP");

  const exe = "https://downloads.example.com/setup/Setup-2.0.EXE";
  const e = setup({ app: { extensions: ALL }, items: [item(1, exe, { filename: "C:\\dl\\Setup-2.0.EXE" })] });
  observe(e.ctl, exe, { ctype: "application/x-msdownload" });
  assert.equal((await e.ctl.handleCreated(e.b.store.get(1))).outcome, "committed", "EXE");

  const png = "https://img.example.com/p.png";
  const a = setup({ app: { mode: "ask", extensions: ALL }, items: [item(1, png, { filename: "C:\\dl\\p.png" })] });
  observe(a.ctl, png, { type: "image", ctype: "image/png" });
  assert.equal((await a.ctl.handleCreated(a.b.store.get(1))).outcome, "committed", "Ask mode approval");
  const no = setup({ app: { mode: "ask", decision: "declined", extensions: ALL }, items: [item(1, png, { filename: "C:\\dl\\p.png" })] });
  observe(no.ctl, png, { type: "image", ctype: "image/png" });
  assert.equal((await no.ctl.handleCreated(no.b.store.get(1))).reason, "user_declined", "Ask mode rejection");
  assert.equal(no.b.store.get(1).paused, false);

  const off = setup({ app: { mode: "disabled", extensions: ALL }, items: [item(1, png, { filename: "C:\\dl\\p.png" })] });
  observe(off.ctl, png, { type: "image", ctype: "image/png" });
  assert.equal((await off.ctl.handleCreated(off.b.store.get(1))).reason, "disabled", "Off mode");
  assert.deepEqual(off.b.log, []);
});

test("regression_exclusions_with_pdfs_and_images", async () => {
  const cases = [
    [{ app: { extensions: ALL, excluded: ["example.com"] } }, item(1, "https://img.example.com/p.png", { filename: "C:\\dl\\p.png" }), "site_excluded"],
    [{ app: { extensions: ALL } }, item(1, "https://e.com/p.png", { filename: "C:\\dl\\p.png", incognito: true }), "incognito"],
    [{ app: { extensions: ALL } }, item(1, "https://e.com/r.pdf", { filename: "C:\\dl\\r.pdf", danger: "file" }), "dangerous"],
  ];
  for (const [opts, it, reason] of cases) {
    const s = setup({ ...opts, items: [it] });
    observe(s.ctl, it.url, { ctype: "image/png" });
    assert.equal((await s.ctl.handleCreated(s.b.store.get(1))).reason, reason);
    assert.ok(untouched(s), reason);
  }
  // Duplicate protection.
  const url = "https://e.com/d.jpg";
  const d = setup({ app: { extensions: ALL }, items: [item(1, url, { filename: "C:\\dl\\d.jpg" })] });
  observe(d.ctl, url, { type: "image", ctype: "image/jpeg" });
  const [x, y] = await Promise.all([d.ctl.handleCreated(d.b.store.get(1)), d.ctl.handleCreated(d.b.store.get(1))]);
  assert.deepEqual([x.outcome, y.reason].sort(), ["committed", "duplicate_event"].sort());
  assert.equal(actions(d.app).filter((a) => a === "commit_capture").length, 1);
});
