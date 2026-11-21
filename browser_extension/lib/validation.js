// My Download Manager - shared validation helpers (Stage 6A).
// Mirrors mdm/browser_integration/request_validator.py. The desktop
// application re-validates everything; this module exists so users get
// immediate, understandable feedback in the browser.

export const NATIVE_HOST_NAME = "com.mydownloadmanager.native";
export const MAX_URL_LENGTH = 32 * 1024;
export const MAX_REFERRER_LENGTH = 8 * 1024;
export const MAX_PAGE_TITLE_LENGTH = 512;

const ALLOWED_SCHEMES = new Set(["http", "https"]);
// eslint-disable-next-line no-control-regex
const FORBIDDEN_URL_CHARS = /[\u0000- \u007f]/;
// eslint-disable-next-line no-control-regex
const CONTROL_CHARS = /[\u0000-\u001f\u007f]/g;
const SCHEME_RE = /^([a-zA-Z][a-zA-Z0-9+.-]*):/;

function fail(code, message) {
  return { ok: false, code, message };
}

/**
 * Validates a download URL. Returns {ok:true, url} or {ok:false, code, message}.
 * Only absolute http:// and https:// URLs without embedded credentials pass.
 * Query strings and percent-encoded characters are preserved verbatim.
 */
export function validateDownloadUrl(raw, maxLength = MAX_URL_LENGTH) {
  if (typeof raw !== "string") return fail("invalid_url", "Enter a download link.");
  const url = raw.trim();
  if (!url) return fail("missing_field", "Enter a download link.");
  if (url.length > maxLength) return fail("invalid_url", "This link is too long.");
  if (FORBIDDEN_URL_CHARS.test(url)) {
    return fail("invalid_url", "Links cannot contain spaces or control characters.");
  }
  const schemeMatch = SCHEME_RE.exec(url);
  if (!schemeMatch) {
    return fail("invalid_url", "Enter a full link starting with http:// or https://.");
  }
  const scheme = schemeMatch[1].toLowerCase();
  if (!ALLOWED_SCHEMES.has(scheme)) {
    return fail("unsupported_scheme",
      `Only HTTP and HTTPS links are supported ("${scheme.slice(0, 20)}:" links cannot be downloaded).`);
  }
  if (!url.slice(schemeMatch[0].length - 1).startsWith("://")) {
    return fail("invalid_url", "Enter a full link starting with http:// or https://.");
  }
  let parsed;
  try {
    parsed = new URL(url);
  } catch (_e) {
    return fail("invalid_url", "This is not a valid web address.");
  }
  if (parsed.username || parsed.password) {
    return fail("embedded_credentials",
      "Links containing a username or password are not accepted for security reasons.");
  }
  if (!parsed.hostname) return fail("invalid_url", "This link has no host name.");
  return { ok: true, url };
}

/** Referrer is informational: anything that is not a clean http(s) URL is dropped. */
export function cleanReferrer(raw) {
  if (typeof raw !== "string" || !raw.trim()) return null;
  const result = validateDownloadUrl(raw, MAX_REFERRER_LENGTH);
  return result.ok ? result.url : null;
}

export function cleanPageTitle(raw) {
  if (typeof raw !== "string") return null;
  const title = raw.replace(CONTROL_CHARS, " ").trim();
  return title ? title.slice(0, MAX_PAGE_TITLE_LENGTH) : null;
}

/** Returns "edge" for Microsoft Edge, otherwise "chrome". */
export function detectBrowser(nav = globalThis.navigator) {
  try {
    const brands = nav && nav.userAgentData && Array.isArray(nav.userAgentData.brands)
      ? nav.userAgentData.brands : [];
    if (brands.some((b) => /Microsoft Edge/i.test(b.brand))) return "edge";
    const ua = (nav && nav.userAgent) || "";
    if (/\bEdg(e|A|iOS)?\//.test(ua)) return "edge";
  } catch (_e) {
    // fall through
  }
  return "chrome";
}

export function browserLabel(browser) {
  return browser === "edge" ? "Microsoft Edge" : "Google Chrome";
}

export function newRequestId() {
  if (globalThis.crypto && typeof globalThis.crypto.randomUUID === "function") {
    return globalThis.crypto.randomUUID();
  }
  const bytes = new Uint8Array(16);
  globalThis.crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

/** Builds the exact add_download message understood by the native host. */
export function buildAddDownloadMessage({ url, referrer, pageTitle, browser, requestId }) {
  return {
    action: "add_download",
    request_id: requestId || newRequestId(),
    url,
    filename: null,
    referrer: cleanReferrer(referrer),
    page_title: cleanPageTitle(pageTitle),
    browser: browser === "edge" ? "edge" : "chrome",
  };
}

/**
 * Maps a chrome.runtime.sendNativeMessage failure to a stable status code.
 *   native_host_unavailable - host not registered / not allowed for this extension ID
 *   connection_error        - host crashed, exited or produced an invalid response
 */
export function classifyNativeError(message) {
  const text = String(message || "");
  if (/not found/i.test(text)) {
    return {
      ok: false, status: "native_host_unavailable", error_code: "native_host_not_found",
      message: "The native messaging host is not installed. Run install_native_host.ps1, then restart the browser.",
    };
  }
  if (/forbidden/i.test(text)) {
    return {
      ok: false, status: "native_host_unavailable", error_code: "native_host_forbidden",
      message: "The native host is installed but this extension ID is not allowed. Re-run install_native_host.ps1 with this extension's ID.",
    };
  }
  if (/exited/i.test(text)) {
    return {
      ok: false, status: "connection_error", error_code: "native_host_exited",
      message: "The native messaging host stopped unexpectedly. Check that the host executable exists.",
    };
  }
  return {
    ok: false, status: "connection_error", error_code: "native_host_error",
    message: "Could not communicate with the native messaging host.",
  };
}

const APP_UNAVAILABLE_CODES = new Set(["app_not_running", "invalid_endpoint", "shutting_down", "connection_lost"]);

/** Maps a host response to one of: connected | app_not_running | native_host_unavailable | connection_error. */
export function connectionStatusFromResponse(response) {
  if (!response || typeof response !== "object") return "connection_error";
  if (response.ok) return "connected";
  if (response.status === "native_host_unavailable") return "native_host_unavailable";
  if (APP_UNAVAILABLE_CODES.has(response.error_code)) return "app_not_running";
  return "connection_error";
}

/** Validation failures are permanent; retrying them can never succeed. */
const NON_RETRYABLE = new Set([
  "invalid_url", "unsupported_scheme", "embedded_credentials", "invalid_field",
  "missing_field", "invalid_message", "unsupported_action", "message_too_large",
  "request_id_conflict",
]);

export function isRetryable(response) {
  return !!response && !response.ok && !NON_RETRYABLE.has(response.error_code);
}
