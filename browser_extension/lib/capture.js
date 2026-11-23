// My Download Manager - automatic download capture, browser side (Stage 6B).
//
// The browser has ALREADY started transferring when its "download created" event fires,
// so the flow is: pause -> verify -> prepare (desktop durably records it)
// -> desktop approves -> cancel + confirm -> commit. On any doubt the original
// download is resumed (or simply never touched). Nothing is ever cancelled
// before the desktop application has accepted the capture.
//
// All browser APIs are injected (createCaptureController(deps)) so this module
// is unit-testable with mocks; service_worker.js wires the real chrome.* APIs.

import { validateDownloadUrl, cleanReferrer, newRequestId as defaultNewRequestId } from "./validation.js";
import {
  basename, extensionMatches, extensionForMime, fileExtension, filenameFromUrl, hostOf, hostIsExcluded,
  isBrowserPdfViewer, isPdfLike, normalizeMime, parseContentDispositionFilename,
} from "./capture_rules.js";

export const DEFAULT_TIMING = Object.freeze({
  configMaxAgeMs: 30_000,      // re-fetch the desktop configuration after this
  metadataWaitMs: 2_000,       // max time a download stays paused while we gather facts
  metadataPollMs: 150,
  pollIntervalMs: 1_500,       // capture_status polling while the desktop decides
  decisionTimeoutMs: 150_000,  // > desktop dialog timeout (120 s) + probe
  maxStatusFailures: 3,
  cancelConfirmMs: 5_000,
  commitRetryDelays: [1_000, 3_000, 10_000],
  commitGiveUpMs: 10 * 60_000, // then the browser re-downloads the file itself
  observationTtlMs: 120_000,   // webRequest observations: how long they prove a GET
  // A GET that delivered a PDF document or an image keeps proving the method for
  // longer: the PDF viewer's Download button and "Save image as…" re-request the
  // same URL from the browser cache, usually minutes after it was displayed.
  extendedObservationTtlMs: 30 * 60_000,
  observationFlushMs: 500,     // batching of the (hashed) session-storage copy
});

export const STORAGE_KEYS = Object.freeze({
  config: "captureConfig",
  captures: "captures",           // request_id -> in-flight capture (needed for recovery)
  handled: "handledDownloads",    // browser download id -> processing marker
  observations: "observedResponses", // SHA-256(url) -> [{method, status, ...}] (session storage)
  diagnostics: "captureDiagnostics", // bounded, privacy-safe decision history (session storage)
});

const MAX_HANDLED = 500;
const MAX_OBSERVED_URLS = 500;
const MAX_OBSERVED_IMAGE_URLS = 1000; // separate pool: a gallery page must not evict downloads
const MAX_PERSISTED_OBSERVATIONS = 400;
const MAX_DIAGNOSTICS = 25;
const EXTENDED_TYPES = new Set(["main_frame", "sub_frame", "image"]);
const OK_STATUSES = new Set([200, 206, 304]);

// Commit failures that will never succeed: hand the download back to the browser.
const DEFINITIVE_COMMIT_ERRORS = new Set([
  "capture_not_found", "capture_not_approved", "request_id_conflict", "capture_unavailable",
  "invalid_field", "missing_field", "invalid_url", "unsupported_scheme", "embedded_credentials",
  "invalid_message", "unsupported_action",
]);
const APP_UNAVAILABLE_CODES = new Set(["app_not_running", "invalid_endpoint", "connection_lost",
  "shutting_down", "connection_failed"]);

const RESUME_NOTICES = {
  auth_required: "This download needs your browser session (sign-in/cookies), so it continues in the browser.",
  html_response: "This site needs your browser session, so the download continues in the browser.",
  tls_error: "The secure connection could not be verified by My Download Manager; continuing in the browser.",
  not_found: "My Download Manager could not access this file; continuing in the browser.",
  http_error: "The server refused My Download Manager; continuing in the browser.",
  probe_timeout: "The server did not answer My Download Manager; continuing in the browser.",
  probe_failed: "My Download Manager could not reach the file; continuing in the browser.",
  app_unavailable: "My Download Manager is not reachable; the download continues in the browser.",
  native_host_unavailable: "The native messaging host is not installed; the download continues in the browser.",
  timeout: "No answer from My Download Manager in time; the download continues in the browser.",
  cancel_failed: "The browser download could not be handed over; it continues in the browser.",
  cancel_unconfirmed: "The browser download could not be handed over; it continues in the browser.",
};

function sanitizeReason(reason) {
  const r = String(reason || "other").toLowerCase().replace(/[^a-z_]/g, "_").slice(0, 40);
  return r || "other";
}

function urlKey(u) {
  try {
    const x = new URL(u);
    x.hash = "";
    return x.href;
  } catch (_e) {
    return null;
  }
}

/** Hex SHA-256 of a URL. Only this fingerprint (never the URL, which may carry a
 *  signed token) is written to session storage. null when WebCrypto is missing. */
async function urlHash(key) {
  try {
    const subtle = globalThis.crypto && globalThis.crypto.subtle;
    if (!subtle || !key) return null;
    const digest = await subtle.digest("SHA-256", new TextEncoder().encode(key));
    return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
  } catch (_e) {
    return null;
  }
}

function cleanMethod(m) {
  const v = String(m || "").toUpperCase();
  return /^[A-Z]{1,12}$/.test(v) ? v : "";
}

/** Proof of a GET that may be used for longer than observationTtlMs. */
function isExtendedObservation(o) {
  return o.method === "GET" && EXTENDED_TYPES.has(o.type) && OK_STATUSES.has(o.status)
    && (o.ctype === "application/pdf" || (typeof o.ctype === "string" && o.ctype.startsWith("image/")));
}

function mimeFamily(m) {
  const v = normalizeMime(m);
  if (!v) return null;
  if (v === "application/pdf") return "pdf";
  if (v.startsWith("image/")) return "image";
  return v;
}

export function createCaptureController(deps) {
  const {
    downloads, storageLocal, sendNative,
    storageSession = storageLocal,
    notify = () => {},
    browser = "chrome",
    extensionId = "",
    extensionVersion = "",
    hasPermissions = async () => true,
    reportDiagnostic = null,  // optional: forwards a decision summary to the desktop app
  } = deps;
  const newRequestId = deps.newRequestId || defaultNewRequestId;
  const now = deps.now || (() => Date.now());
  const sleep = deps.sleep || ((ms) => new Promise((r) => setTimeout(r, ms)));
  const schedule = deps.schedule || ((fn, ms) => {
    const t = setTimeout(fn, ms);
    if (t && typeof t.unref === "function") t.unref();
    return t;
  });
  const T = { ...DEFAULT_TIMING, ...(deps.timing || {}) };

  // url -> [{method, status, at, cdName, ctype, type, fromCache, rid}] (memory only)
  const observations = new Map();
  const imageObservations = new Map();
  const pendingPersist = new Map();  // url -> entries not yet copied (hashed) to session storage
  let flushTimer = null;
  const changeWaiters = new Map();   // download id -> Set(resolve)
  const activeDownloads = new Set();
  const runningCaptures = new Set();
  let lock = Promise.resolve();
  let recovering = null;
  let lastConfigError = null;

  // ------------------------------------------------------------- storage
  function withLock(fn) {
    const run = lock.then(fn, fn);
    lock = run.catch(() => {});
    return run;
  }
  async function readMap(area, key) {
    const got = await area.get(key);
    const v = got && got[key];
    return v && typeof v === "object" ? v : {};
  }
  function updateMap(area, key, mutate) {
    return withLock(async () => {
      const map = await readMap(area, key);
      mutate(map);
      await area.set({ [key]: map });
      return map;
    });
  }
  const saveRecord = (rec) => updateMap(storageLocal, STORAGE_KEYS.captures, (m) => { m[rec.request_id] = { ...rec }; });
  const removeRecord = (rid) => updateMap(storageLocal, STORAGE_KEYS.captures, (m) => { delete m[rid]; });
  function markHandled(id, info) {
    return updateMap(storageSession, STORAGE_KEYS.handled, (m) => {
      m[String(id)] = { ...info, at: now() };
      const keys = Object.keys(m);
      if (keys.length > MAX_HANDLED) {
        keys.sort((a, b) => m[a].at - m[b].at).slice(0, keys.length - MAX_HANDLED).forEach((k) => delete m[k]);
      }
    });
  }
  async function getHandled(id) {
    return (await readMap(storageSession, STORAGE_KEYS.handled))[String(id)] || null;
  }

  // ---------------------------------------------------- browser helpers
  async function getItem(id) {
    try {
      const items = await downloads.search({ id });
      return Array.isArray(items) && items.length ? items[0] : null;
    } catch (_e) {
      return null;
    }
  }
  async function resumeIfPaused(id) {
    const item = await getItem(id);
    if (item && item.state === "in_progress" && item.paused) {
      try {
        await downloads.resume(id);
        return true;
      } catch (_e) {
        return false;
      }
    }
    return false;
  }
  function waitForChange(id, ms) {
    return new Promise((resolve) => {
      let done = false;
      const finish = () => {
        if (done) return;
        done = true;
        const set = changeWaiters.get(id);
        if (set) {
          set.delete(finish);
          if (!set.size) changeWaiters.delete(id);
        }
        resolve();
      };
      if (!changeWaiters.has(id)) changeWaiters.set(id, new Set());
      changeWaiters.get(id).add(finish);
      sleep(ms).then(finish, finish);
    });
  }

  // ------------------------------------------------ webRequest correlation
  /** webRequest.onHeadersReceived / onResponseStarted listener (observe-only):
   *  remembers the method, status, resource type, Content-Type and the
   *  Content-Disposition *filename* of recent http(s) responses. Cookies,
   *  Authorization headers and bodies are never read. The URL itself stays in
   *  memory; session storage receives only a SHA-256 fingerprint of it, so a
   *  restarted service worker can still confirm a GET. */
  function recordResponse(details) {
    if (!details || typeof details.url !== "string") return;
    const key = urlKey(details.url);
    if (!key || !/^https?:/i.test(key)) return;
    const headers = Array.isArray(details.responseHeaders) ? details.responseHeaders : [];
    const header = (name) => {
      const h = headers.find((x) => x && typeof x.name === "string" && x.name.toLowerCase() === name);
      return h && typeof h.value === "string" ? h.value : null;
    };
    const cd = header("content-disposition");
    const entry = {
      method: cleanMethod(details.method),
      status: Number.isInteger(details.statusCode) ? details.statusCode : null,
      at: now(),
      cdName: cd ? (parseContentDispositionFilename(cd) || null) : null,
      ctype: normalizeMime(header("content-type")),
      type: typeof details.type === "string" ? details.type.slice(0, 20) : null,
      fromCache: details.fromCache === true,
      rid: details.requestId != null ? String(details.requestId).slice(0, 32) : null,
    };
    const isImage = entry.type === "image";
    const map = isImage ? imageObservations : observations;
    const list = map.get(key) || [];
    const last = list[list.length - 1];
    if (last && entry.rid && last.rid === entry.rid && last.status === entry.status) {
      // The same response reported by both listeners: merge instead of duplicating.
      last.cdName = last.cdName || entry.cdName;
      last.ctype = last.ctype || entry.ctype;
      last.fromCache = last.fromCache || entry.fromCache;
    } else {
      list.push(entry);
    }
    map.delete(key);
    map.set(key, list.slice(-5));
    const cap = isImage ? MAX_OBSERVED_IMAGE_URLS : MAX_OBSERVED_URLS;
    while (map.size > cap) map.delete(map.keys().next().value);
    queuePersist(key, entry);
  }

  function queuePersist(key, entry) {
    const list = pendingPersist.get(key) || [];
    list.push(entry);
    pendingPersist.set(key, list.slice(-3));
    if (!flushTimer) {
      flushTimer = schedule(() => { flushTimer = null; flushObservations(); }, T.observationFlushMs);
    }
  }

  /** Copies pending observations to session storage keyed by SHA-256(url). */
  async function flushObservations() {
    if (!pendingPersist.size) return;
    const batch = [...pendingPersist.entries()];
    pendingPersist.clear();
    const hashed = [];
    for (const [key, entries] of batch) {
      const h = await urlHash(key);
      if (h) hashed.push([h, entries]);
    }
    if (!hashed.length) return;
    try {
      await updateMap(storageSession, STORAGE_KEYS.observations, (m) => {
        for (const [h, entries] of hashed) {
          const stored = entries.map(({ rid: _rid, ...rest }) => rest);
          m[h] = [...(Array.isArray(m[h]) ? m[h] : []), ...stored].slice(-3);
        }
        const keys = Object.keys(m);
        if (keys.length > MAX_PERSISTED_OBSERVATIONS) {
          const latest = (k) => Math.max(...m[k].map((o) => o.at || 0));
          keys.sort((a, b) => latest(a) - latest(b))
            .slice(0, keys.length - MAX_PERSISTED_OBSERVATIONS).forEach((k) => delete m[k]);
        }
      });
    } catch (_e) {
      // Persistence is best-effort; the in-memory copy still works.
    }
  }

  function memoryObservations(key) {
    return [...(observations.get(key) || []), ...(imageObservations.get(key) || [])];
  }

  /** The decision rule shared by the memory and the session-storage lookups.
   *  method is "GET" only when the response that delivered the file was observed
   *  AND every observed hop was a GET. A missing/unparseable method is "UNKNOWN". */
  function decideRequest(url, finalUrl, listFor) {
    const usable = (o) => now() - o.at <= (isExtendedObservation(o) ? T.extendedObservationTtlMs : T.observationTtlMs);
    const byTime = (a, b) => a.at - b.at;
    const finalKey = urlKey(finalUrl || url);
    const finalObs = listFor(finalKey).filter(usable).sort(byTime);
    if (!finalObs.length) return { method: null };
    const keys = [...new Set([urlKey(url), finalKey].filter(Boolean))];
    const all = keys.flatMap((k) => listFor(k).filter(usable));
    const nonGet = all.find((o) => o.method !== "GET");
    if (nonGet) return { method: nonGet.method || "UNKNOWN", type: nonGet.type || null };
    const last = finalObs[finalObs.length - 1];
    return {
      method: "GET", cdName: last.cdName || null, ctype: last.ctype || null, status: last.status,
      type: last.type || null, fromCache: !!last.fromCache, source: last.source || "memory",
      extended: now() - last.at > T.observationTtlMs,
    };
  }

  /** Synchronous lookup over the in-memory observations only. */
  function lookupRequest(url, finalUrl) {
    return decideRequest(url, finalUrl, memoryObservations);
  }

  /** Memory first, then the hashed session-storage copy (survives a worker restart). */
  async function lookupRequestFull(url, finalUrl) {
    const mem = lookupRequest(url, finalUrl);
    if (mem.method) return mem;
    let persisted = {};
    try {
      persisted = await readMap(storageSession, STORAGE_KEYS.observations);
    } catch (_e) {
      return mem;
    }
    if (!Object.keys(persisted).length) return mem;
    const keys = [...new Set([urlKey(url), urlKey(finalUrl || url)].filter(Boolean))];
    const hashes = new Map();
    for (const k of keys) hashes.set(k, await urlHash(k));
    return decideRequest(url, finalUrl, (k) => {
      const stored = Array.isArray(persisted[hashes.get(k)]) ? persisted[hashes.get(k)] : [];
      return [...memoryObservations(k), ...stored.map((o) => ({ ...o, source: "session" }))];
    });
  }

  // -------------------------------------------------------- configuration
  async function refreshConfig() {
    let perms = false;
    try {
      perms = !!(await hasPermissions());
    } catch (_e) {
      perms = false;
    }
    const r = await sendNative({
      action: "get_capture_config", browser, capture_permissions: perms, extension_version: extensionVersion,
    });
    if (!r || !r.ok || !r.data) {
      lastConfigError = r || null;
      return null;
    }
    lastConfigError = null;
    const cfg = { ...r.data, fetched_at: now() };
    await storageLocal.set({ [STORAGE_KEYS.config]: cfg });
    return cfg;
  }

  /** Cached desktop configuration. If the app cannot be reached the effective
   *  mode is "disabled": downloads are left completely alone. */
  async function getConfig({ refresh = false } = {}) {
    const got = await storageLocal.get(STORAGE_KEYS.config);
    const cached = got && got[STORAGE_KEYS.config];
    if (!refresh && cached && now() - (cached.fetched_at || 0) <= T.configMaxAgeMs) return cached;
    const fresh = await refreshConfig();
    if (fresh) return fresh;
    return { mode: "disabled", extensions: [], excluded_sites: [], fetched_at: 0, unreachable: true };
  }

  /** Last configuration received from the desktop, whatever its age (or null). */
  async function cachedConfig() {
    try {
      const got = await storageLocal.get(STORAGE_KEYS.config);
      const cfg = got && got[STORAGE_KEYS.config];
      return cfg && typeof cfg === "object" ? cfg : null;
    } catch (_e) {
      return null;
    }
  }

  const captureEnabled = (cfg) => !!cfg && !cfg.unreachable && (cfg.mode === "ask" || cfg.mode === "automatic");

  // ------------------------------------------------------------ eligibility
  /** "this_extension" | "browser_pdf_viewer" | "other_extension" | "browser" (diagnostics only). */
  function initiatorOf(item) {
    if (!item || !item.byExtensionId) return "browser";
    if (item.byExtensionId === extensionId) return "this_extension";
    return isBrowserPdfViewer(item.byExtensionId, browser) ? "browser_pdf_viewer" : "other_extension";
  }

  function checkItem(item, cfg) {
    if (!item) return "missing";
    if (item.incognito) return "incognito";
    if (item.byExtensionId) {
      if (item.byExtensionId === extensionId) return "own_download";
      // Never any other extension - except the browser's own PDF viewer, for a PDF
      // only (re-checked with the final filename in evaluate()).
      if (!isBrowserPdfViewer(item.byExtensionId, browser)) return "other_extension";
      const name = basename(item.filename);
      if ((name || normalizeMime(item.mime)) && !isPdfLike(name, item.mime)) return "other_extension";
    }
    if (!validateDownloadUrl(item.url || "").ok) return "unsupported_url";
    if (item.finalUrl && item.finalUrl !== item.url && !validateDownloadUrl(item.finalUrl).ok) return "unsupported_url";
    if (item.danger && item.danger !== "safe") return "dangerous";
    if (item.state && item.state !== "in_progress") return "not_in_progress";
    const sites = cfg.excluded_sites || [];
    if ([item.url, item.finalUrl, item.referrer].some((u) => hostIsExcluded(hostOf(u), sites))) return "site_excluded";
    return null;
  }

  /** Filename priority: the browser's resolved name, then Content-Disposition, then
   *  the URL *path* (final URL first, then the original). Query strings are never used. */
  function candidateFilename(item, req) {
    const fromUrl = () => {
      const fin = filenameFromUrl(item.finalUrl || item.url);
      if (fileExtension(fin) || !item.finalUrl || item.finalUrl === item.url) return fin;
      return fileExtension(filenameFromUrl(item.url)) ? filenameFromUrl(item.url) : fin;
    };
    return basename(item.filename)
      || (req && req.cdName ? basename(req.cdName) : "")
      || fromUrl();
  }

  /** Full decision once the download is paused and metadata collected. */
  function evaluate(item, req, cfg) {
    const r = checkItem(item, cfg);
    if (r) return { ok: false, reason: r };
    if (!item.paused) return { ok: false, reason: "not_paused" };
    if (!req || !req.method || req.method === "UNKNOWN") return { ok: false, reason: "method_unknown" };
    if (req.method !== "GET") return { ok: false, reason: "unsupported_method" };
    if (req.extended) {
      // An older (cached-content) observation only counts for the same kind of file.
      const a = mimeFamily(item.mime);
      if (a && a !== mimeFamily(req.ctype)) return { ok: false, reason: "method_unknown" };
    }
    let filename = candidateFilename(item, req);
    if (!filename) return { ok: false, reason: "no_filename" };
    if (initiatorOf(item) === "browser_pdf_viewer" && !isPdfLike(filename, item.mime || req.ctype)) {
      return { ok: false, reason: "other_extension" };
    }
    if (!extensionMatches(filename, cfg.extensions)) {
      // "download" + Content-Type application/pdf -> "download.pdf" (only when the name has no extension).
      const mimeExt = extensionForMime(item.mime) || extensionForMime(req.ctype);
      if (!mimeExt || fileExtension(filename) || !extensionMatches(`file.${mimeExt}`, cfg.extensions)) {
        return { ok: false, reason: "extension_not_selected" };
      }
      filename = `${filename}.${mimeExt}`;
    }
    return { ok: true, filename };
  }

  async function collectMetadata(id) {
    const deadline = now() + T.metadataWaitMs;
    let item;
    let req = { method: null };
    for (;;) {
      item = await getItem(id);
      req = item ? await lookupRequestFull(item.url, item.finalUrl) : { method: null };
      if (!item || item.state !== "in_progress" || (basename(item.filename) && req.method)) break;
      if (now() >= deadline) break;
      await waitForChange(id, T.metadataPollMs);
    }
    return { item, req };
  }

  // -------------------------------------------------------------- events
  function handleChanged(delta) {
    if (!delta || typeof delta.id !== "number") return;
    const set = changeWaiters.get(delta.id);
    if (set) [...set].forEach((resolve) => resolve());
  }

  /** "Download created" listener (registered by service_worker.js). Resolves with {outcome, reason}. */
  async function handleCreated(item) {
    if (!item || typeof item.id !== "number") return { outcome: "ignored", reason: "invalid" };
    if (activeDownloads.has(item.id)) return { outcome: "ignored", reason: "duplicate_event" };
    activeDownloads.add(item.id);
    const ctx = { item, req: { method: null }, cfg: null, filename: null };
    try {
      const result = await processCreated(item, ctx);
      if (result.reason !== "duplicate_event") await recordDecision(ctx, result);
      return result;
    } finally {
      activeDownloads.delete(item.id);
    }
  }

  async function processCreated(item, ctx) {
    if (await getHandled(item.id)) return { outcome: "ignored", reason: "duplicate_event" };
    // Small files (most images, many PDFs) can finish within milliseconds, so the
    // pause must not wait for a native round trip: when the last configuration
    // received from the desktop enabled capture, it is used for the quick checks
    // and refreshed right after pausing.
    let cfg = await cachedConfig();
    const early = captureEnabled(cfg);
    if (!early) cfg = await getConfig();
    ctx.cfg = cfg;
    if (!captureEnabled(cfg)) {
      return { outcome: "ignored", reason: cfg.unreachable ? "app_unavailable" : "disabled" };
    }
    const pre = checkItem(item, cfg);
    const known = pre ? { method: null } : await lookupRequestFull(item.url, item.finalUrl);
    ctx.req = known;
    const quickName = basename(item.filename) || (known.cdName ? basename(known.cdName) : "");
    const reason = pre
      || (known.method === "UNKNOWN" ? "method_unknown" : null)
      || (known.method && known.method !== "GET" ? "unsupported_method" : null)
      || (quickName && !extensionMatches(quickName, cfg.extensions) ? "extension_not_selected" : null);
    if (reason) {
      await markHandled(item.id, { phase: "skipped", reason });
      return { outcome: "skipped", reason };
    }

    // The transfer is already running: pause it while we decide.
    await markHandled(item.id, { phase: "evaluating", paused: false });
    try {
      await downloads.pause(item.id);
    } catch (_e) {
      const current = await getItem(item.id);
      if (current) ctx.item = current;
      const why = current && current.state === "complete" ? "completed_before_pause" : "pause_failed";
      await markHandled(item.id, { phase: "skipped", reason: why });
      return { outcome: "skipped", reason: why };
    }
    await markHandled(item.id, { phase: "evaluating", paused: true });

    if (early) {
      cfg = await getConfig();  // no native call while the cached copy is still fresh
      ctx.cfg = cfg;
      if (!captureEnabled(cfg)) {
        await resumeIfPaused(item.id);
        const why = cfg.unreachable ? "app_unavailable" : "disabled";
        await markHandled(item.id, { phase: "skipped", reason: why });
        return { outcome: "resumed", reason: why };
      }
    }

    const { item: fresh, req } = await collectMetadata(item.id);
    if (fresh) ctx.item = fresh;
    ctx.req = req;
    const verdict = evaluate(fresh, req, cfg);
    if (!verdict.ok) {
      await resumeIfPaused(item.id);
      await markHandled(item.id, { phase: "skipped", reason: verdict.reason });
      return { outcome: "resumed", reason: verdict.reason };
    }
    ctx.filename = verdict.filename;

    const total = Number.isInteger(fresh.totalBytes) && fresh.totalBytes > 0 ? fresh.totalBytes
      : (Number.isInteger(fresh.fileSize) && fresh.fileSize > 0 ? fresh.fileSize : null);
    const rec = {
      request_id: newRequestId(),
      download_id: fresh.id,
      url: fresh.url,
      filename: verdict.filename,
      mime: normalizeMime(fresh.mime) || req.ctype || null,
      total_bytes: total,
      referrer: cleanReferrer(fresh.referrer),
      mode: cfg.mode,
      phase: "preparing",
      created_at: now(),
      cancelled_at: null,
      attempts: 0,
    };
    await saveRecord(rec);
    await markHandled(item.id, { phase: "capturing", request_id: rec.request_id });
    return await runCapture(rec);
  }

  // -------------------------------------------------------- diagnostics
  /** Privacy-safe summary of one capture decision: no URL, query string, header
   *  value, cookie or token - only the facts needed to explain the outcome. */
  function buildDiagnostic(ctx, result, latest) {
    const item = latest || ctx.item || {};
    const req = ctx.req || {};
    let filename = ctx.filename || "";
    if (!filename) {
      try {
        filename = candidateFilename(item, req);
      } catch (_e) {
        filename = "";
      }
    }
    filename = String(filename || "").replace(/[\u0000-\u001f\u007f]/g, " ").slice(0, 120);
    const method = req.method === null || req.method === undefined ? null : (cleanMethod(req.method) || "UNKNOWN");
    return {
      at: now(),
      download_id: Number.isInteger(item.id) ? item.id : null,
      browser,
      resource_type: typeof req.type === "string" ? req.type : null,
      method,
      method_source: req.method === "GET" ? (req.source || "memory") : null,
      from_cache: req.method === "GET" ? !!req.fromCache : null,
      filename,
      extension: fileExtension(filename) || null,
      mime: normalizeMime(item.mime) || req.ctype || null,
      state: typeof item.state === "string" ? item.state : null,
      paused: !!item.paused,
      by_extension: !!item.byExtensionId,
      initiator: initiatorOf(item),
      mode: ctx.cfg && typeof ctx.cfg.mode === "string" ? ctx.cfg.mode : null,
      decision: sanitizeReason(result.outcome),
      reason: result.reason ? sanitizeReason(result.reason) : null,
    };
  }

  async function recordDecision(ctx, result) {
    let latest = null;
    if (ctx.item && typeof ctx.item.id === "number" && result.outcome !== "ignored") {
      latest = await getItem(ctx.item.id);
    }
    const entry = buildDiagnostic(ctx, result, latest);
    try {
      await withLock(async () => {
        const got = await storageSession.get(STORAGE_KEYS.diagnostics);
        const list = Array.isArray(got && got[STORAGE_KEYS.diagnostics]) ? got[STORAGE_KEYS.diagnostics] : [];
        list.push(entry);
        await storageSession.set({ [STORAGE_KEYS.diagnostics]: list.slice(-MAX_DIAGNOSTICS) });
      });
    } catch (_e) {
      // Diagnostics never affect a capture.
    }
    // The desktop app only hears about downloads it could have received.
    if (reportDiagnostic && captureEnabled(ctx.cfg)) {
      try {
        Promise.resolve(reportDiagnostic(entry)).catch(() => {});
      } catch (_e) {
        // best-effort
      }
    }
    return entry;
  }

  async function listDiagnostics() {
    try {
      const got = await storageSession.get(STORAGE_KEYS.diagnostics);
      const list = got && got[STORAGE_KEYS.diagnostics];
      return Array.isArray(list) ? list.slice().reverse() : [];
    } catch (_e) {
      return [];
    }
  }

  // ---------------------------------------------------------- handshake
  function prepareMessage(rec) {
    return {
      action: "prepare_capture", request_id: rec.request_id, url: rec.url, filename: rec.filename,
      mime: rec.mime, total_bytes: rec.total_bytes, referrer: rec.referrer, browser,
      browser_download_id: rec.download_id, method: "GET", danger: "safe", incognito: false,
    };
  }

  function appErrorReason(r) {
    if (!r) return "app_unavailable";
    if (r.status === "native_host_unavailable") return "native_host_unavailable";
    if (APP_UNAVAILABLE_CODES.has(r.error_code)) return "app_unavailable";
    if (r.error_code === "timeout") return "timeout";
    return r.error_code || "prepare_failed";
  }

  async function runCapture(rec) {
    if (runningCaptures.has(rec.request_id)) return { outcome: "already_running" };
    runningCaptures.add(rec.request_id);
    try {
      return await drive(rec);
    } finally {
      runningCaptures.delete(rec.request_id);
    }
  }

  async function drive(rec) {
    if (rec.phase === "preparing") {
      const item = await getItem(rec.download_id);
      if (!item || item.state !== "in_progress") {
        return finish(rec, item && item.state === "complete" ? "download_completed" : "browser_changed",
          { resume: false, abort: false });
      }
      const r = await sendNative(prepareMessage(rec));
      if (!r || !r.ok) {
        if (r && r.error_code === "duplicate_capture") {
          return finish(rec, "duplicate_capture", { resume: false, abort: false });
        }
        return finish(rec, appErrorReason(r), { resume: true, abort: false });
      }
      const state = r.data && r.data.state;
      if (state === "committed") return finish(rec, "committed", { resume: false, abort: false });
      if (state === "declined" || state === "expired" || state === "aborted") {
        return finish(rec, (r.data && r.data.reason) || state, { resume: true, abort: false });
      }
      rec.phase = "awaiting";
      await saveRecord(rec);
    }
    if (rec.phase === "awaiting") {
      const d = await waitForDecision(rec);
      if (d.outcome !== "approved") return finish(rec, d.reason, d);
      rec.phase = "cancelling";
      await saveRecord(rec);
    }
    if (rec.phase === "cancelling") {
      const c = await cancelAndConfirm(rec.download_id);
      if (!c.cancelled) return finish(rec, c.reason, { resume: c.resumable, abort: true });
      rec.phase = "committing";
      rec.cancelled_at = rec.cancelled_at || now();
      await saveRecord(rec);
    }
    if (rec.phase === "committing") return commitWithRetry(rec);
    return { outcome: rec.phase };
  }

  async function waitForDecision(rec) {
    let failures = 0;
    for (;;) {
      if (now() - rec.created_at > T.decisionTimeoutMs) return { outcome: "stop", reason: "timeout", resume: true, abort: true };
      const item = await getItem(rec.download_id);
      if (!item) return { outcome: "stop", reason: "browser_removed", resume: false, abort: true };
      if (item.state === "complete") return { outcome: "stop", reason: "download_completed", resume: false, abort: true };
      if (item.state === "interrupted") return { outcome: "stop", reason: "browser_cancelled", resume: false, abort: true };
      if (!item.paused) return { outcome: "stop", reason: "browser_resumed", resume: false, abort: true };

      const r = await sendNative({ action: "capture_status", request_id: rec.request_id, browser });
      if (r && r.ok && r.data) {
        failures = 0;
        const st = r.data.state;
        if (st === "approved") return { outcome: "approved" };
        if (st === "declined" || st === "expired" || st === "aborted") {
          return { outcome: "stop", reason: r.data.reason || st, resume: true, abort: false };
        }
      } else if (r && r.error_code === "capture_not_found") {
        return { outcome: "stop", reason: "expired", resume: true, abort: false };
      } else {
        failures += 1;
        if (failures >= T.maxStatusFailures) return { outcome: "stop", reason: appErrorReason(r), resume: true, abort: true };
      }
      await waitForChange(rec.download_id, T.pollIntervalMs);
    }
  }

  async function cancelAndConfirm(id) {
    let item = await getItem(id);
    if (!item) return { cancelled: false, reason: "browser_removed", resumable: false };
    if (item.state === "complete") return { cancelled: false, reason: "download_completed", resumable: false };
    try {
      await downloads.cancel(id);
    } catch (_e) {
      item = await getItem(id);
      return { cancelled: false, reason: "cancel_failed", resumable: !!(item && item.state === "in_progress" && item.paused) };
    }
    const deadline = now() + T.cancelConfirmMs;
    for (;;) {
      item = await getItem(id);
      if (!item || item.state === "interrupted") return { cancelled: true };
      if (item.state === "complete") return { cancelled: false, reason: "download_completed", resumable: false };
      if (now() >= deadline) {
        return { cancelled: false, reason: "cancel_unconfirmed", resumable: item.state === "in_progress" && item.paused };
      }
      await waitForChange(id, 100);
    }
  }

  async function commitWithRetry(rec) {
    for (let attempt = 0; ; attempt += 1) {
      const r = await sendNative({ action: "commit_capture", request_id: rec.request_id, url: rec.url, browser });
      if (r && r.ok) {
        await removeRecord(rec.request_id);
        await markHandled(rec.download_id, { phase: "done", reason: "committed" });
        const name = (r.data && r.data.filename) || rec.filename;
        notify(r.status === "duplicate" ? "Already in My Download Manager" : "Sent to My Download Manager", name, false);
        return { outcome: "committed", download_id: r.data && r.data.download_id, status: r.status };
      }
      if (r && DEFINITIVE_COMMIT_ERRORS.has(r.error_code)) return browserFallback(rec, r.error_code);
      rec.attempts = (rec.attempts || 0) + 1;
      rec.last_error = (r && r.error_code) || "unknown";
      await saveRecord(rec);
      if (now() - (rec.cancelled_at || now()) > T.commitGiveUpMs) return browserFallback(rec, "commit_timeout");
      if (attempt >= T.commitRetryDelays.length) return { outcome: "commit_pending", reason: rec.last_error };
      await sleep(T.commitRetryDelays[attempt]);
    }
  }

  /** The browser download was cancelled but the desktop app cannot take it:
   *  restart it as a normal browser download (with the browser's own cookies).
   *  Downloads started by this extension are never captured (byExtensionId). */
  async function browserFallback(rec, reason) {
    try {
      const newId = await downloads.download({ url: rec.url });
      if (typeof newId === "number") await markHandled(newId, { phase: "skipped", reason: "own_download" });
      await removeRecord(rec.request_id);
      await markHandled(rec.download_id, { phase: "done", reason: "browser_fallback" });
      notify("Continuing in your browser", `${rec.filename} could not be handed to My Download Manager, `
        + "so the browser is downloading it again.", true);
      return { outcome: "browser_fallback", reason, download_id: newId };
    } catch (_e) {
      rec.phase = "fallback_failed";
      rec.last_error = reason;
      await saveRecord(rec);
      notify("Download needs attention", `${rec.filename} could not be restarted. Open the extension popup to retry.`, true);
      return { outcome: "fallback_failed", reason };
    }
  }

  async function finish(rec, reason, { resume = true, abort = true } = {}) {
    const why = sanitizeReason(reason);
    let committed = false;
    if (abort) {
      const r = await sendNative({ action: "abort_capture", request_id: rec.request_id, reason: why, browser });
      committed = !!(r && r.ok && r.data && r.data.state === "committed");
    }
    let resumed = false;
    if (resume && !committed) resumed = await resumeIfPaused(rec.download_id);
    await removeRecord(rec.request_id);
    await markHandled(rec.download_id, { phase: "done", reason: why });
    if (RESUME_NOTICES[why]) notify("My Download Manager", RESUME_NOTICES[why], why !== "auth_required");
    return { outcome: resumed ? "resumed" : "left", reason: why };
  }

  // ------------------------------------------------------------ recovery
  /** Called whenever the service worker starts: continues or safely unwinds
   *  every capture that was in flight when the previous worker stopped. */
  function recover() {
    if (recovering) return recovering;
    recovering = (async () => {
      const handled = await readMap(storageSession, STORAGE_KEYS.handled);
      for (const [id, h] of Object.entries(handled)) {
        const numId = Number(id);
        if (h && h.phase === "evaluating" && !activeDownloads.has(numId)) {
          await resumeIfPaused(numId);
          await markHandled(numId, { phase: "done", reason: "recovered" });
        }
      }
      const captures = await readMap(storageLocal, STORAGE_KEYS.captures);
      const runs = [];
      for (const rec of Object.values(captures)) {
        if (!rec || rec.phase === "fallback_failed" || runningCaptures.has(rec.request_id)) continue;
        if (rec.phase === "cancelling") {
          const item = await getItem(rec.download_id);
          if (!item || item.state === "interrupted") {
            rec.phase = "committing";
            rec.cancelled_at = rec.cancelled_at || now();
            await saveRecord(rec);
          }
        }
        runs.push(runCapture(rec));
      }
      return Promise.all(runs);
    })().finally(() => { recovering = null; });
    return recovering;
  }

  // -------------------------------------------------------------- popup
  async function listCaptures() {
    const caps = await readMap(storageLocal, STORAGE_KEYS.captures);
    // URLs stay in storage for recovery only; the popup gets names and phases.
    return Object.values(caps).map((c) => ({
      request_id: c.request_id, filename: c.filename, phase: c.phase, created_at: c.created_at,
      last_error: c.last_error || null,
    }));
  }

  async function pendingCount() {
    return (await listCaptures()).length;
  }

  async function setMode(mode) {
    const r = await sendNative({ action: "set_capture_mode", mode, browser });
    if (r && r.ok && r.data) await storageLocal.set({ [STORAGE_KEYS.config]: { ...r.data, fetched_at: now() } });
    return r;
  }

  async function claimFallback(requestId) {
    const r = await sendNative({ action: "claim_fallback", request_id: requestId, browser });
    if (!r || !r.ok || !r.data) return r || { ok: false, message: "My Download Manager is not reachable." };
    const check = validateDownloadUrl(r.data.url);
    if (!check.ok) return { ok: false, message: "The returned link is not a valid web address." };
    try {
      const newId = await downloads.download({ url: check.url });
      if (typeof newId === "number") await markHandled(newId, { phase: "skipped", reason: "own_download" });
      return { ok: true, download_id: newId, message: "Downloading in the browser." };
    } catch (_e) {
      return { ok: false, message: "The browser could not start the download." };
    }
  }

  async function retryFallback(requestId) {
    const caps = await readMap(storageLocal, STORAGE_KEYS.captures);
    const rec = caps[requestId];
    if (!rec) return { ok: false, message: "Nothing to retry." };
    const out = await browserFallback(rec, rec.last_error || "retry");
    return { ok: out.outcome === "browser_fallback", outcome: out.outcome };
  }

  return {
    handleCreated, handleChanged, recordResponse, lookupRequest, recover,
    getConfig, refreshConfig, setMode, listCaptures, pendingCount, claimFallback, retryFallback,
    evaluate, checkItem, lookupRequestFull, flushObservations, listDiagnostics,
    get lastConfigError() { return lastConfigError; },
  };
}
