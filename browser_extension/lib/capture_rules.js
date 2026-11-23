// My Download Manager - automatic capture rules (Stage 6B).
// Mirrors mdm/browser_integration/capture_rules.py. The desktop application
// owns the configuration and re-checks every rule; the extension uses these
// helpers only to avoid pausing downloads that can never be captured.

const EXTENSION_RE = /^[a-z0-9]{1,10}(\.[a-z0-9]{1,10})?$/;

/** Optional permissions requested (after a user click) to enable automatic capture.
 *  Never includes cookies, tabs, history or anything password-related. */
export const CAPTURE_PERMISSIONS = Object.freeze({
  permissions: ["downloads", "webRequest"],
  origins: ["http://*/*", "https://*/*"],
});

export const CAPTURE_MODE_LABELS = Object.freeze({
  disabled: "Off",
  ask: "Ask before downloading",
  automatic: "Automatic",
});

/** ".ZIP" -> "zip", "*.tar.gz" -> "tar.gz"; null when malformed. */
export function normalizeExtension(value) {
  if (typeof value !== "string") return null;
  let ext = value.trim().toLowerCase();
  if (ext.startsWith("*")) ext = ext.slice(1);
  ext = ext.replace(/^\.+/, "");
  return EXTENSION_RE.test(ext) ? ext : null;
}

/** Last component of a Windows or POSIX path (DownloadItem.filename is a full path). */
export function basename(name) {
  if (typeof name !== "string" || !name) return "";
  const parts = name.split(/[\\/]/);
  return (parts[parts.length - 1] || "").trim();
}

/** Filename from the URL path only; the query string (signed tokens) is never touched. */
export function filenameFromUrl(url) {
  try {
    const path = new URL(url).pathname.replace(/\/+$/, "");
    const last = path.slice(path.lastIndexOf("/") + 1);
    try {
      return basename(decodeURIComponent(last));
    } catch (_e) {
      return basename(last);
    }
  } catch (_e) {
    return "";
  }
}

/** Longest enabled extension that `filename` ends with, or null. */
export function extensionMatches(filename, enabled) {
  const name = basename(filename).toLowerCase().replace(/[. ]+$/, "");
  if (!name || !Array.isArray(enabled)) return null;
  const sorted = [...new Set(enabled)].filter((e) => typeof e === "string").sort((a, b) => b.length - a.length);
  for (const ext of sorted) {
    const suffix = `.${ext}`;
    if (name.endsWith(suffix) && name.length > suffix.length) return ext;
  }
  return null;
}

/** Lowercase ASCII (punycode) hostname of an http(s) URL, or null. */
export function hostOf(url) {
  if (typeof url !== "string" || !url) return null;
  try {
    const u = new URL(url);
    if (u.protocol !== "http:" && u.protocol !== "https:") return null;
    return u.hostname.replace(/\.$/, "").toLowerCase() || null;
  } catch (_e) {
    return null;
  }
}

/** Exact host or a subdomain of an excluded site ("example.com" excludes
 *  "a.example.com" but never "notexample.com"). Sites are pre-normalised by the app. */
export function hostIsExcluded(host, excludedSites) {
  if (!host || !Array.isArray(excludedSites)) return false;
  const h = host.toLowerCase().replace(/\.$/, "");
  return excludedSites.some((site) => typeof site === "string" && site
    && (h === site || h.endsWith(`.${site}`)));
}

/** Repairs a header value whose UTF-8 bytes were exposed one byte per character
 *  (webRequest reports raw header bytes as Latin-1): "rÃ©sumÃ©.pdf" -> "résumé.pdf".
 *  Values that are not valid UTF-8 byte sequences are returned unchanged. */
export function repairUtf8Mojibake(value) {
  if (typeof value !== "string" || !/[\u0080-\u00ff]/.test(value) || /[^\u0000-\u00ff]/.test(value)) return value;
  try {
    const bytes = Uint8Array.from(value, (c) => c.charCodeAt(0));
    return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch (_e) {
    return value;
  }
}

function decodeFilenameValue(raw) {
  let text = raw;
  try {
    text = decodeURIComponent(raw);
  } catch (_e) {
    text = raw;
  }
  return basename(repairUtf8Mojibake(text));
}

/** Filename from a Content-Disposition header (RFC 6266 filename* preferred).
 *  Handles quoted values with escapes, unquoted values containing spaces and
 *  UTF-8 names. Path components are always stripped. */
export function parseContentDispositionFilename(header) {
  if (typeof header !== "string" || !header) return null;
  const star = /filename\*\s*=\s*"?([^;'"]+)'[^']*'([^;"]+)"?/i.exec(header);
  if (star) {
    try {
      const name = basename(decodeURIComponent(star[2].trim()));
      if (name) return name;
    } catch (_e) {
      // fall through to the plain parameter
    }
  }
  const quoted = /(?:^|;)\s*filename\s*=\s*"((?:[^"\\]|\\.)*)"/i.exec(header);
  if (quoted) {
    const name = decodeFilenameValue(quoted[1].replace(/\\(.)/g, "$1").trim());
    if (name) return name;
  }
  const plain = /(?:^|;)\s*filename\s*=\s*([^";][^;]*)/i.exec(header);
  if (plain) {
    const name = decodeFilenameValue(plain[1].trim());
    if (name) return name;
  }
  return null;
}

// ------------------------------------------------------------ PDFs and images
/** Image extensions added to the default capture list (mirrors IMAGE_EXTENSIONS in capture_rules.py). */
export const IMAGE_EXTENSIONS = Object.freeze(["jpg", "jpeg", "png", "gif", "webp", "bmp", "tif", "tiff", "ico"]);

/** webRequest resource types observed (never blocked) to confirm the method of a download.
 *  "image" is needed for "Save image as…", which the browser serves from the cache. */
export const OBSERVED_RESOURCE_TYPES = Object.freeze(["main_frame", "sub_frame", "other", "image"]);

/** Only MIME types whose file extension is unambiguous; used when neither the browser,
 *  Content-Disposition nor the URL path gives a file extension. */
const MIME_EXTENSIONS = Object.freeze({
  "application/pdf": "pdf",
  "image/jpeg": "jpg", "image/pjpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp",
  "image/bmp": "bmp", "image/x-ms-bmp": "bmp", "image/tiff": "tiff", "image/x-icon": "ico",
  "image/vnd.microsoft.icon": "ico",
});

export function normalizeMime(value) {
  if (typeof value !== "string") return null;
  const m = value.split(";")[0].trim().toLowerCase();
  return /^[a-z0-9][a-z0-9!#$&^_.+-]{0,126}\/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}$/.test(m) ? m : null;
}

export function extensionForMime(mime) {
  return MIME_EXTENSIONS[normalizeMime(mime) || ""] || null;
}

/** File extension of a name ("Report.PDF" -> "pdf"), "" when there is none. */
export function fileExtension(name) {
  const base = basename(name).toLowerCase().replace(/[. ]+$/, "");
  const dot = base.lastIndexOf(".");
  if (dot <= 0 || dot === base.length - 1) return "";
  const ext = base.slice(dot + 1);
  return /^[a-z0-9]{1,10}$/.test(ext) ? ext : "";
}

export function isPdfLike(filename, mime) {
  return fileExtension(filename) === "pdf" || normalizeMime(mime) === "application/pdf";
}

/** The browsers' built-in PDF viewer is a component extension. If a browser ever reports
 *  it as the initiator of a download (DownloadItem.byExtensionId), that download is still a
 *  user download of the viewed PDF. Only these exact IDs are trusted, only for PDFs over
 *  http(s) - every other extension's downloads stay excluded. Edge ships the Chromium
 *  viewer under the same ID. */
export const BROWSER_PDF_VIEWER_IDS = Object.freeze({
  chrome: Object.freeze(["mhjfbmdgcfjbbpaeojofohoefgiehjai"]),
  edge: Object.freeze(["mhjfbmdgcfjbbpaeojofohoefgiehjai"]),
});

export function isBrowserPdfViewer(extensionId, browser) {
  const ids = BROWSER_PDF_VIEWER_IDS[browser];
  return typeof extensionId === "string" && Array.isArray(ids) && ids.includes(extensionId);
}
