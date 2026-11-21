// My Download Manager - background service worker (Manifest V3, Stage 6A).
//
// Owns every native-messaging exchange. The popup talks to this worker via
// chrome.runtime.sendMessage; only messages from this extension are accepted.
//
// What is (and is not) sent to the desktop application:
//  * The extension never reads browser cookies, saved passwords, session
//    storage or Authorization headers - it has no permission to.
//  * The link URL itself IS forwarded exactly as it appears on the page.
//    Signed download links (CDN / cloud storage) often carry an access token
//    in their query string (e.g. ?X-Amz-Signature=...&token=...); stripping it
//    would break the download, so it is kept intact. Such URLs are treated as
//    sensitive: the extension does not log them, and the native host and the
//    application log only "scheme://host/..." and store only a SHA-256
//    fingerprint of the URL for duplicate detection.
//  * Exception, by design: a request that could not be delivered (application
//    not running, host missing) is kept - full URL included - in this
//    extension's private chrome.storage.local so it can be retried from the
//    popup. At most 20 are kept; each is removed on successful delivery or
//    when the user clicks Dismiss. Nothing is synced (storage.sync is unused).
//  * URLs with embedded "user:password@" credentials are rejected.

import {
  NATIVE_HOST_NAME,
  validateDownloadUrl,
  buildAddDownloadMessage,
  detectBrowser,
  browserLabel,
  newRequestId,
  classifyNativeError,
  connectionStatusFromResponse,
  isRetryable,
} from "./lib/validation.js";

export const MENU_ID = "mdm-download-link";
export const MENU_TITLE = "Download with My Download Manager";
const PENDING_KEY = "pendingRequests";
const MAX_PENDING = 20;
const BROWSER = detectBrowser();

// ---------------------------------------------------------------- context menu
export function setupContextMenu() {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create(
      {
        id: MENU_ID,
        title: MENU_TITLE,
        contexts: ["link"],
        // Only offered on HTTP/HTTPS links; everything is validated again on click.
        targetUrlPatterns: ["http://*/*", "https://*/*"],
      },
      () => void chrome.runtime.lastError,
    );
  });
}

chrome.runtime.onInstalled.addListener(setupContextMenu);
chrome.runtime.onStartup.addListener(setupContextMenu);

chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId !== MENU_ID) return;
  submitDownload({
    url: info.linkUrl,
    referrer: info.pageUrl || info.frameUrl || null,
    pageTitle: tab && typeof tab.title === "string" ? tab.title : null,
  });
});

// ---------------------------------------------------------------- native host
export async function sendNative(message) {
  try {
    const response = await chrome.runtime.sendNativeMessage(NATIVE_HOST_NAME, message);
    if (!response || typeof response !== "object") {
      return { ok: false, status: "connection_error", error_code: "invalid_response",
        message: "The native host returned an invalid response." };
    }
    return response;
  } catch (err) {
    return classifyNativeError(err && err.message);
  }
}

// --------------------------------------------------------------- notifications
function notify(title, message, isError) {
  try {
    if (chrome.notifications && chrome.notifications.create) {
      chrome.notifications.create(
        "",
        { type: "basic", iconUrl: "icons/icon128.png", title, message, priority: isError ? 1 : 0 },
        () => void chrome.runtime.lastError,
      );
    }
  } catch (_e) {
    // Notifications are best-effort; the badge below always works.
  }
  chrome.action.setBadgeBackgroundColor({ color: isError ? "#dc2626" : "#2563eb" });
  chrome.action.setBadgeText({ text: isError ? "!" : "✓" });
  setTimeout(() => chrome.action.setBadgeText({ text: "" }), 5000);
}

// ---------------------------------------------------------- pending requests
async function getPending() {
  const stored = await chrome.storage.local.get(PENDING_KEY);
  return Array.isArray(stored[PENDING_KEY]) ? stored[PENDING_KEY] : [];
}

async function savePending(list) {
  await chrome.storage.local.set({ [PENDING_KEY]: list.slice(-MAX_PENDING) });
}

async function rememberFailed(message, response) {
  const list = (await getPending()).filter((p) => p.message.request_id !== message.request_id);
  list.push({
    message,
    failed_at: Date.now(),
    error_code: response.error_code || null,
    error: response.message || "Request failed.",
  });
  await savePending(list);
}

async function forgetPending(requestId) {
  const list = await getPending();
  await savePending(list.filter((p) => p.message.request_id !== requestId));
}

// ------------------------------------------------------------------ download
/**
 * Validates and sends one download request. Failures that can succeed later
 * (application not running, host not installed, ...) are kept in storage so
 * the user can retry from the popup - the request is never silently dropped.
 */
export async function submitDownload({ url, referrer = null, pageTitle = null }, existing = null) {
  const message = existing || (() => {
    const check = validateDownloadUrl(url);
    if (!check.ok) return check;
    return buildAddDownloadMessage({ url: check.url, referrer, pageTitle, browser: BROWSER });
  })();

  if (message.ok === false) {
    notify("Link not supported", message.message, true);
    return { ok: false, status: "error", error_code: message.code, message: message.message };
  }

  const response = await sendNative(message);
  if (response.ok) {
    await forgetPending(message.request_id);
    const name = response.download && response.download.filename;
    const dup = response.status === "duplicate";
    notify(
      dup ? "Already sent to My Download Manager" : "Download added",
      name ? `${name}` : (response.message || "The download was added to My Download Manager."),
      false,
    );
  } else {
    if (isRetryable(response)) await rememberFailed(message, response);
    notify("My Download Manager", response.message || "The download could not be added.", true);
  }
  return { ...response, connection_status: connectionStatusFromResponse(response) };
}

// ------------------------------------------------------------ popup bridge
async function getStatus() {
  const response = await sendNative({ action: "ping", request_id: newRequestId() });
  return {
    status: connectionStatusFromResponse(response),
    message: response.message || "",
    error_code: response.error_code || null,
    app_version: response.app_version || null,
    host_version: response.host_version || null,
    open_app_supported: !!response.open_app_supported,
    browser: BROWSER,
    browser_label: browserLabel(BROWSER),
    extension_version: chrome.runtime.getManifest().version,
  };
}

export async function handlePopupMessage(request) {
  switch (request && request.type) {
    case "getStatus":
      return getStatus();
    case "addUrl":
      return submitDownload({ url: request.url });
    case "openApp":
      return sendNative({ action: "open_app", request_id: newRequestId() });
    case "listPending":
      return { pending: await getPending() };
    case "retryPending": {
      const item = (await getPending()).find((p) => p.message.request_id === request.requestId);
      if (!item) return { ok: false, message: "This request is no longer pending." };
      // Same request_id: the application de-duplicates, so a retry can never add twice.
      return submitDownload({}, item.message);
    }
    case "dismissPending":
      await forgetPending(request.requestId);
      return { ok: true };
    default:
      return { ok: false, message: "Unsupported request." };
  }
}

chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (!sender || sender.id !== chrome.runtime.id) return false;
  handlePopupMessage(request).then(sendResponse, (err) =>
    sendResponse({ ok: false, message: String((err && err.message) || err) }));
  return true; // async response
});
